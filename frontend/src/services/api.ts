import axios from 'axios';

const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';

const api = axios.create({
  baseURL: API_BASE_URL,
  headers: {
    'Content-Type': 'application/json',
  },
});

export interface CandlePoint {
  timestamp: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface MarketSummary {
  current_price: number;
  price_change_pct: number;
  volume_trend: string;
  volume_avg: number;
  money_flow: number;
  momentum: number;
  volatility: number;
}

export interface MarketDataSymbol {
  summary: MarketSummary;
  candles: CandlePoint[];
}

export interface MarketDataResponse {
  data: Record<string, MarketDataSymbol>;
}

export interface Trade {
  trade_id: number;
  symbol: string;
  side: string;
  quantity: number;
  entry_price: number;
  exit_price: number | null;
  pnl: number;
  confidence: number;
  status: string;
  take_profit: number;
  early_exit_target: number;
  stop_loss: number;
  trailing_stop: number | null;
  exit_reason: string | null;
  ai_reasoning: string | null;
  partial_exit_pct: number | null;
  created_at: string;
  entry_time: string;
  closed_at: string | null;
}

export interface AccountBalance {
  balance: number;
  buying_power: number;
  open_positions: number;
  daily_pnl: number;
}

export interface AIAnalysis {
  direction: string;
  confidence: number;
  entry_price: number | null;
  take_profit: number;
  early_exit_profit: number;
  stop_loss: number;
  use_trailing_stop: boolean;
  skip_trade: boolean;
  recommended_symbol?: string | null;
  volume_trend?: string | null;
  volatility_trend?: string | null;
  momentum_state?: string | null;
  long_term_bias?: string | null;
  notes?: string | null;
}

export interface BotStatus {
  running: boolean;
  active_trade_id: number | null;
  last_run: string | null;
}

export const tradingAPI = {
  getAuthUrl: async () => {
    const response = await api.get('/auth/tastytrade/url');
    return response.data;
  },

  authenticate: async (code: string) => {
    const response = await api.post('/auth/tastytrade', null, {
      params: { code },
    });
    return response.data;
  },

  fetchData: async (symbols: string[] = ['TNA', 'TZA']): Promise<MarketDataResponse> => {
    const response = await api.get('/data/fetch', {
      params: { symbols: symbols.join(',') },
    });
    return response.data;
  },

  analyzeMarket: async (symbols: string[] = ['TNA', 'TZA']): Promise<AIAnalysis> => {
    const response = await api.post('/ai/analyze', {
      symbols,
      timeframe: '1min',
      lookback_minutes: 60,
    });
    return response.data;
  },

  fetchEntrySignal: async (symbol: string, ai_analysis: AIAnalysis) => {
    const response = await api.post('/strategy/entry', {
      symbol,
      ai_analysis,
    });
    return response.data;
  },

  fetchExitSignal: async (payload: any) => {
    const response = await api.post('/strategy/exit', payload);
    return response.data;
  },

  executeTrade: async (symbol: string, quantity: number = 1) => {
    const response = await api.post('/trade/execute', { symbol, quantity });
    return response.data as { status: string; trade: Trade; analysis: AIAnalysis; order: any };
  },

  closeTrade: async (tradeId: number, reason?: string) => {
    const response = await api.post(`/trade/close/${tradeId}`, null, {
      params: reason ? { reason } : undefined,
    });
    return response.data as Trade;
  },

  getLogs: async (limit: number = 100): Promise<Trade[]> => {
    const response = await api.get('/logs', { params: { limit } });
    return response.data;
  },

  getBalance: async (): Promise<AccountBalance> => {
    const response = await api.get('/account/balance');
    return response.data;
  },

  startBot: async (): Promise<BotStatus> => {
    const response = await api.post('/bot/start');
    return response.data;
  },

  stopBot: async (): Promise<BotStatus> => {
    const response = await api.post('/bot/stop');
    return response.data;
  },

  getBotStatus: async (): Promise<BotStatus> => {
    const response = await api.get('/bot/status');
    return response.data;
  },
};

