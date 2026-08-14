"""Shaping and scoring helpers behind the market-discovery dashboard.

Pure functions only. Every input is provider data that a caller already
fetched, and every output is a JSON-safe dict; the HTTP lives in
:mod:`src.api.market_routes`. Keeping the arithmetic here makes the ranking
rules unit-testable without a network round-trip.

Two families of helpers:

* :func:`score_screener_rows` — a cross-sectional percentile blend over an
  Eastmoney full-market screener slice (see ``src.tools.market_screener_tool``),
  used to rank "which names are worth looking at right now".
* :func:`shape_option_chain` — per-contract derived economics (mid, spread,
  moneyness, break-even, Black-Scholes greeks, a liquidity score) plus
  chain-level summary stats over a Yahoo options chain.

Accuracy rules that hold everywhere in this module:

* Derived numbers are arithmetic on provider fields. Nothing is estimated,
  smoothed or back-filled.
* A missing or unusable input yields ``None``, never a zero that would read as
  a real quote.
* Percentile scores are explicitly *relative to the rows handed in*, not to the
  whole market. Callers surface that caveat to the user.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Screener scoring
# ---------------------------------------------------------------------------

# Blend weights for :func:`score_screener_rows`. Turnover value (real money
# traded) dominates because an "opportunity" you cannot get filled in is not
# one; turnover rate and absolute move split the rest. Weights of components
# whose input is missing are redistributed across the components that are
# present, so a row with no turnover-rate cell is not silently penalised.
SCREENER_WEIGHTS: dict[str, float] = {
    "liquidity": 0.4,  # percentile of turnover value (amount)
    "activity": 0.3,  # percentile of turnover rate
    "momentum": 0.3,  # percentile of |change percent|
}


def percentile_ranks(values: Sequence[float | None]) -> list[float | None]:
    """Rank ``values`` into 0-100 percentiles, highest value scoring 100.

    Ties share the average of the ranks they span, so two identical turnover
    figures cannot be ordered arbitrarily by list position. ``None`` inputs stay
    ``None`` and take no part in the ranking.

    Args:
        values: The cross-section to rank. May contain ``None`` holes.

    Returns:
        A list the same length as ``values``: a float in ``[0, 100]`` for each
        ranked entry, ``None`` where the input was ``None``. A single ranked
        value scores 100.
    """
    present = [(index, value) for index, value in enumerate(values) if value is not None]
    ranks: list[float | None] = [None] * len(values)
    if not present:
        return ranks
    if len(present) == 1:
        ranks[present[0][0]] = 100.0
        return ranks

    present.sort(key=lambda pair: pair[1])
    span = len(present) - 1
    position = 0
    while position < len(present):
        # Walk the run of equal values and give every member the mean position.
        end = position
        while end + 1 < len(present) and present[end + 1][1] == present[position][1]:
            end += 1
        shared = (position + end) / 2
        score = 100.0 * shared / span
        for offset in range(position, end + 1):
            ranks[present[offset][0]] = score
        position = end + 1
    return ranks


def _weighted_blend(components: Mapping[str, float | None]) -> float | None:
    """Blend present percentile components using :data:`SCREENER_WEIGHTS`.

    Args:
        components: Mapping of weight key -> percentile (or ``None``).

    Returns:
        The weight-normalised blend in ``[0, 100]``, or ``None`` when every
        component is missing.
    """
    total_weight = 0.0
    total = 0.0
    for key, score in components.items():
        if score is None:
            continue
        weight = SCREENER_WEIGHTS[key]
        total += weight * score
        total_weight += weight
    if total_weight <= 0:
        return None
    return round(total / total_weight, 2)


def score_screener_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Attach cross-sectional percentile scores to screener rows.

    Each row gains four fields:

    * ``liquidity_pct`` — percentile of ``amount`` (turnover value).
    * ``activity_pct`` — percentile of ``turnover_rate``.
    * ``momentum_pct`` — percentile of ``abs(change_pct)``; direction is left in
      the untouched ``change_pct`` field so a dashboard can colour it.
    * ``flow_score`` — the :data:`SCREENER_WEIGHTS` blend of the three.

    The percentiles are computed *within the rows passed in*. Ranking the top 30
    by turnover and ranking the top 200 give different percentiles for the same
    instrument, which is correct — the score answers "how does this name compare
    to the others on screen", not "how does it compare to the whole market".

    Args:
        rows: Screener records shaped by ``market_screener_tool``. Unknown extra
            keys are preserved.

    Returns:
        A new list of shallow-copied dicts with the score fields added. Input
        rows are never mutated.
    """
    materialised = [dict(row) for row in rows]
    if not materialised:
        return []

    amounts = [_finite(row.get("amount")) for row in materialised]
    turnovers = [_finite(row.get("turnover_rate")) for row in materialised]
    moves = [
        None if (value := _finite(row.get("change_pct"))) is None else abs(value)
        for row in materialised
    ]

    liquidity = percentile_ranks(amounts)
    activity = percentile_ranks(turnovers)
    momentum = percentile_ranks(moves)

    for index, row in enumerate(materialised):
        components = {
            "liquidity": liquidity[index],
            "activity": activity[index],
            "momentum": momentum[index],
        }
        row["liquidity_pct"] = _round_or_none(liquidity[index])
        row["activity_pct"] = _round_or_none(activity[index])
        row["momentum_pct"] = _round_or_none(momentum[index])
        row["flow_score"] = _weighted_blend(components)
    return materialised


