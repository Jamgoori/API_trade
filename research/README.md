# Small-cap short research (Binance USD-M)

Question: does shorting Binance small-cap perps mechanically pay, and can a pump signal tell
"stop out" from "this will dump"?

## Data

`binance_data.py` downloads 1h klines and funding for every USDT-margined coin perp from
[data.binance.vision](https://data.binance.vision), including delisted symbols, so dead coins are in the sample.
Equities, commodities, FX, pre-IPO, index, RWA and stablecoin perps are dropped using
`underlyingType`/`underlyingSubType` from `exchangeInfo`. Raw files are cached under `data/binance/` (gitignored).

```bash
python3 research/binance_data.py --start 2021-01 --end 2026-10 --workers 48
```

```bash
python3 research/short_study.py --part all
```

## Method

- **Tier**: each coin's rank by trailing 30-day average daily volume, using only past days.
  "101+" = outside the top 100 by volume; "31–100" = the next band below the majors.
- **Trades**: enter at a 1h bar open; 0.05% taker fee and 0.1% slippage per side; 0.5% extra slippage on stop
  fills; funding paid/received every interval on the current notional.
- **Isolated margin**: liquidation when equity falls to a 2.5% maintenance margin; a liquidated position loses
  its whole margin. A stop fills at the worse of the stop price and the bar open (gaps).
- **Pump signal**: 4h return ≥ +20% and that hour's volume ≥ 5× its trailing 7-day hourly average.
- **BTC hedge**: a BTC long with the same notional as each short, opened and closed with it
  (needs extra margin on top of the shorts).

Limits: 1h bars hide moves inside the hour; the 2.5% maintenance margin is a simplification of Binance's
tiered brackets; equal margin per coin ignores that the smallest coins cannot absorb much size.

## Results

See `results/` for the raw CSVs. Summary below.
