# Next Steps - Setup & Testing Guide

## 🚀 Immediate Actions

### 1. Review All Changes ✅
- [ ] Read `CLIENT_REQUIREMENTS.md` - Understand client needs
- [ ] Read `ENHANCEMENTS_SUMMARY.md` - See what was implemented
- [ ] Review code changes in backend files

### 2. Set Up PostgreSQL Database 🗄️

**Option A: Local PostgreSQL**
```bash
# Install PostgreSQL (see SETUP_POSTGRESQL.md)
# Create database
psql -U postgres
CREATE DATABASE tradebot;
\q
```

**Option B: Use Cloud PostgreSQL (Recommended for Testing)**
- AWS RDS, Heroku Postgres, or ElephantSQL (free tier available)
- Get connection string and update `.env`

**Update `.env` file:**
```
DATABASE_URL=postgresql://user:password@localhost:5432/tradebot
```

### 3. Install/Update Dependencies 📦

```bash
cd backend
pip install -r requirements.txt
```

**New dependencies added:**
- `flask` (replaced FastAPI)
- `flask-cors`
- `psycopg2-binary` (PostgreSQL driver)
- `python-dateutil` (for date parsing)

### 4. Update Environment Variables 🔐

Make sure your `.env` file has:

```bash
# Tastytrade (USE SANDBOX FIRST!)
TASTYTRADE_CLIENT_ID=your_client_id
TASTYTRADE_CLIENT_SECRET=your_client_secret
TASTYTRADE_REDIRECT_URI=https://localhost
TASTYTRADE_ENV=sandbox  # ⚠️ IMPORTANT: Use sandbox first!

# Alpha Vantage
ALPHAVANTAGE_API_KEY=BAK0PFQWV70EANSC

# OpenAI
OPENAI_API_KEY=sk-your-openai-key
OPENAI_MODEL=gpt-4o-mini

# PostgreSQL Database
DATABASE_URL=postgresql://user:password@localhost:5432/tradebot

# Config
TRADING_BOT_CONFIG=config.json
```

### 5. Initialize Database 🏗️

```bash
cd backend
python -c "from backend.database import init_db; init_db(); print('Database initialized!')"
```

Or run the Flask app once (it will auto-initialize):
```bash
python -m flask run
```

### 6. Test the Application 🧪

**Start Backend:**
```bash
cd backend
python -m flask run --host=0.0.0.0 --port=8000
```

**Start Frontend (in separate terminal):**
```bash
cd frontend
npm install  # if not done already
npm run dev
```

**Test API:**
```bash
# Health check
curl http://localhost:8000/

# Get bot status
curl http://localhost:8000/bot/status

# Fetch market data
curl http://localhost:8000/data/fetch?symbols=TNA,TZA
```

---

## 🧪 Testing Checklist

### Before Live Trading:

- [ ] **Database Connection**: Verify PostgreSQL connection works
- [ ] **API Endpoints**: Test all Flask routes respond correctly
- [ ] **Sandbox Trading**: Test with Tastytrade sandbox account
- [ ] **Dual AI System**: Verify Tactics Monitor can override Pattern AI
- [ ] **Exit Logic**: Test hourly volume/money flow analysis
- [ ] **Trend Detection**: Verify enhanced trend analysis works
- [ ] **Frontend**: Ensure React dashboard connects to Flask API

### Key Features to Test:

1. **Tactics Monitor Override**
   - Create a profitable trade (>3%)
   - Verify Tactics Monitor exits even if Pattern AI says "hold"

2. **Same-Day Profit Protection**
   - Create a small profit trade (0.5-1%)
   - Simulate money leaving market
   - Verify trade exits to protect profit

3. **Hourly Analysis**
   - Check `analyze_hourly_trends()` returns correct data
   - Verify money flow direction detection

4. **Trend Detection**
   - Verify 6-month trend analysis
   - Check seasonal pattern detection (Nov-Dec, Jan-Feb)

---

## 🔧 Configuration

### Update `config.json` (if needed):
```json
{
  "timezone": "US/Eastern",
  "min_confidence": 65,
  "entry_window_start": "09:30",
  "entry_window_end": "10:00",
  "forced_exit_time": "15:30",
  "trailing_stop_pct": 0.02,
  "poll_interval_seconds": 60,
  "default_quantity": 100,
  "symbols": ["TNA", "TZA"]
}
```

---

## 🎯 Tastytrade Account Setup

### Sandbox First! ⚠️

**Client Account Info:**
- Username: `nivanko900`
- Password: `Sh@#30041971`
- **IMPORTANT**: Test in sandbox mode first!

**Steps:**
1. Set `TASTYTRADE_ENV=sandbox` in `.env`
2. Get OAuth credentials from Tastytrade
3. Update `TASTYTRADE_CLIENT_ID` and `TASTYTRADE_CLIENT_SECRET`
4. Test authentication flow
5. Only switch to production after thorough testing

---

## 📊 Monitoring & Debugging

### Check Logs:
```bash
# Flask logs will show in terminal
# Database queries will show (echo=True in database.py)
```

### Common Issues:

1. **PostgreSQL Connection Error**
   - Verify PostgreSQL is running
   - Check DATABASE_URL format
   - Verify user permissions

2. **Module Import Errors**
   - Make sure you're in the right directory
   - Check Python path
   - Verify all dependencies installed

3. **API Errors**
   - Check Flask is running on port 8000
   - Verify CORS is enabled for frontend
   - Check error messages in terminal

---

## 🚨 Important Reminders

1. **Use Sandbox First**: Never test with real money initially
2. **Backup Database**: Before live trading, backup your database
3. **Monitor Closely**: Watch first few trades closely
4. **Client Account**: Note previous "Cashflow Bot" was on same account (now inactive)
5. **Daily Limits**: Remember $25k minimum for day trading rules

---

## 📝 Next Phase After Testing

Once sandbox testing is successful:

1. **Tune Thresholds**: Adjust profit thresholds based on results
2. **Monitor Performance**: Track Tactics Monitor override frequency
3. **Optimize**: Fine-tune hourly analysis parameters
4. **Client Review**: Get feedback on enhancements
5. **Go Live**: Switch to production after validation

---

## 🆘 Need Help?

- Check `ENHANCEMENTS_SUMMARY.md` for implementation details
- Review `CLIENT_REQUIREMENTS.md` for requirements
- Check Flask logs for error messages
- Verify all environment variables are set correctly

