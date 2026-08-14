"""Unit tests for the market-discovery scoring and chain-shaping helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.market_discovery import (
    liquidity_score,
    percentile_ranks,
    score_screener_rows,
    shape_option_chain,
    summarize_chain,
)

NOW = datetime(2026, 1, 2, 15, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# percentile_ranks
# ---------------------------------------------------------------------------


def test_percentile_ranks_spans_zero_to_one_hundred():
    assert percentile_ranks([1.0, 2.0, 3.0]) == [0.0, 50.0, 100.0]


def test_percentile_ranks_gives_ties_the_same_average_rank():
    # Two identical values must not be ordered by list position.
    ranks = percentile_ranks([5.0, 5.0, 9.0])
    assert ranks[0] == ranks[1] == 25.0
    assert ranks[2] == 100.0


def test_percentile_ranks_preserves_none_holes():
    assert percentile_ranks([1.0, None, 3.0]) == [0.0, None, 100.0]


def test_percentile_ranks_single_value_scores_top():
    assert percentile_ranks([None, 7.0]) == [None, 100.0]


def test_percentile_ranks_all_missing_returns_all_none():
    assert percentile_ranks([None, None]) == [None, None]


# ---------------------------------------------------------------------------
# score_screener_rows
# ---------------------------------------------------------------------------


def _row(**kwargs):
    base = {
        "code": "AAA",
        "name": "Test",
        "price": 10.0,
        "change_pct": 1.0,
        "change": 0.1,
        "volume": 1000.0,
        "amount": 1_000_000.0,
        "turnover_rate": 2.0,
    }
    base.update(kwargs)
    return base


def test_score_screener_rows_ranks_liquidity_and_blends():
    rows = score_screener_rows(
        [
            _row(code="LOW", amount=1.0, turnover_rate=1.0, change_pct=1.0),
            _row(code="HIGH", amount=100.0, turnover_rate=10.0, change_pct=9.0),
        ]
    )

    low, high = rows
    assert low["liquidity_pct"] == 0.0
    assert high["liquidity_pct"] == 100.0
    assert low["flow_score"] == 0.0
    assert high["flow_score"] == 100.0


def test_score_screener_rows_uses_absolute_move_for_momentum():
    # A -9% name is as interesting to a trader as a +9% one; direction stays
    # readable in change_pct.
    rows = score_screener_rows(
        [_row(code="DOWN", change_pct=-9.0), _row(code="FLAT", change_pct=0.5)]
    )

    assert rows[0]["momentum_pct"] == 100.0
    assert rows[0]["change_pct"] == -9.0
    assert rows[1]["momentum_pct"] == 0.0


def test_score_screener_rows_renormalises_when_a_component_is_missing():
    # Halted names come back with no turnover-rate cell. The row still scores on
    # the components it does have rather than being dragged to the bottom.
    rows = score_screener_rows(
        [
            _row(code="HALTED", turnover_rate=None, amount=100.0, change_pct=9.0),
            _row(code="OTHER", turnover_rate=1.0, amount=1.0, change_pct=0.1),
        ]
    )

    halted = rows[0]
    assert halted["activity_pct"] is None
    assert halted["flow_score"] == 100.0


def test_score_screener_rows_does_not_mutate_input():
    original = _row()
    score_screener_rows([original])
    assert "flow_score" not in original


def test_score_screener_rows_handles_empty_input():
    assert score_screener_rows([]) == []


# ---------------------------------------------------------------------------
# liquidity_score
# ---------------------------------------------------------------------------


def test_liquidity_score_rewards_tight_deep_contracts():
    tight = liquidity_score(spread_pct=0.01, open_interest=8000, volume=2000)
    wide = liquidity_score(spread_pct=0.40, open_interest=3, volume=0)

    assert tight > 95
    # A 40%-wide quote on three open contracts scores only the sliver its depth
    # leg earns — nowhere near tradeable.
    assert wide < 10


def test_liquidity_score_zeroes_the_spread_leg_past_the_floor():
    # A quote wider than a quarter of its own mid earns nothing on spread, but
    # depth still counts.
    scored = liquidity_score(spread_pct=0.5, open_interest=5000, volume=1000)
    assert scored == pytest.approx(50.0, abs=0.5)


def test_liquidity_score_renormalises_around_a_missing_leg():
    without_volume = liquidity_score(spread_pct=0.01, open_interest=8000, volume=None)
    assert without_volume > 95


def test_liquidity_score_returns_none_without_any_input():
    assert liquidity_score(spread_pct=None, open_interest=None, volume=None) is None


# ---------------------------------------------------------------------------
# shape_option_chain
# ---------------------------------------------------------------------------


def _chain(**overrides):
    expiry = int((NOW + timedelta(days=30)).timestamp())
    call = {
        "contractSymbol": "TEST260201C00100000",
        "strike": 100.0,
        "lastPrice": 5.0,
        "bid": 4.9,
        "ask": 5.1,
        "volume": 250,
        "openInterest": 1200,
        "impliedVolatility": 0.31,
        "inTheMoney": False,
        "expiration": expiry,
    }
    put = {
        "contractSymbol": "TEST260201P00100000",
        "strike": 100.0,
        "lastPrice": 4.0,
        "bid": 3.9,
        "ask": 4.1,
        "volume": 100,
        "openInterest": 800,
        "impliedVolatility": 0.29,
        "inTheMoney": False,
        "expiration": expiry,
    }
    chain = {
        "quote": {"regularMarketPrice": 100.0, "currency": "USD"},
        "expirationDates": [expiry, expiry + 604_800],
        "options": [{"expirationDate": expiry, "calls": [call], "puts": [put]}],
    }
    chain.update(overrides)
    return chain


def test_shape_option_chain_derives_mid_spread_and_break_even():
    shaped = shape_option_chain(_chain(), ticker="TEST", now=NOW)

    call = shaped["calls"][0]
    assert call["mid"] == pytest.approx(5.0)
    assert call["spread"] == pytest.approx(0.2)
    assert call["spread_pct"] == pytest.approx(0.04)
    assert call["break_even"] == pytest.approx(105.0)
    assert call["notional"] == pytest.approx(500.0)

    put = shaped["puts"][0]
    assert put["break_even"] == pytest.approx(96.0)


def test_shape_option_chain_reports_moneyness_and_days():
    shaped = shape_option_chain(_chain(), ticker="TEST", now=NOW)

    assert shaped["spot"] == 100.0
    assert shaped["currency"] == "USD"
    assert shaped["days_to_expiry"] == pytest.approx(30.0, abs=0.01)
    assert shaped["calls"][0]["moneyness"] == pytest.approx(0.0)


def test_shape_option_chain_computes_greeks_from_provider_iv():
    shaped = shape_option_chain(_chain(), ticker="TEST", now=NOW)

    greeks = shaped["calls"][0]["greeks"]
    # A 30-day at-the-money call sits near 0.5 delta with positive gamma/vega
    # and time decay working against the holder.
    assert 0.4 < greeks["delta"] < 0.65
    assert greeks["gamma"] > 0
    assert greeks["vega"] > 0
    assert greeks["theta"] < 0
    assert shaped["calls"][0]["iv_source"] == "provider"


def test_shape_option_chain_inverts_iv_when_the_provider_omits_it():
    chain = _chain()
    chain["options"][0]["calls"][0]["impliedVolatility"] = 0.0

    shaped = shape_option_chain(chain, ticker="TEST", now=NOW)

    call = shaped["calls"][0]
    assert call["iv_source"] == "implied"
    assert call["implied_volatility"] > 0
    assert call["greeks"] is not None


def test_shape_option_chain_leaves_one_sided_quotes_unpriced():
    # A zero bid is a stale strike: averaging it into a mid would invent a price
    # nobody will trade at, so every mid-derived field stays blank.
    chain = _chain()
    chain["options"][0]["calls"][0]["bid"] = 0.0

    call = shape_option_chain(chain, ticker="TEST", now=NOW)["calls"][0]

    assert call["mid"] is None
    assert call["spread_pct"] is None
    assert call["break_even"] is None
    assert call["notional"] is None


def test_shape_option_chain_survives_a_missing_quote_block():
    chain = _chain()
    chain.pop("quote")

    shaped = shape_option_chain(chain, ticker="TEST", now=NOW)

    assert shaped["spot"] is None
    call = shaped["calls"][0]
    assert call["moneyness"] is None
    assert call["greeks"] is None
    # Quote-independent economics still resolve.
    assert call["mid"] == pytest.approx(5.0)


def test_shape_option_chain_zeroes_time_for_a_past_expiration():
    chain = _chain()
    past = int((NOW - timedelta(days=1)).timestamp())
    chain["options"][0]["expirationDate"] = past

    shaped = shape_option_chain(chain, ticker="TEST", now=NOW)

    assert shaped["days_to_expiry"] == 0.0
    assert shaped["calls"][0]["greeks"] is None


def test_shape_option_chain_handles_an_empty_result():
    shaped = shape_option_chain({}, ticker="TEST", now=NOW)

    assert shaped["calls"] == []
    assert shaped["puts"] == []
    assert shaped["expirations"] == []
    assert shaped["summary"]["contracts"] == 0


def test_shape_option_chain_never_reads_in_the_money_as_a_number():
    # ``inTheMoney`` sits beside the numeric fields; float(True) would be 1.0.
    shaped = shape_option_chain(_chain(), ticker="TEST", now=NOW)
    assert shaped["calls"][0]["in_the_money"] is False


def test_shape_option_chain_caps_a_deep_chain_around_spot():
    expiry = int((NOW + timedelta(days=30)).timestamp())
    calls = [
        {
            "contractSymbol": f"TEST{strike}",
            "strike": float(strike),
            "bid": 1.0,
            "ask": 1.2,
            "volume": 1,
            "openInterest": 1,
            "impliedVolatility": 0.3,
            "expiration": expiry,
        }
        for strike in range(1, 401)
    ]
    chain = _chain()
    chain["options"][0]["calls"] = calls

    shaped = shape_option_chain(chain, ticker="TEST", now=NOW)

    strikes = [row["strike"] for row in shaped["calls"]]
    assert len(strikes) == 120
    # Spot is 100, so the retained window must straddle it rather than starting
    # at the deepest in-the-money strike.
    assert min(strikes) < 100.0 < max(strikes)


# ---------------------------------------------------------------------------
# summarize_chain
# ---------------------------------------------------------------------------


def test_summarize_chain_reports_put_call_ratios_and_atm_iv():
    shaped = shape_option_chain(_chain(), ticker="TEST", now=NOW)
    summary = shaped["summary"]

    assert summary["call_open_interest"] == 1200
    assert summary["put_open_interest"] == 800
    assert summary["put_call_oi_ratio"] == pytest.approx(0.6667, abs=1e-4)
    assert summary["put_call_volume_ratio"] == pytest.approx(0.4)
    assert summary["atm_iv"] == pytest.approx(0.30, abs=1e-6)
    assert summary["contracts"] == 2


def test_summarize_chain_returns_none_rather_than_zero_when_empty():
    summary = summarize_chain([], [], spot=100.0)

    assert summary["call_open_interest"] is None
    assert summary["put_call_oi_ratio"] is None
    assert summary["atm_iv"] is None
    assert summary["median_spread_pct"] is None
    assert summary["contracts"] == 0
