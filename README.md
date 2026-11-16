# hedgebot: Entropy 전용 헤지 포인트 봇

Hyperliquid의 HIP-3 거래소 **Entropy(`hl:io`) 한 곳에서만** 거래합니다. 서로 강하게 같이 움직이는 메모리 종목끼리
한쪽은 롱, 다른 쪽은 숏으로 잡아서 시장 방향 위험을 줄인 채 **Entropy 포지션(OI)과 거래량**을 쌓습니다.
Arcus·trade.xyz는 쓰지 않습니다. 돈은 Entropy 계정 하나에만 두면 되고 거래소 간 이체가 없습니다.

```
MEM-US   DRAM(메모리 바스켓)  ⇄  SNDK(샌디스크)     교차증거금   DRAM $12,000 : SNDK $9,600
MEM-KR   SKHX(SK하이닉스)     ⇄  EWY(한국 ETF)      격리 전용    SKHX $5,600  : EWY $8,960
         ────────────────────────────────────────────────────────────────
         Entropy OI 약 $36k (잔고 $18k의 2배) · 3일마다 한 번 전부 닫고 다시 쌓음
```

> 투자 조언이 아닙니다. 서로 다른 종목으로 헤지하므로 **가격 위험이 0이 아닙니다**(2장). Entropy 포인트 제도는
> 아직 공식 발표가 없고, 아래 수치는 모두 과거 데이터와 가정에 기반한 추정입니다.

---

## 1. 전략

### 왜 이 종목들인가

Entropy에서 거래되는 종목 중 같은 종목이 두 번 상장된 것은 없습니다. 그래서 같은 거래소 안에서 헤지하려면
**같이 움직이는 다른 종목**을 써야 합니다. 120일 실데이터로 모든 조합을 비교했고, 메모리 반도체 묶음이 압도적이었습니다.

| 조합 | 일간 수익률 상관 | 헤지한 뒤 연 변동성 (총 명목가 $1당) |
|---|---|---|
| **DRAM ⇄ SNDK** | 0.91~0.96 | 17~19% |
| **SKHX ⇄ EWY** | 0.93~0.96 | 12~13% |
| 두 페어를 합치면 | | **약 10%** (서로 다르게 흔들려서 상쇄) |
| ANTH ⇄ OAI (프리 IPO) | 0.5 | 25% (OAI 호가도 얇음) |
| NVDA ⇄ AAPL, NBIS ⇄ 기타 | 0.4~0.75 | 30~40% |

- 두 페어 모두 "단일 종목 ⇄ 바스켓"(SNDK ⇄ 메모리 바스켓, SK하이닉스 ⇄ 한국 ETF) 구조라 방향을 어느 쪽으로 잡아도 헤지 효과가 같습니다.
  그래서 방향은 **펀딩을 받는 쪽**으로 자유롭게 정합니다.
- **변동성 비율로 금액을 맞춥니다**(`hedge_notional_ratio`). SNDK가 DRAM보다 1.25배 크게 움직이므로 SNDK는 DRAM의 0.8배 금액,
  SKHX가 EWY보다 1.6배 크게 움직이므로 EWY는 SKHX의 1.6배 금액입니다. 금액을 1:1로 맞추면 변동성이 약 30% 커집니다.
- 메이커(호가에 걸어서 체결)는 스프레드가 넓은 쪽(DRAM, SKHX), 헤지는 호가가 두꺼운 쪽(SNDK, EWY)에 둡니다.
- Entropy 수수료는 growth mode로 메이커 0.3bp·테이커 0.9bp라 비용은 거의 스프레드입니다.

### 3일 주기

```
쌓기(1~2시간) → 보유 66~78시간 → 미국 정규장(09:30~16:00 ET)에 100% 닫기 → 바로 다시 쌓기 → …
```

