"""pumpfade command line: scan, paper, report, status."""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timezone

from .config import Settings
from .signals import DAY_MS


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="pumpfade", description="Small-cap pump fade on Binance (paper mode)")
    ap.add_argument("--base-url", help="Binance futures REST base (default https://fapi.binance.com)")
    ap.add_argument("--db", help="SQLite file (default data/pumpfade.sqlite)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("scan", help="show which coins qualify for a UTC day (no positions are opened)")
    sc.add_argument("--date", help="UTC date of the +20%% day, YYYY-MM-DD (default: the day that closed last)")
    sub.add_parser("paper", help="run the paper bot (scan at 09:01 KST, track positions every 30s)")
    sub.add_parser("report", help="paper results per book")
    sub.add_parser("status", help="open and pending paper positions")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = Settings.from_env()
    if a.base_url:
        s.base_url = a.base_url
    if a.db:
        from pathlib import Path
        s.db_path = Path(a.db)

    if a.cmd == "scan":
        from .binance import BinancePublic
        from .signals import run_scan
        day = (int(datetime.strptime(a.date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000) // DAY_MS
               if a.date else int(time.time() * 1000) // DAY_MS - 1)
        cands, majors = run_scan(BinancePublic(s.base_url), s.strategy, day)
        print(f"UTC day {datetime.fromtimestamp(day * 86400, timezone.utc):%Y-%m-%d}: majors {majors:+.2%}, "
              f"{len(cands)} coins up {s.strategy.trigger:.0%}+")
        for c in cands:
            extra = f" ratio {c.ratio:.2f}" if c.ratio is not None else ""
            extra += f" funding {c.funding:+.4%}/{c.funding_interval_h}h" if c.funding is not None else ""
            print(f"  {c.symbol:<16} {c.change:+6.0%} rank {c.rank!s:>4} retrace {c.retrace:4.0%}{extra}  "
                  f"{'PASS' if c.passed else c.reason}")
        return

    from .runner import Runner
    r = Runner(s)
    if a.cmd == "paper":
        r.run_forever()
    elif a.cmd == "report":
        print(r.report())
    elif a.cmd == "status":
        for lg in r.store.active_legs():
            print(f"{lg.book:<8} {lg.symbol:<14} {lg.part} {lg.kind:<8} {lg.status:<7} "
                  f"entry {lg.entry_px or 0:.6g} TP {lg.tp_px or 0:.6g} stop {lg.stop_px or 0:.6g} "
                  f"limit {lg.limit_px or 0:.6g}")
