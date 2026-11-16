# hedgebot: Entropy ⇄ trade.xyz 헤지 포인트 봇

같은 종목을 Hyperliquid의 HIP-3 거래소 **Entropy(`hl:io`)와 trade.xyz(`hl:xyz`)에 같은 수량, 반대 방향**으로 잡습니다.
같은 종목끼리라 가격 위험이 거의 없고, **Entropy OI·거래량**이 쌓입니다. Arcus는 쓰지 않습니다.

```
            Entropy (메이커로 쌓음)         trade.xyz (즉시 헤지)
mem-us      SNDK 롱  $9,600            ⇄    SNDK 숏  $9,600
            DRAM 숏  $12,000           ⇄    DRAM 롱  $12,000
mem-kr      SKHX 롱  $5,600            ⇄    SKHX 숏  $5,600
            EWY  숏  $8,960            ⇄    EWY  롱  $8,960
            ──────────────────────          ──────────────────────
            OI 약 $36k (잔고 $9k)            OI 약 $36k (잔고 $9k)
            3일마다 한 번 전부 닫고 다시 쌓음 (묶음 방향은 펀딩으로 결정, 위는 +1일 때)
```

> 투자 조언이 아닙니다. Entropy 포인트 제도는 아직 공식 발표가 없고, 아래 수치는 모두 과거 데이터와 가정에 기반한 추정입니다.

---

## 1. 전략

### 같은 종목으로 헤지

Entropy와 trade.xyz에 둘 다 있는 종목(SNDK, DRAM, SKHX, EWY, NVDA, NBIS, AAPL, CRCL) 중 메모리 4종목을 씁니다.
같은 주식이라 두 거래소 가격은 거의 같이 움직입니다(일간 상관 1.00, 차이의 연 변동성 1~2%).

- **Entropy = 메이커, trade.xyz = 헤지.** Entropy 호가는 넓고(EWY 10bp, SKHX 9bp) trade.xyz는 훨씬 촘촘합니다(0.1~1.7bp, 호가 깊이 수십 배).
  그래서 Entropy에 호가를 걸어 체결시키고, 체결되는 즉시 trade.xyz에서 테이커로 반대 포지션을 잡습니다. Entropy 거래량은 전부 메이커 거래량입니다.
- 수수료는 두 거래소 모두 growth mode라 메이커 0.3bp·테이커 0.9bp입니다.

### 거래소마다 롱·숏을 섞는 이유 (direction_group)

같은 종목 헤지라도 돈은 두 거래소에 나뉘어 있습니다. Entropy에서 롱만 들고 있으면 주가가 오를 때 Entropy 잔고는 늘고
trade.xyz 잔고는 줄어서, 자주 돈을 옮겨야 합니다. 그래서 같이 움직이는 두 종목을 묶어 **한 거래소 안에서 하나는 롱, 하나는 숏**으로 둡니다.

- mem-us: SNDK ⇄ DRAM(메모리 바스켓), mem-kr: SKHX ⇄ EWY(한국 ETF). 일간 상관 0.91~0.96.
- 금액은 변동성 비율로 맞춥니다(SNDK가 DRAM보다 1.25배, SKHX가 EWY보다 1.6배 크게 움직임).
- 그러면 한 거래소의 잔고는 업종 전체가 아니라 **두 종목의 차이만큼만** 움직이고(연 12~18%), 이체가 드물어집니다.
- 묶음의 방향(+1/−1)은 두 페어의 펀딩을 합쳐서 정합니다. 예: Entropy EWY 롱은 trade.xyz보다 연 6% 정도 펀딩을 더 내므로, Entropy EWY 숏이 유리합니다.
  회전 때 반대 방향이 연 2% 이상 유리하면 묶음 전체가 같이 방향을 바꿉니다.

### 3일 주기

```
쌓기(수 분~1시간) → 보유 66~78시간 → 미국 정규장(09:30~16:00 ET)에 100% 닫기 → 바로 다시 쌓기 → …
```

- 보유하는 동안은 거래하지 않습니다. 실제 회전 간격은 정규장을 기다리느라 평균 약 3.7일입니다.
- 전부 닫았다 다시 쌓으면 거래량이 생기고, 격리증거금이 초기화되고, 묶음 방향을 펀딩에 맞춰 다시 정합니다.
- 정규장에만 회전하는 이유: 두 거래소의 주식 오라클이 살아 있어 거래소 간 가격 차이가 가장 작은 시간입니다.

### 포인트