- 보유하는 동안은 거래하지 않습니다. 실제 회전 간격은 정규장 시간을 기다리느라 평균 약 3.7일입니다.
- 전부 닫았다 다시 쌓는 이유: ① 거래량이 생기고 ② 헤지 비율을 그날 가격으로 다시 맞추고 ③ 격리증거금이 초기화되고
  ④ 7일 평균 펀딩을 받는 방향으로 다시 정합니다(같은 비용으로 방향 전환까지).
- 정규장에만 회전하는 이유: 주식 오라클이 살아 있고 호가가 가장 촘촘한 시간입니다.

### 포인트

Entropy는 아직 포인트 계산식을 공개하지 않았습니다(2026년 10월 기준 리더보드 "Coming Soon", 백엔드에 `pointsMultiplier`
값만 보임). 그래서 어느 쪽으로 계산하든 쌓이게 했습니다:
**OI**(시간의 약 97% 동안 보유), **거래량**(3일마다 전체 명목가의 2배), **4개 종목**에 걸친 거래 이력.

---

## 2. 위험 (꼭 읽어 주세요)

예전 구조(Arcus ⇄ Entropy, 같은 종목)는 가격 위험이 거의 없었습니다. **지금 구조는 두 종목의 차이만큼 손익이 흔들립니다.**
SNDK가 메모리 바스켓보다 더 오르거나, SK하이닉스가 한국 ETF와 다르게 움직이면 그만큼 벌거나 잃습니다.
장기 기대값은 0 근처(+ 펀딩 − 비용)지만 중간에 흔들리는 폭은 작지 않습니다.

기본 크기(잔고의 2배)에서 112일 백테스트 기준:

- 하루 평균 ±$260 (잔고의 1.4%), 가장 나빴던 날 **-$1,018**
- 최고점 대비 최대 **-$2,900~3,800** (잔고의 15~19%)
- 실적 발표(SanDisk, SK하이닉스, 마이크론)나 한국·미국 장 시간 차이로 한쪽만 크게 움직일 때가 가장 위험합니다.

보호 장치(위에서부터 먼저 작동):

| 장치 | 내용 |
|---|---|
| 페어 구성 | 상관 0.9 이상 종목끼리, 변동성 비율로 금액 맞춤, 두 페어로 분산 |
| 교차증거금 (MEM-US) | 한쪽 손실을 다른 쪽 이익이 바로 받쳐 줌. 두 종목 차이가 50% 넘게 벌어져야 청산 수준 |
| 격리 다리 (SKHX, EWY) 3배 | 한 다리가 약 30% 불리하게 움직여야 청산. **청산가 8% 이내면 계정 여유 잔고로 자동 보충**(15%까지) |
| 거래소 손절 | 봇이 꺼져 있어도 작동. 격리 다리는 청산가 2% 앞, 교차 계정은 증거금 비율 1.25. 한 다리가 닫히면 봇이 반대쪽도 정리 |
| 일일 손실 3% | 그날 신규 진입·회전 중단 (포지션은 유지) |
| **손실 한도** | 자산이 최고점 대비 **12% 빠지면 텔레그램 알림, 25% 빠지면 모든 페어를 닫고 멈춤**. `hedgebot resume`으로만 다시 시작. 입금·출금은 손익으로 안 셈 |
| 불균형 수리·헤지 실패 처리 | 두 다리 차이가 $40 넘으면 큰 쪽을 줄임. 헤지가 안 되면 메이커 쪽을 즉시 되돌림 (한쪽만 들고 있지 않음) |

흔들림을 줄이고 싶으면 크기를 줄이세요(아래 표의 1.5배: `notional_usd`를 12000→9000, 5600→4200).

---

## 3. 백테스트 (실데이터 112일, 잔고 $18k)

`hedgebot backtest --days 112 --interval 1h --price-proxy hl:io=hl:xyz` (2026-07-27 ~ 11-16).
Entropy에 상장되기 전 기간(SKHX·EWY·DRAM은 최근 상장)은 **trade.xyz의 같은 종목 가격**으로 채웠습니다.
무작위 요소(보유 시간 등)를 바꿔 5번 돌린 범위입니다.

