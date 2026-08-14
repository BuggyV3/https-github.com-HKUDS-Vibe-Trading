"""Contract tests for the market-discovery dashboard routes.

Every provider call is monkeypatched: these assert the HTTP surface (validation,
caching, error mapping, payload shape), never network behaviour.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

import api_server
from src.api import market_routes


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("API_AUTH_KEY", raising=False)
    monkeypatch.setattr(api_server, "_API_KEY", "")
    market_routes.clear_cache()
    return TestClient(api_server.app, client=("127.0.0.1", 50000))


# ---------------------------------------------------------------------------
# GET /market/screener
# ---------------------------------------------------------------------------


def _screener_rows() -> list[dict[str, Any]]:
    return [
        {
            "code": "AAPL",
            "name": "Apple",
            "price": 200.0,
            "change_pct": 1.5,
            "change": 3.0,
            "volume": 5e7,
            "amount": 1e10,
            "turnover_rate": 0.4,
        },
        {
            "code": "TSLA",
            "name": "Tesla",
            "price": 300.0,
            "change_pct": -4.0,
            "change": -12.0,
            "volume": 9e7,
            "amount": 2e10,
            "turnover_rate": 1.2,
        },
    ]


def _patch_screener(monkeypatch: pytest.MonkeyPatch, rows=None, error=None) -> list[dict]:
    calls: list[dict] = []

    def fake_screen(market: str, *, sort_by: str, top_n: int):
        calls.append({"market": market, "sort_by": sort_by, "top_n": top_n})
        if error is not None:
            raise error
        return rows if rows is not None else _screener_rows()

    monkeypatch.setattr(
        "src.tools.market_screener_tool._screen_market", fake_screen, raising=True
    )
    return calls


def test_screener_returns_scored_rows(client: TestClient, monkeypatch):
    _patch_screener(monkeypatch)

    response = client.get("/market/screener", params={"market": "us", "top_n": 2})

    assert response.status_code == 200
    body = response.json()
    assert body["market"] == "us"
    assert body["source"] == "eastmoney"
    assert body["as_of"]
    assert body["weights"]["liquidity"] == 0.4
    codes = [row["code"] for row in body["rows"]]
    assert codes == ["AAPL", "TSLA"]
    # TSLA trades more money and moves harder, so it must outscore AAPL.
    scores = {row["code"]: row["flow_score"] for row in body["rows"]}
    assert scores["TSLA"] > scores["AAPL"]


def test_screener_passes_query_through_to_the_provider(client: TestClient, monkeypatch):
    calls = _patch_screener(monkeypatch)

    client.get(
        "/market/screener",
        params={"market": "hk", "sort_by": "turnover", "top_n": 7},
    )

    assert calls == [{"market": "hk", "sort_by": "turnover", "top_n": 7}]


def test_screener_rejects_an_unknown_market(client: TestClient, monkeypatch):
    calls = _patch_screener(monkeypatch)

    response = client.get("/market/screener", params={"market": "jp"})

    assert response.status_code == 400
    assert calls == []


def test_screener_rejects_an_unknown_sort(client: TestClient, monkeypatch):
    _patch_screener(monkeypatch)

    response = client.get(
        "/market/screener", params={"market": "us", "sort_by": "sharpe"}
    )

    assert response.status_code == 400


def test_screener_caps_top_n(client: TestClient, monkeypatch):
    _patch_screener(monkeypatch)

    response = client.get("/market/screener", params={"market": "us", "top_n": 5000})

    assert response.status_code == 422


def test_screener_maps_a_provider_failure_to_502(client: TestClient, monkeypatch):
    _patch_screener(monkeypatch, error=RuntimeError("connection reset"))

    response = client.get("/market/screener", params={"market": "us"})

    assert response.status_code == 502
    assert "connection reset" in response.json()["detail"]


def test_screener_serves_repeat_requests_from_cache(client: TestClient, monkeypatch):
    calls = _patch_screener(monkeypatch)

    first = client.get("/market/screener", params={"market": "us", "top_n": 2})
    second = client.get("/market/screener", params={"market": "us", "top_n": 2})

    assert len(calls) == 1
    # The cache hit reports the original fetch time, so a stale panel reads stale.
    assert first.json()["as_of"] == second.json()["as_of"]


def test_screener_caches_per_query(client: TestClient, monkeypatch):
    calls = _patch_screener(monkeypatch)

    client.get("/market/screener", params={"market": "us", "top_n": 2})
    client.get("/market/screener", params={"market": "hk", "top_n": 2})

    assert len(calls) == 2


# ---------------------------------------------------------------------------
# GET /market/options
# ---------------------------------------------------------------------------


def _yahoo_chain() -> dict[str, Any]:
    expiry = int((datetime.now(timezone.utc) + timedelta(days=21)).timestamp())
    return {
        "quote": {"regularMarketPrice": 150.0, "currency": "USD"},
        "expirationDates": [expiry],
        "options": [
            {
                "expirationDate": expiry,
                "calls": [
                    {
                        "contractSymbol": "AAPL260101C00150000",
                        "strike": 150.0,
                        "bid": 4.0,
                        "ask": 4.2,
                        "volume": 900,
                        "openInterest": 4000,
                        "impliedVolatility": 0.28,
                        "inTheMoney": False,
                        "expiration": expiry,
                    }
                ],
                "puts": [
                    {
                        "contractSymbol": "AAPL260101P00150000",
                        "strike": 150.0,
                        "bid": 3.5,
                        "ask": 3.7,
                        "volume": 400,
                        "openInterest": 2500,
                        "impliedVolatility": 0.30,
                        "inTheMoney": False,
                        "expiration": expiry,
                    }
                ],
            }
        ],
    }


def _patch_provider(
    monkeypatch: pytest.MonkeyPatch, provider: str, chain=None, error=None
) -> list[dict]:
    """Stub one chain provider, recording every call it receives."""
    calls: list[dict] = []

    def fake_get_options(symbol: str, *, expiration=None):
        calls.append({"symbol": symbol, "expiration": expiration})
        if error is not None:
            raise error
        return _yahoo_chain() if chain is None else chain

    monkeypatch.setattr(
        f"backtest.loaders.{provider}_client.get_options", fake_get_options, raising=True
    )
    return calls


def _patch_options(monkeypatch: pytest.MonkeyPatch, chain=None, error=None) -> list[dict]:
    """Stub Yahoo, and make Cboe unavailable so the primary path is isolated."""
    _patch_provider(monkeypatch, "cboe", error=RuntimeError("cboe not stubbed"))
    return _patch_provider(monkeypatch, "yahoo", chain=chain, error=error)


def test_options_returns_an_enriched_chain(client: TestClient, monkeypatch):
    _patch_options(monkeypatch)

    response = client.get("/market/options", params={"ticker": "aapl"})

    assert response.status_code == 200
    body = response.json()
    assert body["ticker"] == "AAPL"
    assert body["source"] == "yahoo"
    assert body["spot"] == 150.0
    call = body["calls"][0]
    assert call["mid"] == pytest.approx(4.1)
    assert call["break_even"] == pytest.approx(154.1)
    assert call["greeks"]["delta"] > 0
    assert call["liquidity_score"] > 50
    assert body["summary"]["put_call_oi_ratio"] == pytest.approx(0.625)


def test_options_forwards_the_requested_expiration(client: TestClient, monkeypatch):
    calls = _patch_options(monkeypatch)

    client.get("/market/options", params={"ticker": "AAPL", "expiration": 1800000000})

    assert calls == [{"symbol": "AAPL", "expiration": 1800000000}]


def test_options_honours_the_risk_free_rate(client: TestClient, monkeypatch):
    _patch_options(monkeypatch)

    low = client.get("/market/options", params={"ticker": "AAPL", "risk_free_rate": 0.0})
    market_routes.clear_cache()
    high = client.get(
        "/market/options", params={"ticker": "AAPL", "risk_free_rate": 0.2}
    )

    assert low.json()["risk_free_rate"] == 0.0
    # A higher discount rate lifts call delta; the rate is really reaching the
    # pricing code rather than just being echoed back.
    assert high.json()["calls"][0]["greeks"]["delta"] > (
        low.json()["calls"][0]["greeks"]["delta"]
    )


def test_options_rejects_a_malformed_ticker(client: TestClient, monkeypatch):
    calls = _patch_options(monkeypatch)

    response = client.get("/market/options", params={"ticker": "../etc/passwd"})

    assert response.status_code == 400
    assert calls == []


def test_options_rejects_an_out_of_range_rate(client: TestClient, monkeypatch):
    _patch_options(monkeypatch)

    response = client.get(
        "/market/options", params={"ticker": "AAPL", "risk_free_rate": 5.0}
    )

    assert response.status_code == 422


def test_options_maps_a_total_provider_failure_to_502(client: TestClient, monkeypatch):
    _patch_provider(monkeypatch, "yahoo", error=RuntimeError("yahoo 401"))
    _patch_provider(monkeypatch, "cboe", error=RuntimeError("cboe 503"))

    response = client.get("/market/options", params={"ticker": "AAPL"})

    assert response.status_code == 502
    # The primary provider's failure is the one reported — it is the read the
    # caller would otherwise have been served.
    assert "yahoo 401" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Chain provider fallback
# ---------------------------------------------------------------------------


def test_options_falls_back_to_cboe_when_yahoo_errors(client: TestClient, monkeypatch):
    _patch_provider(monkeypatch, "yahoo", error=RuntimeError("yahoo 401"))
    cboe_calls = _patch_provider(monkeypatch, "cboe")

    response = client.get("/market/options", params={"ticker": "AAPL"})

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "cboe"
    assert body["attempted"] == ["yahoo", "cboe"]
    assert len(cboe_calls) == 1
    # The fallback chain is shaped by the same code path, greeks and all.
    assert body["calls"][0]["greeks"]["delta"] > 0


def test_options_falls_back_when_yahoo_lists_nothing(client: TestClient, monkeypatch):
    # A symbol Yahoo has quietly stopped serving comes back empty rather than
    # raising; falling through is as useful there as on an error.
    _patch_provider(monkeypatch, "yahoo", chain={})
    _patch_provider(monkeypatch, "cboe")

    response = client.get("/market/options", params={"ticker": "AAPL"})

    assert response.status_code == 200
    assert response.json()["source"] == "cboe"


def test_options_prefers_yahoo_when_it_answers(client: TestClient, monkeypatch):
    _patch_provider(monkeypatch, "yahoo")
    cboe_calls = _patch_provider(monkeypatch, "cboe")

    body = client.get("/market/options", params={"ticker": "AAPL"}).json()

    assert body["source"] == "yahoo"
    assert body["attempted"] == ["yahoo"]
    assert cboe_calls == []


def test_options_can_pin_the_read_to_one_provider(client: TestClient, monkeypatch):
    yahoo_calls = _patch_provider(monkeypatch, "yahoo")
    _patch_provider(monkeypatch, "cboe")

    body = client.get(
        "/market/options", params={"ticker": "AAPL", "provider": "cboe"}
    ).json()

    assert body["source"] == "cboe"
    assert yahoo_calls == []


def test_options_pinned_to_a_broken_provider_does_not_fall_through(
    client: TestClient, monkeypatch
):
    _patch_provider(monkeypatch, "yahoo")
    _patch_provider(monkeypatch, "cboe", error=RuntimeError("cboe 503"))

    response = client.get(
        "/market/options", params={"ticker": "AAPL", "provider": "cboe"}
    )

    assert response.status_code == 502
    assert "cboe 503" in response.json()["detail"]


def test_options_rejects_an_unknown_provider(client: TestClient, monkeypatch):
    yahoo_calls = _patch_options(monkeypatch)

    response = client.get(
        "/market/options", params={"ticker": "AAPL", "provider": "bloomberg"}
    )

    assert response.status_code == 400
    assert yahoo_calls == []


def test_fallback_translates_a_real_cboe_payload_end_to_end(
    client: TestClient, monkeypatch
):
    """Yahoo down, Cboe serving: the raw CDN shape must survive the whole path.

    Everything between the CDN JSON and the response runs for real here — the
    OCC symbol parsing, the Yahoo-shape translation, then the same enrichment
    that prices Yahoo's chains.
    """
    from backtest.loaders import cboe_client

    cboe_client.clear_cache()
    _patch_provider(monkeypatch, "yahoo", error=RuntimeError("yahoo 401"))

    expiry = datetime.now(timezone.utc) + timedelta(days=28)
    stamp = expiry.strftime("%y%m%d")
    monkeypatch.setattr(
        cboe_client,
        "throttled_get_json",
        lambda url, **kwargs: {
            "data": {
                "symbol": "AAPL",
                "current_price": 150.0,
                "options": [
                    {
                        "option": f"AAPL{stamp}C00150000",
                        "bid": 4.0,
                        "ask": 4.2,
                        "volume": 900,
                        "open_interest": 4000,
                        "iv": 0.28,
                        "last_trade_price": 4.1,
                    },
                    {
                        "option": f"AAPL{stamp}P00150000",
                        "bid": 3.5,
                        "ask": 3.7,
                        "volume": 400,
                        "open_interest": 2500,
                        "iv": 0.30,
                        "last_trade_price": 3.6,
                    },
                ],
            }
        },
    )

    body = client.get("/market/options", params={"ticker": "AAPL"}).json()

    assert body["source"] == "cboe"
    assert body["spot"] == 150.0
    call = body["calls"][0]
    assert call["strike"] == 150.0
    assert call["mid"] == pytest.approx(4.1)
    assert call["break_even"] == pytest.approx(154.1)
    assert call["implied_volatility"] == pytest.approx(0.28)
    assert call["iv_source"] == "provider"
    assert call["greeks"]["delta"] > 0
    assert call["liquidity_score"] > 50
    assert body["summary"]["put_call_oi_ratio"] == pytest.approx(0.625)
    cboe_client.clear_cache()


def test_options_reports_404_when_no_provider_lists_the_symbol(
    client: TestClient, monkeypatch
):
    _patch_provider(monkeypatch, "yahoo", chain={})
    _patch_provider(monkeypatch, "cboe", chain={})

    response = client.get("/market/options", params={"ticker": "NOPE"})

    assert response.status_code == 404


def test_options_serves_repeat_requests_from_cache(client: TestClient, monkeypatch):
    calls = _patch_options(monkeypatch)

    client.get("/market/options", params={"ticker": "AAPL"})
    client.get("/market/options", params={"ticker": "AAPL"})

    assert len(calls) == 1
