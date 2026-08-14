#!/usr/bin/env python3
"""Check whether the market-discovery dashboard can reach its data providers.

Run this before (or instead of) starting the server to find out what actually
works from a given machine::

    python agent/scripts/market_preflight.py
    python agent/scripts/market_preflight.py --ticker MSFT --market hk

Every source the dashboard uses is free and needs no API key or account, so a
failure here is a network or provider-availability problem, never a missing
credential. Each check reports the wall time and a sample of the real payload,
because "reachable" and "returning usable data" are different things.

Exit status is 0 when the dashboard is fully usable, 1 when it would run
degraded (the screener works but no chain provider answers, or the reverse),
and 2 when nothing is reachable.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Callable

# Allow running straight from a checkout without installing the package.
AGENT_DIR = Path(__file__).resolve().parent.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

OK = "OK"
FAIL = "FAIL"
EMPTY = "EMPTY"


class Check:
    """One provider probe and its outcome."""

    def __init__(self, name: str, detail: str) -> None:
        self.name = name
        self.detail = detail
        self.status = FAIL
        self.seconds = 0.0
        self.sample = ""
        self.error = ""

    def run(self, probe: Callable[[], tuple[bool, str]]) -> "Check":
        """Execute ``probe``, timing it and capturing any failure."""
        started = time.monotonic()
        try:
            usable, sample = probe()
            self.status = OK if usable else EMPTY
            self.sample = sample
        except Exception as exc:  # noqa: BLE001 — a failed probe is the result
            self.error = f"{exc.__class__.__name__}: {exc}"
        finally:
            self.seconds = time.monotonic() - started
        return self


def _screener_probe(market: str, sort_by: str) -> Callable[[], tuple[bool, str]]:
    def probe() -> tuple[bool, str]:
        from src.tools.market_screener_tool import _screen_market

        rows = _screen_market(market, sort_by=sort_by, top_n=5)
        if not rows:
            return False, "no rows returned"
        top = rows[0]
        return True, (
            f"{len(rows)} rows; top: {top.get('code')} {top.get('name')} "
            f"@ {top.get('price')} ({top.get('change_pct')}%)"
        )

    return probe


def _chain_probe(provider: str, ticker: str) -> Callable[[], tuple[bool, str]]:
    def probe() -> tuple[bool, str]:
        from backtest.loaders import cboe_client, yahoo_client

        client = {"yahoo": yahoo_client, "cboe": cboe_client}[provider]
        result = client.get_options(ticker)
        if not result:
            return False, "no chain returned"
        block = (result.get("options") or [{}])[0]
        calls = block.get("calls") or []
        puts = block.get("puts") or []
        spot = (result.get("quote") or {}).get("regularMarketPrice")
        expirations = result.get("expirationDates") or []
        if not calls and not puts:
            return False, "chain had no contracts"
        return True, (
            f"spot {spot}; {len(expirations)} expirations; "
            f"{len(calls)} calls / {len(puts)} puts on the near expiry"
        )

    return probe


def _render(checks: list[Check]) -> None:
    """Print a readable report of every probe."""
    width = max(len(check.name) for check in checks)
    print()
    for check in checks:
        mark = {OK: "  ok  ", EMPTY: " empty", FAIL: " fail "}[check.status]
        print(f"[{mark}] {check.name.ljust(width)}  {check.seconds:5.2f}s  {check.detail}")
        if check.sample:
            print(f"{' ' * (width + 11)}{check.sample}")
        if check.error:
            print(f"{' ' * (width + 11)}{check.error}")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ticker", default="AAPL", help="US underlying to probe (default AAPL)")
    parser.add_argument(
        "--market",
        default="us",
        choices=["us", "a", "hk"],
        help="Screener market to probe (default us)",
    )
    args = parser.parse_args(argv)

    print("Probing the market-discovery data sources. All are free and keyless.")

    checks = [
        Check(
            f"screener ({args.market})",
            "Eastmoney push2 — full-market ranking",
        ).run(_screener_probe(args.market, "amount")),
        Check(
            f"chain: yahoo ({args.ticker})",
            "Yahoo v7 options — primary chain source",
        ).run(_chain_probe("yahoo", args.ticker)),
        Check(
            f"chain: cboe ({args.ticker})",
            "Cboe delayed quotes — fallback chain source",
        ).run(_chain_probe("cboe", args.ticker)),
    ]
    _render(checks)

    screener_ok = checks[0].status == OK
    chains_ok = [check for check in checks[1:] if check.status == OK]

    if screener_ok and chains_ok:
        served_by = ", ".join(check.name.split(":")[1].split("(")[0].strip() for check in chains_ok)
        print(f"Ready. Screener live, option chains available via: {served_by}.")
        if len(chains_ok) == 1:
            print("Only one chain provider answered — the dashboard will use it, "
                  "but you have no fallback left if it goes down.")
        print("\nQuotes from these free feeds are delayed (typically ~15 minutes).")
        print("The dashboard's timestamp is when it fetched, not when the exchange printed.")
        return 0

    if not screener_ok and not chains_ok:
        print("Nothing reachable. Check your network, a proxy, or a firewall —")
        print("no credentials are involved, so this is not an auth problem.")
        return 2

    if screener_ok:
        print("Degraded: the screener works but no option chain provider answered.")
        print("The market table will load; the chain panel will show an error.")
    else:
        print("Degraded: option chains work but the screener did not answer.")
        print("Type a ticker into the chain panel directly — the table will be empty.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
