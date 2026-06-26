# TradingView Webhook on Azure - KStockBot

## 핵심 원칙

KStockBot의 FastAPI 서버는 내부적으로 `127.0.0.1:8090` 또는 `0.0.0.0:8090`에서 실행할 수 있다. 그러나 TradingView 외부 webhook을 붙일 때는 일반적으로 HTTPS 443 endpoint를 사용해야 한다.

권장 구조:

```text
TradingView Alert
→ https://your-domain.com/webhook/tradingview
→ Nginx HTTPS reverse proxy
→ http://127.0.0.1:8090/webhook/tradingview
→ KStockBot FastAPI
```

## 왜 Nginx/HTTPS가 필요한가

* TradingView webhook은 외부에서 접근 가능한 URL이 필요하다.
* 포트는 80 또는 443을 기준으로 준비한다.
* webhook 응답은 빠르게 끝나야 한다 (TradingView의 3초 제한).
* **Fast-ACK 모드**: `KSTOCK_WEBHOOK_FAST_ACK=true` (기본값)는 Webhook Secret을 동기적으로 먼저 검증한 뒤 유효하면 즉시 `HTTP 202 Accepted`를 반환하고, 실제 주문 및 Telegram 전송 등은 FastAPI `BackgroundTasks`를 통해 비동기로 백그라운드 처리합니다. Secret 검증 실패 시에는 즉시 `HTTP 403 Forbidden`을 반환합니다.
* Nginx proxy timeouts (`proxy_read_timeout`, `proxy_connect_timeout`) 설정은 TradingView의 3초 제약 조건에 맞게 `3s`로 설정할 것을 권장합니다.

## Fast ACK 처리 결과 확인

Fast ACK 모드에서는 TradingView에는 먼저 HTTP 202 Accepted를 반환한다. 실제 처리 결과는 다음 위치에서 확인한다.

```text
Telegram: /webhooks
Runtime: runtime/webhook_events.json
Log: logs/kstockbot.log
```

`runtime/webhook_events.json`에는 event_id, action, symbol, status, message 같은 요약만 저장한다. webhook secret과 API key, 계좌번호는 저장하지 않는다.

* KStockBot은 `analysis`, `paper`, `manual_live`, `live` 모드를 구분한다.
* MVP에서 `live` 완전자동 실전 주문은 거부된다.

## MVP 테스트 순서

1. Azure VM 내부에서 health 확인

```bash
curl http://127.0.0.1:8090/health
curl http://127.0.0.1:8090/status
```

2. 내부 webhook 테스트

```bash
curl -X POST http://127.0.0.1:8090/webhook/tradingview \
  -H "Content-Type: application/json" \
  -d '{"secret":"실제_SECRET","source":"tradingview","strategy":"test","action":"BUY","symbol":"005930","price":80000.75,"time":"2026-06-10T10:00:00+09:00","timeframe":"1D"}'
```

3. Telegram 점검

```text
/status
/mode
/kischeck
/dryrun
/price 005930
/balance
/cash 005930 80000
```

4. 그 다음에만 Nginx/HTTPS를 붙인다.

## TradingView Alert JSON 예시

```json
{
  "secret": "실제_SECRET",
  "source": "tradingview",
  "strategy": "kstock_swing_v1",
  "action": "BUY",
  "symbol": "005930",
  "name": "Samsung Electronics",
  "price": "{{close}}",
  "time": "{{time}}",
  "timeframe": "{{interval}}",
  "confidence": 0.72,
  "reason": "trend_breakout"
}
```

## 보안 주의

* webhook body에 API key, app secret, Telegram token, 계좌번호를 넣지 않는다.
* `secret`은 강한 랜덤 문자열로 설정한다.
* `.env`와 GitHub Secrets 외에는 민감정보를 저장하지 않는다.
* `analysis` 모드에서는 주문이 없어야 한다.
* `paper` 모드에서만 모의투자 또는 내부 paper ledger가 가능하다.
* `manual_live`는 approval 생성까지만 간다.
* `live` 완전자동 주문은 MVP에서 거부된다.
