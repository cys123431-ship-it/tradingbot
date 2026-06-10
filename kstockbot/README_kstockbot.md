# KStockBot V2 (MVP)

A secure, low-frequency automated trading bot for Korean Stocks using KIS Open API and TradingView Webhooks.

## Architecture & Safety
This bot focuses heavily on **Risk Management and Safety**:
- **Default Mode**: `analysis` (Read-only, Webhook logging only)
- **Paper Mode**: `paper` (Mock trading or internal ledger only)
- **Live Mode**: `manual_live` (Webhook -> Telegram Approval), `live` (Fully automated, but strictly disabled by default)

Market orders are strictly forbidden in this MVP. High-frequency loops are avoided in favor of async webhook processing and low-frequency APScheduler tasks.

## 1. Setup
1. Duplicate `.env.example` to `.env`
2. Fill in the credentials:
   - `KSTOCK_KIS_APP_KEY`, `KSTOCK_KIS_APP_SECRET` from KIS Developers portal.
   - `KSTOCK_KIS_ACCOUNT_NO` (계좌번호 앞 8자리)
   - `KSTOCK_KIS_ACCOUNT_PRODUCT_CODE` (보통 01)
   - `KSTOCK_TELEGRAM_BOT_TOKEN`, `KSTOCK_TELEGRAM_OWNER_ID`
   - `KSTOCK_MAX_SCAN_SYMBOLS` (Optional, default 20)

주의: `KSTOCK_TELEGRAM_OWNER_ID`는 텔레그램 사용자 숫자 ID여야 한다. 봇 토큰이나 사용자명(@username)이 아니다.
3. Set `KSTOCK_MODE` to one of:

```env
KSTOCK_MODE=analysis
KSTOCK_BROKER=KIS
KSTOCK_KIS_APP_KEY=your_app_key
KSTOCK_KIS_APP_SECRET=your_app_secret
KSTOCK_KIS_ACCOUNT_NO=12345678
KSTOCK_KIS_ACCOUNT_PRODUCT_CODE=01
KSTOCK_KIS_IS_MOCK=true
KSTOCK_TELEGRAM_BOT_TOKEN=your_bot_token
KSTOCK_TELEGRAM_OWNER_ID=your_chat_id
KSTOCK_WEBHOOK_SECRET=your_secret_string
KSTOCK_HOST=0.0.0.0
KSTOCK_PORT=8090
KSTOCK_TIMEZONE=Asia/Seoul
KSTOCK_CONFIRM_LIVE_TRADING=no
```

## 2. Running Locally (Testing)

테스트는 반드시 requirements 설치 후 실행한다.

```bash
cd kstockbot
python -m pip install -r requirements.txt
pytest -q
```

**권장 방법 (repository root에서 실행)**
```bash
cd <repo-root>
python3 -m venv kstockbot/.venv
source kstockbot/.venv/bin/activate
pip install -r kstockbot/requirements.txt

python -m uvicorn kstockbot.app.main:app --host 0.0.0.0 --port 8090
```

또는 kstockbot 내부에서 실행:
```bash
cd kstockbot
PYTHONPATH=.. python -m uvicorn kstockbot.app.main:app --host 0.0.0.0 --port 8090
```

## Test Requirements

테스트는 반드시 requirements 설치 후 실행한다. `APScheduler`, `python-telegram-bot`, `FastAPI`, `psutil` 등이 설치되지 않은 환경에서는 일부 테스트가 import 단계에서 실패할 수 있다.

```bash
cd kstockbot
python -m pip install -r requirements.txt
python -m py_compile app/*.py
pytest -q
```

부모 경로에서도 import를 확인한다.

```bash
cd ..
PYTHONPATH=. pytest -q kstockbot/tests
PYTHONPATH=. python -c "import kstockbot; import kstockbot.app.main; print('import ok')"
```

KIS 조회 API는 dry-run 단계에서 `rt_cd != "0"` 또는 `rt_cd` 누락을 실패로 처리한다. 따라서 `/price`, `/balance`, `/cash`, `/dryrun`에서 KIS 오류가 명확히 Telegram에 표시되어야 정상이다.

## 3. Deployment via GitHub Actions
Ensure the following Secrets are set in your GitHub repository:
- `AZURE_VM_HOST`
- `AZURE_VM_USER`
- `AZURE_VM_SSH_KEY`
- `KSTOCK_ENV_FILE` (Contains the full `.env` string securely)

