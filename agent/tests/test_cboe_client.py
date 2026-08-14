"""Unit tests for the Cboe delayed-quote option-chain fallback client."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from backtest.loaders import cboe_client


def _occ(root: str, expiry: datetime, side: str, strike: float) -> str:
    return f"{root}{expiry:%y%m%d}{side}{int(round(strike * 1000)):08d}"


def _payload(spot: float = 150.0, expiries: list[datetime] | None = None) -> dict:
    if expiries is None:
        expiries = [datetime.now(timezone.utc) + timedelta(days=days) for days in (7, 30)]
    contracts = []
    for expiry in expiries:
        for strike in (140.0, 150.0, 160.0):
            for side in ("C", "P"):
                contracts.append({
                    "option": _occ("AAPL", expiry, side, strike),
                    "bid": 4.0,
                    "ask": 4.4,
                    "volume": 120,
                    "open_interest": 3400,
                    "iv": 0.31,
                    "last_trade_price": 4.2,
                    "delta": 0.5,
                })
    return {"data": {"symbol": "AAPL", "current_price": spot, "options": contracts}}


@pytest.fixture(autouse=True)
def _clear_cache():
    cboe_client.clear_cache()
    yield
    cboe_client.clear_cache()


def _patch_fetch(monkeypatch, payload=None, error=None) -> list[str]:
    calls: list[str] = []

    def fake_get_json(url, **kwargs):
        calls.append(url)
        if error is not None:
            raise error
        return _payload() if payload is None else payload

    monkeypatch.setattr(cboe_client, "throttled_get_json", fake_get_json)
    return calls


# ---------------------------------------------------------------------------
# Symbol mapping
# ---------------------------------------------------------------------------


def test_map_symbol_normalizes_case_and_suffix():
    assert cboe_client.map_symbol(" aapl ") == "AAPL"
    assert cboe_client.map_symbol("AAPL.US") == "AAPL"


def test_map_symbol_translates_index_prefix():
    # Yahoo writes indices with a caret; Cboe files use an underscore.
    assert cboe_client.map_symbol("^SPX") == "_SPX"


# ---------------------------------------------------------------------------
# Shaping
# ---------------------------------------------------------------------------


def test_get_options_returns_yahoo_shaped_chain(monkeypatch):
    _patch_fetch(monkeypatch)

    result = cboe_client.get_options("AAPL")

    assert result["quote"]["regularMarketPrice"] == 150.0
    assert len(result["expirationDates"]) == 2
    block = result["options"][0]
    assert block["expirationDate"] == result["expirationDates"][0]
    assert [row["strike"] for row in block["calls"]] == [140.0, 150.0, 160.0]
    assert len(block["puts"]) == 3


def test_get_options_maps_contract_fields_to_yahoo_names(monkeypatch):
    _patch_fetch(monkeypatch)

    call = cboe_client.get_options("AAPL")["options"][0]["calls"][0]

    assert call["bid"] == 4.0
    assert call["ask"] == 4.4
    assert call["openInterest"] == 3400
    assert call["impliedVolatility"] == 0.31
    assert call["lastPrice"] == 4.2
    assert call["volume"] == 120


def test_get_options_derives_in_the_money_from_spot(monkeypatch):
    _patch_fetch(monkeypatch)

    block = cboe_client.get_options("AAPL")["options"][0]
    calls = {row["strike"]: row["inTheMoney"] for row in block["calls"]}
    puts = {row["strike"]: row["inTheMoney"] for row in block["puts"]}

    # Spot is 150: the 140 call is in the money, the 140 put is not.
    assert calls[140.0] is True
    assert calls[160.0] is False
    assert puts[140.0] is False
    assert puts[160.0] is True


def test_get_options_picks_the_nearest_unexpired_expiration(monkeypatch):
    past = datetime.now(timezone.utc) - timedelta(days=5)
    near = datetime.now(timezone.utc) + timedelta(days=3)
    far = datetime.now(timezone.utc) + timedelta(days=40)
    _patch_fetch(monkeypatch, payload=_payload(expiries=[past, near, far]))

    result = cboe_client.get_options("AAPL")

    expected = int(
        near.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    )
    assert result["options"][0]["expirationDate"] == expected
    # The passed expiration stays in the list — it is simply not the one served.
    assert len(result["expirationDates"]) == 3
    assert min(result["expirationDates"]) < int(time.time())


def test_get_options_honours_a_requested_expiration(monkeypatch):
    _patch_fetch(monkeypatch)
    listed = cboe_client.get_options("AAPL")["expirationDates"]

    result = cboe_client.get_options("AAPL", expiration=listed[1])

    assert result["options"][0]["expirationDate"] == listed[1]
    assert all(row["expiration"] == listed[1] for row in result["options"][0]["calls"])


def test_get_options_falls_back_to_the_last_expiration_when_all_have_passed(monkeypatch):
    stale = [
        datetime.now(timezone.utc) - timedelta(days=30),
        datetime.now(timezone.utc) - timedelta(days=5),
    ]
    _patch_fetch(monkeypatch, payload=_payload(expiries=stale))

    result = cboe_client.get_options("AAPL")

    assert result["options"][0]["expirationDate"] == max(result["expirationDates"])


def test_get_options_parses_a_padded_occ_root(monkeypatch):
    # The strict OCC form pads the root to six characters with spaces.
    expiry = datetime.now(timezone.utc) + timedelta(days=10)
    padded = f"AAPL  {expiry:%y%m%d}C00150000"
    _patch_fetch(monkeypatch, payload={
        "data": {"current_price": 150.0, "options": [{"option": padded, "bid": 1.0, "ask": 1.2}]},
    })

    call = cboe_client.get_options("AAPL")["options"][0]["calls"][0]

    assert call["strike"] == 150.0


def test_get_options_skips_unparseable_rows_without_losing_the_chain(monkeypatch):
    payload = _payload()
    payload["data"]["options"].insert(0, {"option": "not-an-occ-symbol", "bid": 1.0})
    payload["data"]["options"].insert(1, {"bid": 1.0})  # no symbol at all
    _patch_fetch(monkeypatch, payload=payload)

    result = cboe_client.get_options("AAPL")

    assert len(result["options"][0]["calls"]) == 3


def test_get_options_returns_empty_for_a_symbol_with_no_options(monkeypatch):
    _patch_fetch(monkeypatch, payload={"data": {"current_price": 10.0, "options": []}})

    assert cboe_client.get_options("NOPE") == {}


def test_get_options_returns_empty_for_a_payload_without_data(monkeypatch):
    _patch_fetch(monkeypatch, payload={"status": "error"})

    assert cboe_client.get_options("NOPE") == {}


def test_get_options_raises_on_a_non_object_payload(monkeypatch):
    _patch_fetch(monkeypatch, payload=["unexpected"])

    with pytest.raises(ValueError):
        cboe_client.get_options("AAPL")


def test_get_options_propagates_a_transport_failure(monkeypatch):
    _patch_fetch(monkeypatch, error=RuntimeError("404 Not Found"))

    with pytest.raises(RuntimeError, match="404"):
        cboe_client.get_options("AAPL")


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_walking_the_expiry_ladder_downloads_the_file_once(monkeypatch):
    # One Cboe file holds every expiration and can run to megabytes; clicking
    # through the ladder must not re-download it each time.
    calls = _patch_fetch(monkeypatch)
    listed = cboe_client.get_options("AAPL")["expirationDates"]

    for epoch in listed:
        cboe_client.get_options("AAPL", expiration=epoch)

    assert len(calls) == 1


def test_cache_is_per_symbol(monkeypatch):
    calls = _patch_fetch(monkeypatch)

    cboe_client.get_options("AAPL")
    cboe_client.get_options("MSFT")

    assert len(calls) == 2


def test_expired_cache_entries_are_refetched(monkeypatch):
    calls = _patch_fetch(monkeypatch)
    monkeypatch.setattr(cboe_client, "_CACHE_TTL_SECONDS", -1.0)

    cboe_client.get_options("AAPL")
    cboe_client.get_options("AAPL")

    assert len(calls) == 2
