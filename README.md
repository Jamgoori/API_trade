# hedgebot: Arcus ↔ Hyperliquid HIP-3 헤지 포인트 봇

Arcus(Robinhood Chain)와 Hyperliquid HIP-3 거래소(trade.xyz = `hl:xyz`, Entropy = `hl:io`)에
**같은 종목을 같은 수량, 반대 방향**으로 잡아서 가격 위험 없이 **Entropy 포지션·거래량(주력, 자산 75%)**과
Arcus 포인트(자산 25%)를 쌓는 봇입니다.

```
RWA 작업  Arcus NVDA·TSLA  메이커(수수료 0%)  ──체결──▶  trade.xyz  반대 방향 헤지
io 작업   Arcus SNDK·DRAM·NBIS 메이커          ──체결──▶  Entropy    반대 방향 헤지 (주력)
          헤지: 메이커로 먼저 걸고, 20초 경과 또는 3bp 불리하면 테이커
```

> 투자 조언이 아닙니다. 포인트 배분은 각 거래소 재량이고, 아래 수치는 모두 과거 데이터와 가정에 기반한 추정입니다.

---

## 1. 구조

자산을 두 작업으로 나눕니다(총 $18k 예시: Arcus $8k / Entropy $8k / trade.xyz $2k, `config.example.yaml`).

| 작업 | 비중 | 구성 | 목적 |
|---|---|---|---|
| **RWA 작업** | 약 22% ($4k) | Arcus NVDA $3k·TSLA $2k ↔ **trade.xyz** 헤지 (Arcus 약 $2k + xyz $2k) | Arcus 포인트(거래량), 6~12시간마다 30~70% 회전 |
| **io 작업 (주력)** | 약 78% ($14k) | Entropy SNDK $16.8k·DRAM $5.1k·NBIS $5.1k (5배, 목표 $27k) ↔ **Arcus** 헤지 (Entropy $8k + Arcus 약 $6k) | Entropy OI 최대, 24~48시간마다 30~60% 회전. **3일마다 리밸런싱 전제** |

- Arcus와 Entropy에 함께 있는 종목은 SNDK·DRAM·NBIS뿐입니다(ANTH·OAI 같은 프리 IPO는 Arcus에 없음).
- 헤지된 페어는 양쪽 거래량이 같습니다. 그래서 Arcus를 메이커(수수료 0%)로 두고 Entropy를 헤지 쪽으로 둬도
  Entropy 거래량은 똑같이 쌓이고, 비용은 이 배치가 가장 쌉니다.
- 회전 주기·비율은 페어별로 바꿀 수 있고, `rotate: false`면 회전 없이 보유만 합니다.

| 항목 | 구현과 이유 |
|---|---|
| 증거금 | Arcus·trade.xyz는 **교차증거금**(계정 전체가 받쳐 줘서 청산까지 여유가 큼). Entropy는 격리 전용 |
| Entropy 레버리지 | **5배**(청산까지 약 17%). 청산가 8% 이내로 오면 **Entropy 계정의 여유 잔고로 격리증거금을 자동 보충**, 3일마다 Arcus 이익을 Entropy로 옮김 |
| 방향 | **펀딩비 7일 평균**으로 결정. 차이가 작으면 같은 거래소 안에서 롱·숏이 상쇄되게 배치 |
| 헤지 체결 | 반대쪽에 **메이커로 먼저 걸고, 20초 경과 또는 3bp 불리 시 테이커**. `hedge_mode: taker`면 즉시 테이커 |
| 회전 시간대 | Arcus 장외시간(포인트 부스트 시간대)에만 |

안전장치:

- **불균형 자동 수리**: 두 다리 수량 차이가 $40 이상이면 8초 확인 후 큰 쪽을 줄여서 맞춤(reduce-only)
- **헤지 실패 시**: IOC를 5/15/40bp로 넓혀도 안 되면 Arcus 쪽을 즉시 되돌림. 한쪽만 들고 있지 않음
- **거래소 손절(봇이 꺼져 있어도 작동)**: 각 다리에 reduce-only 손절 주문을 거래소에 걸어 둠.
  순서는 `봇 축소(증거금 비율 1.7) → 거래소 손절(1.25 / 격리는 청산가 2% 앞) → 청산`.
  Arcus는 포지션 전체 손절(positionTpsl), Hyperliquid는 트리거 주문이며 포지션이 바뀌면 봇이 다시 겁니다.
  잔고가 충분해서 청산이 불가능한 다리(예: 100% 넘게 빠져야 청산)에는 걸지 않습니다.
