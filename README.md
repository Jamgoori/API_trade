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
- **봇이 멈추거나 오류가 날 때**: 메이커 주문을 취소하고 이미 체결된 만큼은 헤지한 뒤 종료합니다. 포지션과 거래소 손절은 그대로 둡니다.
  한 페어에서 예상 못 한 오류가 나도 그 페어만 멈추고 나머지·리스크 관리·손절은 계속 돕니다. 시작할 때 거래소가 안 되면 기다렸다 다시 시도합니다.
- **헤지 거래소 장애**: Hyperliquid가 20초 넘게 오류를 내면 체결된 Arcus 쪽을 되돌려 한쪽만 남기지 않습니다.
- **Arcus 데드맨 스위치는 기본으로 끔**: Arcus는 스위치가 발동하면 서브계정의 "모든" 주문을 지우는데, 여기에 Arcus 손절도 포함됩니다
  (테스트넷에서 확인). 그래서 손절을 지키는 쪽을 택했습니다. 대신 VPS가 통째로 죽으면 걸려 있던 메이커 주문 하나(최대 $2,000×배수)가 남을 수 있고,
  다시 켜면 봇이 그 체결분을 맞춥니다. `arcus.dead_mans_switch_s`로 켤 수 있습니다.
- **장외시간 규칙 반영**: Arcus 장외 초기증거금 상향(신규 진입 레버리지 한도), 가격 밴드, 04:00·20:00 ET 경계 전후 3분 진입 금지
- **일일 비용 예산**($20×배수) 초과 시 회전 중단, **일일 손실 2%** 초과 시 신규 진입 중단(헤지 포지션은 유지).
  입금·출금·거래소 간 이체는 손익으로 치지 않습니다.
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
그래서 실전 처음 며칠은 `hedgebot report --hours 24`로 실제 체결률·비용·헤지 지연을 확인하세요.

---

## 3. 실전 시작 (VPS에서 24시간)

봇은 계속 켜져 있어야 하므로 VPS(Ubuntu 22.04/24.04, 1 vCPU·1GB RAM이면 충분)에서 돌립니다.
이 클라우드 세션은 계속 켜져 있지 않습니다.

```bash
# 1) 설치: 가상환경 + config.yaml/.env 생성 + systemd 서비스 등록
git clone -b claude/kind-knuth-nt3une https://github.com/Jamgoori/API_trade && cd API_trade
bash deploy/install.sh

# 2) 키 입력 (아래 5장). 채팅이나 다른 곳에 붙여넣지 마세요
nano .env

# 3) 키·종목·잔고 확인
.venv/bin/hedgebot check --mode live

# 4) Hyperliquid 계정이 Standard 모드인지 확인 (통합 계정이면 봇이 시작을 거부합니다)
.venv/bin/hedgebot hl-mode
#    "unifiedAccount" 등이면: 웹에서 바꾸거나 아래 명령 후, trade.xyz·Entropy 잔고를 각각 옮기세요
#    .venv/bin/hedgebot hl-mode --set-standard --i-understand-live

# 5) 주문 기능 점검: 거래소마다 최소 금액(약 $12)으로 실제 주문 걸기 → 거래소 기록과 대조 → 취소,
#    --fill 이면 최소 수량 진입 → 거래소 손절 걸기/취소 → (Entropy) 격리증거금 $1 추가 → 청산까지
.venv/bin/hedgebot selftest --mode live --fill --i-understand-live

# 6) 24시간 실행 (재부팅·오류 시 자동 재시작)
sudo systemctl start hedgebot
journalctl -u hedgebot -f              # 실시간 로그 (Ctrl+C 로 빠져나와도 봇은 계속 돎)
```

| 할 일 | 명령 |
|---|---|
| 상태 보기 | `.venv/bin/hedgebot status`, `.venv/bin/hedgebot report --hours 24` |
| 멈추기 | `sudo systemctl stop hedgebot` (메이커 주문 취소, 포지션과 거래소 손절은 그대로) |
| 다시 켜기 | `sudo systemctl start hedgebot` (실제 포지션을 읽어 이어서 진행) |
| 업데이트 | `git pull && .venv/bin/pip install -e . && sudo systemctl restart hedgebot` |
| 리밸런싱 금액 | `.venv/bin/hedgebot rebalance` |
| 비상 전체 청산 | `sudo systemctl stop hedgebot && .venv/bin/hedgebot flatten --mode live --confirm --i-understand-live` |

`selftest`와 `flatten`은 봇이 돌고 있으면 실행을 거부합니다(먼저 `sudo systemctl stop hedgebot`).
설치 스크립트가 만드는 `config.yaml`은 `mode: live`입니다. 모의 거래(`--mode paper`)는 별도 저널(`data/journal-paper.sqlite`)을 써서 실전 상태와 섞이지 않습니다.

### 자금을 더 넣을 때 (설정 수정 필요 없음)

`sizing.scale_with_equity: true`(기본)면 모든 페어 크기가 **실제 잔고 합계 ÷ `capital` 합계** 배수로 자동 조정됩니다.