Any push to `kstockbot/**` on the `main` branch will automatically deploy to `/home/azureuser/kstockbot` and restart the service via `scripts/kstock_ctl.sh`.

배포 workflow 파일은 repository root 기준 `.github/workflows/deploy-kstockbot.yml`에 있어야 한다. `kstockbot/.github/workflows/`에 두면 GitHub Actions가 실행하지 않는다.

배포 후 점검 명령:
```bash
./scripts/kstock_ctl.sh health
./scripts/doctor.sh
```

## Azure Dry Run

Azure VM에서 배포 후 아래 순서로 점검한다.

```bash
cd /home/azureuser/kstockbot
./scripts/doctor.sh
./scripts/kstock_ctl.sh status
./scripts/kstock_ctl.sh health
tail -80 logs/kstockbot.log
```

Telegram에서 아래 명령어를 순서대로 실행한다.

```text
/status
/mode
/kischeck
/price 005930
/balance
/cash 005930 80000
/watchlist
/risk
/scan
```

주의:

* `/kischeck`, `/price`, `/balance`, `/cash`는 주문을 실행하지 않는다.
* `analysis` 모드에서는 webhook을 받아도 주문하지 않는다.
* `paper` 모드로 바꾸기 전에는 반드시 위 점검을 완료한다.
* `live` 완전자동 주문은 MVP에서 거부된다.

## TradingView External Webhook Preparation

TradingView 외부 webhook을 실제로 붙이려면 내부 FastAPI 포트 `8090`을 직접 쓰지 말고, Nginx/HTTPS reverse proxy를 통해 `/webhook/tradingview`로 전달하는 구조를 사용한다.

권장 구조:

```text
TradingView
→ https://your-domain.com/webhook/tradingview
→ Nginx HTTPS reverse proxy
→ http://127.0.0.1:8090/webhook/tradingview
```

### TradingView Fast ACK Mode
TradingView는 Webhook 호출에 대해 3초 이내의 빠른 응답을 요구합니다. KStockBot은 이 제약을 준수하기 위해 Fast-ACK 모드를 제공합니다.

* **동작 원리**: `KSTOCK_WEBHOOK_FAST_ACK=true` (기본값) 일 때, Webhook 요청이 들어오면 우선 동기(sync)로 Webhook Secret의 유효성만 검증합니다.
  * Secret이 올바르지 않거나 미설정된 경우: 즉시 `HTTP 403 Forbidden`을 반환합니다.
  * Secret이 올바른 경우: 즉시 `HTTP 202 Accepted` ({"status":"accepted", "message":"Webhook validation passed, processing asynchronously"}) 를 반환하며, 실제 주문 로직(Telegram 알림, 리스크 체크, KIS 주문 전송 등)은 FastAPI의 `BackgroundTasks`를 통해 비동기로 실행합니다.
* **설정**: `.env`에 `KSTOCK_WEBHOOK_FAST_ACK=true` 또는 `false`로 설정할 수 있습니다.
* **확인**: `/health` 및 `/status` API 응답에서 `"webhook_fast_ack": true` 값을 확인하여 현재 활성화 상태를 검증할 수 있습니다.

### Fast ACK 결과 확인

Fast ACK 모드에서는 HTTP 응답이 먼저 `accepted`로 반환되고, 실제 처리 결과는 background task에서 기록된다.

확인 방법:

```text
/webhooks
/webhooks 10
```

또는 Azure VM에서:

```bash
cd /home/azureuser/kstockbot
./scripts/doctor.sh
tail -80 logs/kstockbot.log
```

`runtime/webhook_events.json`에는 최근 webhook 처리 요약만 저장된다. secret, app key, token, 계좌번호는 저장하지 않는다.

준비 파일:

```text
docs/TRADINGVIEW_WEBHOOK_AZURE.md
deploy/nginx/kstockbot.nginx.example
scripts/webhook_smoke.sh
```

내부 smoke test:

```bash
cd /home/azureuser/kstockbot
./scripts/webhook_smoke.sh
```

주의:

* `analysis` 모드에서는 webhook을 받아도 주문하지 않는다.
* `paper` 모드에서는 모의주문 또는 내부 paper ledger가 가능하다.
* `manual_live`는 approval 생성까지만 간다.
* `live` 완전자동 주문은 MVP에서 거부된다.