Entropy는 아직 포인트 계산식을 공개하지 않았습니다(2026년 10월 기준 리더보드 "Coming Soon", 백엔드에 `pointsMultiplier`
값만 보임). 그래서 어느 쪽으로 계산하든 쌓이게 했습니다:
**OI**(시간의 약 97% 동안 보유), **거래량**(3일마다 전체 명목가의 2배, 전부 메이커), **4개 종목**에 걸친 거래 이력.

---

## 2. 위험과 보호 장치

같은 종목 헤지라 **두 거래소 합계는 거의 안 움직입니다**(112일 백테스트 최대 손실 1.6%, $289). 남는 위험은 이렇습니다.

| 위험 | 내용과 대응 |
|---|---|
| 거래소 간 잔고 차이 | 한쪽이 벌면 다른 쪽이 잃습니다. 봇이 72시간마다 확인해서 **$1,000 이상 차이 날 때만** "Entropy → trade.xyz $X 옮기세요"를 텔레그램으로 알립니다(112일에 2~5번). Hyperliquid 웹에서 두 거래소 사이로 USDC를 옮기면 됩니다. 안 옮겨도 112일 동안 손절·청산은 없었습니다(잔고 차이 최대 $5.8k) |
| 격리 다리 | Entropy SKHX·EWY, trade.xyz EWY는 격리 전용이라 다리마다 따로 청산됩니다. 3배라 약 30% 움직여야 청산. **청산가 8% 이내면 그 거래소의 여유 잔고로 자동 보충**, 거래소 손절은 청산가 2% 앞 |
| 한쪽 다리가 닫힘 | 손절·청산·상장폐지로 한쪽이 사라지면 봇이 다른 거래소의 반대쪽도 바로 닫고 6시간 뒤 다시 쌓습니다. **봇이 꺼져 있으면 남은 쪽이 혼자 열려 있게 됩니다** → systemd로 자동 재시작, 텔레그램 필수 |
| 거래소 간 가격 차이 | 같은 종목이라도 두 거래소 가격이 수 bp 다릅니다. 평소보다 8bp 이상 불리하면 진입을 미루고, 정규장에만 회전합니다 |
| Entropy 운영 | Entropy는 신생 거래소이고 예전에 MU·GOOGL·META 등을 상장폐지한 적이 있습니다. 두 거래소 모두 Hyperliquid 위라 Hyperliquid 장애는 양쪽에 같이 영향을 줍니다 |
| 일일 손실 2% | 두 거래소 합계가 하루 2% 넘게 줄면 그날 신규 진입·회전 중단 (포지션 유지) |
| **손실 한도** | 합계가 최고점 대비 **6% 빠지면 알림, 12% 빠지면 모든 페어를 닫고 멈춤**(정상이면 1~2% 이상 안 움직이므로 고장 신호). `hedgebot resume`으로만 다시 시작. 입금·출금·거래소 간 이체는 손익으로 안 셈 |
| 불균형·헤지 실패 | 두 다리 차이가 $40 넘으면 큰 쪽을 줄임. 헤지가 안 되면 Entropy 쪽을 즉시 되돌림 (한쪽만 들고 있지 않음) |

---

## 3. 백테스트 (실데이터 112일, 잔고 $18k = Entropy $9k + trade.xyz $9k)

`hedgebot backtest --days 112 --interval 1h --price-proxy hl:io=hl:xyz` (2026-07-27 ~ 11-16).
Entropy에 상장되기 전 기간(SKHX·EWY·DRAM은 최근 상장)은 **trade.xyz의 같은 종목 가격**으로 채웠습니다(그 기간은 거래소 간 가격 차이 0).
무작위 요소(보유 시간 등)를 바꿔 5번 돌린 범위입니다.

| 크기 (거래소별 명목가/잔고) | Entropy OI 평균 | Entropy 거래량/일 | 112일 순손익 | 최대 손실 | 이체 (≥$1k) | 손절·청산 | 격리 청산가 최근접 |
|---|---|---|---|---|---|---|---|
| 3배 | $26.8k | $14.9k | -$67 ~ -$153 | $216 (1.2%) | 1~5번 | 0 | 7.6% |
| **4배 (기본)** | **$35.7k** | **$19.9k** | **-$90 ~ -$204** | **$289 (1.6%)** | **2~5번** | **0** | **7.6%** |
| 5배 | $43.3k | $23.1k | -$51 ~ -$215 | $329 (1.8%) | 3~6번 | 0 | 10.2% |

기본 크기의 손익 구성(5번 평균): 수수료+스프레드 -$513, 펀딩 +$171, 거래소 간 가격 차이 +$201(운) → 하루 약 -$1~2.

세 구조 비교 (같은 $18k):

