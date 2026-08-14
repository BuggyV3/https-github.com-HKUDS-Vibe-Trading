"""Free, keyless delayed option chains from Cboe's public quote CDN.

Yahoo's ``v7/finance/options`` endpoint is the primary chain source, but it is
unofficial: it periodically tightens its cookie/crumb handshake, rate-limits by
IP, or simply starts answering 401 for a while. When that happens the whole
options view goes dark. This module is the second source, so it does not.

Cboe publishes a delayed quote file per underlying at

    https://cdn.cboe.com/api/global/delayed_quotes/options/<SYMBOL>.json

with no key, no account and no handshake. One file carries *every* listed
expiration for the symbol, each contract identified by its OCC symbol, along
with bid/ask, volume, open interest and implied volatility.

:func:`get_options` deliberately returns the same structure as
``yahoo_client.get_options`` — a ``quote`` block, an ``expirationDates`` list
and one ``options[0]`` block of ``calls``/``puts`` in Yahoo's field names — so
callers can swap sources without a second shaping path. The translation is the
whole point of this module.

Two caveats worth knowing, both surfaced to callers rather than hidden:

* The data is delayed (Cboe publishes it as such), the same order of delay as
  the free Yahoo feed. Neither is a real-time OPRA feed.
* One file covers all expirations, and for an active underlying it is large.
  Clicking through an expiry ladder must not re-download it each time, so
  responses are cached per symbol for :data:`_CACHE_TTL_SECONDS`.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backtest.loaders._http import resolve_min_interval, throttled_get_json

logger = logging.getLogger(__name__)

_BASE = "https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"

# Shared throttle bucket. The CDN is far more tolerant than Eastmoney, but one
# file can be several megabytes, so spacing keeps a click-happy dashboard from
# pulling it repeatedly.
HOST_KEY = "cboe"
_MIN_INTERVAL_ENV = "VIBE_TRADING_CBOE_MIN_INTERVAL"
_DEFAULT_MIN_INTERVAL_S = 1.0

# Whole-file cache TTL. Long enough that walking an expiry ladder costs one
# download, short enough that a refresh during the session gets new quotes.
_CACHE_TTL_SECONDS = 60.0
_cache: Dict[str, tuple[float, Dict[str, Any]]] = {}
_cache_lock = threading.Lock()

# OCC contract symbol: root, then YYMMDD, then C/P, then strike in thousandths.
# The root is padded to six characters in the strict OCC form and unpadded in
# Cboe's JSON, so spaces are stripped before matching.
_OCC_RE = re.compile(r"^(?P<root>[A-Z0-9.^/-]+?)(?P<date>\d{6})(?P<side>[CP])(?P<strike>\d{8})$")

# Cboe's per-contract keys -> Yahoo's, so the shaping layer sees one vocabulary.
_FIELD_MAP = (
    ("bid", "bid"),
    ("ask", "ask"),
    ("volume", "volume"),
    ("open_interest", "openInterest"),
    ("iv", "impliedVolatility"),
    ("last_trade_price", "lastPrice"),
)


def _min_interval() -> float:
    """Resolve the minimum spacing between Cboe requests, in seconds."""
    return resolve_min_interval(_MIN_INTERVAL_ENV, _DEFAULT_MIN_INTERVAL_S)


def map_symbol(symbol: str) -> str:
    """Translate a project-side symbol into Cboe's file naming.

    Cboe prefixes index products with an underscore (``_SPX``) where Yahoo uses
    a caret (``^SPX``); a plain equity ticker is unchanged apart from case and a
    dropped ``.US`` suffix.

    Args:
        symbol: Project-side symbol, e.g. ``AAPL``, ``AAPL.US`` or ``^SPX``.

    Returns:
        The symbol as it appears in the CDN path.
    """
    cleaned = symbol.strip().upper()
    if cleaned.endswith(".US"):
        cleaned = cleaned[: -len(".US")]
    if cleaned.startswith("^"):
        return "_" + cleaned[1:]
    return cleaned


def get_options(symbol: str, *, expiration: Optional[int] = None) -> Dict[str, Any]:
    """Fetch one expiration of an option chain, in ``yahoo_client`` shape.

    Args:
        symbol: Underlying symbol (mapped via :func:`map_symbol`).
        expiration: Desired expiration as epoch seconds, matching one of the
            values in the returned ``expirationDates``. Omit for the nearest
            expiration that has not yet passed.

    Returns:
        ``{"quote": {...}, "expirationDates": [...], "options": [{...}]}``, or
        ``{}`` when Cboe lists no options for the symbol.

    Raises:
        requests.RequestException: On a network failure or non-2xx status —
            including the 404 the CDN returns for an unlisted symbol.
        ValueError: If the payload is not the expected JSON object.
    """
    payload = _fetch(map_symbol(symbol))
    return _shape(payload, expiration=expiration)


def _fetch(cboe_symbol: str) -> Dict[str, Any]:
    """Return the (possibly cached) quote file for one symbol."""
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(cboe_symbol)
        if entry is not None and now - entry[0] <= _CACHE_TTL_SECONDS:
            return entry[1]

    payload = throttled_get_json(
        _BASE.format(symbol=cboe_symbol),
        host_key=HOST_KEY,
        min_interval=_min_interval(),
        timeout=30.0,
    )
    if not isinstance(payload, dict):
        raise ValueError(f"Cboe returned a non-object payload for {cboe_symbol}")

    with _cache_lock:
        _cache[cboe_symbol] = (time.monotonic(), payload)
    return payload


def clear_cache() -> None:
    """Drop every cached quote file. Used by tests and forced refreshes."""
    with _cache_lock:
        _cache.clear()


def _shape(payload: Dict[str, Any], *, expiration: Optional[int]) -> Dict[str, Any]:
    """Translate one Cboe quote file into the Yahoo-shaped chain structure."""
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    raw_contracts = data.get("options")
    if not isinstance(raw_contracts, list) or not raw_contracts:
        return {}

    spot = _float(data.get("current_price")) or _float(data.get("close"))

    parsed: List[Dict[str, Any]] = []
    for entry in raw_contracts:
        if not isinstance(entry, dict):
            continue
        contract = _parse_contract(entry, spot=spot)
        if contract is not None:
            parsed.append(contract)
    if not parsed:
        return {}

    expirations = sorted({contract["expiration"] for contract in parsed})
    chosen = _choose_expiration(expirations, expiration)
    selected = [contract for contract in parsed if contract["expiration"] == chosen]

    calls = sorted(
        (c["contract"] for c in selected if c["side"] == "C"),
        key=lambda row: row["strike"],
    )
    puts = sorted(
        (c["contract"] for c in selected if c["side"] == "P"),
        key=lambda row: row["strike"],
    )

    return {
        "quote": {"regularMarketPrice": spot, "currency": "USD"},
        "expirationDates": expirations,
        "options": [{"expirationDate": chosen, "calls": calls, "puts": puts}],
    }


def _choose_expiration(expirations: List[int], requested: Optional[int]) -> int:
    """Pick the expiration to return: the requested one, else the nearest live one.

    An exact match wins. Otherwise this returns the first expiration that has
    not yet passed, matching what Yahoo serves when no date is given, and falls
    back to the last listed one when every expiration is in the past.
    """
    if requested is not None and requested in expirations:
        return requested
    now = int(time.time())
    for epoch in expirations:
        if epoch >= now:
            return epoch
    return expirations[-1]


def _parse_contract(entry: Dict[str, Any], *, spot: Optional[float]) -> Optional[Dict[str, Any]]:
    """Turn one Cboe contract row into a Yahoo-shaped contract plus routing keys.

    Returns:
        ``{"expiration": epoch, "side": "C"|"P", "contract": {...}}``, or
        ``None`` when the OCC symbol is missing or unparseable — a malformed row
        is skipped rather than allowed to poison the chain.
    """
    occ = entry.get("option")
    if not isinstance(occ, str):
        return None
    match = _OCC_RE.match(occ.replace(" ", "").upper())
    if match is None:
        logger.debug("unparseable Cboe contract symbol: %r", occ)
        return None

    try:
        expiry = datetime.strptime(match.group("date"), "%y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        logger.debug("bad expiry in Cboe contract symbol: %r", occ)
        return None
    epoch = int(expiry.timestamp())
    # Strike is carried in thousandths of a currency unit.
    strike = int(match.group("strike")) / 1000.0
    side = match.group("side")

    contract: Dict[str, Any] = {
        "contractSymbol": occ,
        "strike": strike,
        "expiration": epoch,
        "currency": "USD",
    }
    for cboe_key, yahoo_key in _FIELD_MAP:
        contract[yahoo_key] = entry.get(cboe_key)
    if spot is not None:
        contract["inTheMoney"] = strike < spot if side == "C" else strike > spot
    else:
        contract["inTheMoney"] = None

    return {"expiration": epoch, "side": side, "contract": contract}


def _float(value: Any) -> Optional[float]:
    """Coerce a payload cell to float, or ``None`` when it is not numeric."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None