- **한쪽이 닫힌 뒤**: 손절·청산으로 한쪽 다리가 사라지면 봇이 반대쪽도 바로 정리해서 맨 포지션을 남기지 않고,
  6시간(`stop_cooldown_h`) 쉰 뒤 방향을 새로 정해 증거금이 허락하는 만큼만 다시 쌓습니다. 봇이 꺼져 있던 동안
  손절이 났다면 다시 켜는 즉시 같은 처리를 합니다.
- **Arcus 데드맨 스위치**: 봇이 죽으면 60초 뒤 Arcus 미체결 주문 자동 취소
- **장외시간 규칙 반영**: Arcus 장외 초기증거금 상향(신규 진입 레버리지 한도), 가격 밴드, 04:00·20:00 ET 경계 전후 3분 진입 금지
- **일일 비용 예산**($20) 초과 시 회전 중단, **일일 손실 2%** 초과 시 신규 진입 중단(헤지 포지션은 유지)
- 재시작해도 상태 유지(SQLite 저널 + 실제 포지션으로 복구)

---

## 2. 백테스트 결과 (실데이터, 기본 설정 $18k)

`hedgebot backtest`로 Arcus/Hyperliquid 실제 캔들과 펀딩비 이력을 그대로 재생한 결과입니다(2026-10-25 기준).

| 기간 | 순손익 | 펀딩 | 수수료+스프레드 | Arcus 거래량/일 (RWA / io) | Entropy 거래량/일 | 포인트/주* | 청산·손절 |
|---|---|---|---|---|---|---|---|
| 45일, 15분봉 | **+$104** | +$295 | -$213 | $23.7k ($8.2k / $15.5k) | **$15.5k** | 5.5 | 0 |

Entropy 평균 OI **$25.7k**, 자동 보충 5회($3.0k), 3일 리밸런싱 10회(평균 $1.55k), 청산가에 가장 가까웠던 거리 6.9%,
Arcus 최저 증거금 비율 2.76. 경고선 3.0 밑으로 내려가면 봇이 추가 이체 금액을 알려 주는데, 45일간 5번 있었습니다.
그 경고를 무시하고 3일 리밸런싱만 해도 Arcus 최저 2.33으로 자동 축소선(1.7) 위였고, 축소·손절·청산은 0회였습니다(`--no-transfers`).
Entropy $8k 중 $5.4k(= $27k ÷ 5배)가 포지션 증거금이고, 나머지는 청산가에 가까워질 때 자동으로 넣는 보충분입니다.

Entropy 목표 크기 비교 (Arcus $8k / Entropy $8k / xyz $2k, Entropy 5배, 24~48시간 회전, 3일 리밸런싱, 45일):

| Entropy 목표 | 순손익 | Entropy OI 평균 | Entropy 거래량/일 | 청산가 최근접 | Arcus 최저 증거금 비율 | 경고 시 추가 이체 | 3일 리밸런싱 이동액 |
|---|---|---|---|---|---|---|---|
| $22.5k | +$90 | $21.6k | $12.7k | 6.5% | 2.86 | 1회 | $5.5k |
| **$27k (기본)** | **+$104** | **$25.7k** | **$15.5k** | 6.9% | 2.76 | 5회 | $15.5k |
| $32k | +$128 | $26.8k | $11.5k | 6.0% | 2.36 | 14회 | $39k |
| $36k | +$188 | $28.4k | $8.8k | 6.4% | 2.33 | 14회 | $42k |

- 포지션 크기의 한계는 **Arcus 증거금**입니다. Arcus가 RWA와 Entropy 헤지를 같이 떠안아서, Entropy를 키울수록 Arcus 증거금 비율이 내려갑니다.
- $27k를 넘기면 OI는 거의 늘지 않습니다($27k → $36k에 OI +10%). 증거금이 꽉 차서 가격이 움직이면 다시 채우지 못하고,
  회전도 줄어서 거래량은 오히려 떨어집니다. 대신 옮겨야 하는 금액이 3배 가까이 늘어납니다.