| | 예전 (Arcus ⇄ Entropy) | Entropy만 (서로 다른 종목) | **Entropy ⇄ trade.xyz (지금)** |
|---|---|---|---|
| Entropy OI 평균 | $25.7k | $35.6k | **$35.7k** |
| Entropy 거래량/일 | $15.5k | $19.5k | **$19.9k** |
| 가격 위험 (최고점 대비 최대 손실) | 거의 없음 | 15~19% | **1.6%** |
| 거래소 간 이체 | 3일마다 | 없음 | **112일에 2~5번** |

회전 주기 비교 (4배, 같은 기간, 비용 = 수수료 + 스프레드 − 펀딩):

| 회전 | Entropy 거래량/일 | Entropy OI 평균 | 112일 비용 |
|---|---|---|---|
| 보유만 (회전 없음) | $0.3k | $38.6k | -$146 (펀딩 수입) |
| 8~16시간마다 | $50.7k | $35.1k | $1,146 |
| 24~48시간마다 | $30.2k | $35.4k | $604 |
| **3일마다 (기본)** | **$19.9k** | **$35.8k** | **$338** |
| 5~7일마다 | $10.0k | $35.7k | $88 |
| 3일마다, 50%만 | $10.7k | $35.9k | $102 |

- 포인트가 거래량 위주라면 회전을 늘리고(24~48시간), OI 위주라면 5~7일로 늘리면 됩니다. 3일은 그 중간입니다.
- trade.xyz 호가가 촘촘해서 헤지를 메이커로 먼저 걸어도(`passive_first`) 비용이 거의 같습니다. 그래서 기본은 즉시 테이커(`taker`)입니다.

백테스트가 알 수 없는 것: Entropy 메이커 주문이 실제로 얼마나 빨리 체결될지(과거 호가창 없음. 특히 EWY는 Entropy 거래량이 하루 $13만 정도라 쌓는 데 시간이 걸릴 수 있음),
실제 스프레드(Entropy 반 스프레드 2~3.5bp 가정), 실제 포인트 공식. 실전 처음 며칠은 `hedgebot report --hours 72`로 확인하세요.

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

그다음 Arcus에 남은 돈을 Hyperliquid로 옮겨서 **Entropy(io)와 trade.xyz(xyz) 거래소에 반씩** 넣고 아래 3)부터 진행하세요.

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
#    그리고 Hyperliquid 웹에서 USDC 를 Entropy(io)와 trade.xyz(xyz) 거래소에 반씩 옮기세요 (봇 키는 이체 권한이 없습니다)

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
| 이체 알림을 받았을 때 | Hyperliquid 웹에서 알림 금액만큼 trade.xyz ⇄ Entropy 이동 (`.venv/bin/hedgebot rebalance`로 다시 확인) |
| **한 달에 한 번: 묶음 비율 점검** | `.venv/bin/hedgebot pairs` (아래) |
| 멈추기 | `sudo systemctl stop hedgebot` (걸린 주문 취소, 포지션과 거래소 손절은 그대로) |
| 다시 켜기 | `sudo systemctl start hedgebot` (실제 포지션을 읽어 이어서 진행) |
| 손실 한도로 멈췄을 때 | `sudo systemctl stop hedgebot && .venv/bin/hedgebot resume && sudo systemctl start hedgebot` |
| 업데이트 | `git pull && .venv/bin/pip install -e . && sudo systemctl restart hedgebot` |
| 비상 전체 청산 | `sudo systemctl stop hedgebot && .venv/bin/hedgebot flatten --mode live --confirm --i-understand-live` |

모의 거래: `hedgebot run --mode paper` (키 없이 실시세, 별도 기록 `data/journal-paper.sqlite`).

### 묶음 비율 점검 (`hedgebot pairs`)

```
Direction groups: A = group_side +1, B = group_side -1 (ratio = B notional per A notional)
                       window   corr   vol A   vol B ratio now suggested  book vol now  suggested
mem-us SNDK/DRAM          90d   0.91     96%     77%      1.25      1.25         18.1%      18.1%
mem-kr SKHX/EWY           90d   0.93     89%     53%      1.60      1.67         12.8%      12.6%
```

- 위쪽 표(같은 종목 페어)는 `corr` 1.00, `book vol` 몇 %면 정상입니다(두 거래소 가격이 같이 움직임).
- 아래쪽 표(묶음)의 `suggested`가 60~90일 기준으로 지금 비율과 15% 넘게 차이 나면, 묶음 안 두 페어의 `notional_usd` 비율을 맞추고 재시작하세요
  (예: mem-us 비율 1.25 = DRAM `notional_usd` ÷ SNDK `notional_usd`). 새 크기는 다음 회전부터 쓰입니다.
