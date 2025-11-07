import React, { useState } from 'react';
import { Card } from '../components/Card';
import { Button } from '../components/Button';
import { tradingAPI, AIAnalysis } from '../services/api';
import { Brain, TrendingUp, TrendingDown, Minus, Loader2 } from 'lucide-react';

export const AIInsights: React.FC = () => {
  const [symbol, setSymbol] = useState<'TNA' | 'TZA'>('TNA');
  const [analysis, setAnalysis] = useState<AIAnalysis | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleAnalyze = async () => {
    setLoading(true);
    setError(null);
    try {
      const alt = symbol === 'TNA' ? 'TZA' : 'TNA';
      const result = await tradingAPI.analyzeMarket([symbol, alt]);
      setAnalysis(result);
    } catch (err: any) {
      console.error('Failed to analyze:', err);
      setError(err?.response?.data?.detail || 'Unable to fetch AI analysis.');
    } finally {
      setLoading(false);
    }
  };

  const getDirectionIcon = () => {
    if (!analysis) return null;
    if (analysis.direction === 'bullish') return <TrendingUp className="h-6 w-6 text-green-600" />;
    if (analysis.direction === 'bearish') return <TrendingDown className="h-6 w-6 text-red-600" />;
    return <Minus className="h-6 w-6 text-gray-600" />;
  };

  return (
    <div className="space-y-6">
      <h1 className="text-3xl font-bold">AI Insights</h1>

      <Card>
        <div className="space-y-4">
          <div className="flex gap-4 items-end">
            <div className="flex-1">
              <label className="block text-sm font-medium mb-2">Primary Symbol</label>
              <select
                value={symbol}
                onChange={(e) => setSymbol(e.target.value as 'TNA' | 'TZA')}
                className="w-full px-3 py-2 border rounded-md"
              >
                <option value="TNA">TNA (Bull)</option>
                <option value="TZA">TZA (Bear)</option>
              </select>
            </div>
            <Button onClick={handleAnalyze} disabled={loading}>
              {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Run Analysis'}
            </Button>
          </div>
          {error && <div className="text-sm text-red-600">{error}</div>}
        </div>
      </Card>

      {analysis && (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          <Card className="lg:col-span-2">
            <div className="flex items-center gap-3 mb-4">
              <Brain className="h-6 w-6 text-primary" />
              <div>
                <h2 className="text-xl font-semibold">Directional Outlook</h2>
                {analysis.recommended_symbol && (
                  <p className="text-xs text-muted-foreground">Recommended instrument: {analysis.recommended_symbol}</p>
                )}
              </div>
            </div>
            <div className="space-y-4 text-sm">
              <InsightRow label="Direction" value={analysis.direction} icon={getDirectionIcon()} />
              <InsightRow label="Confidence" value={`${analysis.confidence.toFixed(1)}%`} />
              <InsightRow label="Take Profit" value={`${(analysis.take_profit * 100).toFixed(1)}%`} />
              <InsightRow label="Early Exit" value={`${(analysis.early_exit_profit * 100).toFixed(1)}%`} />
              <InsightRow label="Stop Loss" value={`${(analysis.stop_loss * 100).toFixed(1)}%`} />
              <InsightRow label="Volume Trend" value={analysis.volume_trend || 'n/a'} />
              <InsightRow label="Volatility" value={analysis.volatility_trend || 'n/a'} />
              <InsightRow label="Momentum" value={analysis.momentum_state || 'n/a'} />
              <InsightRow label="Long-Term Bias" value={analysis.long_term_bias || 'Neutral'} />
            </div>
          </Card>

          <Card>
            <h2 className="text-xl font-semibold mb-4">Execution Notes</h2>
            <div className="space-y-3 text-sm">
              <InsightRow label="Trailing Stop" value={analysis.use_trailing_stop ? 'Consider 1-2%' : 'Disabled'} />
              <InsightRow label="Skip Trade" value={analysis.skip_trade ? 'Yes' : 'No'} highlight={analysis.skip_trade ? 'text-red-600' : 'text-green-600'} />
              {analysis.notes && (
                <div className="text-xs text-muted-foreground bg-muted/50 p-3 rounded-md leading-relaxed">
                  {analysis.notes}
                </div>
              )}
            </div>
          </Card>
        </div>
      )}
    </div>
  );
};

interface InsightRowProps {
  label: string;
  value: string;
  icon?: React.ReactNode;
  highlight?: string;
}

const InsightRow: React.FC<InsightRowProps> = ({ label, value, icon, highlight }) => (
  <div className="flex items-center justify-between gap-3">
    <span className="text-muted-foreground">{label}</span>
    <div className={`flex items-center gap-2 font-medium ${highlight || ''}`}>
      {icon}
      <span>{value}</span>
    </div>
  </div>
);

