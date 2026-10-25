"""Summary of a paper / live run from the journal (the 3-day test report)."""

from __future__ import annotations

import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal

from .config import Config
from .journal import Journal
from .util import ZERO, D


def _t(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def build_report(journal: Journal, cfg: Config, start: float | None = None, end: float | None = None,
                 points_override: float | None = None) -> str:
    end = end or time.time()
    start = start or journal.first_ts() or end - 86400
    hours = max((end - start) / 3600, 1e-9)
    fills = journal.fills_between(start, end)
    events = journal.events_between(start, end)
    snaps = journal.snapshots_between(start, end)

    # equity per venue: first and last snapshot in the window
    first: dict[str, dict] = {}
    last: dict[str, dict] = {}
    for s in snaps:
        first.setdefault(s["venue"], s)
        last[s["venue"]] = s
    lines = [f"Run report  {_t(start)} -> {_t(end)}  ({hours:.1f}h, mode={cfg.mode})", ""]
    lines.append("  Equity")
    total_start = total_end = ZERO
    for venue in sorted(first):
        a, b = D(first[venue]["equity"]), D(last[venue]["equity"])
        total_start += a
        total_end += b
        lines.append(f"    {venue:<8} ${a:>12,.2f} -> ${b:>12,.2f}   ({b - a:+,.2f})")
    lines.append(f"    {'total':<8} ${total_start:>12,.2f} -> ${total_end:>12,.2f}   ({total_end - total_start:+,.2f})")

    # volume, fees, slippage
    vol = defaultdict(lambda: ZERO)
    fee = defaultdict(lambda: ZERO)
    slip = defaultdict(lambda: ZERO)
    maker_vol = defaultdict(lambda: ZERO)
    off_vol = defaultdict(lambda: ZERO)
    latencies = []
    for f in fills:
        v = f["venue"]
        n = D(f["notional"])
        vol[v] += n
        fee[v] += D(f["fee"] or 0)
        slip[v] += D(f["slippage_usd"] or 0) if f["liquidity"] == "taker" else ZERO
        if f["liquidity"] == "maker":
            maker_vol[v] += n
        if f["outside_rth"]:
            off_vol[v] += n
        if f["hedge_latency_ms"] is not None:
            latencies.append(int(f["hedge_latency_ms"]))
    lines += ["", "  Volume / cost (journal; fees are venue rates x notional unless reconciled)"]
    for v in sorted(vol):
        share = off_vol[v] / vol[v] if vol[v] else ZERO
        lines.append(f"    {v:<8} volume ${vol[v]:>12,.0f}  maker {maker_vol[v] / vol[v] if vol[v] else 0:>4.0%}  "
                     f"off-hours {share:>4.0%}  fees ${fee[v]:>8,.2f}  taker slippage ${slip[v]:>8,.2f}")
    venue_fills = journal.venue_fills_between(int(start * 1000), int(end * 1000))
    if venue_fills:
        actual = defaultdict(lambda: ZERO)
        for f in venue_fills:
            actual[f["venue"]] += D(f["fee"] or 0)
        lines.append("    reconciled venue fees: " + ", ".join(f"{v} ${x:,.2f}" for v, x in sorted(actual.items())))

    arcus_vol = vol.get("arcus", ZERO)
    rate = D(points_override) if points_override is not None else cfg.points.arcus_points_per_musd
    points = arcus_vol / Decimal(1_000_000) * rate
    cost = sum(fee.values(), ZERO) + sum((max(ZERO, x) for x in slip.values()), ZERO)
    weekly = f" (~{points / D(hours) * 168:,.1f}/week)" if hours >= 24 else " (run >= 24h for a weekly rate)"
    lines += ["", "  Points (estimate)",
              f"    Arcus volume ${arcus_vol:,.0f} -> ~{points:,.1f} points at {rate} pts/$1M{weekly}"]
    if points > 0:
        lines.append(f"    execution cost per point ${cost / points:,.2f}; equity change per point "
                     f"${-(total_end - total_start) / points:,.2f}")

    # execution quality
    kinds = Counter(e["kind"] for e in events)
    rejects = Counter((e["message"] or "").split("rejected:")[-1].strip()[:40] for e in events if e["kind"] == "reject")
    lines += ["", "  Execution"]
    if latencies:
        latencies.sort()
        p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
        lines.append(f"    maker fill -> hedge latency: median {statistics.median(latencies):.0f}ms, p95 {p95}ms, "
                     f"max {latencies[-1]}ms over {len(latencies)} hedges")
    lines.append(f"    rotations {kinds.get('rotation', 0)}, imbalance repairs {kinds.get('imbalance', 0)}, "
                 f"hedge failures {kinds.get('hedge_failed', 0)}, de-risk {kinds.get('derisk', 0)}, "
                 f"halts {kinds.get('halt', 0)}, venue errors {kinds.get('venue_error', 0)}")
    if rejects:
        lines.append("    rejects: " + ", ".join(f"{k or '?'} x{n}" for k, n in rejects.most_common(5)))

    # uptime from snapshot cadence
    per_venue = Counter(s["venue"] for s in snaps)
    if per_venue:
        expected = hours * 3600 / cfg.poll.snapshot_s
        uptime = min(1.0, max(per_venue.values()) / expected) if expected else 0
        lines.append(f"    uptime (snapshot coverage) {uptime:.0%}")

    # checklist for the paper test
    lines += ["", "  3-day test checklist"]
    # passive_first waits up to passive_hedge_timeout_s for a maker hedge before crossing the spread
    ex = cfg.execution
    limit_s = 5 + (ex.passive_hedge_timeout_s + 10 if ex.hedge_mode == "passive_first" else 0)
    checks = [
        ("no hedge failures", kinds.get("hedge_failed", 0) == 0),
        ("imbalance repairs rare (<= 1 per day)", kinds.get("imbalance", 0) <= max(1, hours / 24)),
        ("no liquidation / de-risk", kinds.get("derisk", 0) == 0 and kinds.get("liq_distance", 0) == 0),
        ("no halts", kinds.get("halt", 0) == 0),
        (f"hedge latency p95 < {limit_s:g}s",
         not latencies or latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] < limit_s * 1000),
        ("bot stayed up (>= 95% coverage)", bool(per_venue) and max(per_venue.values()) >= 0.95 * hours * 3600 / cfg.poll.snapshot_s),
    ]
    for label, ok in checks:
        lines.append(f"    [{'x' if ok else ' '}] {label}")
    return "\n".join(lines)