- 묶음 비율이 조금 틀려도 손익에는 영향이 거의 없고(같은 종목 헤지), 거래소 간 잔고가 조금 더 벌어질 뿐입니다.

### 자금을 더 넣을 때

`sizing.scale_with_equity: true`(기본)면 모든 페어가 **두 거래소 잔고 합계 ÷ `capital` 합계($18k)** 배수로 자동 조정됩니다.
$36k가 되면 모든 페어가 2배. **두 거래소에 반씩** 넣으세요(크기는 돈이 적은 쪽 배수까지만 커짐). 입금은 30분, 출금은 6시간
유지되면 반영합니다. 크게 키울 때는 Entropy EWY 호가가 얇다는 점(10bp 안에 약 $7k, 하루 거래량 약 $13만)을 감안하세요.

---

## 5. 키 설정 (.env)

| 변수 | 내용 | 권한 |
|---|---|---|
| `HL_ACCOUNT_ADDRESS` | Hyperliquid **메인 지갑 주소** (잔고가 있는 주소) | |
| `HL_AGENT_PRIVKEY` | app.hyperliquid.xyz/API 에서 Generate 한 **API 지갑 개인키** | 거래 전용, 출금·거래소 간 이체 불가 |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | 알림 (강력 권장: 이체·손실 한도·보충·손절 알림을 받는 유일한 통로) | |

- Arcus 키는 필요 없습니다(비워 두세요).
- 키는 `.env`에만 두세요(권한 600). `.env`는 git에 올라가지 않습니다. API 지갑에는 유효기간이 있으니 만료 전에 새로 발급해 바꾸고 재시작하세요.
- **봇 전용 지갑을 쓰세요.** 봇은 시작할 때 그 계정의 미체결 주문을 모두 취소하고, 봇이 모르는 포지션은 알림만 보냅니다.
- **Hyperliquid 계정은 Standard(수동) 모드여야 합니다.** 통합 계정에서는 Entropy·trade.xyz 잔고가 따로 보이지 않아 봇이 시작을 거부합니다.

---

## 6. 주요 설정 (`config.yaml`)

| 키 | 기본값 | 설명 |
|---|---|---|
| `capital` | hl:io 9000 / hl:xyz 9000 | 크기의 1배 기준(paper에서는 모의 잔고). 두 거래소 같은 금액 |
| `pairs[].maker / hedge` | io:종목 / xyz:종목 | Entropy가 메이커, trade.xyz가 헤지 (같은 종목, 같은 수량) |
| `pairs[].notional_usd` | 9600 / 12000 / 5600 / 8960 | 한쪽 다리 금액. 묶음 안 비율 = 두 종목 변동성 비율 |
| `pairs[].direction_group / group_side` | mem-us, mem-kr / ±1 | 묶음이 +1일 때 이 페어의 Entropy 방향. 같은 묶음은 같이 방향을 바꿈 |
| `pairs[].leverage` | 10 / 10 / 3 / 3 | 교차 다리는 증거금 사용량만, 격리 다리는 청산까지 거리(3배 ≈ 30%)를 정함 |
| `strategy.min_hold_h / max_hold_h` | 66 / 78 | 회전 간격 |
| `strategy.rotation_fraction_min/max` | 1.0 / 1.0 | 회전 때 닫는 비율. 1.0이면 전부 닫고 다시 쌓음 |
| `strategy.rotate_when` | us_session | 미국 정규장에만 회전 (`any` = 아무 때나) |
| `strategy.maker_price` | improve | Entropy 스프레드가 3틱 이상이면 1틱 안쪽에 걸어 먼저 체결 |
| `strategy.basis_guard_bps` | 8 | 두 거래소 가격 차이가 평소보다 이만큼 불리하면 대기 |
| `strategy.max_daily_cost_usd` | 40 | 하루 수수료+슬리피지 한도 (크기 배수만큼 같이 늘어남) |
| `execution.hedge_mode` | taker | 즉시 테이커 헤지 (`passive_first` = 메이커로 먼저 20초) |
| `rebalance.every_h / min_transfer_usd` | 72 / 1000 | 두 거래소 잔고 차이 확인 주기와 알림 기준 금액 |
| `risk.max_daily_loss_pct` | 2 | 초과 시 그날 신규 진입·회전 중단 |
| `risk.drawdown_warn_pct / max_drawdown_pct` | 6 / 12 | 최고점 대비 손실: 알림 / 전부 닫고 멈춤 (0 = 끔) |
| `risk.isolated_topup_enabled / target_pct` | true / 15 | 격리 다리가 청산가 8% 이내면 여유 잔고로 15%까지 보충 |
| `risk.max_account_leverage` | 6 | 거래소별 명목가 / 자산 상한 |
| `risk.backstop_isolated_buffer_pct` | 2 | 격리 다리 거래소 손절: 청산가 2% 앞 |

