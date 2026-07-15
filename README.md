# Trading Bot

바이낸스 USDT 무기한 선물을 중심으로 동작하는 Python 자동매매 봇입니다.
현재 실운영 핵심은 **UTBreak Set64(EV Adaptive)**, **RSPT-v3**, **QH-Flow v2**, **Funding-OI Crowding Unwind**, **M-TREND**, 두 전략을 묶는 **Dual**, 세 전략을 결합하는 **Triple**, 다섯 전략을 결합하는 **5-Strategy Alpha**입니다.

> **주의**
> 이 저장소는 실주문·레버리지·TP/SL·자동 배포 코드를 포함합니다. 메인넷 사용 전 반드시 테스트넷과 paper/forward test로 주문 수량, 최소 주문금액, 포지션 모드, 보호 주문, 텔레그램 제어를 확인하세요. API Key, Secret, Telegram Token, SSH Key는 절대 저장소에 커밋하지 마세요.

## 현재 운영 기준

| 항목 | 현재 코드 기준 |
|---|---|
| 공식 실행 진입점 | `scripts/launch_emas.py` |
| 기본 거래소 모드 | `binance_testnet` |
| 지원 모드 | Binance Testnet, Binance Mainnet, Upbit |
| 핵심 런타임 프로필 | `ev_adaptive_v3_profit_engine` |
| UTBreak 실거래 Set | **Set64만 허용** |
| 기존 Set1~63 | 연구·진단용 레거시 Set |
| UTBreak 시간프레임 | AUTO `15m / 30m / 1h`, 진입·청산 `15m`, HTF `1h` |
| RSPT-v3 시간프레임 | 신호 `4h`, 장기 추세 `1d` |
| QH-Flow v2 시간구조 | 매시각 `00/15/30/45분` 첫 10초, 신호 유효 120초 |
| M-TREND 시간프레임 | 완료된 `15m / 1h / 4h` Donchian 돌파 + `4h` EMA 추세 |
| 공통 유동성 보호 | 상위 20호가 기반 L2 `CALM/MIXED/STRESSED` Gate |
| 포지션 제한 | 전체 봇 기준 동시 포지션 1개 |
| 배포 | `main` push → 전체 테스트 → Azure 자동 배포 |

`Set64`는 여러 고정 필터를 중첩한 과거 Set의 연장선이 아니라, **EV Adaptive 라우터를 안정적으로 식별하기 위한 단일 실거래 껍데기**입니다. 실제 진입 여부는 장세, 방향, 거래비용, 품질, 기대값과 위험 배율을 종합해 결정합니다.

## 전략 구조

### 1. UTBreak Set64 — EV Adaptive

UTBot 신호를 그대로 주문하지 않고 다음 단계를 통과한 후보만 진입 계획으로 만듭니다.

```text
CoinSelector 후보
    ↓
15m / 30m / 1h 추세·돌파 판단
    ↓
장세·방향·재가속·신호 노후도 확인
    ↓
거래량·ADX·상대강도·파생시장·시장 품질 확인
    ↓
수수료·슬리피지·펀딩·스프레드 차감 기대값
    ↓
위험 배율·손절·익절·추적청산 계획
    ↓
공통 주문 게이트웨이
```

주요 특징:

- 라이브 AUTO는 Set64만 사용하며 Set1~63 자동 전환을 차단합니다.
- `15m`, `30m`, `1h` 중 적합한 시간프레임을 선택하되 잦은 전환을 제한합니다.
- 추세 지속, 압축 후 돌파, 재가속 여부를 구분하고 불리한 횡보·극단 변동 구간은 차단하거나 수량을 줄입니다.
- 수수료, 예상 슬리피지, 펀딩 버퍼, 스프레드를 기대값에 포함합니다.
- OI, 펀딩, 베이시스, 롱·숏 비율, 오더플로는 독립적인 확정 신호라기보다 품질·과열·위험 판단에 사용합니다.
- 약한 신호를 최소 주문수량으로 억지 복원하지 않고 최종 위험 배율이 낮으면 거래하지 않습니다.

### 2. RSPT-v3 — Residual Relative Strength Pullback Trend

