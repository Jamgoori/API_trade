# hedgebot: Arcus ↔ Hyperliquid HIP-3 헤지 포인트 봇

Arcus(Robinhood Chain)와 Hyperliquid HIP-3 거래소(trade.xyz = `hl:xyz`, Entropy = `hl:io`)에
**같은 종목을 같은 수량, 반대 방향**으로 잡아서 가격 위험 없이 Arcus 포인트(거래량·예치금·OI)를 쌓고,
Entropy 거래 이력도 남기는 봇입니다.

```
Arcus  NVDA-USD  메이커(ALO, 수수료 0%)  ──체결──▶  Hyperliquid xyz:NVDA  반대 방향 즉시 헤지
        ▲ 포인트 적립                                  (메이커 먼저, 20초/3bp 넘으면 테이커)
```

> 투자 조언이 아닙니다. 포인트 배분은 각 거래소 재량이고, 아래 수치는 모두 과거 데이터와 가정에 기반한 추정입니다.

---

## 1. 설계 (논의안에서 바꾼 점 포함)

| 항목 | 이전 논의 | 이 봇의 구현과 이유 |
|---|---|---|
| 증거금 | 양쪽 격리 | **Arcus·trade.xyz는 교차증거금**. 헤지된 포지션은 계정 전체가 받쳐 줘서 청산까지 여유가 훨씬 큼. Entropy는 격리 전용이라 자동으로 격리 |
| 방향 | 고정 | **펀딩비 7일 평균으로 결정**. 지난 45일간 Arcus 롱은 연 10~16%, HL 롱은 연 4~13%를 냄 → "Arcus 숏 / HL 롱"이 구조적으로 펀딩 수취. 차이가 작으면 Arcus 안에서 롱·숏이 상쇄되게 배치(시장 전체 급변에 계정이 한 방향으로 쏠리지 않음) |
| 헤지 체결 | 30초 안에 정리 | Arcus 체결 즉시 HL에 **메이커로 먼저 헤지 → 20초 경과 또는 3bp 불리 시 테이커**. 백테스트상 포인트당 비용 약 25% 절감. `hedge_mode: taker`로 바꾸면 즉시 테이커 |
| 손절 | 고정 손절가 | **계정 증거금 비율 / 청산가까지 거리** 기준 자동 축소(양쪽 다리 동시). 고정 손절은 헤지 포지션에서 한쪽만 잘리는 원인이 됨 |
| 회전 | 짧게 반복 | **Arcus 장외시간(포인트 부스트 시간대)에만**, 6~12시간 보유 후 30~70% 회전. 크기·시간 랜덤. 백테스트상 짧은 회전(1.5~4h)은 포인트당 비용이 2배 |
| Entropy | 4~5배 | **2배**. 격리 전용이라 다른 계정 돈을 못 끌어옴. 백테스트에서 4배는 청산 1회, 리스크 축소 9회 |

안전장치:

- **불균형 자동 수리**: 두 다리 수량 차이가 $40 이상이면 8초 확인 후 큰 쪽을 줄여서 맞춤(reduce-only)
- **헤지 실패 시**: IOC를 5/15/40bp로 넓혀도 안 되면 Arcus 쪽을 즉시 되돌림. 한쪽만 들고 있지 않음
- **Arcus 데드맨 스위치**: 봇이 죽으면 60초 뒤 Arcus 미체결 주문 자동 취소
- **장외시간 규칙 반영**: Arcus 장외 초기증거금 상향(신규 진입 레버리지 한도), 가격 밴드, 04:00·20:00 ET 경계 전후 3분 진입 금지
- **일일 비용 예산**($20) 초과 시 회전 중단, **일일 손실 2%** 초과 시 신규 진입 중단(헤지 포지션은 유지)
- 재시작해도 상태 유지(SQLite 저널 + 실제 포지션으로 복구)

---

## 2. 백테스트 결과 (실데이터, 기본 설정 $14k)