# ---------------------------------------------------------------------------
# Options chain shaping
# ---------------------------------------------------------------------------

# Contracts per side kept after enrichment. A deep chain on a liquid name runs
# to several hundred strikes per expiry; the dashboard only ever renders a
# moneyness band around spot, so the cap bounds the payload without hiding
# anything a trader would act on.
MAX_CONTRACTS_PER_SIDE = 120

# Liquidity-score calibration. A quoted spread at or above ``_SPREAD_FLOOR`` of
# mid scores zero on the spread leg; open interest at or above ``_OI_FULL`` and
# session volume at or above ``_VOLUME_FULL`` score full marks on their legs.
# These are round numbers chosen to separate genuinely tradeable US equity
# contracts from the long tail of stale strikes — they are a display heuristic,
# not a model output, and the API reports them so the UI can say so.
_SPREAD_FLOOR = 0.25
_OI_FULL = 5_000.0
_VOLUME_FULL = 1_000.0
_LIQUIDITY_WEIGHTS = {"spread": 0.5, "open_interest": 0.3, "volume": 0.2}

# Trading days are irrelevant for time-to-expiry here: Black-Scholes takes
# calendar time, and Yahoo expirations are calendar dates.
_DAYS_PER_YEAR = 365.0

# Yahoo contract fields -> our snake_case keys, mirroring
# ``src.tools.options_chain_tool`` so the tool and the dashboard agree.
_CONTRACT_FIELDS = (
    ("contractSymbol", "contract_symbol"),
    ("strike", "strike"),
    ("lastPrice", "last_price"),
    ("bid", "bid"),
    ("ask", "ask"),
    ("volume", "volume"),
    ("openInterest", "open_interest"),
    ("impliedVolatility", "implied_volatility"),
    ("inTheMoney", "in_the_money"),
    ("expiration", "expiration"),
    ("currency", "currency"),
)


