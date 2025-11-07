import React, { useState, useEffect } from 'react';
import { Card } from '../components/Card';
import { PriceChart } from '../components/PriceChart';
import {
  tradingAPI,
  AccountBalance,
  AIAnalysis,
  MarketDataResponse,
  BotStatus,
  Trade,
} from '../services/api';
import {
  TrendingUp,
  TrendingDown,
  DollarSign,
  Activity,
  Bot,
  Signal,
  LineChart as LineChartIcon,
} from 'lucide-react';
import {
  ResponsiveContainer,
  LineChart,
  Line,
  CartesianGrid,
  XAxis,
  YAxis,
  Tooltip,
} from 'recharts';

export const Dashboard: React.FC = () => {
  const [balance, setBalance] = useState<AccountBalance | null>(null);
  const [analysis, setAnalysis] = useState<AIAnalysis | null>(null);
  const [marketData, setMarketData] = useState<MarketDataResponse | null>(null);
  const [botStatus, setBotStatus] = useState<BotStatus | null>(null);
  const [profitData, setProfitData] = useState<Array<{ time: string; pnl: number }>>([]);
  const [selectedSymbol, setSelectedSymbol] = useState<'TNA' | 'TZA'>('TNA');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    loadData();
    const interval = setInterval(loadData, 60000);
    return () => clearInterval(interval);
  }, []);

  const loadData = async () => {
    setError(null);
    try {
      const [balanceRes, logs, marketRes, analysisRes, statusRes] = await Promise.all([
        tradingAPI.getBalance(),
        tradingAPI.getLogs(200),
        tradingAPI.fetchData(),
        tradingAPI.analyzeMarket(),
        tradingAPI.getBotStatus(),
      ]);

      setBalance(balanceRes);
      setMarketData(marketRes);
      setAnalysis(analysisRes);
      setBotStatus(statusRes);

      const closedTrades = (logs as Trade[])
        .filter((trade) => trade.status === 'closed')
        .sort((a, b) => new Date(a.closed_at || a.entry_time).getTime() - new Date(b.closed_at || b.entry_time).getTime());

      const cumulative = closedTrades.reduce<Array<{ time: string; pnl: number }>>((acc, trade, index) => {
        const prev = index > 0 ? acc[index - 1].pnl : 0;
        acc.push({
          time: new Date(trade.closed_at || trade.entry_time).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
          pnl: prev + trade.pnl,
        });
        return acc;
      }, []);
      setProfitData(cumulative);
    } catch (err: any) {
      console.error('Failed to load dashboard data', err);
      setError(err?.response?.data?.detail || 'Unable to load dashboard data.');
    } finally {
      setLoading(false);
    }
  };

  if (loading) {
    return <div className="text-center py-8">Loading...</div>;
  }

  if (error) {
    return <div className="text-center py-8 text-red-600">{error}</div>;
  }

  const selectedData = marketData?.data[selectedSymbol] ?? null;
  const marketSummary = marketData?.data || {};

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between flex-wrap gap-4">
        <h1 className="text-3xl font-bold">Trading Dashboard</h1>
        {analysis && (
          <div className="text-sm text-muted-foreground">
            Updated {new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4">
        <MetricCard
          icon={<DollarSign className="h-6 w-6 text-primary" />}
          label="Account Balance"
          value={balance ? `$${balance.balance.toFixed(2)}` : '--'}
        />
        <MetricCard
          icon={<Activity className="h-6 w-6 text-primary" />}
          label="Buying Power"
          value={balance ? `$${balance.buying_power.toFixed(2)}` : '--'}
        />
        <MetricCard
          icon={<Activity className="h-6 w-6 text-primary" />}
          label="Open Positions"
          value={balance ? `${balance.open_positions}` : '--'}
        />
        <MetricCard
          icon={(balance?.daily_pnl || 0) >= 0 ? <TrendingUp className="h-6 w-6 text-green-500" /> : <TrendingDown className="h-6 w-6 text-red-500" />}
          label="Daily P&L"
          value={balance ? `$${balance.daily_pnl.toFixed(2)}` : '--'}
          valueClass={(balance?.daily_pnl || 0) >= 0 ? 'text-green-600' : 'text-red-600'}
        />
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-6">
        <Card className="xl:col-span-2">
          <div className="flex items-center justify-between mb-4">
            <div>
              <h2 className="text-xl font-semibold flex items-center gap-2">
                <LineChartIcon className="h-5 w-5 text-primary" />
                {selectedSymbol} 1-min Chart
              </h2>
              <p className="text-sm text-muted-foreground">
                Volume overlay with intraday data
              </p>
            </div>
            <div className="flex gap-2">
              {(['TNA', 'TZA'] as const).map((symbol) => (
                <button
                  key={symbol}
                  onClick={() => setSelectedSymbol(symbol)}
                  className={`px-3 py-1 rounded-md border text-sm ${selectedSymbol === symbol ? 'bg-primary text-white border-primary' : 'border-border text-muted-foreground'}`}
                >
                  {symbol}
                </button>
              ))}
            </div>
          </div>
          {selectedData ? (
            <PriceChart data={selectedData.candles} symbol={selectedSymbol} />
          ) : (
            <div className="text-sm text-muted-foreground">No market data available.</div>
          )}
        </Card>

        <Card>
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-xl font-semibold flex items-center gap-2">
              <Signal className="h-5 w-5 text-primary" />
              AI Outlook
            </h2>
            {analysis?.recommended_symbol && (
              <span className="text-xs px-3 py-1 rounded-full bg-primary/10 text-primary font-medium">
                Focus: {analysis.recommended_symbol}
              </span>
            )}
          </div>
          {analysis ? (
            <div className="space-y-3 text-sm">
              <InfoRow label="Direction" value={analysis.direction.toUpperCase()} highlight={analysis.direction === 'bullish' ? 'text-green-600' : analysis.direction === 'bearish' ? 'text-red-600' : undefined} />
              <InfoRow label="Confidence" value={`${analysis.confidence.toFixed(1)}%`} />
              <InfoRow label="Take Profit" value={`${(analysis.take_profit * 100).toFixed(1)}%`} />
              <InfoRow label="Early Exit" value={`${(analysis.early_exit_profit * 100).toFixed(1)}%`} />
              <InfoRow label="Stop Loss" value={`${(analysis.stop_loss * 100).toFixed(1)}%`} />
              <InfoRow label="Volume Trend" value={analysis.volume_trend || 'n/a'} />
              <InfoRow label="Momentum" value={analysis.momentum_state || 'n/a'} />
              <InfoRow label="Long-term Bias" value={analysis.long_term_bias || 'Neutral'} />
              {analysis.notes && (
                <div className="mt-3 text-xs text-muted-foreground bg-muted/50 p-3 rounded-md">
                  {analysis.notes}
                </div>
              )}
            </div>
          ) : (
            <div className="text-sm text-muted-foreground">AI analysis unavailable.</div>
          )}
        </Card>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-6">
        <Card className="xl:col-span-2">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-xl font-semibold">Profit Curve</h2>
            <span className="text-xs text-muted-foreground">Realised P&L</span>
          </div>
          <ResponsiveContainer width="100%" height={280}>
            <LineChart data={profitData}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="time" minTickGap={24} />
              <YAxis tickFormatter={(value) => `$${value.toFixed(0)}`} />
              <Tooltip formatter={(value: number) => `$${value.toFixed(2)}`} />
              <Line type="monotone" dataKey="pnl" stroke="#3b82f6" strokeWidth={2} dot={false} />
            </LineChart>
          </ResponsiveContainer>
        </Card>

        <Card>
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-xl font-semibold flex items-center gap-2">
              <Bot className="h-5 w-5 text-primary" />
              Bot Status
            </h2>
          </div>
          {botStatus ? (
            <div className="space-y-3 text-sm">
              <InfoRow label="Running" value={botStatus.running ? 'Yes' : 'No'} highlight={botStatus.running ? 'text-green-600' : 'text-red-600'} />
              <InfoRow label="Active Trade ID" value={botStatus.active_trade_id ? botStatus.active_trade_id.toString() : 'None'} />
              <InfoRow label="Last Run" value={botStatus.last_run ? new Date(botStatus.last_run).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : 'Never'} />
              {analysis && (
                <InfoRow label="Trailing Stop" value={analysis.use_trailing_stop ? 'Active' : 'Inactive'} />
              )}
            </div>
          ) : (
            <div className="text-sm text-muted-foreground">Bot status unavailable.</div>
          )}
        </Card>
      </div>

      <Card>
        <h2 className="text-xl font-semibold mb-4">Market Snapshot</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {Object.entries(marketSummary).map(([symbol, data]) => (
            <div key={symbol} className="rounded-md border border-border p-4">
              <div className="flex items-center justify-between mb-2">
                <span className="font-semibold">{symbol}</span>
                <span className={`text-sm ${data.summary.price_change_pct >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                  {(data.summary.price_change_pct * 100).toFixed(2)}%
                </span>
              </div>
              <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs text-muted-foreground">
                <dt>Current Price</dt>
                <dd className="text-right text-foreground">${data.summary.current_price.toFixed(2)}</dd>
                <dt>Volume Trend</dt>
                <dd className="text-right">{data.summary.volume_trend}</dd>
                <dt>Momentum</dt>
                <dd className="text-right">{data.summary.momentum.toFixed(4)}</dd>
                <dt>Volatility</dt>
                <dd className="text-right">{data.summary.volatility.toFixed(4)}</dd>
              </dl>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
};

interface MetricCardProps {
  icon: React.ReactNode;
  label: string;
  value: string;
  valueClass?: string;
}

const MetricCard: React.FC<MetricCardProps> = ({ icon, label, value, valueClass }) => (
  <Card>
    <div className="flex items-center justify-between">
      <div>
        <p className="text-sm text-muted-foreground">{label}</p>
        <p className={`text-2xl font-bold ${valueClass || ''}`}>{value}</p>
      </div>
      {icon}
    </div>
  </Card>
);

interface InfoRowProps {
  label: string;
  value: string;
  highlight?: string;
}

const InfoRow: React.FC<InfoRowProps> = ({ label, value, highlight }) => (
  <div className="flex items-center justify-between">
    <span className="text-muted-foreground">{label}</span>
    <span className={`font-medium ${highlight || ''}`}>{value}</span>
  </div>
);

