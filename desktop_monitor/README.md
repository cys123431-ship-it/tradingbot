# TradingBot Monitor (Rust)

오래된 Windows 11 노트북에서도 가볍게 실행하도록 만든 네이티브 모니터입니다.
WebView/Electron과 별도 브라우저를 사용하지 않으며, 최근 300개 캔들만 메모리에 보관합니다.

## 제공 화면

- 바이낸스 선물 1분봉 실시간 차트
- 실제 거래소 포지션(봇 진입 및 수동 진입 모두)
- 진입가, 현재가, SL, TP 주문 라인
- 증거금, 레버리지, 미실현 PnL, ROE, 청산가
- 텔레그램과 같은 전략 신호 상태 및 봇 계좌 요약
- 상단 버튼으로 다크/라이트 테마 전환(선택값 자동 저장)
- 포지션이 없을 때 차트는 현재 봇 상태표의 감시 종목을 우선 표시
- 멀리 있는 SL/TP는 봉을 축소하지 않고 차트 가장자리에 방향·가격·거리로 표시
- 주황색 현재선은 마지막 봉 체결가와 일치하고, 비율은 진입가 대비 방향별 수익률로 표시

## 보안 및 동작

- 거래소 API 키는 Windows 앱으로 복사하지 않습니다.
- 기존 `azure-trading-bot` SSH 별칭으로 서버의 읽기 전용 스트림만 받습니다.
- 앱에는 주문 생성, 취소, 청산, 설정 변경 기능이 없습니다.
- SSH가 끊기면 3초 후 자동으로 다시 연결합니다.

## 빌드 및 실행

PowerShell에서 다음을 실행합니다.

```powershell
.\desktop_monitor\build_windows.ps1
.\desktop_monitor\target\release\tradingbot-monitor.exe
```

다른 SSH 별칭을 써야 할 때만 실행 전에 `TRADINGBOT_SSH_HOST` 환경 변수를 설정합니다.