RSPT-v3는 UTBreak 방향을 전달받지 않고 **자체적으로 롱·숏을 판단하는 독립 전략**입니다.

```text
유동성 후보군 구성
    ↓
BTC·ETH 공통 움직임 제거
    ↓
잔차 상대강도 순위 계산
    ↓
1d 장기 추세 + 4h 방향 확인
    ↓
선행 충격/돌파 → 정상 눌림 → 재돌파 확인
    ↓
추격 진입·과도한 변동성·부적절한 손절거리 차단
```

현재 기본 설계:

- BTC·ETH 수익률에 대한 베타를 제거한 잔차 모멘텀을 사용합니다.
- 최대 30개 유동성 후보군에서 롱 상위 20%, 숏 하위 10%를 기본 진입 구간으로 사용합니다.
- 선행 돌파가 없는 단순 EMA 접촉은 눌림목으로 인정하지 않습니다.
- 최근 2~8개 봉 안의 충격, `0.4~1.2 ATR` 눌림, 재돌파를 확인합니다.
- 신호 종가보다 실제 가격이 불리하게 `0.35 ATR` 이상 이동하면 추격 진입을 차단합니다.
- 변동성이 높으면 기본 위험을 70%, 극단적이면 35%로 축소합니다.
- 구조적 손절거리가 `0.6~2.0 ATR` 범위를 벗어나면 거래하지 않습니다.
- 세부 내용은 [`docs/RSPT_V2.md`](docs/RSPT_V2.md)를 참고하세요.

### 3. QH-Flow v2 — Quarter-Hour Order Flow

QH-Flow v2는 매시각 `00분`, `15분`, `30분`, `45분` 직후 첫 10초의 Binance Futures aggregate trades와 상위 20호가를 실시간으로 분석하는 독립 전략입니다.

```text
15분 경계 도착
    ↓
첫 10초 Taker 매수·매도 체결 집계
    ↓
최근 8개 경계 구간 대비 imbalance·거래대금·거래 건수 이상치 계산
    ↓
가격 움직임 유지 여부 + 펀딩·베이시스·롱숏 과열 확인
    ↓
L2 CALM/MIXED/STRESSED Gate
    ↓
15분 ATR 기반 손절·익절·시간청산 계획
```

현재 기본 설계:

- Taker imbalance 절대값, z-score, 거래대금 배율, 거래 건수 배율을 모두 확인합니다.
- 첫 10초 방향과 가격 변화가 반대이면 신호를 거부합니다.
- `CALM`은 정상 위험, `MIXED`는 축소 위험, `STRESSED`는 신규 진입 차단입니다.
- 양의 펀딩·베이시스·롱 과밀은 롱 위험을 줄이고, 음의 과밀은 숏 위험을 줄입니다.
- 별도 장기 데이터 수집기나 백테스트 DB 없이 실행 시점에 최근 경계 구간을 REST로 비교합니다.
- 단독 전략 선택은 `/utbreak qh on`입니다.

### 4. Dual — 독립 전략 합의 라우팅

Dual은 UTBreak와 RSPT-v3를 각각 계산한 뒤 결과만 결합합니다.

| UTBreak | RSPT-v3 | 처리 |
|---|---|---|
| LONG | LONG | 정상 위험 100% |
| SHORT | SHORT | 정상 위험 100% |
| 신호 있음 | 상대 전략 신호 없음 | 정상 위험의 60% |
| LONG | SHORT | 거래 차단 |
| SHORT | LONG | 거래 차단 |

따라서 Dual은 같은 UT 방향을 두 번 확인하는 구조가 아니라, **돌파 기반 전략과 잔차 상대강도 눌림목 전략의 합의 여부**를 확인합니다.

### 5. Triple — UTBreak + RSPT-v3 + QH-Flow v2

Triple은 세 전략을 각각 독립적으로 계산하고, 방향이 충돌하면 거래하지 않습니다.

| 유효한 동일 방향 신호 | 위험 배율 |
|---|---:|
| 3개 | 100% |
| 2개 | 85% |
| 1개 | 55% |
| LONG·SHORT 혼재 | 거래 차단 |