- 손익이 $36k에서 높은 건 SNDK 펀딩 수입이 커서이고, 펀딩 방향이 바뀌면 그대로 손실 쪽으로 커집니다.
- Entropy 거래량은 싸게 만들어집니다(Arcus 메이커 0% + Entropy 헤지 약 1bp). 회전을 짧게(예: 8~16시간) 하면 거래량이 늘고 하루 비용이 몇 달러 늘어납니다.

\* Arcus 포인트는 **가정치**입니다: 커뮤니티 공개 기록 기준 RWA 거래량 $100만당 약 33포인트.

- **펀딩비 수입이 비용을 메웁니다.** 지난 45일간 Arcus 롱은 연 10~16%, HL 롱은 연 4~13%를 냈고, 봇은 받는 쪽으로 방향을 잡았습니다.
- **회전을 줄이면 거래량·포인트는 줄고 수익은 늘어납니다.** 회전 없이 보유만 하면 45일 +$49, 1.5~4시간 회전이면 -$67에 포인트 2배입니다(`--sweep`).
- **거래소 간 잔고가 벌어집니다.** 헤지라 합계는 같아도 한쪽이 벌면 다른 쪽이 잃습니다. Entropy 5배는 이 차이에 민감해서
  **3일마다 봇이 알려 주는 금액만큼 Arcus → Entropy(또는 반대)로 옮기는 것을 전제**로 크기를 잡았습니다.

### 3일마다 리밸런싱하면? (io 작업만, 45일 실데이터)

Arcus ↔ Entropy 잔고를 3일마다 계획 비율로 되돌리는 경우를 Entropy 레버리지별로 비교했습니다(`--rebalance-every-h 72`,
이 표는 Entropy $5,250 / Arcus $5,250 배분 기준).

| Entropy 레버리지 (명목가) | 리밸런싱 | Entropy OI 평균 | Entropy 거래량/일 | 청산가에 가장 가까웠던 거리 | 자동 보충 | 이체 횟수 (평균 금액) |
|---|---|---|---|---|---|---|
| 2배 ($8k) | 없음 | $7.5k | $2.8k | 30.9% | 0 | 0 |
| 2배 | 3일마다 | $7.7k | $3.1k | 30.9% | 0 | 8회 ($390) |
| 4배 ($16k) | 없음 | $13.8k | $4.5k | 4.8% | 4 | 0 |
| **4배** | **3일마다** | **$15.4k** | **$6.3k** | **7.8%** | 0 | 10회 ($580) |
| 5배 ($20k) | 없음 | $16.3k | $5.7k | 0.6% (손절 1회) | 9 | 0 |
| 5배 | 3일마다 | $18.9k | $7.2k | 5.5% | 7 | 9회 ($620) |

- **2배에서는 리밸런싱이 필요 없습니다.** 청산까지 30% 넘게 여유가 남습니다.
- 리밸런싱의 가치는 **같은 돈으로 Entropy 레버리지를 4배로 올릴 수 있게 해 주는 것**입니다. OI·거래량이 약 2배가 됩니다.
- 3일 안에 오는 급변(SNDK 하루 ±13%)은 리밸런싱으로 못 막습니다. 그건 자동 보충과 거래소 손절이 맡습니다.
- 4배면 Arcus 계정 증거금 비율이 3.4까지 내려갑니다(경고 3.0). 그래서 Arcus 쪽 여유도 함께 봐야 합니다.

봇은 `rebalance` 설정에 따라 72시간마다 "Arcus → Entropy $X"처럼 금액을 알려 주고, `hedgebot rebalance`로 언제든 확인할 수 있습니다.
RWA 쪽(trade.xyz)은 리밸런싱 대상에서 뺐습니다.

백테스트가 알 수 없는 것: 메이커 주문이 실제로 체결됐을지(과거 호가창 없음), 실제 스프레드(실측 기반 가정값 사용), 실제 포인트 공식.
그래서 **3일 모의 거래로 체결률과 비용을 실측**하는 단계가 필요합니다.

---

## 3. 설치 (VPS 권장: 이 클라우드 세션은 계속 켜져 있지 않습니다)

```bash
git clone https://github.com/Jamgoori/API_trade && cd API_trade
python3 -m venv .venv && . .venv/bin/activate      # Python 3.11 이상
pip install -e ".[dev]"
cp config.example.yaml config.yaml                   # 금액·종목 수정
cp .env.example .env                                 # testnet/live 때만 키 입력
pytest -q                                            # 72개 테스트
```

## 4. 사용 순서