## 4. Telegram Commands
- `/start` : Start interaction
- `/status` : View daily buys, memory, mock state, and pending approvals
- `/mode` : View current operation mode and live trading guard
- `/watchlist` : View loaded symbols
- `/risk` : View active risk config
- `/scan` : Scan the watchlist (No trades made)
- `/stoptrading` : Emergency stop all new trades
- `/starttrading CONFIRM` : Resume trading
- `/approve <id>` : Approve manual order in `manual_live` mode
- `/reject <id>` : Reject manual order
- `/kischeck` : KIS token/account/mock 설정 점검, 주문 없음
- `/dryrun` : KIS token/price/balance/cash read-only 통합 점검, 주문 없음
- `/price <종목코드>` : 현재가 조회, 주문 없음
- `/balance` : KIS 잔고 요약 조회, 주문 없음
- `/cash [종목코드] [가격]` : 주문가능금액 조회, 주문 없음
- `/webhooks [n]` : 최근 TradingView webhook 처리 결과 확인, 주문 없음

## 5. Stopping the Existing Crypto Bot
If the old `emas.py` crypto bot is running on the same server, you must stop it manually to avoid memory/CPU conflicts.
```bash
cd /home/azureuser/tradingbot
PID_FILE="/home/azureuser/emas.pid" LOG_FILE="/home/azureuser/emas.log" STATE_DIR="/home/azureuser/tradingbot/runtime" ./scripts/bot_ctl.sh stop
crontab -l # Ensure no cron restarts it
```

## 6. TradingView Webhook JSON Example
```json
{
  "secret": "your_secret_string",
  "source": "tradingview",
  "strategy": "swing_v1",
  "action": "BUY",
  "symbol": "005930",
  "name": "Samsung Electronics",
  "price": 80000,
  "time": "2026-06-10T10:00:00Z",
  "timeframe": "1D"
}
```

## 7. Live Trading Policy - MVP에서는 완전자동 실전주문 봉인
현재 MVP에서는 `live` 완전자동 실전주문을 의도적으로 거부한다. 실전 테스트는 반드시 `manual_live` 모드에서 Telegram `/approve <id>` 수동 승인 방식으로만 진행한다. `live` 모드는 코드상 guard를 테스트하기 위해 존재하지만, TradingView webhook이 직접 실전 주문을 실행하지 않는다.

실전 전환 전 최소 조건:
1. `analysis` 모드에서 webhook 수신과 Telegram 알림 검증
2. `paper` 모드에서 KIS 모의투자 주문 2주 이상 검증
3. 주문 실패/미체결/취소/중복신호 처리 검증
4. `manual_live` 모드에서 1주 단위 수동승인 주문만 테스트
5. `live` 완전자동 주문은 MVP 이후 별도 승인 전까지 사용 금지

## Paper Trading Test Checklist
1. `.env` 세팅:
```text
KSTOCK_MODE=paper
KSTOCK_KIS_IS_MOCK=true
KSTOCK_WEBHOOK_SECRET=<강한 랜덤 문자열>
```

2. 서버 실행:
```bash
cd /home/azureuser
/home/azureuser/kstockbot/.venv/bin/python -m uvicorn kstockbot.app.main:app --host 0.0.0.0 --port 8090
```

3. 상태(Health) 확인:
```bash
curl http://127.0.0.1:8090/health
```

4. Telegram 작동 테스트:
- `/status`
- `/mode`
- `/watchlist`
- `/risk`
- `/scan`

5. TradingView Webhook 로컬 테스트:
```bash
curl -X POST http://127.0.0.1:8090/webhook/tradingview \
  -H "Content-Type: application/json" \
  -d '{"secret":"실제_SECRET","source":"tradingview","strategy":"test","action":"BUY","symbol":"005930","price":80000,"time":"2026-06-10T10:00:00+09:00","timeframe":"1D"}'
```

6. 기대 결과:
- `analysis` mode: 알림만 발송, 주문 없음
- `paper` mode: 리스크 통과 시 모의주문 전송 또는 내부 paper ledger 기록
- `manual_live` mode: approval 생성만 됨
- `live` mode: MVP 제약으로 인해 실행 명시적 거부

## KIS 모의투자 실연 전 체크리스트

1. `.env` 확인
   - `KSTOCK_MODE=analysis`로 먼저 시작
   - `KSTOCK_KIS_IS_MOCK=true`
   - `KSTOCK_WEBHOOK_SECRET`는 `change-me`가 아닌 강한 랜덤 문자열
   - KIS app key/secret/account 정보는 코드가 아니라 `.env`에만 저장

2. 서버 기동 확인
   - `curl http://127.0.0.1:8090/health`
   - `curl http://127.0.0.1:8090/status`

