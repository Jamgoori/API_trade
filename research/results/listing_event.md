## Listing pumps of existing perps (219 events, Binance perp prices)

### What the perp did (median; positive = price went up, bad for a short)

| venue | events | 24h before listing | 72h before | listing hour | +24h | +48h | +7d | max rise in 48h |
|---|---|---|---|---|---|---|---|---|
| all | 219 | +9.9% | +12.9% | -1.0% | -4.6% | -5.6% | -10.3% | +6.8% |
| binance_spot | 27 | +9.9% | +20.2% | -6.8% | -7.7% | -8.6% | -15.4% | +5.9% |
| bithumb | 101 | +8.1% | +8.4% | -0.9% | -4.5% | -5.5% | -9.7% | +4.8% |
| upbit | 91 | +18.4% | +23.8% | +0.7% | -4.5% | -5.8% | -10.3% | +11.6% |

Means (the tail matters for a short): all: +48h -3.1%, +7d -8.1%, binance_spot: +48h -4.7%, +7d -10.3%, bithumb: +48h -3.8%, +7d -7.7%, upbit: +48h -1.7%, +7d -7.9%

### Shorting the perp (per trade on margin, 1x)

| venue | entry | filter | exits | 2024-01..2025-06 | 2025-07..2026-10 |
|---|---|---|---|---|---|
| all | listing hour | none | TP −15% / stop +15% / 48h | 91, +2.4%, win 69% | 128, -0.2%, win 58% |
| all | listing hour | none | TP −15% / stop +30% / 7d | 91, +2.3%, win 68% | 128, -0.8%, win 66% |
| all | listing hour | pumped 20%+ in the 24h before | TP −15% / stop +15% / 48h | 23, +3.9%, win 70% | 40, +2.0%, win 65% |
| all | listing hour | pumped 20%+ in the 24h before | TP −15% / stop +30% / 7d | 23, +1.7%, win 65% | 40, +1.8%, win 72% |
| all | listing hour | ratio > 1.5 at entry | TP −15% / stop +15% / 48h | 45, +2.4%, win 67% | 41, -0.4%, win 56% |
| all | listing hour | ratio > 1.5 at entry | TP −15% / stop +30% / 7d | 45, +2.9%, win 67% | 41, -0.3%, win 66% |
| all | +1h | none | TP −15% / stop +15% / 48h | 91, +2.6%, win 68% | 128, +2.0%, win 62% |
| all | +1h | none | TP −15% / stop +30% / 7d | 91, +2.5%, win 68% | 128, +2.2%, win 73% |
| all | +1h | pumped 20%+ in the 24h before | TP −15% / stop +15% / 48h | 23, +4.1%, win 65% | 40, +2.6%, win 68% |
| all | +1h | pumped 20%+ in the 24h before | TP −15% / stop +30% / 7d | 23, +1.7%, win 65% | 40, +2.4%, win 78% |
| all | +1h | ratio > 1.5 at entry | TP −15% / stop +15% / 48h | 46, +1.7%, win 65% | 36, +6.1%, win 72% |
| all | +1h | ratio > 1.5 at entry | TP −15% / stop +30% / 7d | 46, +3.2%, win 65% | 36, +8.1%, win 83% |
| all | +4h | none | TP −15% / stop +15% / 48h | 91, +1.8%, win 65% | 128, +1.0%, win 58% |
| all | +4h | none | TP −15% / stop +30% / 7d | 91, +2.2%, win 66% | 128, +1.1%, win 68% |
| all | +4h | pumped 20%+ in the 24h before | TP −15% / stop +15% / 48h | 23, +3.7%, win 65% | 40, +1.2%, win 60% |
| all | +4h | pumped 20%+ in the 24h before | TP −15% / stop +30% / 7d | 23, +3.1%, win 65% | 40, +0.1%, win 65% |
| all | +4h | ratio > 1.5 at entry | TP −15% / stop +15% / 48h | 49, +1.8%, win 63% | 37, +5.7%, win 76% |
| all | +4h | ratio > 1.5 at entry | TP −15% / stop +30% / 7d | 49, +3.4%, win 67% | 37, +7.7%, win 78% |
| all | next 00:00 UTC | none | TP −15% / stop +15% / 48h | 91, -0.8%, win 52% | 128, +0.7%, win 54% |
| all | next 00:00 UTC | none | TP −15% / stop +30% / 7d | 91, +1.6%, win 62% | 128, +4.0%, win 76% |
| all | next 00:00 UTC | pumped 20%+ in the 24h before | TP −15% / stop +15% / 48h | 23, +0.1%, win 52% | 40, +1.8%, win 65% |
| all | next 00:00 UTC | pumped 20%+ in the 24h before | TP −15% / stop +30% / 7d | 23, +0.2%, win 52% | 40, +5.1%, win 75% |
| all | next 00:00 UTC | ratio > 1.5 at entry | TP −15% / stop +15% / 48h | 0, — | 0, — |
| all | next 00:00 UTC | ratio > 1.5 at entry | TP −15% / stop +30% / 7d | 0, — | 0, — |
| binance_spot | listing hour | none | TP −15% / stop +15% / 48h | 10, +5.3%, win 70% | 17, +0.8%, win 59% |
| binance_spot | listing hour | none | TP −15% / stop +30% / 7d | 10, +3.4%, win 60% | 17, -0.5%, win 71% |
| binance_spot | listing hour | pumped 20%+ in the 24h before | TP −15% / stop +15% / 48h | 5, +8.7%, win 80% | 4, — |
| binance_spot | listing hour | pumped 20%+ in the 24h before | TP −15% / stop +30% / 7d | 5, +6.6%, win 60% | 4, — |
| binance_spot | listing hour | ratio > 1.5 at entry | TP −15% / stop +15% / 48h | 4, — | 8, -0.1%, win 50% |
| binance_spot | listing hour | ratio > 1.5 at entry | TP −15% / stop +30% / 7d | 4, — | 8, +0.2%, win 75% |
| binance_spot | +1h | none | TP −15% / stop +15% / 48h | 10, +2.3%, win 60% | 17, +0.5%, win 53% |
| binance_spot | +1h | none | TP −15% / stop +30% / 7d | 10, +2.6%, win 60% | 17, -1.2%, win 65% |
| binance_spot | +1h | pumped 20%+ in the 24h before | TP −15% / stop +15% / 48h | 5, +3.6%, win 60% | 4, — |
| binance_spot | +1h | pumped 20%+ in the 24h before | TP −15% / stop +30% / 7d | 5, +5.8%, win 60% | 4, — |
| binance_spot | +1h | ratio > 1.5 at entry | TP −15% / stop +15% / 48h | 4, — | 4, — |
| binance_spot | +1h | ratio > 1.5 at entry | TP −15% / stop +30% / 7d | 4, — | 4, — |
| binance_spot | +4h | none | TP −15% / stop +15% / 48h | 10, +1.8%, win 70% | 17, +0.2%, win 53% |
| binance_spot | +4h | none | TP −15% / stop +30% / 7d | 10, +2.0%, win 70% | 17, +1.9%, win 76% |
| binance_spot | +4h | pumped 20%+ in the 24h before | TP −15% / stop +15% / 48h | 5, +4.6%, win 80% | 4, — |
| binance_spot | +4h | pumped 20%+ in the 24h before | TP −15% / stop +30% / 7d | 5, +7.1%, win 80% | 4, — |
| binance_spot | +4h | ratio > 1.5 at entry | TP −15% / stop +15% / 48h | 3, — | 3, — |
| binance_spot | +4h | ratio > 1.5 at entry | TP −15% / stop +30% / 7d | 3, — | 3, — |
| binance_spot | next 00:00 UTC | none | TP −15% / stop +15% / 48h | 10, -2.7%, win 40% | 17, -1.9%, win 35% |
| binance_spot | next 00:00 UTC | none | TP −15% / stop +30% / 7d | 10, -6.7%, win 40% | 17, +1.3%, win 76% |
| binance_spot | next 00:00 UTC | pumped 20%+ in the 24h before | TP −15% / stop +15% / 48h | 5, -1.5%, win 40% | 4, — |
| binance_spot | next 00:00 UTC | pumped 20%+ in the 24h before | TP −15% / stop +30% / 7d | 5, -4.0%, win 40% | 4, — |
| binance_spot | next 00:00 UTC | ratio > 1.5 at entry | TP −15% / stop +15% / 48h | 0, — | 0, — |
| binance_spot | next 00:00 UTC | ratio > 1.5 at entry | TP −15% / stop +30% / 7d | 0, — | 0, — |
| bithumb | listing hour | none | TP −15% / stop +15% / 48h | 51, +2.9%, win 73% | 50, +2.5%, win 66% |
| bithumb | listing hour | none | TP −15% / stop +30% / 7d | 51, +2.7%, win 69% | 50, +3.2%, win 72% |
| bithumb | listing hour | pumped 20%+ in the 24h before | TP −15% / stop +15% / 48h | 3, — | 6, +13.7%, win 100% |
| bithumb | listing hour | pumped 20%+ in the 24h before | TP −15% / stop +30% / 7d | 3, — | 6, +14.2%, win 100% |
| bithumb | listing hour | ratio > 1.5 at entry | TP −15% / stop +15% / 48h | 30, +2.9%, win 70% | 17, +3.1%, win 71% |
| bithumb | listing hour | ratio > 1.5 at entry | TP −15% / stop +30% / 7d | 30, +2.6%, win 67% | 17, +5.0%, win 71% |
| bithumb | +1h | none | TP −15% / stop +15% / 48h | 51, +2.0%, win 69% | 50, +2.0%, win 62% |
| bithumb | +1h | none | TP −15% / stop +30% / 7d | 51, +2.1%, win 65% | 50, +1.1%, win 66% |
| bithumb | +1h | pumped 20%+ in the 24h before | TP −15% / stop +15% / 48h | 3, — | 6, +7.3%, win 83% |
| bithumb | +1h | pumped 20%+ in the 24h before | TP −15% / stop +30% / 7d | 3, — | 6, +6.0%, win 83% |
| bithumb | +1h | ratio > 1.5 at entry | TP −15% / stop +15% / 48h | 33, +1.2%, win 67% | 18, +3.9%, win 67% |
| bithumb | +1h | ratio > 1.5 at entry | TP −15% / stop +30% / 7d | 33, +1.3%, win 61% | 18, +4.5%, win 67% |
| bithumb | +4h | none | TP −15% / stop +15% / 48h | 51, +1.8%, win 63% | 50, +0.9%, win 58% |
| bithumb | +4h | none | TP −15% / stop +30% / 7d | 51, +2.3%, win 65% | 50, +0.8%, win 68% |
| bithumb | +4h | pumped 20%+ in the 24h before | TP −15% / stop +15% / 48h | 3, — | 6, +2.1%, win 67% |
| bithumb | +4h | pumped 20%+ in the 24h before | TP −15% / stop +30% / 7d | 3, — | 6, -2.0%, win 67% |
| bithumb | +4h | ratio > 1.5 at entry | TP −15% / stop +15% / 48h | 35, +2.1%, win 66% | 18, +5.5%, win 72% |
| bithumb | +4h | ratio > 1.5 at entry | TP −15% / stop +30% / 7d | 35, +3.2%, win 66% | 18, +8.2%, win 78% |
| bithumb | next 00:00 UTC | none | TP −15% / stop +15% / 48h | 51, -0.4%, win 53% | 50, +0.5%, win 50% |
| bithumb | next 00:00 UTC | none | TP −15% / stop +30% / 7d | 51, +2.3%, win 65% | 50, +4.8%, win 78% |
| bithumb | next 00:00 UTC | pumped 20%+ in the 24h before | TP −15% / stop +15% / 48h | 3, — | 6, +2.4%, win 67% |
| bithumb | next 00:00 UTC | pumped 20%+ in the 24h before | TP −15% / stop +30% / 7d | 3, — | 6, +10.1%, win 83% |
| bithumb | next 00:00 UTC | ratio > 1.5 at entry | TP −15% / stop +15% / 48h | 0, — | 0, — |
| bithumb | next 00:00 UTC | ratio > 1.5 at entry | TP −15% / stop +30% / 7d | 0, — | 0, — |
| upbit | listing hour | none | TP −15% / stop +15% / 48h | 30, +0.6%, win 63% | 61, -2.7%, win 51% |
| upbit | listing hour | none | TP −15% / stop +30% / 7d | 30, +1.2%, win 70% | 61, -4.1%, win 61% |
| upbit | listing hour | pumped 20%+ in the 24h before | TP −15% / stop +15% / 48h | 15, +2.0%, win 67% | 30, +1.0%, win 63% |
| upbit | listing hour | pumped 20%+ in the 24h before | TP −15% / stop +30% / 7d | 15, +0.3%, win 67% | 30, +2.2%, win 73% |
| upbit | listing hour | ratio > 1.5 at entry | TP −15% / stop +15% / 48h | 11, +1.4%, win 64% | 16, -4.3%, win 44% |
| upbit | listing hour | ratio > 1.5 at entry | TP −15% / stop +30% / 7d | 11, +3.5%, win 73% | 16, -6.1%, win 56% |
| upbit | +1h | none | TP −15% / stop +15% / 48h | 30, +3.7%, win 70% | 61, +2.5%, win 64% |
| upbit | +1h | none | TP −15% / stop +30% / 7d | 30, +3.1%, win 77% | 61, +4.0%, win 82% |
| upbit | +1h | pumped 20%+ in the 24h before | TP −15% / stop +15% / 48h | 15, +3.0%, win 60% | 30, +3.0%, win 70% |
| upbit | +1h | pumped 20%+ in the 24h before | TP −15% / stop +30% / 7d | 15, -0.4%, win 67% | 30, +4.6%, win 83% |
| upbit | +1h | ratio > 1.5 at entry | TP −15% / stop +15% / 48h | 9, +5.5%, win 67% | 14, +9.1%, win 86% |
| upbit | +1h | ratio > 1.5 at entry | TP −15% / stop +30% / 7d | 9, +11.3%, win 89% | 14, +12.5%, win 100% |
| upbit | +4h | none | TP −15% / stop +15% / 48h | 30, +1.8%, win 67% | 61, +1.4%, win 59% |
| upbit | +4h | none | TP −15% / stop +30% / 7d | 30, +2.2%, win 67% | 61, +1.0%, win 66% |
| upbit | +4h | pumped 20%+ in the 24h before | TP −15% / stop +15% / 48h | 15, +2.4%, win 60% | 30, +2.3%, win 63% |
| upbit | +4h | pumped 20%+ in the 24h before | TP −15% / stop +30% / 7d | 15, +1.5%, win 60% | 30, +1.7%, win 67% |
| upbit | +4h | ratio > 1.5 at entry | TP −15% / stop +15% / 48h | 11, +1.4%, win 55% | 16, +5.8%, win 81% |
| upbit | +4h | ratio > 1.5 at entry | TP −15% / stop +30% / 7d | 11, +3.6%, win 73% | 16, +7.2%, win 75% |
| upbit | next 00:00 UTC | none | TP −15% / stop +15% / 48h | 30, -1.0%, win 53% | 61, +1.6%, win 62% |
| upbit | next 00:00 UTC | none | TP −15% / stop +30% / 7d | 30, +3.0%, win 63% | 61, +4.1%, win 74% |
| upbit | next 00:00 UTC | pumped 20%+ in the 24h before | TP −15% / stop +15% / 48h | 15, -0.6%, win 53% | 30, +1.9%, win 67% |
| upbit | next 00:00 UTC | pumped 20%+ in the 24h before | TP −15% / stop +30% / 7d | 15, +1.2%, win 53% | 30, +4.3%, win 73% |
| upbit | next 00:00 UTC | ratio > 1.5 at entry | TP −15% / stop +15% / 48h | 0, — | 0, — |
| upbit | next 00:00 UTC | ratio > 1.5 at entry | TP −15% / stop +30% / 7d | 0, — | 0, — |