| 크기 (명목가/잔고) | Entropy OI 평균 | 거래량/일 (양쪽 다리) | 112일 비용* | 최고점 대비 최대 손실 | 하루 변동 (1σ) | 순손익 범위 |
|---|---|---|---|---|---|---|
| 1.5배 | $26.6k | $14.6k | $46 | -$2.1k~2.8k (11~14%) | $196 | +$45 ~ +$1,357 |
| **2배 (기본)** | **$35.6k** | **$19.5k** | **$58** | **-$2.9k~3.8k (15~19%)** | **$262** | **+$20 ~ +$1,800** |
| 2.5배 | $44.3k | $24.2k | $89 | -$3.6k~4.8k (18~23%) | $333 | -$26 ~ +$1,371 |
| 3배 | $52.9k | $29.0k | $87 | -$4.3k~5.9k (21~27%) | $395 | +$131 ~ +$1,805 |

\* 수수료 + 스프레드 − 펀딩 수입. 모든 크기·모든 실행에서 청산·거래소 손절 0회 (2배 기준 5번 실행 중 격리 자동 보충 총 2회).

- **순손익이 플러스인 건 이 기간 운이 좋았던 것**입니다(대부분 두 종목 차이에서 나옴). 기대값으로 보지 마세요. 비용 자체는 하루 $0.5~1 수준입니다.
- 예전 구조(Arcus $8k + Entropy $8k + trade.xyz $2k)는 Entropy OI $25.7k·거래량 $15.5k/일에 3일마다 거래소 간 이체가 필요했습니다.
  지금은 같은 $18k로 **Entropy OI $35.6k·거래량 $19.5k/일**, 이체 없음. 대신 2장의 가격 위험이 생깁니다.

회전 주기 비교 (2배, 같은 기간):

| 회전 | 거래량/일 | OI 평균 | 112일 비용* |
|---|---|---|---|
| 보유만 (회전 없음) | $0.3k | $38.5k | $180 (펀딩 방향을 못 바꿔서 오히려 비쌈) |
| 8~16시간마다 | $50.4k | $34.7k | $471 |
| 24~48시간마다 | $29.9k | $35.3k | $141 |
| **3일마다 (기본)** | **$19.7k** | **$35.7k** | **$98** |
| 5~7일마다 | $10.0k | $35.7k | -$70 (펀딩 수입이 더 큼) |
| 3일마다, 50%만 | $11.7k | $35.7k | -$20 |

- 포인트가 거래량 위주라면 회전을 늘리고(24~48시간), OI 위주라면 5~7일로 늘리면 됩니다. 3일은 그 중간입니다.
- 헤지를 바로 테이커로 하면(`hedge_mode: taker`) 비용이 약 3배가 됩니다. 기본은 메이커로 먼저 걸고 20초 뒤 테이커입니다.

백테스트가 알 수 없는 것: 메이커 주문이 실제로 체결됐을지(과거 호가창 없음, 헤지의 50%가 메이커로 잡힌다고 가정),
실제 스프레드(Entropy 반 스프레드 2~3.5bp 가정), 실제 포인트 공식. 실전 처음 며칠은 `hedgebot report --hours 24`로 확인하세요.

---

## 4. 실전 시작 (VPS에서 24시간)

봇은 계속 켜져 있어야 하므로 VPS(Ubuntu 22.04/24.04, 1 vCPU·1GB RAM이면 충분)에서 돌립니다.

### 예전 구조(Arcus)를 돌리고 있었다면 먼저 정리