3. Telegram 확인
   - `/start`
   - `/status`
   - `/mode`
   - `/watchlist`
   - `/risk`
   - `/scan`

4. TradingView webhook 확인
   - analysis 모드에서는 알림만 오고 주문이 없어야 한다.
   - paper 모드에서만 KIS 모의주문 또는 내부 paper ledger가 가능하다.
   - manual_live에서는 approval만 생성된다.
   - live는 MVP에서 거부된다.

5. 실전 금지
   - 현재 MVP에서는 live 완전자동 실전주문을 의도적으로 봉인한다.
   - 실전 테스트는 나중에 manual_live + 1주 + Telegram 승인 방식으로만 진행한다.

KIS 연결 점검 순서:

0. `/dryrun`
   - KIS token, 현재가, 잔고, 주문가능금액을 한 번에 read-only로 확인한다.
   - 이 명령은 주문을 실행하지 않는다.

1. `/kischeck`
   - Token OK가 True인지 확인
   - Mock이 True인지 확인
   - App Credentials / Account Configured가 True인지 확인

2. `/price 005930`
   - 현재가 조회가 되는지 확인

3. `/balance`
   - 모의투자 계좌 잔고 응답이 오는지 확인

4. `/cash 005930 80000`
   - 주문가능금액 조회 응답이 오는지 확인

5. 위 네 가지가 모두 정상일 때만 `KSTOCK_MODE=paper`로 전환한다.

## GitHub Actions 배포 위치

배포 workflow는 `kstockbot/` 내부가 아니라 repository root의 아래 경로에 있어야 한다.

```text
.github/workflows/deploy-kstockbot.yml
```

`kstockbot/.github/workflows/`에 만들면 GitHub Actions가 인식하지 못한다.

## 수동 배포/재시작

GitHub Actions를 쓰기 전에는 Azure VM에서 아래처럼 수동 실행할 수 있다.

```bash
cd /home/azureuser
/home/azureuser/kstockbot/.venv/bin/python -m uvicorn kstockbot.app.main:app --host 0.0.0.0 --port 8090
```

또는 스크립트 사용:

```bash
cd /home/azureuser/kstockbot
./scripts/kstock_ctl.sh restart
./scripts/kstock_ctl.sh status
tail -80 logs/kstockbot.log
```

## Current Safety Status

현재 MVP는 다음 원칙을 따른다.

- `analysis`: webhook 수신과 Telegram 알림만 수행, 주문 없음
- `paper`: KIS 모의투자 또는 내부 paper ledger만 허용
- `manual_live`: Telegram approval 생성까지만 허용
- `live`: MVP에서는 TradingView webhook 직접 실전주문을 거부

시장가 주문은 금지되어 있으며, 초단위/틱단위 고빈도 매매 기능은 구현하지 않는다.

## Next Step: KIS Paper Trading Dry Run

KIS 모의투자 실연 순서:

1. `.env`를 아래처럼 설정한다.

```env
KSTOCK_MODE=analysis
KSTOCK_KIS_IS_MOCK=true
KSTOCK_WEBHOOK_SECRET=<강한 랜덤 문자열>
```

2. 서버를 실행한다.

```bash
cd /home/azureuser
/home/azureuser/kstockbot/.venv/bin/python -m uvicorn kstockbot.app.main:app --host 0.0.0.0 --port 8090
```

3. health/status를 확인한다.

```bash
curl http://127.0.0.1:8090/health
curl http://127.0.0.1:8090/status
```

4. Telegram에서 확인한다.

```text
/status
/mode
/watchlist
/risk
/scan
```

5. analysis 모드 webhook을 테스트한다.

```bash
curl -X POST http://127.0.0.1:8090/webhook/tradingview \
  -H "Content-Type: application/json" \
  -d '{"secret":"실제_SECRET","source":"tradingview","strategy":"test","action":"BUY","symbol":"005930","price":80000,"time":"2026-06-10T10:00:00+09:00","timeframe":"1D"}'
```

6. analysis 모드에서 주문이 없는 것을 확인한 뒤에만 `KSTOCK_MODE=paper`로 바꾼다.

7. paper 모드에서 같은 webhook을 보내 KIS 모의주문 또는 내부 paper ledger 기록을 확인한다.

주의: 실제 secret 값, app key, app secret, 계좌번호는 README나 로그에 출력하지 않는다.
실전 주문은 MVP 단계에서 전면 차단되어 있으며, live 모드는 동작하지 않고 거부됩니다.
