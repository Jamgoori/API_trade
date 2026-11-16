"""pumpfade command line: scan, paper, report, status."""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timezone

from .config import Settings
from .signals import DAY_MS


def single_instance(db_path):
    """Hold an exclusive lock next to the database so two live bots can never trade the same account book."""
    import os
    from pathlib import Path

    path = Path(str(db_path) + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise SystemExit(f"another pumpfade live process is already using {db_path}: stop it first "
                         "(close its window, or stop the scheduled task / pumpfade-live service)")
    return fh


def telegram_check(token: str | None, chat: str | None, out=print) -> bool:
    """Find the chat id for TELEGRAM_CHAT_ID, or send a test message when both are set."""
    import requests

    if not token:
        out("TELEGRAM_BOT_TOKEN is empty: put the token from @BotFather into .env first")
        return False
    base = f"https://api.telegram.org/bot{token}"
    try:
        if not chat:
            body = requests.get(f"{base}/getUpdates", timeout=10).json()
            if not body.get("ok"):
                out(f"Telegram refused the token: {body.get('description')}. Check TELEGRAM_BOT_TOKEN in .env")
                return False
            chats = {}
            for u in body.get("result", []):
                c = (u.get("message") or u.get("channel_post") or {}).get("chat") or {}
                if "id" in c:
                    chats[c["id"]] = c.get("username") or c.get("first_name") or c.get("title") or ""
            if not chats:
                out("no messages yet: open your bot in Telegram, press Start (or send hi), then run this again")
                return False
            for cid, name in chats.items():
                out(f"found chat {name}: put this line into .env ->  TELEGRAM_CHAT_ID={cid}")
            return True
        body = requests.post(f"{base}/sendMessage", json={"chat_id": chat, "text": "✅ pumpfade alerts work"},
                             timeout=10).json()
        if body.get("ok"):
            out("test message sent: check Telegram. Restart the bot so it picks up the new settings")
            return True
        out(f"Telegram refused the message: {body.get('description')}. Check TELEGRAM_CHAT_ID, and press Start "
            "in the bot chat")
        return False
    except requests.RequestException as exc:
        out(f"could not reach Telegram: {str(exc).replace(token, '<token>')}")
        return False


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="pumpfade", description="Small-cap pump fade on Binance (paper mode)")
    ap.add_argument("--base-url", help="Binance futures REST base (default https://fapi.binance.com)")
    ap.add_argument("--db", help="SQLite file (default data/pumpfade.sqlite)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--log-file", help="also write the log to this file (UTF-8, rotated at 5 MB)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("scan", help="show which coins qualify for a UTC day (no positions are opened)")
    sc.add_argument("--date", help="UTC date of the +20%% day, YYYY-MM-DD (default: the day that closed last)")
    sub.add_parser("paper", help="run the paper bot (scan at 09:01 KST, track positions every 30s)")
    lv = sub.add_parser("live", help="trade for real on Binance (needs BINANCE_API_KEY / BINANCE_API_SECRET)")
    lv.add_argument("--i-understand-live", action="store_true", help="required: real orders with real money")
    lv.add_argument("--resume", action="store_true", help="only clear a drawdown/flatten halt and exit; then start the bot again")
    st = sub.add_parser("selftest", help="check the live order path (testnet by default)")
    st.add_argument("--mainnet", action="store_true", help="run against the real exchange (no fill unless --fill)")
    st.add_argument("--fill", action="store_true", help="on mainnet too: open and close a minimum-size short")
    st.add_argument("--symbol", default="BTCUSDT")
    st.add_argument("--close-leftover", action="store_true",
                    help="with --fill: first close a position on --symbol left by an earlier selftest")
    fl = sub.add_parser("flatten", help="emergency: cancel the bot's orders and close its live positions")
    fl.add_argument("--i-understand-live", action="store_true")
    rp = sub.add_parser("report", help="results (paper books, or the live account with --live)")
    rp.add_argument("--live", action="store_true")
    ss = sub.add_parser("status", help="open positions (paper, or live with --live)")
    ss.add_argument("--live", action="store_true")
    sub.add_parser("telegram", help="set up alerts: find TELEGRAM_CHAT_ID, then send a test message")
    a = ap.parse_args(argv)

    handlers = [logging.StreamHandler()]
    if a.log_file:
        from logging.handlers import RotatingFileHandler
        from pathlib import Path
        Path(a.log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(a.log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    logging.getLogger("urllib3").setLevel(logging.WARNING)  # its DEBUG lines print full URLs
    s = Settings.from_env()
    if a.base_url:
        s.base_url = a.base_url
    if a.db:
        from pathlib import Path
        s.db_path = Path(a.db)

    if a.cmd == "telegram":
        raise SystemExit(0 if telegram_check(s.telegram_token, s.telegram_chat) else 1)

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
    from .trader import MAINNET, TESTNET, BinanceTrader

    if a.cmd == "selftest":
        import os
        key = os.getenv("BINANCE_TESTNET_API_KEY") if not a.mainnet else s.api_key
        secret = os.getenv("BINANCE_TESTNET_API_SECRET") if not a.mainnet else s.api_secret
        from .selftest import run as selftest
        ok = selftest(BinanceTrader(key, secret, MAINNET if a.mainnet else TESTNET), a.symbol,
                      fill=a.fill or not a.mainnet, close_leftover=a.close_leftover)
        raise SystemExit(0 if ok else 1)

    live_cmd = a.cmd in ("live", "flatten") or getattr(a, "live", False)
    if a.cmd in ("live", "flatten") and not a.i_understand_live:
        raise SystemExit("refusing to touch the real account without --i-understand-live")
    trader = BinanceTrader(s.api_key, s.api_secret, MAINNET) if live_cmd else None
    if live_cmd and not a.db:
        from pathlib import Path
        s.db_path = Path(str(s.db_path).replace(".sqlite", "-live.sqlite"))
    lock = single_instance(s.db_path) if a.cmd in ("live", "flatten") else None  # noqa: F841 (held until exit)
    r = Runner(s, trader=trader)
    if a.cmd == "paper":
        r.run_forever()
    elif a.cmd == "live":
        trader.sync_time()
        if trader.dual_side_position():
            raise SystemExit("the account is in hedge mode: switch Binance futures to one-way mode first")
        if a.resume:
            r.store.put("live_halted", None)
            r.store.put("live_peak", None)
            print("halt cleared; the equity peak restarts from the current equity. Start the bot again.")
            return
        r.run_forever()
    elif a.cmd == "flatten":
        import time
        trader.sync_time()
        for n in r.engine.flatten():
            print(n)
        for _ in range(12):  # wait for the buys to settle and record the results (about a minute)
            if not r.engine.book.all(("entering", "open", "closing")):
                break
            time.sleep(10)
            r.engine.tick()
        left = r.engine.book.all(("entering", "open", "closing"))
        print("all flat; new entries stay stopped until `pumpfade live --resume`" if not left
              else f"still open, check Binance: {', '.join(p.symbol for p in left)}")
    elif a.cmd == "report":
        print(r.report())
    elif a.cmd == "status" and r.live:
        for p in r.engine.book.all(("entering", "open", "closing")):
            print(f"{p.symbol:<14} {p.sleeve:<8} {p.status:<8} qty {p.qty:g} entry {p.entry_avg or 0:.6g} "
                  f"stop {p.sl_px or 0:.6g} TP {p.tp_px or 0:.6g} deadline {p.deadline_ms}")
    elif a.cmd == "status":
        for lg in r.store.active_legs():
            print(f"{lg.book:<8} {lg.symbol:<14} {lg.part} {lg.kind:<8} {lg.status:<7} "
                  f"entry {lg.entry_px or 0:.6g} TP {lg.tp_px or 0:.6g} stop {lg.stop_px or 0:.6g} "
                  f"limit {lg.limit_px or 0:.6g}")