---

## 7. 그 밖의 명령

```bash
hedgebot pairs                                   # 같은 종목 페어·묶음 비율 점검 (30/60/90일)
hedgebot rebalance                               # 지금 옮겨야 할 금액
hedgebot backtest --days 112 --interval 1h --price-proxy hl:io=hl:xyz          # config.yaml 기준
hedgebot backtest --days 112 --interval 1h --price-proxy hl:io=hl:xyz --sweep  # 회전 주기·크기·헤지 방식 비교
hedgebot run --mode paper                        # 키 없이 실시세 모의 거래
hedgebot resume                                  # 손실 한도 정지 해제 (봇을 멈춘 상태에서)
```

백테스트 옵션: `--half-spread hl:io=2.0:3.5`(정규장:장외 bp), `--passive-hedge-fill-rate 0.5`, `--seed N`.
`--interval 15m`은 최근 약 50일까지만 데이터가 있습니다.

다른 구조도 설정만 바꾸면 됩니다: Arcus 다리를 넣으면 예전 구조, 한 거래소의 서로 다른 두 종목은 `hedge_notional_ratio`로
(예: io:DRAM ⇄ io:SNDK, 0.8). 다만 서로 다른 종목 헤지는 두 종목 차이만큼 가격 위험이 생깁니다(백테스트 최대 손실 15~19%).

---

## 8. 확인한 것과 남은 것

- **실시세 모의 거래(2026-11-16)**: 두 묶음 모두 +1로 정해져 Entropy SNDK 롱 $9.58k / DRAM 숏 $11.97k / SKHX 롱 $5.58k / EWY 숏 $8.94k,
  trade.xyz는 정확히 반대로 약 1분 만에 쌓였습니다. 거래소별 명목가 $36.2k, 두 거래소 잔고 차이 $15 이내. 8개 다리 모두 거래소 손절이 걸림.
- 테스트 130개 통과 (묶음 방향 결정·전환·재시작, 같은 거래소 두 종목 페어, 손실 한도 정지, 백테스트 포함).
- 이전에 확인된 것: Hyperliquid 주문·레버리지·트리거 손절·격리증거금 서명, HIP-3 종목 매핑.

아직 실전에서 확인하지 못한 것: Entropy·trade.xyz에서 실제 체결, 격리증거금 실제 추가, Entropy 메이커 체결 속도(특히 EWY).
첫 실전의 `hedgebot selftest --mode live --fill`이 거래소마다 최소 금액으로 확인합니다. 실전 첫 회전(약 3일 뒤)도 `journalctl -u hedgebot`으로 지켜보세요.

---

## 9. 구조

```
hedgebot/
  venues/hyperliquid.py  HIP-3 dex별 계정 (공식 SDK + API 지갑)
  venues/paper.py        실시세 기반 모의 체결 (수수료·펀딩·증거금·청산)
  venues/arcus.py        Arcus REST (예전 구조용)
  executor.py            페어 상태 머신: BUILD → HOLD → UNWIND. 메이커 클립 + 헤지 + 불균형 수리
  risk.py                증거금 비율, 격리 청산 거리·자동 보충, 일일 손실, 최고점 대비 손실 한도
  backstop.py            거래소에 걸어 두는 손절 주문
  strategy.py            방향·회전·회전 시간대·사이징 규칙 (실거래와 백테스트가 공유)
  pairstats.py           상관·변동성 비율 (hedgebot pairs)
  rebalance.py           거래소 간 이체 금액 계산
  engine.py              메인 루프, 시작·종료, 묶음 방향(direction_group), 잔고 비례 크기, 손실 한도 정지, 비상 청산
  backtest/              과거 캔들·펀딩 재생 (다른 거래소 같은 종목으로 상장 전 기간 채우기)
  sizing.py, selftest.py, scanner.py, report.py, journal.py, cli.py
deploy/                  install.sh (VPS 설치), hedgebot.service (systemd)
tests/                   130개
```

참고: [Entropy 시장 데이터 (loris.tools)](https://loris.tools/hip3/io), [DefiLlama Entropy](https://defillama.com/rwa/perps/venue/entropy),
[airdropalert Entropy (포인트 미확정)](https://airdropalert.com/airdrops/entropy/).
