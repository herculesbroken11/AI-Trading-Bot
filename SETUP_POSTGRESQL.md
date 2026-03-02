# PostgreSQL Database Setup

## Step 1: Install PostgreSQL

### Windows
Download and install from: https://www.postgresql.org/download/windows/

### macOS
```bash
brew install postgresql
brew services start postgresql
```

### Linux (Ubuntu/Debian)
```bash
sudo apt-get update
sudo apt-get install postgresql postgresql-contrib
sudo systemctl start postgresql
```

## Step 2: Create Database and User

```sql
-- Connect to PostgreSQL (default user is usually 'postgres')
psql -U postgres

-- Create database
CREATE DATABASE tradebot;

-- Create user (optional, or use existing postgres user)
CREATE USER tradebot_user WITH PASSWORD 'your_password';

-- Grant privileges
GRANT ALL PRIVILEGES ON DATABASE tradebot TO tradebot_user;

-- Exit psql
\q
```

## Step 3: Update .env File

Update your `.env` file with PostgreSQL connection string:

```
DATABASE_URL=postgresql://tradebot_user:your_password@localhost:5432/tradebot
```

Or if using default postgres user:
```
DATABASE_URL=postgresql://postgres:your_postgres_password@localhost:5432/tradebot
```

## Step 4: Verify Connection

```python
# Test script
from sqlalchemy import create_engine
import os
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_engine(DATABASE_URL)
conn = engine.connect()
print("✅ Connected to PostgreSQL!")
conn.close()
```