- `capital`은 1배 기준값입니다(기본 $18k). 잔고가 $36k가 되면 모든 포지션 목표가 2배가 됩니다.
- **입금**: 늘어난 잔고가 30분 유지되면 크기를 키웁니다. 텔레그램으로 "position size x1.00 -> x2.00"이 옵니다.
- **비율대로 나눠 넣으세요**: 한 거래소에만 넣으면 돈이 적은 쪽 한도까지만 커집니다. 봇이 3일마다 알려 주는 리밸런싱 금액대로 옮기면 됩니다(`hedgebot rebalance`).
- **거래소 간 이체 중**에는 잔고 합계가 잠깐 줄어 보입니다. 감소는 6시간 넘게 이어질 때만 반영되므로 이체 때문에 포지션을 줄이지 않습니다.
  일일 손실 한도(2%)가 이체 때문에 걸려도, 돈이 도착해서 잔고가 회복되면 자동으로 풀립니다.
- **출금**: 줄어든 잔고가 6시간 유지되면 크기를 줄입니다.
- 클립 크기($600~2,000)는 그대로라서 크기가 커지면 다 쌓는 데 시간이 더 걸립니다. 하루 비용 예산은 배수만큼 같이 늘어납니다.
- 배수 범위는 0.25~10배(`sizing.min_scale / max_scale`)입니다. 3배 이상 키울 때는 `hedgebot scan`으로 Entropy 종목의 거래량과 OI를 먼저 보세요.

## 4. 그 밖의 사용법

```bash
hedgebot scan                          # Arcus RWA ↔ trade.xyz / Entropy 공통 종목, 펀딩비, 스프레드, 레버리지 한도
hedgebot backtest --days 45            # config.yaml 기준 (백테스트는 고정 크기)
hedgebot backtest --days 45 --sweep    # 회전 주기·규모·헤지 방식 비교
hedgebot run --mode paper              # 키 없이 실시세 모의 거래
hedgebot rebalance --apply             # paper 모드에서만: 모의 잔고에 이체 반영
```
백테스트 옵션: `--half-spread arcus=0.3:0.5,hl:xyz=0.3:0.5,hl:io=0.8:1.2`, `--passive-hedge-fill-rate 0.5`,
`--points-per-musd 33`, `--no-transfers`, `--rebalance-every-h 72`.
`paper.maker_fill: through`(기본)는 가격이 내 호가를 **뚫고 지나가야** 체결로 보는 보수적인 설정입니다.

테스트넷: Entropy(io)는 Hyperliquid 테스트넷에 없고, Arcus 테스트넷은 호가 간격이 수 %라서 전략 테스트에는 맞지 않습니다.
주문 기능 확인은 위 `selftest`(실전, 최소 금액)로 합니다.

---

## 5. 키 설정 (.env)

| 변수 | 내용 | 권한 |
|---|---|---|
| `ARCUS_API_PRIVKEY` | app.arcus.xyz/api-keys 에서 Generate 한 **API Signing Key** (64자리 hex) | 거래 전용, 출금 불가 |
| `ARCUS_ETH_ADDRESS` | 그 키를 등록한 지갑 주소 | |
| `HL_ACCOUNT_ADDRESS` | Hyperliquid **메인 지갑 주소** (잔고가 있는 주소) | |
| `HL_AGENT_PRIVKEY` | app.hyperliquid.xyz/API 에서 Generate 한 **API 지갑 개인키** | 거래 전용, 출금 불가 |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | 알림 (강력 권장: 경고·리밸런싱·크기 변경을 받는 유일한 통로) | |

- 키는 `.env`에만 두세요(설치 스크립트가 본인만 읽을 수 있게 권한 600으로 만듭니다). `.env`는 git에 올라가지 않습니다.
- **키에는 만료일이 있습니다.** Arcus API 키는 최대 180일, Hyperliquid API 지갑도 유효기간을 정해 발급합니다.
  발급할 때 가장 길게 잡고 만료 전에 새로 발급해 `.env`를 바꾼 뒤 `sudo systemctl restart hedgebot` 하세요. 만료되면 주문이 거부되고 오류 알림이 옵니다.
- **봇 전용 계정을 쓰세요.** 봇은 시작할 때 그 계정의 미체결 주문을 모두 취소하고, 봇이 모르는 종목의 포지션은 알림만 보냅니다.
  Arcus는 API 키 발급 화면에서 Subaccount #를 고르고 `arcus.account_index`를 같게 맞추세요.
  Hyperliquid에서 직접 거래도 한다면 봇 전용 지갑(새 주소)을 따로 만들어 쓰세요(봇은 HL 서브계정 거래를 지원하지 않습니다).