```bash
cd API_trade
sudo systemctl stop hedgebot
.venv/bin/hedgebot flatten --mode live --confirm --i-understand-live    # 예전 config.yaml 로 모든 포지션 정리
git fetch origin claude/trusting-brown-gaokjv && git checkout claude/trusting-brown-gaokjv && git pull
.venv/bin/pip install -e .
mv config.yaml config-arcus-old.yaml
mv data/journal.sqlite data/journal-arcus-old.sqlite                    # 새 전략은 새 기록으로 시작
cp config.example.yaml config.yaml && sed -i 's/^mode: paper /mode: live  /' config.yaml
```

그다음 Arcus·trade.xyz에 남은 돈을 Hyperliquid의 **Entropy(io) 거래소**로 옮기고 아래 3)부터 진행하세요.

### 새로 설치

```bash
# 1) 설치: 가상환경 + config.yaml/.env 생성 + systemd 서비스 등록
git clone -b claude/trusting-brown-gaokjv https://github.com/Jamgoori/API_trade && cd API_trade
bash deploy/install.sh

# 2) 키 입력 (5장). 채팅이나 다른 곳에 붙여넣지 마세요
nano .env

# 3) Hyperliquid 계정이 Standard 모드인지 확인 (통합 계정이면 봇이 시작을 거부)
.venv/bin/hedgebot hl-mode
#    아니면: .venv/bin/hedgebot hl-mode --set-standard --i-understand-live
#    그리고 Hyperliquid 웹에서 USDC 를 Entropy(io) 거래소로 옮기세요 (봇 키는 이체 권한이 없습니다)

# 4) 키·종목·잔고 확인
.venv/bin/hedgebot check --mode live

# 5) 주문 기능 점검: 최소 금액(약 $12)으로 실제 주문 → 대조 → 취소, --fill 이면 진입 → 손절 → 격리증거금 $1 추가 → 청산
.venv/bin/hedgebot selftest --mode live --fill --i-understand-live

# 6) 24시간 실행 (재부팅·오류 시 자동 재시작)
sudo systemctl start hedgebot
journalctl -u hedgebot -f              # 실시간 로그 (Ctrl+C 로 빠져나와도 봇은 계속 돎)
```

| 할 일 | 명령 |
|---|---|
| 상태 보기 | `.venv/bin/hedgebot status`, `.venv/bin/hedgebot report --hours 72` |
| **한 달에 한 번: 헤지 비율 점검** | `.venv/bin/hedgebot pairs` (5장 아래) |
| 멈추기 | `sudo systemctl stop hedgebot` (걸린 주문 취소, 포지션과 거래소 손절은 그대로) |
| 다시 켜기 | `sudo systemctl start hedgebot` (실제 포지션을 읽어 이어서 진행) |
| 손실 한도로 멈췄을 때 | `sudo systemctl stop hedgebot && .venv/bin/hedgebot resume && sudo systemctl start hedgebot` |
| 업데이트 | `git pull && .venv/bin/pip install -e . && sudo systemctl restart hedgebot` |
| 비상 전체 청산 | `sudo systemctl stop hedgebot && .venv/bin/hedgebot flatten --mode live --confirm --i-understand-live` |

모의 거래: `hedgebot run --mode paper` (키 없이 실시세, 별도 기록 `data/journal-paper.sqlite`).

### 헤지 비율 점검 (`hedgebot pairs`)

```
pair       window   corr  maker vol  hedge vol ratio now suggested  book vol now  suggested
MEM-US        90d   0.91        77%        96%      0.80      0.80         18.2%      18.2%
MEM-KR        90d   0.93        90%        54%      1.60      1.67         12.8%      12.6%
```

- `suggested`가 60~90일 기준으로 지금 값과 15% 넘게 차이 나면 `config.yaml`의 `hedge_notional_ratio`를 바꾸고 재시작하세요.
  새 값은 다음 회전(전부 닫았다 다시 쌓을 때)부터 쓰입니다.
- `corr`이 0.8 밑으로 떨어지면 그 페어는 헤지가 잘 안 되는 상태입니다. 크기를 줄이거나 끄세요(`enabled: false`).

