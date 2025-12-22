# Trading Bot

자동 매매를 위한 봇입니다.

## 설정

1. `config.json` 파일을 열어 다음 항목을 입력하세요:
    - `api_key`: 바이낸스 API Key
    - `secret_key`: 바이낸스 Secret Key
    - `token`: 텔레그램 봇 토큰
    - `chat_id`: 텔레그램 Chat ID

2. 필요한 패키지 설치:
    ```bash
    pip install ccxt pandas pandas_ta numpy pykalman python-telegram-bot
    ```
    (만약 `hurst` 패키지가 필요하면 `pip install hurst` 도 설치)

## 실행

```bash
python emas.py
```
