# AI Trading Bot

Full-stack AI-powered ETF trading bot specialised for the TNA / TZA leveraged ETF pair. The system combines FastAPI, React, OpenAI, Alpha Vantage, and Tastytrade to deliver fully automated intraday trading with strict risk controls.

## 🚀 Feature Highlights

- **Same-Day Risk Controls** – Forced position exit at 3:30 PM ET and programmable 5% stop-loss enforcement
- **Adaptive Profit Management** – AI-guided partial exits (0.5–3%), 5% profit target, and smart trailing stops when volume stays strong
- **Pair Analysis (TNA/TZA)** – OpenAI analyses bull/bear ETFs simultaneously with macro context, seasonal bias, and confidence scoring
- **Configurable Strategy Engine** – Entry/exit modules evaluate volume, momentum, volatility, and money flow within the first 30 minutes of the session
- **Bot Orchestration Loop** – Background manager monitors entry windows, manages trades, and enforces emergency exits
- **Modern Dashboard** – Real-time charts, AI outlook, bot control centre, and detailed trade history with targets and reasoning

## 📁 Project Structure

```
ai_trading_bot/
├── backend/
│   ├── main.py             # FastAPI application & routes
│   ├── bot_manager.py      # Automated trading loop
│   ├── auth_tastytrade.py  # Tastytrade OAuth2
│   ├── data_feed.py        # Alpha Vantage client + indicators
│   ├── ai_decision.py      # OpenAI decision engine
│   ├── strategy.py         # Entry & exit logic
│   ├── trade_exec.py       # Order placement helpers
│   ├── logger.py           # Trade / prediction logging
│   ├── database.py         # SQLAlchemy models (trades, predictions, trends)
│   ├── models.py           # Pydantic schemas
│   ├── config.py           # Runtime configuration loader
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── components/     # Reusable UI components & charts
│   │   ├── pages/          # Dashboard, Controls, AI Insights, History
│   │   ├── services/       # API client wrappers
│   │   └── App.tsx
│   ├── package.json
│   └── tailwind.config.js
├── .env.example
├── SETUP.md
├── config.json             # Default strategy configuration
└── README.md
```

## 🛠️ Quick Start

### 1. Backend

```bash
cd backend
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Create a `.env` file in the repository root (see [Environment Variables](#-environment-variables)) and update credentials. You can customise the strategy runtime via `config.json`.

Run the API locally:

```bash
uvicorn backend.main:app --reload --port 8000
```

### 2. Frontend

```bash
cd frontend
npm install
npm run dev
```

The dashboard is available at **http://localhost:5173**. The API is served from **http://localhost:8000** by default.

## ⚙️ Configuration

- `config.json` controls entry windows, confidence thresholds, quantities, and forced exit time. Override by setting the `TRADING_BOT_CONFIG` environment variable to another JSON file.
- Default quantity is `100` shares; adjust `default_quantity` in the config for live runs.

## 📡 REST API Summary

| Endpoint | Method | Description |
| --- | --- | --- |
| `/auth/tastytrade/url` | GET | Retrieve OAuth2 authorisation URL |
| `/auth/tastytrade` | POST | Exchange OAuth `code` for access token |
| `/data/fetch` | GET | Fetch intraday data + summary for `symbols=TNA,TZA` |
| `/ai/analyze` | POST | Run OpenAI macro + micro trend analysis on symbol list |
| `/strategy/entry` | POST | Compute entry plan (swing low/high, volume surge, window) |
| `/strategy/exit` | POST | Evaluate exit action (hold / exit / partial) with indicators |
| `/trade/execute` | POST | Execute trade via Tastytrade & log analysis/plan |
| `/trade/close/{id}` | POST | Close open trade and log reason |
| `/logs` | GET | Retrieve recent trades with targets & AI reasoning |
| `/account/balance` | GET | Account cash, buying power, open positions, P&L |
| `/bot/start` / `/bot/stop` | POST | Start/stop automated bot loop |
| `/bot/status` | GET | Current bot state and active trade ID |

### Example: Analyse & Plan Entry

```bash
# Request AI outlook for TNA/TZA
curl -X POST http://localhost:8000/ai/analyze \
  -H "Content-Type: application/json" \
  -d '{"symbols":["TNA","TZA"],"timeframe":"1min","lookback_minutes":60}'

# Use AI response to request entry plan
curl -X POST http://localhost:8000/strategy/entry \
  -H "Content-Type: application/json" \
  -d '{"symbol":"TNA","ai_analysis":{...}}'
```

## 🔐 Environment Variables

Create `.env` at the repository root:

```
TASTYTRADE_CLIENT_ID=your_client_id
TASTYTRADE_CLIENT_SECRET=your_client_secret
TASTYTRADE_REDIRECT_URI=https://localhost
ALPHAVANTAGE_API_KEY=BAK0PFQWV70EANSC
OPENAI_API_KEY=sk-your-openai-key
DATABASE_URL=sqlite+aiosqlite:///tradebot.db
TASTYTRADE_ENV=sandbox
TRADING_BOT_CONFIG=config.json
```

## 🧠 Trading Workflow

1. **Pre-market analysis** – OpenAI assesses the last 30–60 minutes for TNA & TZA, returning direction, confidence, and risk parameters.
2. **Entry window (09:30–10:00 ET)** – Entry strategy searches for optimal dip/peak with volume and momentum confirmation before placing orders.
3. **Active monitoring** – Bot loop tracks volume trends, momentum shifts, and trailing stop levels. Positive trades that fail to reach 5% may be closed early to protect gains.
4. **Forced exit** – Every open position is flattened no later than 15:30 ET, regardless of performance.
5. **Logging** – Trades, AI predictions, and trend signals are persisted to SQLite for dashboard and audit trail.

## 📊 Frontend Overview

- **Dashboard** – 1-minute price/volume chart, AI outlook panel, bot status, and profit curve
- **AI Insights** – On-demand OpenAI analysis with confidence, targets, and notes
- **Controls** – Bot start/stop, AI entry preview, and manual trade execution
- **Trade History** – Detailed ledger including take-profit/stop-loss levels and AI reasoning

## 🔧 Development Tips

- FastAPI auto-reloads with `--reload`; interactive docs at `http://localhost:8000/docs`
- Tailwind + Vite hot reload the React UI (`npm run dev`)
- Update `config.json` to experiment with different thresholds without code changes

## ⚠️ Operational Notes

- **Sandbox First** – Keep `TASTYTRADE_ENV=sandbox` until you validate performance end-to-end.
- **API Rate Limits** – Alpha Vantage free tier limits to 5 requests/minute; consider caching or premium keys for production.
- **Security** – Do not commit `.env` or live API keys to version control.
- **Risk** – This project is educational. Real trading carries significant risk.

## 🤝 Contributing

Pull requests and issue reports are welcome! Please open an issue describing proposed changes before large contributions.

