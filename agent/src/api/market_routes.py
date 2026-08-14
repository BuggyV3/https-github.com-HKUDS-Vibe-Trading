"""Market-discovery HTTP routes for the Web UI dashboard.

Mounted by ``agent/api_server.py`` via ``register_market_routes(app)``. These
are read-only market-data reads that back the ``/markets`` dashboard — finding
which instruments are moving and which option contracts are actually tradeable.
Nothing here places, sizes or proposes an order; the live-trading surface stays
in :mod:`src.api.live_routes`.

Routes (auth via the caller-supplied ``require_auth`` dependency):

- ``GET /market/screener`` — full-market movers/turnover ranking (Eastmoney)
- ``GET /market/options``  — one expiration's enriched option chain (Yahoo)

Both reuse the throttled provider clients the agent tools already use
(``backtest.loaders.eastmoney_client`` / ``yahoo_client``), so dashboard polling
shares the same per-host rate limiting as tool calls and cannot get the process
IP-banned by opening a second, unthrottled path to the provider.

Freshness: every response carries ``as_of`` (server UTC at fetch) and
``source``. Responses are cached in-process for :data:`_CACHE_TTL_SECONDS` so a
dashboard on a short refresh interval — or several open tabs — collapses onto
one upstream request. ``as_of`` is the time of the *underlying fetch*, never the
time of the cache hit, so a stale panel is visible as stale.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from fastapi import Depends, FastAPI, HTTPException, Query

from src.market_discovery import score_screener_rows, shape_option_chain

logger = logging.getLogger(__name__)

AuthDep = Callable[..., Awaitable[None]]

# Upstream quote lists move on a seconds cadence and the dashboard polls on a
# user-chosen interval, so a short shared TTL keeps several open tabs (and a
# 10-second refresh) down to one provider request without ever showing a figure
# the user could mistake for live.
_CACHE_TTL_SECONDS = 8.0
_CACHE_MAX_ENTRIES = 64
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()

_VALID_MARKETS = ("a", "us", "hk")
_VALID_SORTS = ("change_pct", "volume", "amount", "turnover")
_MAX_TOP_N = 100

# US option underlyings: letters, digits and the separators Yahoo accepts
# (BRK-B, RDS.A). Bounded so a hostile query string cannot become a long path
# segment against the upstream host.
_TICKER_RE = re.compile(r"^[A-Za-z0-9.\-^]{1,16}$")

# Bounds on the greeks/IV discount rate. Negative policy rates are real, and
# nothing above 25% is a risk-free rate anyone should be pricing with.
_MIN_RATE = -0.05
_MAX_RATE = 0.25


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _cached(key: str) -> dict[str, Any] | None:
    """Return a live cache entry for ``key``, or ``None`` when absent/expired."""
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        stored_at, payload = entry
        if time.monotonic() - stored_at > _CACHE_TTL_SECONDS:
            _cache.pop(key, None)
            return None
        return payload


def _store(key: str, payload: dict[str, Any]) -> None:
    """Cache ``payload`` under ``key``, evicting the oldest entries when full."""
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX_ENTRIES:
            for stale_key in sorted(_cache, key=lambda k: _cache[k][0])[
                : len(_cache) - _CACHE_MAX_ENTRIES + 1
            ]:
                _cache.pop(stale_key, None)
        _cache[key] = (time.monotonic(), payload)


def clear_cache() -> None:
    """Drop every cached response. Used by tests and on-demand refreshes."""
    with _cache_lock:
        _cache.clear()


def _upstream_error(what: str, exc: Exception) -> HTTPException:
    """Map a provider failure onto a 502 without leaking a stack frame.

    The provider's own message is short and useful ("HTTP 404", "connection
    reset"), so it is passed through; anything longer is truncated rather than
    handed to the browser whole.
    """
    logger.warning("%s fetch failed: %s", what, exc, exc_info=True)
    detail = str(exc).strip() or exc.__class__.__name__
    return HTTPException(status_code=502, detail=f"{what} unavailable: {detail[:200]}")


def register_market_routes(app: FastAPI, require_auth: AuthDep | None = None) -> None:
    """Mount the market-discovery routes onto ``app``.

    Args:
        app: The host FastAPI app.
        require_auth: Header-auth dependency for these JSON endpoints. Resolved
            from the host ``api_server`` module when omitted, matching
            ``register_alpha_routes``.
    """
    if require_auth is None:
        import sys as _sys

        host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        if host is None:  # pragma: no cover — only on unusual import setups
            raise RuntimeError(
                "register_market_routes: api_server module not in sys.modules; "
                "pass require_auth explicitly"
            )
        require_auth = host.require_auth

    # -----------------------------------------------------------------------
    # GET /market/screener
    # -----------------------------------------------------------------------

    @app.get("/market/screener", dependencies=[Depends(require_auth)])
    async def market_screener(
        market: str = Query("us"),
        sort_by: str = Query("amount"),
        top_n: int = Query(40, ge=1, le=_MAX_TOP_N),
    ) -> dict[str, Any]:
        """Rank a market's listed instruments and score them cross-sectionally.

        The upstream sort happens server-side at the provider (so the slice is
        the true top of the whole market, not the top of a page we happened to
        fetch); the percentile scores are then computed over the returned slice.

        Args:
            market: ``a`` (China A-share), ``us`` or ``hk``.
            sort_by: ``change_pct``, ``volume``, ``amount`` or ``turnover``.
            top_n: How many rows to rank, 1-100.

        Returns:
            ``{market, sort_by, top_n, source, as_of, rows, weights}`` where each
            row carries the provider quote plus ``liquidity_pct``,
            ``activity_pct``, ``momentum_pct`` and ``flow_score``.

        Raises:
            HTTPException: 400 on an unknown market/sort, 502 when the provider
                request fails.
        """
        if market not in _VALID_MARKETS:
            raise HTTPException(
                status_code=400, detail=f"market must be one of {list(_VALID_MARKETS)}"
            )
        if sort_by not in _VALID_SORTS:
            raise HTTPException(
                status_code=400, detail=f"sort_by must be one of {list(_VALID_SORTS)}"
            )

        key = f"screener:{market}:{sort_by}:{top_n}"
        if (hit := _cached(key)) is not None:
            return hit

        # Imported lazily: the tool module pulls in the loader stack, and the
        # API process should not pay for it until a dashboard actually asks.
        from src.tools.market_screener_tool import _screen_market
        from src.market_discovery import SCREENER_WEIGHTS

        try:
            rows = await _to_thread(
                _screen_market, market, sort_by=sort_by, top_n=top_n
            )
        except Exception as exc:  # noqa: BLE001 — mapped to a 502 below
            raise _upstream_error("market screener", exc) from exc

        payload = {
            "market": market,
            "sort_by": sort_by,
            "top_n": top_n,
            "source": "eastmoney",
            "as_of": _now().isoformat(timespec="seconds"),
            "rows": score_screener_rows(rows),
            "weights": dict(SCREENER_WEIGHTS),
        }
        _store(key, payload)
        return payload

    # -----------------------------------------------------------------------
    # GET /market/options
    # -----------------------------------------------------------------------

    @app.get("/market/options", dependencies=[Depends(require_auth)])
    async def market_options(
        ticker: str = Query(..., min_length=1, max_length=16),
        expiration: int | None = Query(None, ge=0),
        risk_free_rate: float = Query(0.04, ge=_MIN_RATE, le=_MAX_RATE),
    ) -> dict[str, Any]:
        """Fetch one expiration of a US option chain, enriched for trading.

        Args:
            ticker: US underlying symbol (a trailing ``.US`` is accepted and
                mapped by the Yahoo client).
            expiration: Epoch-second expiration from the ``expirations`` list.
                Omit for the nearest expiry.
            risk_free_rate: Annual continuously-compounded rate used for greeks
                and the implied-vol fallback.

        Returns:
            The :func:`src.market_discovery.shape_option_chain` payload plus
            ``source``.

        Raises:
            HTTPException: 400 on a malformed ticker, 404 when the symbol has no
                listed options, 502 when the provider request fails.
        """
        symbol = ticker.strip().upper()
        if not _TICKER_RE.match(symbol):
            raise HTTPException(status_code=400, detail="ticker is not a valid symbol")

        key = f"options:{symbol}:{expiration}:{risk_free_rate}"
        if (hit := _cached(key)) is not None:
            return hit

        from backtest.loaders import yahoo_client

        try:
            result = await _to_thread(
                yahoo_client.get_options, symbol, expiration=expiration
            )
        except Exception as exc:  # noqa: BLE001 — mapped to a 502 below
            raise _upstream_error("options chain", exc) from exc

        if not result:
            raise HTTPException(
                status_code=404, detail=f"no listed options found for {symbol}"
            )

        payload = shape_option_chain(
            result, ticker=symbol, risk_free_rate=risk_free_rate, now=_now()
        )
        payload["source"] = "yahoo"
        _store(key, payload)
        return payload


async def _to_thread(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Run a blocking provider call off the event loop.

    Both clients are synchronous ``requests`` calls behind a throttle, so
    running them inline would stall every other request on the loop for the
    length of the upstream round-trip plus any rate-limit sleep.
    """
    import asyncio

    return await asyncio.to_thread(func, *args, **kwargs)
