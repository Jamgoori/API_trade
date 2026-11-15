## 6. Chicken farm with a long/short-ratio filter

### 2025-01 .. 2026-10 (cohort starts), volume rank 101+, 1x isolated, 30-day hold

Ratio found for 100% of entries and 100% of signal bars; entry filters drop coins with no ratio (mostly listings younger than a day or two).

CAGR and max drawdown are averaged over the four staggered schedules; the worst schedule's drawdown is in brackets.

| rule | coins/cohort | liquidated | closed early | CAGR | max DD (worst) | CAGR, BTC-hedged |
|---|---|---|---|---|---|---|
| no filter | 378 | 7.1% | 0% | -9% | -56% (-64%) | -6% |
| no filter, +30% stop | 378 | 0.0% | 32% | -1% | -45% (-64%) | -2% |
| skip coins listed < 30 days | 374 | 7.0% | 0% | -9% | -56% (-64%) | -6% |
| entry: skip ratio < 0.7 | 363 | 7.0% | 0% | -10% | -57% (-65%) | -6% |
| entry: skip ratio < 1.0 | 333 | 6.9% | 0% | -10% | -57% (-66%) | -6% |
| exit on pump signal (any ratio) | 378 | 2.3% | 17% | -4% | -51% (-56%) | -2% |
| exit on pump signal when ratio < 0.7 | 378 | 6.2% | 2% | -7% | -55% (-63%) | -4% |
| exit on pump signal when ratio < 1.0 | 378 | 4.9% | 6% | -5% | -54% (-61%) | -3% |
| entry < 1.0 skipped + exit when ratio < 1.0 | 333 | 5.0% | 5% | -7% | -56% (-64%) | -3% |
| entry < 1.0 skipped + exit when ratio < 1.0 + skip new listings | 331 | 5.0% | 5% | -6% | -55% (-63%) | -3% |
| entry < 1.0 skipped + exit when ratio < 1.0, +50% stop | 333 | 0.0% | 19% | -3% | -51% (-62%) | -2% |
| entry < 1.0 skipped + exit when ratio < 1.0, +30% stop | 333 | 0.0% | 34% | -2% | -46% (-66%) | -4% |

### 2022-01 .. 2024-12 (cohort starts), volume rank 101+, 1x isolated, 30-day hold

Ratio found for 98% of entries and 100% of signal bars; entry filters drop coins with no ratio (mostly listings younger than a day or two).

CAGR and max drawdown are averaged over the four staggered schedules; the worst schedule's drawdown is in brackets.

| rule | coins/cohort | liquidated | closed early | CAGR | max DD (worst) | CAGR, BTC-hedged |
|---|---|---|---|---|---|---|
| no filter | 97 | 5.1% | 0% | -53% | -93% (-97%) | -12% |
| no filter, +30% stop | 97 | 0.0% | 33% | -36% | -80% (-85%) | -3% |
| skip coins listed < 30 days | 96 | 5.1% | 0% | -54% | -93% (-97%) | -13% |
| entry: skip ratio < 0.7 | 97 | 5.3% | 0% | -58% | -94% (-98%) | -18% |
| entry: skip ratio < 1.0 | 96 | 5.3% | 0% | -58% | -94% (-98%) | -18% |
| exit on pump signal (any ratio) | 97 | 2.2% | 11% | -50% | -92% (-95%) | -9% |
| exit on pump signal when ratio < 0.7 | 97 | 3.9% | 4% | -53% | -93% (-97%) | -12% |
| exit on pump signal when ratio < 1.0 | 97 | 3.3% | 7% | -52% | -92% (-97%) | -11% |
| entry < 1.0 skipped + exit when ratio < 1.0 | 96 | 3.4% | 7% | -57% | -93% (-97%) | -17% |
| entry < 1.0 skipped + exit when ratio < 1.0 + skip new listings | 94 | 3.4% | 7% | -58% | -94% (-97%) | -18% |
| entry < 1.0 skipped + exit when ratio < 1.0, +50% stop | 96 | 0.0% | 20% | -50% | -89% (-91%) | -13% |
| entry < 1.0 skipped + exit when ratio < 1.0, +30% stop | 96 | 0.0% | 35% | -40% | -81% (-86%) | -7% |