### 자금을 더 넣을 때

`sizing.scale_with_equity: true`(기본)면 모든 페어가 **Entropy 잔고 ÷ `capital.hl:io`($18k)** 배수로 자동 조정됩니다.
$36k가 되면 모든 페어가 2배. 입금은 30분, 출금은 6시간 유지되면 반영합니다. 3배 이상 키울 때는 EWY 호가가 얇다는 점
(10bp 안에 약 $12k)을 감안하세요.

---

## 5. 키 설정 (.env)

| 변수 | 내용 | 권한 |
|---|---|---|
| `HL_ACCOUNT_ADDRESS` | Hyperliquid **메인 지갑 주소** (잔고가 있는 주소) | |
| `HL_AGENT_PRIVKEY` | app.hyperliquid.xyz/API 에서 Generate 한 **API 지갑 개인키** | 거래 전용, 출금·이체 불가 |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | 알림 (강력 권장: 손실 한도·보충·손절 알림을 받는 유일한 통로) | |

- Arcus 키는 필요 없습니다(비워 두세요).
- 키는 `.env`에만 두세요(권한 600). `.env`는 git에 올라가지 않습니다. API 지갑에는 유효기간이 있으니 만료 전에 새로 발급해 바꾸고 재시작하세요.
- **봇 전용 지갑을 쓰세요.** 봇은 시작할 때 그 계정의 미체결 주문을 모두 취소하고, 봇이 모르는 Entropy 포지션은 알림만 보냅니다.
- **Hyperliquid 계정은 Standard(수동) 모드여야 합니다.** 통합 계정에서는 Entropy 잔고가 따로 보이지 않아 봇이 시작을 거부합니다.

---

## 6. 주요 설정 (`config.yaml`)

| 키 | 기본값 | 설명 |
|---|---|---|
| `capital.hl:io` | 18000 | 크기의 1배 기준(paper에서는 모의 잔고) |
| `pairs[].notional_usd` | 12000 / 5600 | 메이커 다리 금액. 페어 전체 = `notional_usd × (1 + hedge_notional_ratio)` |
| `pairs[].hedge_notional_ratio` | 0.8 / 1.6 | 메이커 $1당 헤지 금액(변동성 비율). 같은 거래소의 두 종목 페어에는 필수 |
| `pairs[].leverage` | 5 / 3 | 교차 다리는 증거금 사용량만, 격리 다리는 청산까지 거리(3배 ≈ 30%)를 정함 |
| `strategy.min_hold_h / max_hold_h` | 66 / 78 | 회전 간격 |
| `strategy.rotation_fraction_min/max` | 1.0 / 1.0 | 회전 때 닫는 비율. 1.0이면 전부 닫고 새 비율·새 방향으로 다시 쌓음 |
| `strategy.rotate_when` | us_session | 미국 정규장에만 회전 (`any` = 아무 때나) |
| `strategy.max_daily_cost_usd` | 40 | 하루 수수료+슬리피지 한도 (크기 배수만큼 같이 늘어남) |
| `execution.hedge_mode` | passive_first | `taker`면 즉시 테이커 (비용 약 3배) |
| `risk.max_daily_loss_pct` | 3 | 초과 시 그날 신규 진입·회전 중단 |
| `risk.drawdown_warn_pct / max_drawdown_pct` | 12 / 25 | 최고점 대비 손실: 알림 / 전부 닫고 멈춤 (0 = 끔) |
| `risk.isolated_topup_enabled / target_pct` | true / 15 | 격리 다리가 청산가 8% 이내면 여유 잔고로 15%까지 보충 |
| `risk.backstop_isolated_buffer_pct` | 2 | 격리 다리 거래소 손절: 청산가 2% 앞 |
| `risk.max_account_leverage` | 4 | 계정 전체 명목가 / 자산 상한 |

