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


def _patch_options(monkeypatch: pytest.MonkeyPatch, chain=None, error=None) -> list[dict]:
    calls: list[dict] = []

    def fake_get_options(symbol: str, *, expiration=None):
        calls.append({"symbol": symbol, "expiration": expiration})
        if error is not None:
            raise error
        return _yahoo_chain() if chain is None else chain

    monkeypatch.setattr(
        "backtest.loaders.yahoo_client.get_options", fake_get_options, raising=True
    )
    return calls


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


def test_options_returns_404_when_the_symbol_has_no_chain(client: TestClient, monkeypatch):
    _patch_options(monkeypatch, chain={})

    response = client.get("/market/options", params={"ticker": "NOPE"})

    assert response.status_code == 404


def test_options_maps_a_provider_failure_to_502(client: TestClient, monkeypatch):
    _patch_options(monkeypatch, error=RuntimeError("yahoo 401"))

    response = client.get("/market/options", params={"ticker": "AAPL"})

    assert response.status_code == 502
    assert "yahoo 401" in response.json()["detail"]


def test_options_serves_repeat_requests_from_cache(client: TestClient, monkeypatch):
    calls = _patch_options(monkeypatch)

    client.get("/market/options", params={"ticker": "AAPL"})
    client.get("/market/options", params={"ticker": "AAPL"})

    assert len(calls) == 1