Triple 내부에서는 UTBreak와 RSPT의 QH 확인을 잠시 끄고 QH-Flow v2를 별도 세 번째 투표로 계산하므로, 같은 정보를 중복 계산하지 않습니다. 최종 주문은 점수가 가장 높은 전략의 기존 TP/SL 계획을 선택한 뒤 합의 개수에 따라 수량과 위험금액만 축소합니다.

### 6. M-TREND — Multi-Timeframe Volatility-Adjusted Trend

M-TREND는 완료된 `15m`, `1h`, `4h` 봉에서 새 Donchian 돌파를 찾고 `4h EMA20/EMA50` 추세와 같은 방향일 때만 후보를 만듭니다. 세 시간프레임이 모두 일치해야 하는 AND 구조가 아니며, 한 시간프레임의 신선한 돌파만 있어도 45% 위험으로 단독 진입할 수 있습니다. 두 시간프레임은 75%, 세 시간프레임은 100% 위험을 사용합니다. L2·시장 품질·데이터·주문·단일 포지션 보호는 기존 공통 경로를 그대로 사용합니다.

단독 선택은 `/utbreak mtrend on`, 상태 확인은 `/utbreak mtrend status`입니다.

### 7. 5-Strategy Alpha — UTBreak + RSPT-v3 + QH-Flow v2 + Crowding + M-TREND

기존 `QUAD_ALPHA` 호환 키를 유지하면서 다섯 전략을 각각 독립적으로 계산합니다. 한 전략만 유효해도 진입 후보가 되며, 다른 전략이 같은 방향이면 위험과 신뢰도를 올립니다. 반대 방향 신호가 섞이면 **방향 충돌로 신규 진입을 차단**합니다.

| 유효한 동일 방향 신호 | 위험 배율 |
|---|---:|
| 5개 | 100% |
| 4개 | 100% |
| 3개 | 90% |
| 2개 | 75% |
| 1개 | 45% |
| LONG·SHORT 혼재 | 거래 차단 |

최종 주문은 다섯 전략 중 점수가 가장 높은 기존 진입 계획을 사용하며, 진입가·손절가·익절가는 유지하고 수량과 위험금액만 합의 배율에 맞춰 줄입니다. 텔레그램의 `5-ALL ON/OFF/STATUS` 버튼으로 전체를 제어하며 각 단독 전략 버튼도 유지합니다.

### 8. 레거시 전략

`UTBOT`, `UTSMC`, `UTRSI`, `UTRSIBB`, `UTBB`, `RSIBB` 등의 코드 경로는 호환·연구 목적으로 남아 있습니다. 현재 README의 실운영 기준은 UTBreak Set64, RSPT-v3, Dual이며, 과거 Triple SMA/HMA 설명은 현재 핵심 런타임을 나타내지 않습니다.

## 위험관리와 주문 안전장치

전략 신호와 실제 주문은 분리되어 있으며, 모든 실주문은 공통 안전 계층을 통과합니다.

- **공식 런처 강제**: `emas.py` 직접 실행 대신 `scripts/launch_emas.py`를 사용합니다.
- **프로세스 락**: 동일 서버에서 봇 중복 실행을 차단합니다.
- **글로벌 단일 포지션**: 전략이 달라도 전체 봇에서 포지션 하나만 허용합니다.
- **주문 상태 저장**: 로컬 SQLite 상태와 거래소 주문 상태를 연결합니다.
- **멱등 보호 주문**: TP/SL 중복 생성과 재시작 후 중복 주문을 방지합니다.
- **시작·재접속 조정**: 포지션, 일반 주문, Algo 주문을 다시 조회하고 불완전하면 신규 진입을 막습니다.
- **User Data Stream**: 체결·부분체결·취소 이벤트를 추적하고 연결 복구 시 REST 조정을 수행합니다.
- **공통 L2 Gate**: UTBreak, RSPT-v3, QH-Flow v2, Crowding, M-TREND와 합산 전략 모두 상위 20호가의 스프레드·깊이·불균형을 확인합니다.
- **QH 확인**: UTBreak와 RSPT-v3는 15분 경계가 가까우면 첫 10초 주문흐름을 기다리고, 반대 QH 신호면 진입을 취소합니다.
- **청산가 보호**: 손절가가 청산가 안전 버퍼 안쪽에 들어가면 레버리지·주문 계획을 재검사합니다.
- **Critical Pause**: 주문·상태 불일치 등 위험 상황에서는 자동 진입을 잠그고 수동 재개 절차를 요구합니다.
- **최종 손익 정산**: 거래소 체결 수수료와 펀딩을 포함해 종료된 거래 통계를 확정합니다.
- **텔레그램 소유자 제한**: 설정된 `chat_id` 외 명령을 거부합니다.

