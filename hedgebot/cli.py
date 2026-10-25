"""Command line entry point: scan | backtest | run | report | status | check | flatten."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
import time
from dataclasses import replace
from pathlib import Path

from dotenv import load_dotenv

from .config import Config, ConfigError, Secrets, load_config

log = logging.getLogger("hedgebot")

INTERVAL_MS = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000}
# Half-spreads in bps (RTH, off-hours), calibrated from live `hedgebot scan` books on large caps.
DEFAULT_HALF_SPREAD = {"arcus": (0.3, 0.5), "hl:xyz": (0.3, 0.5), "hl:io": (0.8, 1.2)}


LOG_FORMAT = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")


def setup_logging(log_dir: str | None, verbose: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(LOG_FORMAT)
    root.addHandler(console)
    if log_dir:
        add_file_log(log_dir)
    for noisy in ("urllib3", "websocket"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def add_file_log(log_dir: str) -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(Path(log_dir) / "hedgebot.log", maxBytes=20_000_000, backupCount=5)
    fh.setFormatter(LOG_FORMAT)
    logging.getLogger().addHandler(fh)


def _load(args) -> Config:
    path = Path(args.config)
    if not path.exists():
        example = Path("config.example.yaml")
        if args.config == "config.yaml" and example.exists():
            print("config.yaml not found - using config.example.yaml (copy it to config.yaml to customise)")
            path = example
        else:
            raise SystemExit(f"config file {path} not found")
    try:
        cfg = load_config(path)
    except ConfigError as exc:
        raise SystemExit(f"config error: {exc}") from exc
    if getattr(args, "mode", None):
        cfg.mode = args.mode
    return cfg


# ---------------------------------------------------------------------- scan
def cmd_scan(args) -> int:
    from .scanner import format_table, scan, write_csv

    rows = scan(args.dexes.split(","), args.network, with_books=not args.no_books)
    print(format_table(rows))
    print("\nbasis = (Arcus - HL) / HL; carry = funding earned per year by the better direction; "
          "lev A(off) = Arcus max leverage (off-hours, for new positions); iso = HL market is isolated-only")
    if args.csv:
        write_csv(rows, args.csv)
        print(f"written {args.csv}")
    return 0


# ------------------------------------------------------------------ backtest
def _leg_specs(cfg: Config, venues, half_spread: dict[str, tuple[float, float]]):
    from .backtest.sim import LegSpec

    specs = {}
    for pair in cfg.pairs:
        if not pair.enabled:
            continue
        for leg in (pair.maker, pair.hedge):
            m = venues[leg.venue].market(leg.symbol)
            hs = half_spread.get(leg.venue, (2.0, 4.0))
            liq_fee = float(m.extra.get("liquidationFeePpm") or 10_000) / 1e6
            specs[(leg.venue, leg.symbol)] = LegSpec(
                venue=leg.venue, maker_fee=float(m.maker_fee), taker_fee=float(m.taker_fee),
                mmf=float(m.maintenance_margin_fraction), max_lev=float(m.max_leverage),
                off_hours_lev=float(m.off_hours_max_leverage),
                isolated=m.isolated_only or (leg.venue == "arcus" and cfg.arcus.margin_mode == "isolated")
                or (leg.venue.startswith("hl:") and cfg.hyperliquid.margin_mode == "isolated"),
                half_spread_bps=hs[0], half_spread_off_bps=hs[1], liquidation_fee=liq_fee)
    return specs


def _parse_half_spread(text: str | None) -> dict[str, tuple[float, float]]:
    out = dict(DEFAULT_HALF_SPREAD)
    if text:
        for part in text.split(","):
            venue, values = part.split("=")
            rth, _, off = values.partition(":")
            out[venue.strip()] = (float(rth), float(off or rth))
    return out


def cmd_backtest(args) -> int:
    from .backtest import load_pair_data, run_backtest
    from .backtest.report import format_result, format_sweep
    from .backtest.sim import BacktestParams
    from .engine import build_market_venues

    cfg = _load(args)
    cfg.mode = "live"  # mainnet history
    venues = build_market_venues(cfg, None, trading=False)
    for v in venues.values():
        v.refresh_markets()
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(args.days * 86_400_000)
    pairs = [p for p in cfg.pairs if p.enabled]
    datas = [load_pair_data(p, venues, args.interval, INTERVAL_MS[args.interval], start_ms, end_ms, args.cache_dir)
             for p in pairs]
    for d in datas:
        if len(d.maker) == 0 or len(d.hedge) == 0:
            raise SystemExit(f"no history for {d.pair.name} ({len(d.maker)} / {len(d.hedge)} bars)")
        print(f"{d.pair.name}: {len(d.maker)} maker bars, {len(d.hedge)} hedge bars, "
              f"{len(d.maker_funding)} / {len(d.hedge_funding)} funding prints")
    s, r = cfg.strategy, cfg.risk
    base = BacktestParams(
        capital={k: float(v) for k, v in cfg.capital.items()},
        leg_specs=_leg_specs(cfg, venues, _parse_half_spread(args.half_spread)),
        min_hold_h=s.min_hold_h, max_hold_h=s.max_hold_h,
        rotation_fraction_min=float(s.rotation_fraction_min), rotation_fraction_max=float(s.rotation_fraction_max),
        rotate_when=s.rotate_when, max_daily_cost_usd=float(s.max_daily_cost_usd),
        basis_guard_bps=float(s.basis_guard_bps), target_tolerance=float(s.target_tolerance),
        min_carry_edge_apr=float(s.min_carry_edge_apr), flip_carry_apr=float(s.flip_carry_apr),
        funding_window_h=s.funding_window_h,
        hedge_slippage_bps=args.hedge_slippage_bps, maker_adverse_bps=args.maker_adverse_bps,
        passive_hedge_fill_rate=(args.passive_hedge_fill_rate if args.passive_hedge_fill_rate is not None
                                 else 0.5 if cfg.execution.hedge_mode == "passive_first" else 0.0),
        margin_ratio_warn=float(r.margin_ratio_warn), margin_ratio_critical=float(r.margin_ratio_critical),
        derisk_fraction=float(r.derisk_fraction),
        isolated_liq_buffer_critical_pct=float(r.isolated_liq_buffer_critical_pct),
        isolated_liq_buffer_warn_pct=float(r.isolated_liq_buffer_warn_pct),
        isolated_topup_enabled=r.isolated_topup_enabled, isolated_topup_target_pct=float(r.isolated_topup_target_pct),
        max_account_leverage=float(r.max_account_leverage),
        allow_transfers=not args.no_transfers,
        backstop_enabled=r.backstop_enabled, backstop_margin_ratio=float(r.backstop_margin_ratio),
        backstop_isolated_buffer_pct=float(r.backstop_isolated_buffer_pct), stop_cooldown_h=r.stop_cooldown_h,
        points_per_musd=args.points_per_musd or float(cfg.points.arcus_points_per_musd),
        seed=args.seed, label="config",
        rebalance_every_h=(args.rebalance_every_h or None) if args.rebalance_every_h is not None
        else (cfg.rebalance.every_h if cfg.rebalance.enabled else None),
        rebalance_venues=tuple(cfg.rebalance.venues), rebalance_min_usd=float(cfg.rebalance.min_transfer_usd),
    )
    if not args.sweep:
        print()
        print(format_result(run_backtest(datas, base, start_ms, end_ms)))
        return 0
    variants = [
        base,
        replace(base, label="hold only (no rotation)", rotation_enabled=False),
        replace(base, label="rotate 1.5-4h*", min_hold_h=1.5, max_hold_h=4),
        replace(base, label="rotate 8-16h*", min_hold_h=8, max_hold_h=16),
        replace(base, label="rotate any time", rotate_when="any"),
        replace(base, label="notional x1.5", notional_scale=1.5),
        replace(base, label="notional x0.6", notional_scale=0.6),
        replace(base, label="no basis guard", use_basis_guard=False),
        replace(base, label="budget x3", max_daily_cost_usd=base.max_daily_cost_usd * 3),
        replace(base, label="no transfers", allow_transfers=False),
        replace(base, label="no exchange stops", backstop_enabled=False),
        replace(base, label="no isolated top-up", isolated_topup_enabled=False),
        replace(base, label="taker hedge", passive_hedge_fill_rate=0.0) if base.passive_hedge_fill_rate else
        replace(base, label="passive hedge 50% fills", passive_hedge_fill_rate=0.5),
        replace(base, label="passive hedge 50% + 8-16h*", passive_hedge_fill_rate=base.passive_hedge_fill_rate or 0.5,
                min_hold_h=8, max_hold_h=16),
    ]
    results = [run_backtest(datas, v, start_ms, end_ms) for v in variants]
    print()
    print(format_sweep(results))
    print("* applies to pairs without their own min_hold_h / max_hold_h in config")
    print()
    print(format_result(results[0]))
    return 0


# ----------------------------------------------------------------------- run
def cmd_run(args) -> int:
    from .engine import Engine, ensure_dirs

    cfg = _load(args)
    if cfg.mode == "live" and not args.i_understand_live:
        raise SystemExit("live mode trades real money. Re-run with --i-understand-live after the paper test.")
    ensure_dirs(cfg)
    add_file_log(cfg.log_dir)
    secrets = Secrets.from_env()
    engine = Engine(cfg, secrets)
    engine.run(max_runtime_s=args.max_runtime)
    return 0


def cmd_report(args) -> int:
    from .journal import Journal
    from .report import build_report

    cfg = _load(args)
    journal = Journal(cfg.journal_path)
    end = time.time()
    start = end - args.hours * 3600 if args.hours else None
    print(build_report(journal, cfg, start, end, args.points_per_musd))
    return 0


def cmd_status(args) -> int:
    from .journal import Journal

    cfg = _load(args)
    journal = Journal(cfg.journal_path)
    for p in cfg.pairs:
        print(f"{p.name}: {journal.load_pair_state(p.name)}")
    snaps = journal.snapshots_between(time.time() - 3600 * 6, time.time() + 1)
    latest = {}
    for s in snaps:
        latest[s["venue"]] = s
    for venue, s in latest.items():
        print(f"{venue}: equity {s['equity']} maintenance {s['maintenance']} notional {s['gross_notional']} "
              f"positions {s['positions']}")
    return 0


def cmd_check(args) -> int:
    from .engine import build_market_venues

    cfg = _load(args)
    secrets = Secrets.from_env()
    trading = cfg.mode in ("testnet", "live")
    print(f"mode={cfg.mode} network={cfg.network} pairs={[p.name for p in cfg.pairs if p.enabled]}")
    venues = build_market_venues(cfg, secrets, trading=trading)
    ok = True
    for name, venue in venues.items():
        try:
            markets = venue.refresh_markets()
            print(f"  {name}: {len(markets)} markets")
        except Exception as exc:  # report every venue, then fail
            ok = False
            print(f"  {name}: market data FAILED: {exc}")
            continue
        for pair in cfg.pairs:
            for leg in (pair.maker, pair.hedge):
                if leg.venue == name:
                    m = markets.get(leg.symbol)
                    print(f"    {leg.symbol}: " + (f"ok (max {m.max_leverage}x, off-hours {m.off_hours_max_leverage}x, "
                                                   f"maker {m.maker_fee * 10000:.2f}bp, taker {m.taker_fee * 10000:.2f}bp"
                                                   f"{', isolated-only' if m.isolated_only else ''})"
                                                   if m else "NOT FOUND"))
                    ok &= m is not None
        if trading:
            try:
                acct = venue.account()
                print(f"    account: equity ${acct.equity:,.2f}, free ${acct.free_collateral:,.2f}, "
                      f"positions {list(acct.positions)}")
            except Exception as exc:
                ok = False
                print(f"    account FAILED: {exc}")
    if trading and "arcus" in venues:
        signer = venues["arcus"].signer
        print(f"  arcus API key (public): {signer.api_key} for {signer.address} subaccount {signer.account_index}")
    print("telegram:", "on" if secrets.telegram_token and secrets.telegram_chat else "off")
    print("OK" if ok else "PROBLEMS FOUND")
    return 0 if ok else 1


def cmd_rebalance(args) -> int:
    from .engine import Engine, ensure_dirs
    from .journal import Journal
    from .rebalance import describe
    from .venues.paper import PaperVenue

    cfg = _load(args)
    if len(cfg.rebalance.venues) < 2:
        raise SystemExit("set rebalance.venues in the config")
    ensure_dirs(cfg)
    engine = Engine(cfg, Secrets.from_env(), journal=Journal(cfg.journal_path))
    for v in engine.venues.values():
        v.refresh_markets()
    transfers, equity, weights = engine.rebalance_plan()
    print(describe(transfers, equity, weights))
    if args.apply:
        if cfg.mode != "paper":
            raise SystemExit("--apply only simulates transfers in paper mode; move real funds yourself")
        for t in transfers:
            src, dst = engine.venues[t.source], engine.venues[t.dest]
            assert isinstance(src, PaperVenue) and isinstance(dst, PaperVenue)
            src.cash -= t.amount
            dst.cash += t.amount
            src._save()
            dst._save()
        engine.journal.save_pair_state("__rebalance__", {"last_ts": time.time()})
        print("paper balances updated")
    return 0


def cmd_flatten(args) -> int:
    from .engine import Engine, ensure_dirs, flatten_all

    cfg = _load(args)
    if not args.confirm:
        raise SystemExit("this closes every bot position on both venues with market orders; add --confirm")
    if cfg.mode == "live" and not args.i_understand_live:
        raise SystemExit("add --i-understand-live to flatten real positions")
    ensure_dirs(cfg)
    engine = Engine(cfg, Secrets.from_env())
    flatten_all(engine)
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="hedgebot", description="Arcus <-> Hyperliquid HIP-3 hedge points bot")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def with_config(p):
        p.add_argument("-c", "--config", default="config.yaml")
        return p

    p = sub.add_parser("scan", help="list Arcus RWA perps hedgeable on HIP-3 dexes, with live funding/basis")
    p.add_argument("--dexes", default="xyz,io")
    p.add_argument("--network", default="mainnet", choices=["mainnet", "testnet"])
    p.add_argument("--no-books", action="store_true", help="skip per-market BBO calls (faster)")
    p.add_argument("--csv")
    p.set_defaults(func=cmd_scan)

    p = with_config(sub.add_parser("backtest", help="replay the strategy on historical candles + funding"))
    p.add_argument("--days", type=float, default=45)
    p.add_argument("--interval", default="15m", choices=list(INTERVAL_MS))
    p.add_argument("--sweep", action="store_true", help="compare rotation / sizing variants")
    p.add_argument("--no-transfers", action="store_true", help="never top up a venue from the other one")
    p.add_argument("--half-spread", help="e.g. arcus=0.3:0.5,hl:xyz=0.3:0.5,hl:io=0.8:1.2 (RTH:off-hours bps)")
    p.add_argument("--hedge-slippage-bps", type=float, default=0.3, help="taker cost beyond the touch")
    p.add_argument("--maker-adverse-bps", type=float, default=0.5, help="adverse selection on maker fills")
    p.add_argument("--passive-hedge-fill-rate", type=float,
                   help="share of hedges filled as maker (default 0.5 with hedge_mode passive_first, else 0)")
    p.add_argument("--points-per-musd", type=float)
    p.add_argument("--rebalance-every-h", type=float,
                   help="simulate moving money between rebalance.venues every N hours (0 = off; default: config)")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--cache-dir", default="data/cache")
    p.set_defaults(func=cmd_backtest)

    p = with_config(sub.add_parser("run", help="run the bot"))
    p.add_argument("--mode", choices=["paper", "testnet", "live"])
    p.add_argument("--max-runtime", type=float, help="stop after N seconds")
    p.add_argument("--i-understand-live", action="store_true")
    p.set_defaults(func=cmd_run)

    p = with_config(sub.add_parser("report", help="summarise a run from the journal"))
    p.add_argument("--hours", type=float, help="only the last N hours")
    p.add_argument("--points-per-musd", type=float)
    p.set_defaults(func=cmd_report)

    p = with_config(sub.add_parser("status", help="pair states and latest account snapshots"))
    p.set_defaults(func=cmd_status)

    p = with_config(sub.add_parser("check", help="validate config, market symbols, credentials"))
    p.add_argument("--mode", choices=["paper", "testnet", "live"])
    p.set_defaults(func=cmd_check)

    p = with_config(sub.add_parser("rebalance", help="how much to move between venues to get back to plan"))
    p.add_argument("--mode", choices=["paper", "testnet", "live"])
    p.add_argument("--apply", action="store_true", help="paper mode only: apply the transfers to paper balances")
    p.set_defaults(func=cmd_rebalance)

    p = with_config(sub.add_parser("flatten", help="emergency: close all bot positions"))
    p.add_argument("--mode", choices=["paper", "testnet", "live"])
    p.add_argument("--confirm", action="store_true")
    p.add_argument("--i-understand-live", action="store_true")
    p.set_defaults(func=cmd_flatten)

    args = parser.parse_args(argv)
    setup_logging(None, args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