---

## 7. 그 밖의 명령

```bash
hedgebot pairs                                   # 헤지 비율·상관 점검 (30/60/90일)
hedgebot backtest --days 112 --interval 1h --price-proxy hl:io=hl:xyz          # config.yaml 기준
hedgebot backtest --days 112 --interval 1h --price-proxy hl:io=hl:xyz --sweep  # 회전 주기·크기·헤지 방식 비교
hedgebot run --mode paper                        # 키 없이 실시세 모의 거래
hedgebot resume                                  # 손실 한도 정지 해제 (봇을 멈춘 상태에서)
```

백테스트 옵션: `--half-spread hl:io=2.0:3.5`(정규장:장외 bp), `--passive-hedge-fill-rate 0.5`, `--seed N`.
`--interval 15m`은 최근 약 50일까지만 데이터가 있습니다.

Arcus 페어(예전 구조)도 코드에는 그대로 남아 있습니다. `pairs`에 Arcus 다리를 넣으면 예전처럼 동작합니다.

---

## 8. 확인한 것과 남은 것

- **실시세 모의 거래(2026-11-16)**: 두 페어가 약 1분 만에 목표까지 쌓였고, 헤지 금액이 설정 비율과 일치
  (DRAM $11.96k 숏 / SNDK $9.57k 롱 = 0.80, SKHX $5.58k 롱 / EWY $8.94k 숏 = 1.60). 격리 다리 거래소 손절이 청산가 2% 앞에 걸림.
- 테스트 124개 통과 (같은 거래소 두 종목 페어의 헤지 비율·회전·재시작, 손실 한도 정지·유지·출금 구분, 백테스트 포함).
- 이전에 확인된 것: Hyperliquid 주문·레버리지·트리거 손절·격리증거금 서명, HIP-3 종목 매핑.

아직 실전에서 확인하지 못한 것: Entropy에서 실제 체결, 격리증거금 실제 추가. 첫 실전의 `hedgebot selftest --mode live --fill`이
최소 금액으로 이 부분을 확인합니다. 실전 첫 회전(약 3일 뒤)도 `journalctl -u hedgebot`으로 지켜보세요.

---

## 9. 구조

```
hedgebot/
  venues/hyperliquid.py  HIP-3 dex별 계정 (공식 SDK + API 지갑)
  venues/paper.py        실시세 기반 모의 체결 (수수료·펀딩·증거금·청산)
  venues/arcus.py        Arcus REST (예전 구조용)
  executor.py            페어 상태 머신: BUILD → HOLD → UNWIND. 메이커 클립 + 헤지 + 불균형 수리.
                         두 종목 페어는 금액 비율로 헤지하고, 비율은 전부 닫혔을 때 그날 가격으로 다시 정함
  risk.py                증거금 비율, 격리 청산 거리·자동 보충, 일일 손실, 최고점 대비 손실 한도
  backstop.py            거래소에 걸어 두는 손절 주문
  strategy.py            방향·회전·회전 시간대·사이징 규칙 (실거래와 백테스트가 공유)
  pairstats.py           두 다리의 상관·변동성 비율 (hedgebot pairs)
  engine.py              메인 루프, 시작·종료, 잔고 비례 크기, 손실 한도 정지, 비상 청산
  backtest/              과거 캔들·펀딩 재생 (다른 거래소 같은 종목으로 상장 전 기간 채우기)
  sizing.py, rebalance.py, selftest.py, scanner.py, report.py, journal.py, cli.py
deploy/                  install.sh (VPS 설치), hedgebot.service (systemd)
tests/                   124개
```

참고: [Entropy 시장 데이터 (loris.tools)](https://loris.tools/hip3/io), [DefiLlama Entropy](https://defillama.com/rwa/perps/venue/entropy),
[airdropalert Entropy (포인트 미확정)](https://airdropalert.com/airdrops/entropy/).