`hedgebot backtest`로 Arcus/Hyperliquid 실제 캔들과 펀딩비 이력을 그대로 재생한 결과입니다(2026-10-24 기준).

| 기간 | 순손익 | 펀딩 | 수수료+스프레드 | Arcus 거래량/일 | 포인트/주* | 포인트당 비용 | 청산/축소 |
|---|---|---|---|---|---|---|---|
| 45일, 15분봉 | **-$87** (-$1.9/일) | +$138 | -$224 | $24.9k | 5.7 | **$2.35** | 0 / 0 |
| 100일, 1시간봉 | **-$100** (-$1.0/일) | +$204 | -$298 | $14.9k | 3.4 | **$2.02** | 0 / 0 |

\* 포인트는 **가정치**입니다: 커뮤니티 공개 기록 기준 RWA 거래량 $100만당 약 33포인트.

45일 변형 비교(`--sweep`):

| 변형 | 순손익 | 포인트/주 | 포인트당 비용 |
|---|---|---|---|
| 기본 (6~12h 회전, 메이커 헤지) | -$87 | 5.7 | $2.35 |
| 테이커 헤지 | -$111 | 5.7 | $3.03 |
| 8~16h 회전 | -$48 | 4.6 | $1.64 |
| 1.5~4h 회전 | -$371 | 12.9 | $4.46 |
| 회전 없이 보유만 | -$30 | 0.1 | (거래량 포인트 거의 없음, OI만) |
| 명목가 1.5배 | +$59 | 2.8 | 펀딩 수익이 비용보다 큼 |

읽는 법:

- **회전을 빠르게 할수록 포인트는 늘지만 포인트당 비용이 오릅니다.** `min_hold_h / max_hold_h`가 이 조절 손잡이입니다.
- 펀딩비 수취가 비용의 절반 이상을 메웁니다. 명목가를 키우면 펀딩 수익이 커집니다(대신 청산 여유는 줄어듦).
- 100일 동안 Arcus 계정은 $8,000 → $5,154, HL 계정들은 늘었습니다. 헤지라서 합계는 같아도 **거래소 간 잔고가 벌어지니** 경고가 오면 수동으로 옮겨야 합니다.

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
pytest -q                                            # 59개 테스트
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
| `capital` | arcus 8000 / hl:xyz 3000 / hl:io 3000 | 거래소별 자금 (paper 잔고) |
| `pairs[].notional_usd`, `leverage`, `direction` | | 페어별 명목가, 레버리지, `auto` / `long_maker` / `short_maker` |
| `strategy.min_hold_h / max_hold_h` | 6 / 12 | 회전 간격. 짧을수록 포인트↑ 비용↑ |
| `strategy.rotate_when` | off_hours | Arcus 장외시간(20:00~04:00 ET, 주말)에만 회전 |
| `strategy.max_daily_cost_usd` | 20 | 하루 수수료+슬리피지 한도 |
| `strategy.basis_guard_bps` | 8 | 두 거래소 가격 차이가 평소보다 이만큼 불리하면 대기 |
| `execution.hedge_mode` | passive_first | `taker`로 바꾸면 즉시 테이커 헤지 |
| `risk.margin_ratio_warn / critical` | 3.0 / 1.7 | 자산÷유지증거금. 경고(입금 안내) / 자동 30% 축소 |
| `risk.max_daily_loss_pct` | 2 | 초과 시 신규 진입·회전 중단 |

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
  strategy.py            방향·회전·베이시스·사이징 규칙 (실거래와 백테스트가 공유)
  engine.py              메인 루프, 데드맨 스위치, 스냅샷, 수수료 대조
  backtest/              과거 캔들·펀딩 재생 시뮬레이터
  scanner.py, report.py, journal.py, cli.py
tests/                   59개 (서명, 체결, 헤지 실패, 불균형, 회전, 리스크, 백테스트)
```
