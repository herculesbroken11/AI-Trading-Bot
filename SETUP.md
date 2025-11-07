# Quick Setup Guide

## Environment Variables

Create a `.env` file in the repository root with the following content:

```bash
TASTYTRADE_CLIENT_ID=your_client_id
TASTYTRADE_CLIENT_SECRET=your_client_secret
TASTYTRADE_REDIRECT_URI=https://localhost
ALPHAVANTAGE_API_KEY=BAK0PFQWV70EANSC
OPENAI_API_KEY=sk-your-openai-api-key-here
DATABASE_URL=sqlite+aiosqlite:///tradebot.db
TASTYTRADE_ENV=sandbox
TRADING_BOT_CONFIG=config.json
```

**Important**
- Get Tastytrade credentials from: https://developer.tastytrade.com/
- Get OpenAI API key from: https://platform.openai.com/api-keys
- Alpha Vantage API key is provided (free tier)

## Strategy Configuration

`config.json` contains runtime settings (entry window, confidence thresholds, forced exit time, default quantity, etc.). Adjust values before starting the bot, or point `TRADING_BOT_CONFIG` to a custom JSON file.

## Quick Start Commands

### Backend
```bash
cd backend
python -m venv venv
venv\Scripts\activate  # Windows
# or
source venv/bin/activate  # Mac/Linux
pip install -r requirements.txt
uvicorn backend.main:app --reload --port 8000
```

### Frontend
```bash
cd frontend
npm install
npm run dev
```

Then visit: http://localhost:5173