## 프로젝트 구조

```text
.
├── emas.py                         # 메인 엔진, 텔레그램, 전략·주문 연결
├── scripts/launch_emas.py          # 유일한 공식 실행 진입점
├── global_single_position_guard.py # 단일 포지션·실운영 런타임 오버라이드
├── utbreakout_live_hardening_patch.py
├── utbreakout/                     # UTBreak, EV, 장세, 방향, RSPT 등 전략 모듈
├── trading_safety/                 # 주문 게이트웨이, 상태 저장, 조정, 보호 주문
├── prediction/                     # Prediction/Predict.fun 보조 모듈
├── scripts/                        # 운영, 백테스트, 리서치 도구
├── tests/                          # 전략·주문·안전·배포 회귀 테스트
├── tradingview/                    # TradingView 참고 스크립트
├── docs/                           # 전략 및 운영 문서
└── kstockbot/                      # 한국주식용 별도 하위 프로젝트
```

## 설치와 로컬 실행

Python 3.12 기준입니다.

```bash
python3.12 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python3 scripts/launch_emas.py
```

Windows PowerShell에서는 가상환경 활성화만 다음처럼 바꿉니다.

```powershell
.\venv\Scripts\Activate.ps1
```

### 설정 파일

첫 실행 시 `config.json`이 생성될 수 있으며 이 파일은 `.gitignore` 대상입니다. 메인넷 실행 전 다음 항목을 직접 확인하세요.

- `api.exchange_mode`
- Binance testnet/mainnet API Key와 Secret
- `telegram.token`, `telegram.chat_id`
- 포지션 모드와 레버리지
- UTBreak/RSPT/Dual 활성 상태
- 1회 위험, 일일 손실, 일일 거래 횟수 제한
- TP/SL와 보호 주문 설정

`runtime/`, `config.json`, 로그, DB, Prediction 원장은 저장소에 커밋하지 않습니다.

> `python emas.py`로 직접 실행하지 마세요. 공식 런처가 프로세스 락, 단일 포지션 가드, 실운영 하드닝 패치를 먼저 적용합니다.

## 서버 운영

```bash
# 시작
scripts/bot_ctl.sh start

# 상태 및 heartbeat 확인
scripts/bot_ctl.sh status

# 비정상 상태면 재시작
scripts/bot_ctl.sh ensure

# 중지
scripts/bot_ctl.sh stop
```

기본 heartbeat 파일은 `runtime/bot_heartbeat.json`이며, 서버 배포에서는 5분마다 `ensure`를 실행하고 재부팅 시 자동 시작하도록 cron을 등록합니다.

## 텔레그램 명령

주요 명령은 다음과 같습니다.

| 명령 | 기능 |
|---|---|
| `/start` | 메인 메뉴 |
| `/status` | 현재 엔진·포지션·전략 상태 |
| `/history` | 최근 상태·거래 기록 |
| `/stats` | 거래 통계 |
| `/risk` | 위험 설정 메뉴 |
| `/strat` | 전략 선택 메뉴 |
| `/utbreak` | UTBreak/Set64/RSPT/QH-Flow v2/Crowding/M-TREND/Dual/Triple/5-Strategy 메뉴 |
| `/prediction` | Prediction Micro Auto / Predict.fun 메뉴 |
| `/setup` | 거래소·네트워크 전환만 지원 |
| `/log` | 최근 로그 |
| `/close` | 현재 포지션 긴급 청산 |
| `/stop` | 봇 긴급 정지 및 포지션 청산 |
| `/help` | 명령 도움말 |

