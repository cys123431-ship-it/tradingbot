# Advanced Crypto Trading Bot

Python 기반 crypto futures trading bot입니다. 이 문서는 현재 실제 코드 기준으로 정리되어 있으며, 핵심 live 경로는 `emas.py`, `utbreakout/`, `prediction/`, `tests/`에 있습니다.

> 주의: mainnet 실거래 전에는 반드시 testnet/paper forward test로 주문, TP/SL, 텔레그램 제어, 네트워크 설정을 확인해야 합니다. API key, secret, token은 저장소에 커밋하지 마세요.

## 현재 Live Core

현재 `MA_STRATEGIES = set()` 상태라 README에 오래 남아 있던 Triple SMA/HMA 계열은 live core가 아닙니다. 실제 선택 가능한 core 전략은 다음 계열입니다.

- `UTBOT`: 기본 UTBot 방향 전략
- `UTSMC`: UTBot + SMC/internal structure 조합
- `UTRSI`: UTBot + RSI timing 조합
- `UTRSIBB`: UTBot + RSI/Bollinger timing 조합
- `UTBB`: UTBot + Bollinger Band 조합
- `UTBOT_FILTERED_BREAKOUT_V1`: UT Breakout set registry 기반 필터 전략
- `UTBOT_ADAPTIVE_TIMEFRAME_V1`: UT Breakout adaptive timeframe 변형
- `RSIBB`: 순수 RSI Bollinger mean-reversion. 선택은 가능하지만 기본값은 `rsibb_enabled=false`, `rsibb_paper_only=true`, `rsibb_regime_guard_enabled=true`입니다.

## UT Breakout Sets

UT Breakout은 `build_utbreakout_set_registry()`에서 Set1~Set63으로 구성됩니다. Set1~60은 기존 UT/volatility/prediction 계열이며, Set61~63이 추가되었습니다.

- Set61 `UT + Rolling OFI Confirmation`: rolling orderbook imbalance, taker buy/sell ratio, spread/depth guard를 UTBot 방향과 같이 확인합니다.
- Set62 `UT + OI Funding Squeeze`: OI 변화 z-score, OI acceleration, funding, long/short ratio, basis, liquidity guard를 조합합니다.
- Set63 `UT + Squeeze Release Breakout`: BB width percentile, Keltner squeeze state, range expansion, Donchian breakout, ATR guard를 같이 확인합니다.

새 set은 lightweight deterministic feature score를 보조 gate로 사용합니다. Deep Learning 모델은 포함하지 않습니다.

## Legacy / Inactive Notes

과거 README에 있던 Triple SMA, HMA, MicroVBO, Fractal Fisher 설명은 현재 live core 설명으로 보지 마세요. 일부 보조 코드나 legacy 흔적이 남아 있을 수 있지만 현재 전략 선택 집합과 실행 경로는 UTBot/UT hybrid/UT Breakout 중심입니다.

## Risk And Operations

- 기본 exchange mode는 testnet 쪽으로 유지됩니다.
- live 주문을 켜는 기본 설정은 추가하지 않습니다.
- UT Breakout은 fixed TP ladder, ATR 기반 risk plan, market quality, selector quality, diagnostic log를 함께 사용합니다.
- Telegram status/diagnostic에는 포지션, 선택 set, 후보, orderflow/OI/squeeze/feature score 정보가 표시됩니다.

## Setup

```bash
pip install -r requirements.txt
python3 scripts/launch_emas.py
```

공식 실행 진입점은 `scripts/launch_emas.py` 하나입니다. 이 런처가 프로세스 락과 실거래 안전 패치를 적용하므로 `emas.py`를 직접 실행하지 마세요. 실운영 환경에서는 Azure/GitHub Actions 배포 설정을 먼저 확인하고, 로컬에서 live bot을 실행하지 않는 운영 원칙을 유지하세요.

## RSPT-v2 / Dual 전략

RSPT는 BTC·ETH 공통 움직임을 제거한 잔차 상대강도와 실제 돌파 후 눌림을 이용해 UTBreakout과 독립적으로 방향을 결정합니다. Dual은 두 전략이 같은 방향이면 정상 위험, 한 전략만 신호면 60% 위험, 서로 반대면 거래하지 않습니다. 세부 설정과 검증 방법은 [`docs/RSPT_V2.md`](docs/RSPT_V2.md)를 참고하세요.