def shape_option_chain(
    result: Mapping[str, Any],
    *,
    ticker: str,
    risk_free_rate: float = 0.04,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Turn one Yahoo ``optionChain.result[0]`` mapping into the dashboard shape.

    Args:
        result: The mapping returned by ``yahoo_client.get_options``.
        ticker: Underlying symbol, echoed back for the client.
        risk_free_rate: Continuously compounded annual rate used for greeks and
            for the implied-volatility fallback. Echoed in the payload so the
            UI can show which rate produced the numbers.
        now: Clock override for tests. Defaults to the current UTC time.

    Returns:
        A dict with ``ticker``, ``spot``, ``currency``, ``expiration``,
        ``expirations``, ``as_of``, ``risk_free_rate``, ``calls``, ``puts`` and
        ``summary``. ``spot`` is ``None`` when Yahoo omits the quote block, in
        which case moneyness, break-even and greeks are ``None`` throughout —
        the chain still renders, with the price-dependent columns blank.
    """
    moment = now or datetime.now(timezone.utc)
    quote = result.get("quote") if isinstance(result.get("quote"), Mapping) else {}
    spot = _finite(quote.get("regularMarketPrice"))
    currency = quote.get("currency") if isinstance(quote.get("currency"), str) else None

    options = result.get("options") or []
    block = options[0] if options and isinstance(options[0], Mapping) else {}
    expiration = _int_or_none(block.get("expirationDate"))
    expirations = [
        epoch
        for epoch in (_int_or_none(value) for value in result.get("expirationDates") or [])
        if epoch is not None
    ]

    years, days = _time_to_expiry(expiration, moment)
    calls = _shape_side(
        block.get("calls"),
        side="call",
        spot=spot,
        years=years,
        days=days,
        risk_free_rate=risk_free_rate,
    )
    puts = _shape_side(
        block.get("puts"),
        side="put",
        spot=spot,
        years=years,
        days=days,
        risk_free_rate=risk_free_rate,
    )

    return {
        "ticker": ticker,
        "spot": spot,
        "currency": currency,
        "expiration": expiration,
        "expirations": expirations,
        "days_to_expiry": days,
        "risk_free_rate": risk_free_rate,
        "as_of": moment.isoformat(timespec="seconds"),
        "calls": calls,
        "puts": puts,
        "summary": summarize_chain(calls, puts, spot=spot),
    }


def _shape_side(
    raw: Any,
    *,
    side: str,
    spot: float | None,
    years: float | None,
    days: float | None,
    risk_free_rate: float,
) -> list[dict[str, Any]]:
    """Normalise and enrich one calls/puts array, capped and strike-sorted."""
    if not isinstance(raw, list):
        return []
    contracts = [
        _enrich_contract(
            entry,
            side=side,
            spot=spot,
            years=years,
            days=days,
            risk_free_rate=risk_free_rate,
        )
        for entry in raw
        if isinstance(entry, Mapping)
    ]
    # Strikes sort ascending with unpriced rows last; the ``or 0.0`` only ever
    # applies to the None bucket, which the first key element already isolates.
    contracts.sort(key=lambda row: (row["strike"] is None, row["strike"] or 0.0))
    if len(contracts) <= MAX_CONTRACTS_PER_SIDE:
        return contracts
    return _keep_around_spot(contracts, spot)


def _keep_around_spot(
    contracts: list[dict[str, Any]], spot: float | None
) -> list[dict[str, Any]]:
    """Trim a long chain to :data:`MAX_CONTRACTS_PER_SIDE` strikes around spot.

    Truncating from the front would drop the deep-ITM wing and truncating from
    the back would drop the deep-OTM wing; either way the window a trader
    actually looks at can fall off the edge. Centring the window on the strike
    nearest spot keeps the tradeable band whichever way the chain is skewed.
    """
    if spot is None:
        return contracts[:MAX_CONTRACTS_PER_SIDE]
    strikes = [row["strike"] for row in contracts]
    centre = min(
        (index for index, strike in enumerate(strikes) if strike is not None),
        key=lambda index: abs(strikes[index] - spot),
        default=0,
    )
    half = MAX_CONTRACTS_PER_SIDE // 2
    start = max(0, min(centre - half, len(contracts) - MAX_CONTRACTS_PER_SIDE))
    return contracts[start : start + MAX_CONTRACTS_PER_SIDE]


def _enrich_contract(
    entry: Mapping[str, Any],
    *,
    side: str,
    spot: float | None,
    years: float | None,
    days: float | None,
    risk_free_rate: float,
) -> dict[str, Any]:
    """Build one enriched contract row from a raw Yahoo contract mapping.

    Derived fields, all ``None`` when their inputs are unusable:

    * ``mid`` — ``(bid + ask) / 2``, only when both sides are quoted above zero.
      A one-sided or zero quote is a stale strike, and averaging it in would
      manufacture a price no one will trade at.
    * ``spread`` / ``spread_pct`` — ``ask - bid`` in currency and as a fraction
      of mid; the single best read on whether a contract is really tradeable.
    * ``moneyness`` — ``strike / spot - 1``, signed the same way for both sides.
    * ``break_even`` — strike plus mid for calls, strike minus mid for puts, at
      expiry and ignoring commissions.
    * ``volume_oi_ratio`` — session volume over open interest; high values flag
      today's flow rather than legacy positioning.
    * ``greeks`` — Black-Scholes delta/gamma/theta/vega/rho from the contract's
      implied volatility.
    * ``liquidity_score`` — see :func:`liquidity_score`.
    * ``iv_source`` — ``"provider"`` when Yahoo quoted the IV, ``"implied"``
      when it was inverted from mid, ``None`` when neither was possible.
    """
    row: dict[str, Any] = {
        our_key: entry.get(yahoo_key) for yahoo_key, our_key in _CONTRACT_FIELDS
    }
    row["side"] = side

    strike = _finite(row.get("strike"))
    bid = _finite(row.get("bid"))
    ask = _finite(row.get("ask"))
    volume = _finite(row.get("volume"))
    open_interest = _finite(row.get("open_interest"))

    row["strike"] = strike
    row["bid"] = bid
    row["ask"] = ask
    row["last_price"] = _finite(row.get("last_price"))
    row["volume"] = volume
    row["open_interest"] = open_interest

    mid: float | None = None
    spread: float | None = None
    spread_pct: float | None = None
    if bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid:
        mid = (bid + ask) / 2
        spread = ask - bid
        spread_pct = spread / mid if mid > 0 else None
    row["mid"] = _round_or_none(mid, 4)
    row["spread"] = _round_or_none(spread, 4)
    row["spread_pct"] = _round_or_none(spread_pct, 5)

    row["moneyness"] = (
        _round_or_none(strike / spot - 1, 5)
        if strike is not None and spot is not None and spot > 0
        else None
    )
    if strike is not None and mid is not None:
        row["break_even"] = round(strike + mid if side == "call" else strike - mid, 4)
    else:
        row["break_even"] = None
    row["volume_oi_ratio"] = (
        _round_or_none(volume / open_interest, 3)
        if volume is not None and open_interest is not None and open_interest > 0
        else None
    )
    row["notional"] = _round_or_none(mid * 100, 2) if mid is not None else None

    iv, iv_source = _resolve_iv(
        provider_iv=_finite(row.get("implied_volatility")),
        mid=mid,
        spot=spot,
        strike=strike,
        years=years,
        side=side,
        risk_free_rate=risk_free_rate,
    )
    row["implied_volatility"] = _round_or_none(iv, 6)
    row["iv_source"] = iv_source
    row["days_to_expiry"] = _round_or_none(days, 2)
    row["greeks"] = _greeks(
        spot=spot,
        strike=strike,
        years=years,
        sigma=iv,
        side=side,
        risk_free_rate=risk_free_rate,
    )
    row["liquidity_score"] = liquidity_score(
        spread_pct=spread_pct, open_interest=open_interest, volume=volume
    )
    return row


def liquidity_score(
    *,
    spread_pct: float | None,
    open_interest: float | None,
    volume: float | None,
) -> float | None:
    """Score how tradeable a contract looks, on 0-100.

    Three legs, blended by :data:`_LIQUIDITY_WEIGHTS`:

    * spread (weight 0.5) — ``1 - spread_pct / 0.25``, clipped to ``[0, 1]``, so
      a quote wider than a quarter of its own mid scores zero.
    * open interest (0.3) — ``log10(1 + oi) / log10(1 + 5000)``, clipped to 1.
    * volume (0.2) — ``log10(1 + volume) / log10(1 + 1000)``, clipped to 1.

    Log scaling matters: the difference between 10 and 500 open contracts is
    what a trader cares about, and a linear scale would flatten it against the
    handful of strikes carrying six figures of interest.

    Missing legs are dropped and the remaining weights renormalised, so a
    contract with no volume cell is scored on spread and open interest rather
    than being marked illiquid. When every leg is missing the score is ``None``.

    Args:
        spread_pct: Quoted spread as a fraction of mid.
        open_interest: Open contracts.
        volume: Session volume.

    Returns:
        A float in ``[0, 100]`` rounded to one decimal, or ``None``.
    """
    legs: dict[str, float] = {}
    if spread_pct is not None and spread_pct >= 0:
        legs["spread"] = max(0.0, 1.0 - spread_pct / _SPREAD_FLOOR)
    if open_interest is not None and open_interest >= 0:
        legs["open_interest"] = min(
            1.0, math.log10(1 + open_interest) / math.log10(1 + _OI_FULL)
        )
    if volume is not None and volume >= 0:
        legs["volume"] = min(1.0, math.log10(1 + volume) / math.log10(1 + _VOLUME_FULL))
    if not legs:
        return None
    total_weight = sum(_LIQUIDITY_WEIGHTS[name] for name in legs)
    blended = sum(_LIQUIDITY_WEIGHTS[name] * value for name, value in legs.items())
    return round(100.0 * blended / total_weight, 1)


def summarize_chain(
    calls: Sequence[Mapping[str, Any]],
    puts: Sequence[Mapping[str, Any]],
    *,
    spot: float | None,
) -> dict[str, Any]:
    """Compute chain-level statistics a trader reads before picking a strike.

    Args:
        calls: Enriched call rows.
        puts: Enriched put rows.
        spot: Underlying price, or ``None`` when the quote was unavailable.

    Returns:
        A dict with total call/put open interest and volume, the put/call ratios
        for each, the at-the-money implied volatility (the mean of the call and
        put IV at the strike nearest spot), and the median quoted spread
        percentage across both sides. Any statistic whose inputs are absent is
        ``None`` rather than zero.
    """
    call_oi = _total(calls, "open_interest")
    put_oi = _total(puts, "open_interest")
    call_volume = _total(calls, "volume")
    put_volume = _total(puts, "volume")
    spreads = [
        value
        for row in (*calls, *puts)
        if (value := row.get("spread_pct")) is not None
    ]
    return {
        "call_open_interest": call_oi,
        "put_open_interest": put_oi,
        "call_volume": call_volume,
        "put_volume": put_volume,
        "put_call_oi_ratio": _ratio(put_oi, call_oi),
        "put_call_volume_ratio": _ratio(put_volume, call_volume),
        "atm_iv": _atm_iv(calls, puts, spot=spot),
        "median_spread_pct": _round_or_none(_median(spreads), 5),
        "contracts": len(calls) + len(puts),
    }


def _atm_iv(
    calls: Sequence[Mapping[str, Any]],
    puts: Sequence[Mapping[str, Any]],
    *,
    spot: float | None,
) -> float | None:
    """Mean of the call and put implied vol at the strike closest to spot."""
    if spot is None:
        return None
    ivs = [
        iv
        for side in (calls, puts)
        if (nearest := _nearest_strike(side, spot)) is not None
        and (iv := _finite(nearest.get("implied_volatility"))) is not None
    ]
    if not ivs:
        return None
    return round(sum(ivs) / len(ivs), 6)


def _nearest_strike(
    rows: Sequence[Mapping[str, Any]], spot: float
) -> Mapping[str, Any] | None:
    """Return the row whose strike sits closest to ``spot``."""
    candidates = [row for row in rows if _finite(row.get("strike")) is not None]
    if not candidates:
        return None
    return min(candidates, key=lambda row: abs(float(row["strike"]) - spot))


def _greeks(
    *,
    spot: float | None,
    strike: float | None,
    years: float | None,
    sigma: float | None,
    side: str,
    risk_free_rate: float,
) -> dict[str, float] | None:
    """Black-Scholes greeks for one contract, or ``None`` when inputs are unfit.

    Greeks need a spot, a strike, positive time and a positive volatility. Any
    of those missing means there is no honest number to report, so the caller
    gets ``None`` and the dashboard leaves the cells blank.
    """
    if spot is None or strike is None or years is None:
        return None
    if spot <= 0 or strike <= 0 or years <= 0:
        return None
    if sigma is None or sigma <= 0:
        return None
    from src.quantlib.options import bs_greeks

    try:
        greeks = bs_greeks(spot, strike, years, risk_free_rate, sigma, side)
    except (ValueError, ZeroDivisionError, OverflowError) as exc:
        logger.debug("greeks failed for %s strike=%s: %s", side, strike, exc)
        return None
    return {
        name: round(value, 6)
        for name, value in greeks.items()
        if isinstance(value, (int, float)) and math.isfinite(value)
    }


def _resolve_iv(
    *,
    provider_iv: float | None,
    mid: float | None,
    spot: float | None,
    strike: float | None,
    years: float | None,
    side: str,
    risk_free_rate: float,
) -> tuple[float | None, str | None]:
    """Pick the implied volatility to report, inverting from mid if needed.

    Yahoo quotes an implied volatility for most contracts but leaves it at zero
    on thin strikes. Where that happens and a two-sided mid exists, the vol is
    recovered from the mid price with the project's own Black-Scholes inversion
    rather than left blank. The source is reported alongside so the UI can mark
    which numbers came from the provider.

    Returns:
        ``(iv, source)`` where source is ``"provider"``, ``"implied"`` or
        ``None``.
    """
    if provider_iv is not None and provider_iv > 0:
        return provider_iv, "provider"
    if mid is None or spot is None or strike is None or years is None:
        return None, None
    if spot <= 0 or strike <= 0 or years <= 0 or mid <= 0:
        return None, None
    from src.quantlib.options import implied_volatility

    try:
        iv = implied_volatility(mid, spot, strike, years, risk_free_rate, side)
    except (ValueError, ZeroDivisionError, OverflowError) as exc:
        logger.debug("iv inversion failed for %s strike=%s: %s", side, strike, exc)
        return None, None
    if iv is None or not math.isfinite(iv) or iv <= 0:
        return None, None
    return float(iv), "implied"


def _time_to_expiry(
    expiration: int | None, now: datetime
) -> tuple[float | None, float | None]:
    """Convert an epoch-second expiration into (years, days) remaining.

    Yahoo stamps expirations at 00:00 UTC on the expiry date while US equity
    options actually stop trading that afternoon, so the figure is accurate to
    within a fraction of a day — fine for greeks, and the API reports the day
    count it used rather than hiding the assumption.

    Returns:
        ``(years, days)``, both ``None`` when the expiration is missing, and
        both ``0.0`` for an expiration already in the past.
    """
    if expiration is None:
        return None, None
    expiry = datetime.fromtimestamp(expiration, tz=timezone.utc)
    seconds = (expiry - now).total_seconds()
    if seconds <= 0:
        return 0.0, 0.0
    days = seconds / 86_400.0
    return days / _DAYS_PER_YEAR, days


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _finite(value: Any) -> float | None:
    """Coerce a provider cell to a finite float, or ``None``.

    Booleans are rejected outright: ``inTheMoney`` sits next to the numeric
    fields in the same dict, and ``float(True) == 1.0`` would turn a flag into a
    price.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip() in {"", "-", "N/A"}:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _int_or_none(value: Any) -> int | None:
    """Coerce a value to ``int``, or ``None`` when it is not a whole number."""
    number = _finite(value)
    return None if number is None else int(number)


def _round_or_none(value: float | None, digits: int = 2) -> float | None:
    """Round ``value`` to ``digits`` places, passing ``None`` through."""
    return None if value is None else round(value, digits)


def _total(rows: Sequence[Mapping[str, Any]], key: str) -> float | None:
    """Sum one numeric column, returning ``None`` when no row carries it."""
    values = [value for row in rows if (value := _finite(row.get(key))) is not None]
    return sum(values) if values else None


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    """Divide two optional totals, guarding a zero or missing denominator."""
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def _median(values: Sequence[float]) -> float | None:
    """Median of a numeric sequence, or ``None`` when it is empty."""
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2