### ① 종목 확인 (키 필요 없음)
```bash
hedgebot scan                # Arcus RWA ↔ trade.xyz / Entropy 공통 종목, 펀딩비, 스프레드, 레버리지 한도
```

### ② 백테스트 (키 필요 없음)
```bash
hedgebot backtest --days 45            # config.yaml 기준
hedgebot backtest --days 45 --sweep    # 회전 주기·규모·헤지 방식 비교
hedgebot backtest --days 100 --interval 1h
```
옵션: `--half-spread arcus=0.3:0.5,hl:xyz=0.3:0.5,hl:io=0.8:1.2`, `--passive-hedge-fill-rate 0.5`, `--points-per-musd 33`, `--no-transfers`

### ③ 3일 모의 거래 (키 필요 없음, 실시세 사용)
```bash
tmux new -s hedgebot
hedgebot run --mode paper              # Ctrl+B, D 로 빠져나와도 계속 실행
hedgebot report                        # 언제든 중간 결과
hedgebot report --hours 72             # 3일 리포트 + 체크리스트
```
- `paper.maker_fill: through`(기본)는 가격이 내 호가를 **뚫고 지나가야** 체결로 봅니다. 보수적인 설정입니다.
- 리포트의 체크리스트: 헤지 실패 0, 불균형 수리 하루 1회 이하, 청산·축소 0, 정지 0, 헤지 지연 p95 5초 미만, 가동률 95% 이상.

### ④ 테스트넷 (선택)
```bash
hedgebot check --mode testnet          # 키·종목·잔고 확인
hedgebot run --mode testnet
```
Entropy는 테스트넷에 없어서 `hl:io` 페어는 자동으로 빠집니다. Arcus 테스트넷은 웹에서 Testnet Deposit(약 $1,000)을 받으세요.

### ⑤ 실거래 (소액부터)
```bash
hedgebot check --mode live
hedgebot run --mode live --i-understand-live
```
처음 1~2주는 `config.yaml`의 `notional_usd`를 줄여서($500~1,000 규모) 실제 수수료와 포인트를 확인한 뒤 늘리세요.

### 리밸런싱 (돈은 직접 이체)
```bash
hedgebot rebalance                              # 지금 옮겨야 할 금액과 방향
hedgebot rebalance --apply                      # paper 모드에서만: 모의 잔고에 이체를 반영
hedgebot backtest --days 45 --rebalance-every-h 72
```

### 비상 정리
```bash
hedgebot flatten --mode live --confirm --i-understand-live   # 모든 봇 포지션을 양쪽 다 시장가로 청산
```

---

## 5. 키 설정 (.env)

| 변수 | 내용 | 권한 |
|---|---|---|
| `ARCUS_API_PRIVKEY` | app.arcus.xyz/api-keys 에서 Generate 한 **API Signing Key** (64자리 hex) | 거래 전용, 출금 불가 |
| `ARCUS_ETH_ADDRESS` | 그 키를 등록한 지갑 주소 | |
| `HL_ACCOUNT_ADDRESS` | Hyperliquid **메인 지갑 주소** (잔고가 있는 주소) | |
| `HL_AGENT_PRIVKEY` | app.hyperliquid.xyz/API 에서 Generate 한 **API 지갑 개인키** | 거래 전용, 출금 불가 |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | 알림 (선택) | |

- 키는 `.env`에만 두세요. `.env`는 git에 올라가지 않습니다. 채팅에 붙여넣지 마세요.
- **Arcus는 봇 전용 서브계정을 권장합니다.** 봇은 testnet/live 시작 시 해당 서브계정의 미체결 주문을 모두 취소합니다.
  API 키 발급 화면에서 Subaccount #를 고르고 `arcus.account_index`를 같게 맞추세요.
- HL API 지갑은 거래소(dex) 간 자금 이동을 못 합니다. 잔고 경고가 오면 웹에서 직접 옮기세요.

---

## 6. 주요 설정 (`config.yaml`)

