"""Equity-based sizing: every pair targets notional_usd x scale, scale = live equity / configured capital.

Growth needs the higher equity to hold for `grow_after_min` (a deposit has landed); a drop must
hold for `shrink_after_h`, so money in transit between venues (a rebalance) never cuts positions.
"""

from __future__ import annotations

from decimal import Decimal

from .config import SizingConfig
from .util import D


class SizeScaler:
    def __init__(self, cfg: SizingConfig, scale: Decimal = D(1), version: int = 0, known: bool = False):
        self.cfg = cfg
        self.scale = D(scale)
        self.version = version
        self.known = known  # False until the first measurement (first start sizes immediately)
        self.pending: tuple[int, float] | None = None  # (direction, first seen)

    def clamp(self, raw: Decimal) -> Decimal:
        return min(max(D(raw), self.cfg.min_scale), self.cfg.max_scale)

    def observe(self, raw: Decimal, now: float) -> tuple[str, Decimal] | None:
        """Feed a measurement. Returns ("pending", raw) when a change is first seen,
        ("apply", new_scale) when the size changes, ("cleared", raw) when a pending change faded."""
        raw = self.clamp(raw)
        if not self.known:
            return self._apply(raw)
        change = raw / self.scale - 1
        if abs(change) * 100 < self.cfg.min_change_pct:
            faded, self.pending = self.pending is not None, None
            return ("cleared", raw) if faded else None
        direction = 1 if change > 0 else -1
        if self.pending is None or self.pending[0] != direction:
            self.pending = (direction, now)
            return "pending", raw
        wait_s = self.cfg.grow_after_min * 60 if direction > 0 else self.cfg.shrink_after_h * 3600
        if now - self.pending[1] >= wait_s:
            return self._apply(raw)
        return None

    def _apply(self, raw: Decimal) -> tuple[str, Decimal] | None:
        new = raw.quantize(Decimal("0.01"))
        self.known, self.pending = True, None
        if new == self.scale:
            return None
        self.scale, self.version = new, self.version + 1
        return "apply", new

    def state(self) -> dict:
        return {"scale": str(self.scale), "version": self.version}
