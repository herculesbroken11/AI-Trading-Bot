from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime

class AIAnalysisRequest(BaseModel):
    symbols: List[str] = Field(default_factory=lambda: ["TNA", "TZA"])
    timeframe: str = "1min"
    lookback_minutes: int = 60

class AIAnalysisResponse(BaseModel):
    direction: str
    confidence: float
    entry_price: Optional[float]
    take_profit: float
    early_exit_profit: float
    stop_loss: float
    use_trailing_stop: bool
    skip_trade: bool
    recommended_symbol: Optional[str] = None
    bullish_symbol: Optional[str] = None
    bearish_symbol: Optional[str] = None
    volume_trend: Optional[str] = None
    volatility_trend: Optional[str] = None
    momentum_state: Optional[str] = None
    long_term_bias: Optional[str] = None
    early_exit_reason: Optional[str] = None
    trailing_stop_trigger: Optional[str] = None
    notes: Optional[str] = None

class TradeRequest(BaseModel):
    symbol: str
    quantity: Optional[int] = 1
    ai_analysis: Optional[AIAnalysisResponse] = None
    data: Optional[Dict[str, Any]] = None

class EntryStrategyRequest(BaseModel):
    symbol: str
    ai_analysis: AIAnalysisResponse
    intraday_data: Optional[Dict[str, Any]] = None

class EntryStrategyResponse(BaseModel):
    symbol: str
    side: str
    entry_price: float
    entry_window_start: datetime
    entry_window_end: datetime
    confidence: float
    rationale: str
    indicators: Dict[str, Any]

class ExitStrategyRequest(BaseModel):
    trade_id: int
    symbol: str
    side: str
    entry_price: float
    take_profit: float
    early_exit_target: float
    stop_loss: float
    trailing_stop: Optional[float]
    entry_time: datetime
    current_price: float
    ai_analysis: AIAnalysisResponse
    intraday_data: Optional[Dict[str, Any]] = None

class ExitStrategyResponse(BaseModel):
    action: str  # "hold", "exit", "partial_exit"
    exit_price: Optional[float]
    reason: Optional[str]
    trailing_stop: Optional[float]
    indicators: Dict[str, Any] = Field(default_factory=dict)

class TradeResponse(BaseModel):
    trade_id: int
    symbol: str
    side: str
    quantity: int
    entry_price: float
    exit_price: Optional[float]
    pnl: float
    confidence: float
    status: str
    take_profit: float
    early_exit_target: float
    stop_loss: float
    trailing_stop: Optional[float]
    exit_reason: Optional[str]
    ai_reasoning: Optional[str]
    partial_exit_pct: Optional[float]
    created_at: datetime
    entry_time: datetime
    closed_at: Optional[datetime]

    class Config:
        from_attributes = True

class AccountBalance(BaseModel):
    balance: float
    buying_power: float
    open_positions: int
    daily_pnl: float

class BotStatus(BaseModel):
    running: bool
    active_trade_id: Optional[int]
    last_run: Optional[datetime]