| 키 | 기본값 | 설명 |
|---|---|---|
| `capital` | arcus 8000 / hl:xyz 2000 / hl:io 8000 | 거래소별 자금 (paper 잔고). RWA 약 22% / io 약 78% |
| `pairs[].notional_usd`, `leverage`, `direction` | | 페어별 명목가, 레버리지, `auto` / `long_maker` / `short_maker` |
| `pairs[].rotate`, `min_hold_h`, `max_hold_h`, `rotation_fraction_min/max` | io: 24~48h, 30~60% | 페어별 회전 설정. `rotate: false`면 보유만 |
| `strategy.min_hold_h / max_hold_h` | 6 / 12 | 회전 간격. 짧을수록 포인트↑ 비용↑ |
| `strategy.rotate_when` | off_hours | Arcus 장외시간(20:00~04:00 ET, 주말)에만 회전 |
| `strategy.max_daily_cost_usd` | 20 | 하루 수수료+슬리피지 한도 |
| `strategy.basis_guard_bps` | 8 | 두 거래소 가격 차이가 평소보다 이만큼 불리하면 대기 |
| `execution.hedge_mode` | passive_first | `taker`로 바꾸면 즉시 테이커 헤지 |
| `risk.margin_ratio_warn / critical` | 3.0 / 1.7 | 자산÷유지증거금. 경고(입금 안내) / 자동 30% 축소 |
| `risk.max_daily_loss_pct` | 2 | 초과 시 신규 진입·회전 중단 |
| `risk.backstop_margin_ratio` | 1.25 | 거래소 손절 위치 (교차증거금 계정 기준) |
| `risk.backstop_isolated_buffer_pct` | 2 | 격리 포지션 손절: 청산가 몇 % 앞 |
| `risk.stop_cooldown_h` | 6 | 한쪽이 닫힌 뒤 재진입까지 대기. 0이면 자동 재진입 안 함 |
| `risk.isolated_topup_enabled / target_pct` | true / 15 | Entropy 포지션이 청산가 8% 이내면 여유 잔고로 보충해 15%로 되돌림 |
| `rebalance.enabled / every_h / venues` | true / 72 / arcus, hl:io | 정기 리밸런싱 알림. `capital` 비율로 다시 나눈 이체 금액을 텔레그램으로 알림 |

---

## 7. 알려진 한계와 확인할 것

1. **Arcus 퍼프 접근 권한**: 화이트리스트에 없는 주소는 API가 `address not on access whitelist`를 반환합니다. `hedgebot check`로 확인하세요.
2. **데드맨 스위치 서명 방식**: Arcus 문서에 `scheduleCancel`의 서명 방식이 명시되지 않아 `setLeverage`와 같은 방식으로 구현했습니다. 실패하면 봇은 경고만 내고 계속 돌지만, 그러면 봇이 죽었을 때 Arcus 미체결 주문이 남을 수 있습니다. 테스트넷에서 경고가 없는지 확인하세요.
3. **HIP-3 수수료**: trade.xyz·Entropy 종목 대부분이 Growth mode(수수료 90% 할인)라는 공개 정보로 계산했습니다. 실거래에서는 10분마다 실제 체결 수수료를 받아 저널에 기록합니다(`report`의 reconciled fees).
4. **포인트**: 공식 계산식은 비공개입니다. 리포트의 포인트 추정치는 `points.arcus_points_per_musd` 가정입니다. 매주 수요일 실제 배분과 비교해 보정하세요(`hedgebot report --points-per-musd X`).
5. **Entropy 테스트넷 없음**: Entropy 쪽은 모의 거래 후 소액 실거래로만 검증할 수 있습니다.

---

## 8. 구조

```
hedgebot/
  venues/arcus.py        Arcus REST (Ed25519 서명: 주문 typed payload / 레거시 메시지)
  venues/hyperliquid.py  HIP-3 dex별 계정 (공식 SDK + API 지갑)
  venues/paper.py        실시세 기반 모의 체결 (수수료·펀딩·증거금·청산 시뮬레이션)
  executor.py            페어 상태 머신: BUILD → HOLD → UNWIND, 메이커 클립 + 헤지 + 불균형 수리
  risk.py                계정 증거금 비율, 격리 청산 거리, 일일 손실 한도
  backstop.py            거래소에 걸어 두는 손절 주문 관리
  rebalance.py           거래소 간 리밸런싱 이체 계산
  strategy.py            방향·회전·베이시스·사이징 규칙 (실거래와 백테스트가 공유)
  engine.py              메인 루프, 데드맨 스위치, 스냅샷, 수수료 대조
  backtest/              과거 캔들·펀딩 재생 시뮬레이터
  scanner.py, report.py, journal.py, cli.py
tests/                   72개 (서명, 체결, 헤지 실패, 불균형, 회전, 리스크, 거래소 손절, 백테스트)
```
