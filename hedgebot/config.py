"""Configuration: YAML file for strategy/risk settings, environment (.env) for secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints

import yaml

from .util import D

MODES = ("paper", "testnet", "live")
DIRECTIONS = ("auto", "long_maker", "short_maker")


@dataclass
class LegConfig:
    venue: str  # arcus | hl:xyz | hl:io
    symbol: str


@dataclass
class PairConfig:
    name: str
    maker: LegConfig
    hedge: LegConfig
    notional_usd: Decimal
    leverage: int = 5
    direction: str = "auto"
    hedge_ratio: Decimal = Decimal(1)  # hedge qty per 1 unit of maker qty (1 for the same ticker)
    enabled: bool = True
    group: str = ""  # label for reports and capital planning, e.g. rwa / io
    # Per-pair rotation overrides (None = use the strategy section). rotate: false = hold only.
    rotate: bool = True
    min_hold_h: float | None = None
    max_hold_h: float | None = None
    rotation_fraction_min: Decimal | None = None
    rotation_fraction_max: Decimal | None = None
    rotate_when: str | None = None


@dataclass(frozen=True)
class Rotation:
    enabled: bool
    min_hold_h: float
    max_hold_h: float
    fraction_min: Decimal
    fraction_max: Decimal
    window: str


def pair_rotation(pair: PairConfig, s: "StrategyConfig") -> Rotation:
    return Rotation(
        enabled=pair.rotate,
        min_hold_h=pair.min_hold_h if pair.min_hold_h is not None else s.min_hold_h,
        max_hold_h=pair.max_hold_h if pair.max_hold_h is not None else s.max_hold_h,
        fraction_min=pair.rotation_fraction_min if pair.rotation_fraction_min is not None else s.rotation_fraction_min,
        fraction_max=pair.rotation_fraction_max if pair.rotation_fraction_max is not None else s.rotation_fraction_max,
        window=pair.rotate_when or s.rotate_when,
    )


@dataclass
class ArcusConfig:
    account_index: int = 0
    # Arcus dead man's switch: cancel ALL orders of the subaccount if the bot stops refreshing it.
    # That includes the exchange stop-losses on Arcus positions, so it is off by default
    # (the bot cancels its own maker orders when it stops or crashes). 5-300 s when enabled.
    dead_mans_switch_s: int = 0
    margin_mode: str = "cross"  # cross | isolated


@dataclass
class HyperliquidConfig:
    dexes: list[str] = field(default_factory=lambda: ["xyz", "io"])
    base_maker_fee: Decimal = Decimal("0.00015")  # your HL fee tier before the HIP-3 multiplier
    base_taker_fee: Decimal = Decimal("0.00045")
    margin_mode: str = "cross"  # isolated-only markets (Entropy) are always isolated


@dataclass
class StrategyConfig:
    clip_usd_min: Decimal = Decimal(600)
    clip_usd_max: Decimal = Decimal(2000)
    maker_price: str = "join"  # join | improve
    reprice_after_s: float = 15
    clip_timeout_s: float = 900
    min_hold_h: float = 3
    max_hold_h: float = 8
    rotation_fraction_min: Decimal = Decimal("0.3")
    rotation_fraction_max: Decimal = Decimal("0.7")
    rotate_when: str = "off_hours"  # off_hours | any
    max_daily_cost_usd: Decimal = Decimal(20)
    basis_guard_bps: Decimal = Decimal(8)
    max_maker_spread_bps: Decimal = Decimal(25)
    max_hedge_spread_bps: Decimal = Decimal(20)
    min_carry_edge_apr: Decimal = Decimal("0.03")
    flip_carry_apr: Decimal = Decimal("0.08")
    funding_window_h: float = 168  # average funding over this window when choosing / flipping direction
    target_tolerance: Decimal = Decimal("0.08")  # |pos/target - 1| below this counts as "at target"
    session_blackout_min: float = 3  # no new clips this close to the 04:00 / 20:00 ET regime switch
    seed: int | None = None


@dataclass
class ExecutionConfig:
    # taker: hedge every maker fill at once with IOCs (safest).
    # passive_first: post the hedge as a maker order at the touch first and cross with IOCs only
    #   after `passive_hedge_timeout_s` or an adverse move of `passive_hedge_max_adverse_bps`.
    #   Cheaper (maker fee, no spread) at the cost of a few seconds of one-legged exposure.
    hedge_mode: str = "taker"
    passive_hedge_timeout_s: float = 20
    passive_hedge_max_adverse_bps: Decimal = Decimal(3)
    passive_hedge_requote_s: float = 4
    hedge_slippage_bps: list[Decimal] = field(default_factory=lambda: [Decimal(5), Decimal(15), Decimal(40)])
    urgent_slippage_bps: Decimal = Decimal(30)
    imbalance_tolerance_usd: Decimal = Decimal(40)
    imbalance_grace_s: float = 8
    ioc_confirm_timeout_s: float = 6


@dataclass
class RiskConfig:
    margin_ratio_warn: Decimal = Decimal(3)
    margin_ratio_critical: Decimal = Decimal("1.7")
    isolated_liq_buffer_warn_pct: Decimal = Decimal(8)
    isolated_liq_buffer_critical_pct: Decimal = Decimal(4)
    derisk_fraction: Decimal = Decimal("0.3")
    # Exchange-side stop orders (work while the bot is down). They sit behind the bot's own de-risk
    # thresholds and in front of liquidation: cross accounts at this margin ratio, isolated positions
    # this far (% of price) from their liquidation price.
    backstop_enabled: bool = True
    backstop_margin_ratio: Decimal = Decimal("1.25")
    backstop_isolated_buffer_pct: Decimal = Decimal(2)
    backstop_refresh_s: float = 60
    # After a leg is closed outside the bot (stop, liquidation): stay flat this long, then rebuild
    # only if margin allows. 0 = never rebuild automatically.
    stop_cooldown_h: float = 6
    # Isolated positions (Entropy): when the liquidation buffer falls below isolated_liq_buffer_warn_pct,
    # move free collateral of that venue into the position until the buffer is back to this level.
    isolated_topup_enabled: bool = True
    isolated_topup_target_pct: Decimal = Decimal(15)
    max_account_leverage: Decimal = Decimal(5)
    max_daily_loss_pct: Decimal = Decimal(2)
    stale_data_s: float = 20
    max_consecutive_errors: int = 5


@dataclass
class PollConfig:
    active_s: float = 1.0
    idle_s: float = 3.0
    markets_s: float = 60
    account_s: float = 5
    snapshot_s: float = 60
    funding_refresh_s: float = 3600


@dataclass
class PaperConfig:
    maker_fill: str = "through"  # through (conservative) | touch (optimistic)
    state_dir: str = "data/paper"


@dataclass
class RebalanceConfig:
    """Scheduled reminder to move money between venues (you do the transfer; the bot only computes it).

    Every `every_h` hours the equity of `venues` is pooled and split again in the proportions of
    `capital`, e.g. arcus 7000 : hl:io 5250. Transfers below `min_transfer_usd` are skipped.
    """

    enabled: bool = False
    every_h: float = 72
    venues: list[str] = field(default_factory=lambda: ["arcus", "hl:io"])
    min_transfer_usd: Decimal = Decimal(200)


@dataclass
class SizingConfig:
    """Grow or shrink every pair with the money actually on the venues.

    scale = total live equity / total `capital`; each pair targets notional_usd x scale. Deposits are
    picked up after `grow_after_min`; a drop (withdrawal, money in transit between venues) must last
    `shrink_after_h` before positions are cut. Changes under `min_change_pct` are ignored.
    """

    scale_with_equity: bool = True
    check_min: float = 5
    min_change_pct: Decimal = Decimal(10)
    grow_after_min: float = 30
    shrink_after_h: float = 6
    min_scale: Decimal = Decimal("0.25")
    max_scale: Decimal = Decimal(10)


@dataclass
class PointsConfig:
    # Community self-reports: ~41-46 Arcus points per $1.2-1.5M of mostly-RWA volume per week.
    arcus_points_per_musd: Decimal = Decimal(33)


@dataclass
class Config:
    mode: str = "paper"
    capital: dict[str, Decimal] = field(default_factory=dict)  # starting balance per venue (paper) / sizing reference
    arcus: ArcusConfig = field(default_factory=ArcusConfig)
    hyperliquid: HyperliquidConfig = field(default_factory=HyperliquidConfig)
    pairs: list[PairConfig] = field(default_factory=list)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    poll: PollConfig = field(default_factory=PollConfig)
    paper: PaperConfig = field(default_factory=PaperConfig)
    points: PointsConfig = field(default_factory=PointsConfig)
    rebalance: RebalanceConfig = field(default_factory=RebalanceConfig)
    sizing: SizingConfig = field(default_factory=SizingConfig)
    journal_path: str = "data/journal.sqlite"
    log_dir: str = "data/logs"

    @property
    def network(self) -> str:
        return "testnet" if self.mode == "testnet" else "mainnet"

    def venues_needed(self) -> list[str]:
        names: list[str] = []
        for pair in self.pairs:
            if pair.enabled:
                for leg in (pair.maker, pair.hedge):
                    if leg.venue not in names:
                        names.append(leg.venue)
        return names


@dataclass
class Secrets:
    arcus_privkey: str | None = None
    arcus_address: str | None = None
    hl_account: str | None = None
    hl_agent_key: str | None = None
    telegram_token: str | None = None
    telegram_chat: str | None = None

    @classmethod
    def from_env(cls) -> "Secrets":
        def get(name: str) -> str | None:
            value = os.environ.get(name, "").strip()
            return value or None

        return cls(
            arcus_privkey=get("ARCUS_API_PRIVKEY"),
            arcus_address=get("ARCUS_ETH_ADDRESS"),
            hl_account=get("HL_ACCOUNT_ADDRESS"),
            hl_agent_key=get("HL_AGENT_PRIVKEY"),
            telegram_token=get("TELEGRAM_BOT_TOKEN"),
            telegram_chat=get("TELEGRAM_CHAT_ID"),
        )


class ConfigError(ValueError):
    pass


def _coerce(value: Any, hint: Any, path: str) -> Any:
    origin = get_origin(hint)
    if value is None:
        return None
    if is_dataclass(hint):
        return _build(hint, value, path)
    if origin is list:
        (item,) = get_args(hint)
        if not isinstance(value, list):
            raise ConfigError(f"{path}: expected a list")
        return [_coerce(v, item, f"{path}[{i}]") for i, v in enumerate(value)]
    if origin is dict:
        _, item = get_args(hint)
        if not isinstance(value, dict):
            raise ConfigError(f"{path}: expected a mapping")
        return {str(k): _coerce(v, item, f"{path}.{k}") for k, v in value.items()}
    args = [a for a in get_args(hint) if a is not type(None)]
    if args:  # Optional[X] / X | None
        return _coerce(value, args[0], path)
    try:
        if hint is Decimal:
            return D(value)
        if hint is bool:
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if hint in (int, float, str):
            return hint(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return value


def _build(cls, data: dict, path: str):
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping")
    hints = get_type_hints(cls)
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"{path}: unknown keys {sorted(unknown)}")
    kwargs = {k: _coerce(v, hints[k], f"{path}.{k}" if path else k) for k, v in data.items()}
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise ConfigError(f"{path or 'config'}: {exc}") from exc


def validate(cfg: Config) -> None:
    if cfg.mode not in MODES:
        raise ConfigError(f"mode must be one of {MODES}")
    if not cfg.pairs:
        raise ConfigError("no pairs configured")
    names = set()
    for p in cfg.pairs:
        if p.name in names:
            raise ConfigError(f"duplicate pair name {p.name}")
        names.add(p.name)
        if p.direction not in DIRECTIONS:
            raise ConfigError(f"pair {p.name}: direction must be one of {DIRECTIONS}")
        if p.notional_usd <= 0 or p.leverage < 1:
            raise ConfigError(f"pair {p.name}: notional_usd and leverage must be positive")
        if p.hedge_ratio <= 0:
            raise ConfigError(f"pair {p.name}: hedge_ratio must be positive")
        for leg in (p.maker, p.hedge):
            if leg.venue != "arcus" and not leg.venue.startswith("hl:"):
                raise ConfigError(f"pair {p.name}: unknown venue {leg.venue} (use arcus, hl:xyz, hl:io)")
        if p.maker.venue == p.hedge.venue and p.maker.symbol == p.hedge.symbol:
            raise ConfigError(f"pair {p.name}: both legs are the same market")
        rot = pair_rotation(p, cfg.strategy)
        if not (0 < rot.min_hold_h <= rot.max_hold_h):
            raise ConfigError(f"pair {p.name}: need 0 < min_hold_h <= max_hold_h")
        if not (0 < rot.fraction_min <= rot.fraction_max <= 1):
            raise ConfigError(f"pair {p.name}: rotation fractions must satisfy 0 < min <= max <= 1")
        if rot.window not in ("off_hours", "any"):
            raise ConfigError(f"pair {p.name}: rotate_when must be off_hours or any")
    s = cfg.strategy
    if not (0 < s.clip_usd_min <= s.clip_usd_max):
        raise ConfigError("strategy: need 0 < clip_usd_min <= clip_usd_max")
    if not (0 < s.min_hold_h <= s.max_hold_h):
        raise ConfigError("strategy: need 0 < min_hold_h <= max_hold_h")
    if not (0 < s.rotation_fraction_min <= s.rotation_fraction_max <= 1):
        raise ConfigError("strategy: rotation fractions must satisfy 0 < min <= max <= 1")
    if s.rotate_when not in ("off_hours", "any"):
        raise ConfigError("strategy.rotate_when must be off_hours or any")
    if s.maker_price not in ("join", "improve"):
        raise ConfigError("strategy.maker_price must be join or improve")
    if cfg.paper.maker_fill not in ("through", "touch"):
        raise ConfigError("paper.maker_fill must be through or touch")
    r = cfg.risk
    if not (1 < r.margin_ratio_critical < r.margin_ratio_warn):
        raise ConfigError("risk: need 1 < margin_ratio_critical < margin_ratio_warn")
    if not (1 < r.backstop_margin_ratio < r.margin_ratio_critical):
        raise ConfigError("risk: need 1 < backstop_margin_ratio < margin_ratio_critical")
    if r.isolated_topup_target_pct <= r.isolated_liq_buffer_warn_pct:
        raise ConfigError("risk: isolated_topup_target_pct must be above isolated_liq_buffer_warn_pct")
    if not (0 < r.backstop_isolated_buffer_pct < r.isolated_liq_buffer_critical_pct):
        raise ConfigError("risk: need 0 < backstop_isolated_buffer_pct < isolated_liq_buffer_critical_pct")
    if not cfg.execution.hedge_slippage_bps:
        raise ConfigError("execution.hedge_slippage_bps needs at least one step")
    if cfg.execution.hedge_mode not in ("taker", "passive_first"):
        raise ConfigError("execution.hedge_mode must be taker or passive_first")
    rb = cfg.rebalance
    if rb.enabled:
        if len(rb.venues) < 2:
            raise ConfigError("rebalance.venues needs at least two venues")
        missing = [v for v in rb.venues if D(cfg.capital.get(v, 0)) <= 0]
        if missing:
            raise ConfigError(f"rebalance: set capital for {missing} (it defines the target split)")
        if rb.every_h <= 0:
            raise ConfigError("rebalance.every_h must be positive")
    for name, mode in (("arcus", cfg.arcus.margin_mode), ("hyperliquid", cfg.hyperliquid.margin_mode)):
        if mode not in ("cross", "isolated"):
            raise ConfigError(f"{name}.margin_mode must be cross or isolated (got {mode!r})")
    dms = cfg.arcus.dead_mans_switch_s
    if dms and not (5 <= dms <= 300):
        raise ConfigError("arcus.dead_mans_switch_s must be 0 (off) or 5-300 seconds")
    z = cfg.sizing
    if z.scale_with_equity:
        if not (0 < z.min_scale <= 1 <= z.max_scale):
            raise ConfigError("sizing: need 0 < min_scale <= 1 <= max_scale")
        if z.min_change_pct <= 0 or z.check_min <= 0:
            raise ConfigError("sizing: min_change_pct and check_min must be positive")
    for name in cfg.venues_needed():
        if name.startswith("hl:") and name[3:] not in cfg.hyperliquid.dexes:
            raise ConfigError(f"venue {name} is used by a pair but not listed in hyperliquid.dexes")


def load_config(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    cfg = _build(Config, raw, "")
    validate(cfg)
    return cfg
