# 📈 Advanced Crypto Trading Bot

![Python](https://img.shields.io/badge/Python-3.9+-blue.svg) ![Binance](https://img.shields.io/badge/Exchange-Binance-yellow.svg) ![License](https://img.shields.io/badge/License-MIT-green.svg)

Advanced algorithmic trading bot built with Python, designed for the Binance Futures market. It features a robust **Signal Engine** capable of multi-strategy execution, real-time risk management, and a Telegram-based dashboard.

> **Note**: This is a portfolio/educational project demonstrating advanced quantitative trading concepts.

## ✨ Key Features

### 🧠 Intelligent Signal Engine
- **Multi-Strategy Support**:
  - **Triple SMA/HMA**: Classic trend following with adjustable periods.
  - **MicroVBO (Volatility Breakout)**: Captures short-term bursts using ATR thresholds.
  - **Fractal Fisher**: Combines Hurst Exponent for trend validation with Fisher Transform for entry timing.
- **Advanced Filtering**:
  - **Kalman Filter**: Smoothes price data to reduce noise and false signals.
  - **Hurst Exponent**: Measures market memory/trend persistency (Mean Reverting vs Trending).
  - **R-Squared ($R^2$)**: Quantifies the strength of the trend.
  - **Choppiness Index**: Avoids trading in sideways/choppy markets.

### 🛡️ Robust Risk Management
- **Automatic Position Sizing**: Calculates quantity based on risk percentage and account balance.
- **Circuit Breakers**:
  - **Daily Loss Limit**: Automatically stops trading if daily limits are breached.
  - **MMR Alert**: Monitors Maintenance Margin Ratio to prevent liquidation.
- **Isolated Margin**: Ensures risk is contained per position.

### 📱 Real-time Operations
- **AsyncIO Core**: Non-blocking polling architecture for high responsiveness.
- **Telegram Dashboard**: real-time status updates, PnL tracking, and manual override controls via chat commands.
- **SQLite Logging**: Comprehensive trade history and performance tracking.

## 🛠️ Installation & Setup

1. **Clone the repository**
   ```bash
   git clone https://github.com/cys123431-ship-it/tradingbot.git
   cd tradingbot
   ```

2. **Install Dependencies**
   ```bash
   pip install ccxt pandas pandas_ta numpy pykalman python-telegram-bot
   # Optional for Fractal strategies
   pip install hurst
   ```

3. **Configuration**
   - Rename `config.json` and fill in your details (API Keys, Telegram Token).
   > ⚠️ **Security Warning**: Never commit your real API keys to GitHub!

4. **Run**
   ```bash
   python emas.py
   ```

## 📊 Strategy Examples

### Kalman Filter + SMA Cross
Combines the lag-reduction of Kalman Filters with the reliability of Moving Average Crossovers to enter trends earlier with higher confidence.

### Fractal Fisher
Uses the **Hurst Exponent** to determine if the market is trending ($H > 0.5$) and applies the **Fisher Transform** to pinpoint precise entry/exit points within that trend.

---
*Created by [cys123431](https://github.com/cys123431-ship-it)*
