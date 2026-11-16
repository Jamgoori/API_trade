"""Strategy parameters (research/STRATEGY.md) and runtime settings."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class BookSpec:
    """One paper ledger. All books see the same signals; they differ in how they enter and when they give up."""

    name: str
    entry: str            # "market" | "half" | "limit"
    hold_hours: int = 48


@dataclass(frozen=True)
class Strategy:
    trigger: float = 0.20             # daily close vs previous close
    min_rank: int = 100               # only coins ranked below this by trailing 30-day volume
    rank_days: int = 30
    min_ratio: float = 1.5            # global long/short account ratio, last 5-minute snapshot before entry
    majors: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    majors_min: float = -0.03
    majors_max: float = 0.07
    max_retrace: float = 0.30         # close at most 30% below the day's high
    funding_floor_long: float = -0.01   # skip if last funding <= this on 4h/8h intervals
    funding_floor_1h: float = -0.005    # skip if last funding <= this on 1h intervals
    take_profit: float = 0.15
    stop: float = 0.15
    limit_offset: float = 0.005       # limit sell this far above the 09:00 KST price
    limit_window_hours: int = 1       # then cancel and enter at market
    max_slippage: float = 0.003       # IOC limit: fill only within 0.3% of the best bid / ask
    taker_fee: float = 0.0005
    maker_fee: float = 0.0002
    # listing pumps (STRATEGY.md rule 2)
    listing_min_perp_age_days: int = 30
    listing_entry_delay_hours: int = 1   # short this long after the listing's first hour starts
    listing_max_detect_delay_hours: int = 3  # a listing first seen later than this after it opened is ignored
    usdkrw_range: tuple[float, float] = (1100.0, 1800.0)  # sanity check that a KRW ticker is the same coin


@dataclass
class Settings:
    base_url: str = "https://fapi.binance.com"
    spot_url: str = "https://data-api.binance.vision"  # Binance public market data
    upbit_url: str = "https://api.upbit.com"
    bithumb_url: str = "https://api.bithumb.com"
    watch_listings: bool = True
    db_path: Path = Path("data/pumpfade.sqlite")
    start_equity: float = 10_000.0
    margin_fraction: float = 0.12      # margin per signal as a share of the book's equity (STRATEGY.md)
    max_margin_used: float = 0.90      # never commit more than this share of equity as margin
    scan_delay_s: int = 60             # run the daily scan this long after 00:00 UTC (funding settles first)
    scan_deadline_s: int = 30 * 60     # a scan later than this after 00:00 UTC is skipped as stale
    # live trading (pumpfade live): keys from BINANCE_API_KEY / BINANCE_API_SECRET, never from files in the repo
    api_key: str | None = field(default=None, repr=False)
    api_secret: str | None = field(default=None, repr=False)
    live_margin_fraction: float = 0.05   # stage 1 of STRATEGY.md; raise to 0.12 once the stage-1 checks pass
    live_reduce_drawdown: float = 0.25   # below this drawdown from the equity peak, size drops to two thirds
    live_halt_drawdown: float = 0.35     # below this, new entries stop until `pumpfade live --resume`
    telegram_token: str | None = field(default=None, repr=False)
    telegram_chat: str | None = None
    strategy: Strategy = field(default_factory=Strategy)
    books: tuple[BookSpec, ...] = (
        BookSpec("limit", "limit"),          # the strategy as written (STRATEGY.md)
        BookSpec("market", "market"),        # comparisons
        BookSpec("half", "half"),
        BookSpec("limit_3d", "limit", hold_hours=72),
    )

    @classmethod
    def from_env(cls) -> "Settings":
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
        s = cls()
        s.base_url = os.getenv("PUMPFADE_BASE_URL", s.base_url)
        s.spot_url = os.getenv("PUMPFADE_SPOT_URL", s.spot_url)
        s.watch_listings = os.getenv("PUMPFADE_WATCH_LISTINGS", "1") != "0"
        s.db_path = Path(os.getenv("PUMPFADE_DB", str(s.db_path)))
        s.start_equity = float(os.getenv("PUMPFADE_START_EQUITY", s.start_equity))
        s.margin_fraction = float(os.getenv("PUMPFADE_MARGIN_FRACTION", s.margin_fraction))
        s.api_key = os.getenv("BINANCE_API_KEY") or None
        s.api_secret = os.getenv("BINANCE_API_SECRET") or None
        s.live_margin_fraction = float(os.getenv("PUMPFADE_LIVE_MARGIN_FRACTION", s.live_margin_fraction))
        s.telegram_token = os.getenv("TELEGRAM_BOT_TOKEN") or None
        s.telegram_chat = os.getenv("TELEGRAM_CHAT_ID") or None
        return s