- HL API 지갑은 거래소(dex) 간 자금 이동을 못 합니다. 이체는 웹에서 직접 하세요.
- **Hyperliquid 계정은 Standard(수동) 모드여야 합니다.** 통합 계정(unified)·포트폴리오 마진에서는 trade.xyz·Entropy 잔고가 따로 보이지 않아
  크기 조정·증거금 보충·손실 한도가 틀린 숫자로 돌기 때문에, 봇이 시작을 거부합니다. HL도 자동매매에는 Standard를 권장합니다.

---

## 6. 주요 설정 (`config.yaml`)

| 키 | 기본값 | 설명 |
|---|---|---|
| `capital` | arcus 8000 / hl:xyz 2000 / hl:io 8000 | 크기의 1배 기준 + 리밸런싱 비율 (paper에서는 모의 잔고). RWA 약 22% / io 약 78% |
| `sizing.scale_with_equity` | true | 실제 잔고 합계 ÷ `capital` 합계만큼 모든 페어 크기를 자동 조정 (늘 때 30분, 줄 때 6시간 유지 확인, 0.25~10배) |
| `arcus.dead_mans_switch_s` | 0 (끔) | 켜면(5~300초) 봇이 멈췄을 때 Arcus 주문이 모두 취소되는데, Arcus 손절도 같이 사라집니다 |
| `pairs[].notional_usd`, `leverage`, `direction` | | 페어별 명목가, 레버리지, `auto` / `long_maker` / `short_maker` |
| `pairs[].rotate`, `min_hold_h`, `max_hold_h`, `rotation_fraction_min/max` | io: 24~48h, 30~60% | 페어별 회전 설정. `rotate: false`면 보유만 |
| `strategy.min_hold_h / max_hold_h` | 6 / 12 | 회전 간격. 짧을수록 포인트↑ 비용↑ |
| `strategy.rotate_when` | off_hours | Arcus 장외시간(20:00~04:00 ET, 주말)에만 회전 |
| `strategy.max_daily_cost_usd` | 20 | 하루 수수료+슬리피지 한도 (크기 배수만큼 같이 늘어남) |
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

## 7. 실제 서버에서 확인한 것과 남은 것

버리는 지갑(잔고 없음)으로 실제 거래소에 서명된 요청을 보내 확인했습니다(2026-10-25).

| 항목 | 결과 |
|---|---|
| Arcus 테스트넷: API 키 등록, 주문(서명·가격·수량 변환), 거래소 기록 대조, 취소, 손절(positionTpsl), 레버리지, 전체 취소, 계정·미체결·체결 조회 | 통과 |
| Arcus 데드맨 스위치 | 15초 뒤 미체결 주문 취소 확인. **손절도 같이 취소됨** → 기본 끔 |
| Hyperliquid 메인넷·테스트넷: 주문·레버리지·트리거 손절·격리증거금 서명 (서버가 복원한 서명자 = 지갑 주소) | 통과 |
| HIP-3 종목 매핑 (xyz:NVDA → 110002, io:SNDK → 200002) | 공개 메타데이터와 일치 |

아직 실제로 확인하지 못한 것 (잔고가 있어야 함): 실제 체결, 체결 후 포지션·청산가 읽기, Entropy 격리증거금 추가.
처음 실전에서 `hedgebot selftest --mode live --fill`이 바로 이 부분을 최소 금액으로 확인합니다.

그 밖에:

1. **Arcus 퍼프 접근 권한**: 화이트리스트에 없는 주소는 API가 `address not on access whitelist`를 반환합니다. `hedgebot check`로 확인하세요.
2. **HIP-3 수수료**: trade.xyz·Entropy 종목 대부분이 Growth mode(수수료 90% 할인)라는 공개 정보로 계산했습니다. 실거래에서는 10분마다 실제 체결 수수료를 받아 저널에 기록합니다(`report`의 reconciled fees).
3. **포인트**: 공식 계산식은 비공개입니다. 리포트의 포인트 추정치는 `points.arcus_points_per_musd` 가정입니다. 매주 수요일 실제 배분과 비교해 보정하세요(`hedgebot report --points-per-musd X`).
4. **교차증거금 손절 위치**: 모든 종목이 같이 움직인다고 가정해 잡습니다(청산 쪽으로 보수적). 한 종목만 크게 움직이면(SNDK 하루 ±13% 등) 봇의 축소보다 먼저 그 종목의 Arcus 손절이 나갈 수 있고, 봇이 반대쪽을 정리한 뒤 6시간 쉬고 다시 쌓습니다.

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
  engine.py              메인 루프, 시작·종료, 잔고 비례 크기, 스냅샷, 수수료 대조, 비상 청산
  sizing.py              입금·출금에 맞춘 크기 배수 (늘 때 30분, 줄 때 6시간 유지 확인)
  selftest.py            실전 최소 금액 주문 점검
  backtest/              과거 캔들·펀딩 재생 시뮬레이터
  scanner.py, report.py, journal.py, cli.py
deploy/                  install.sh (VPS 설치), hedgebot.service (systemd)
tests/                   99개 (서명, 체결, 헤지 실패·장애, 불균형, 회전, 리스크, 거래소 손절, 크기 배수, 엔진, 백테스트)
```