UTBreak 진단에서 자주 사용하는 하위 명령:

```text
/utbreak status
/utbreak why
/utbreak config
/utbreak configdiff
/utbreak trace [SYMBOL]
/utbreak tracefull [SYMBOL]
/utbreak research
/utbreak qh on|off|status
/utbreak crowding on|off|status
/utbreak mtrend on|off|status
/utbreak dual on|off|status
/utbreak triple on|off|status
/utbreak quad on|off|status
/utbreak bridge on|off
/utbreak watchdog on|off
```

모든 실전 포지션의 주전략·동의전략·수수료·펀딩·R·TP1/TP2/runner/SL 체결 기여를 한 포지션당 한 번만 기록합니다. 매월 1일 오전 9시(KST)에 전월 리포트를 UTF-8 TXT 파일로 같은 텔레그램 채팅에 보내며, 거래가 0건이어도 발송하고 재시작 후 중복 발송을 막습니다.

텔레그램 버튼과 텍스트 명령으로 `STOP`, `PAUSE`, `RESUME`도 처리합니다. `/setup`의 오래된 전략 설정 메뉴는 제거되었으며 현재는 거래소·네트워크 전환만 담당합니다.

## 백테스트와 리서치

```bash
python scripts/utbreakout_backtest.py --help
python scripts/utbreakout_research_report.py --help
```

`--train-months`, `--test-months`는 선택한 봉 간격에 맞춰 캔들 수로 변환됩니다.

- `15m` 한 달: 약 2,880개
- `1h` 한 달: 약 720개
- `4h` 한 달: 약 180개

백테스트에서는 수수료, 슬리피지, 펀딩, 스프레드와 표본외 구간을 동일 조건으로 비교해야 합니다. 최종 holdout 구간을 파라미터 조정에 사용하지 마세요.

관련 문서:

- [`docs/RSPT_V2.md`](docs/RSPT_V2.md)
- [`docs/QH_FLOW.md`](docs/QH_FLOW.md)
- [`docs/utbreakout_research_workflow.md`](docs/utbreakout_research_workflow.md)
- [`docs/codex_ops_notes.md`](docs/codex_ops_notes.md)

## 테스트

```bash
python -m py_compile \
  emas.py \
  scripts/launch_emas.py \
  scripts/utbreakout_backtest.py \
  scripts/utbreakout_research_report.py

python -m pytest -q
```

전략이나 주문 코드를 수정할 때는 전체 테스트 외에도 해당 전략, 주문 게이트웨이, TP/SL, 조정, 단일 포지션 테스트를 함께 확인해야 합니다.

## GitHub Actions와 Azure 배포

`main` 브랜치에 push하면 `.github/workflows/deploy.yml`이 실행됩니다.

1. Python 3.12 설정
2. 주요 파일 `py_compile`
3. 의존성 설치
4. 전체 `pytest`
5. Azure 서버에서 `main` 강제 동기화
6. 기존 `config.json` 백업·복원
7. Telegram Token 검증
8. 기존 프로세스 종료 후 공식 런처로 재시작
9. heartbeat·프로세스·UTBreak 상태 계약 확인
10. 중복 Telegram poller 확인
11. 재부팅 및 5분 health-check cron 등록

테스트가 실패하면 Azure 배포 단계로 진행하지 않습니다.

## 보안

- API Key에는 출금 권한을 부여하지 마세요.
- 가능하면 IP 화이트리스트를 사용하세요.
- Testnet과 Mainnet 키를 분리하세요.
- Telegram `chat_id`를 반드시 고정하세요.
- `config.json`, `runtime/`, 로그, DB를 외부에 공유하지 마세요.
- 실거래 설정 변경 후에는 포지션과 TP/SL 주문을 읽기 전용으로 다시 확인하세요.

## 면책

이 프로젝트는 개인 연구·자동화 목적의 소프트웨어입니다. 어떤 전략도 수익을 보장하지 않으며, 레버리지 선물 거래에서는 원금 전액을 잃을 수 있습니다. 실제 주문 여부와 손실 책임은 운영자에게 있습니다.
