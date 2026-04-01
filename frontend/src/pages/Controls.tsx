import React, { useState, useEffect } from 'react';
import { Card } from '../components/Card';
import { Button } from '../components/Button';
import { Modal } from '../components/Modal';
import { Alert } from '../components/Alert';
import { tradingAPI, AIAnalysis, BotStatus, EntrySignal, ExecutionProfile } from '../services/api';
import { ExecutionProfileCard } from '../components/ExecutionProfileCard';
import {
  Play,
  Square,
  RefreshCw,
  TrendingUp,
  Radar,
  Target,
  Loader2,
} from 'lucide-react';

export const Controls: React.FC = () => {
  const [botStatus, setBotStatus] = useState<BotStatus | null>(null);
  const [showManualTrade, setShowManualTrade] = useState(false);
  const [symbol, setSymbol] = useState<'TNA' | 'TZA'>('TNA');
  const [quantity, setQuantity] = useState(100);
  const [message, setMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null);
  const [analysis, setAnalysis] = useState<AIAnalysis | null>(null);
  const [entryPreview, setEntryPreview] = useState<EntrySignal | null>(null);
  const [loadingAnalysis, setLoadingAnalysis] = useState(false);
  const [executionProfile, setExecutionProfile] = useState<ExecutionProfile | null>(null);
  const [profileLoading, setProfileLoading] = useState(true);

  useEffect(() => {
    refreshStatus();
    const interval = setInterval(refreshStatus, 10000);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const p = await tradingAPI.getExecutionProfile();
        if (!cancelled) setExecutionProfile(p);
      } catch {
        if (!cancelled) setExecutionProfile(null);
      } finally {
        if (!cancelled) setProfileLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const refreshStatus = async () => {
    try {
      const status = await tradingAPI.getBotStatus();
      setBotStatus(status);
    } catch (error) {
      console.error('Failed to check bot status:', error);
    }
  };

  const notify = (type: 'success' | 'error', text: string) => {
    setMessage({ type, text });
    setTimeout(() => setMessage(null), 5000);
  };

  const handleStartBot = async () => {
    try {
      const status = await tradingAPI.startBot();
      setBotStatus(status);
      notify('success', 'Bot started successfully');
    } catch (error: any) {
      notify('error', error.response?.data?.detail || 'Failed to start bot');
    }
  };

  const handleStopBot = async () => {
    try {
      const status = await tradingAPI.stopBot();
      setBotStatus(status);
      notify('success', 'Bot stopped successfully');
    } catch (error: any) {
      notify('error', error.response?.data?.detail || 'Failed to stop bot');
    }
  };

  const handleManualTrade = async () => {
    try {
      const result = await tradingAPI.executeTrade(symbol, quantity);
      notify('success', `Trade executed for ${result.trade.symbol}`);
      setShowManualTrade(false);
    } catch (error: any) {
      notify('error', error.response?.data?.detail || 'Failed to execute trade');
    }
  };

  const handleRefreshData = async () => {
    try {
      await tradingAPI.fetchData();
      notify('success', 'Market data refreshed');
    } catch (error: any) {
      notify('error', 'Failed to refresh data');
    }
  };

  const handlePreviewEntry = async () => {
    setLoadingAnalysis(true);
    try {
      const alt = symbol === 'TNA' ? 'TZA' : 'TNA';
      const ai = await tradingAPI.analyzeMarket([symbol, alt]);
      setAnalysis(ai);
      if (ai.skip_trade) {
        setEntryPreview(null);
        notify('error', 'AI recommends skipping trade today.');
      } else if (ai.recommended_symbol) {
        const entry = await tradingAPI.fetchEntrySignal(ai.recommended_symbol, ai);
        setEntryPreview(entry);
        notify('success', `Entry plan prepared for ${entry.symbol}`);
      }
    } catch (error: any) {
      console.error('Preview entry failed', error);
      notify('error', error.response?.data?.detail || 'Failed to fetch entry signal');
    } finally {
      setLoadingAnalysis(false);
    }
  };

  return (
    <div className="space-y-6">
      <h1 className="text-3xl font-bold">Controls</h1>

      <ExecutionProfileCard profile={executionProfile} loading={profileLoading} />

      {message && (
        <Alert variant={message.type === 'success' ? 'success' : 'error'} onClose={() => setMessage(null)}>
          {message.text}
        </Alert>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <Card>
          <h2 className="text-xl font-semibold mb-4">Bot Control</h2>
          <div className="space-y-4 text-sm">
            <StatusRow label="Status" value={botStatus?.running ? 'Running' : 'Stopped'} highlight={botStatus?.running ? 'text-green-600' : 'text-red-600'} />
            <StatusRow label="Active Trade ID" value={botStatus?.active_trade_id ? botStatus.active_trade_id.toString() : 'None'} />
            <StatusRow label="Last Run" value={botStatus?.last_run ? new Date(botStatus.last_run).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : 'Never'} />
            <div className="flex gap-2 pt-2">
              <Button onClick={handleStartBot} disabled={botStatus?.running} className="flex-1">
                <Play className="h-4 w-4 mr-2" />
                Start Bot
              </Button>
              <Button onClick={handleStopBot} disabled={!botStatus?.running} variant="danger" className="flex-1">
                <Square className="h-4 w-4 mr-2" />
                Stop Bot
              </Button>
            </div>
          </div>
        </Card>

        <Card>
          <h2 className="text-xl font-semibold mb-4">AI Entry Planner</h2>
          <div className="space-y-3 text-sm">
            <StatusRow label="Primary Symbol" value={symbol} />
            <div className="flex gap-2">
              <Button onClick={() => setSymbol('TNA')} variant={symbol === 'TNA' ? 'primary' : 'outline'} className="flex-1">
                Bull (TNA)
              </Button>
              <Button onClick={() => setSymbol('TZA')} variant={symbol === 'TZA' ? 'primary' : 'outline'} className="flex-1">
                Bear (TZA)
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">
              The strategy runs only while the bot is <strong>started</strong>; use Preview after Start so entry rules (window + pullback) can evaluate.
            </p>
            <Button onClick={handlePreviewEntry} disabled={loadingAnalysis} className="w-full">
              {loadingAnalysis ? <Loader2 className="h-4 w-4 animate-spin mr-2" /> : <Radar className="h-4 w-4 mr-2" />}
              Preview Entry
            </Button>
            {analysis && (
              <div className="bg-muted/50 rounded-md p-3 text-xs space-y-1">
                <div className="flex justify-between">
                  <span>Direction</span>
                  <strong className={analysis.direction === 'bullish' ? 'text-green-600' : analysis.direction === 'bearish' ? 'text-red-600' : ''}>
                    {analysis.direction.toUpperCase()}
                  </strong>
                </div>
                <div className="flex justify-between">
                  <span>Confidence</span>
                  <strong>{analysis.confidence.toFixed(1)}%</strong>
                </div>
                <div className="flex justify-between">
                  <span>Take Profit</span>
                  <strong>{(analysis.take_profit * 100).toFixed(1)}%</strong>
                </div>
                <div className="flex justify-between">
                  <span>Stop Loss</span>
                  <strong>{(analysis.stop_loss * 100).toFixed(1)}%</strong>
                </div>
              </div>
            )}
            {entryPreview && (
              <div className="bg-primary/5 border border-primary/20 rounded-md p-3 text-xs space-y-1">
                <div className="flex justify-between">
                  <span>Symbol</span>
                  <strong>{entryPreview.symbol}</strong>
                </div>
                <div className="flex justify-between">
                  <span>Side</span>
                  <strong>{entryPreview.side}</strong>
                </div>
                <div className="flex justify-between">
                  <span>Order type</span>
                  <strong>{entryPreview.order_type ?? executionProfile?.order_type ?? 'Market'}</strong>
                </div>
                <div className="flex justify-between">
                  <span>Entry Price</span>
                  <strong>${entryPreview.entry_price.toFixed(2)}</strong>
                </div>
                <div className="flex justify-between">
                  <span>Window</span>
                  <strong>
                    {new Date(entryPreview.entry_window_start).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} -{' '}
                    {new Date(entryPreview.entry_window_end).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                  </strong>
                </div>
                {entryPreview.indicators?.entry_window && (
                  <div className="flex justify-between">
                    <span>Band (ET)</span>
                    <strong>{entryPreview.indicators.entry_window}</strong>
                  </div>
                )}
                {entryPreview.indicators &&
                  typeof entryPreview.indicators.pullback_retrace_or_bounce_pct === 'number' && (
                    <div className="flex justify-between">
                      <span>Pullback (retrace / bounce)</span>
                      <strong>
                        {(entryPreview.indicators.pullback_retrace_or_bounce_pct * 100).toFixed(3)}%
                      </strong>
                    </div>
                  )}
                {entryPreview.indicators &&
                  typeof entryPreview.indicators.session_high === 'number' &&
                  typeof entryPreview.indicators.session_low === 'number' && (
                    <div className="flex justify-between gap-2">
                      <span>Session high / low</span>
                      <strong>
                        ${entryPreview.indicators.session_high.toFixed(2)} / ${entryPreview.indicators.session_low.toFixed(2)}
                      </strong>
                    </div>
                  )}
                <div className="flex justify-between">
                  <span>Confidence</span>
                  <strong>{entryPreview.confidence.toFixed(1)}%</strong>
                </div>
                <div className="text-muted-foreground mt-1">{entryPreview.rationale}</div>
                <Button
                  onClick={async () => {
                    try {
                      const result = await tradingAPI.executeTrade(entryPreview.symbol, quantity);
                      notify('success', `Trade executed for ${result.trade.symbol}`);
                    } catch (error: any) {
                      notify('error', error.response?.data?.detail || 'Failed to execute trade');
                    }
                  }}
                  className="w-full mt-3"
                >
                  <Target className="h-4 w-4 mr-2" />
                  Execute This Plan
                </Button>
              </div>
            )}
          </div>
        </Card>

        <Card>
          <h2 className="text-xl font-semibold mb-4">Utilities</h2>
          <div className="space-y-3 text-sm">
            <StatusRow label="Default Quantity" value={`${quantity}`} />
            <div className="flex gap-2">
              <Button onClick={() => setShowManualTrade(true)} variant="outline" className="flex-1">
                <TrendingUp className="h-4 w-4 mr-2" />
                Manual Trade
              </Button>
              <Button onClick={handleRefreshData} variant="outline" className="flex-1">
                <RefreshCw className="h-4 w-4 mr-2" />
                Refresh Data
              </Button>
            </div>
            <div className="text-xs text-muted-foreground bg-muted/40 rounded-md p-3">
              All positions are force-closed at 3:30 PM ET. Use manual trade to queue paper executions.
            </div>
          </div>
        </Card>
      </div>

      <Modal isOpen={showManualTrade} onClose={() => setShowManualTrade(false)} title="Manual Trade">
        <div className="space-y-4">
          <div>
            <label className="block text-sm font-medium mb-2">Symbol</label>
            <select
              value={symbol}
              onChange={(e) => setSymbol(e.target.value as 'TNA' | 'TZA')}
              className="w-full px-3 py-2 border rounded-md"
            >
              <option value="TNA">TNA (Bull)</option>
              <option value="TZA">TZA (Bear)</option>
            </select>
          </div>
          <div>
            <label className="block text-sm font-medium mb-2">Quantity</label>
            <input
              type="number"
              value={quantity}
              onChange={(e) => setQuantity(parseInt(e.target.value, 10) || 1)}
              className="w-full px-3 py-2 border rounded-md"
              min={1}
            />
          </div>
          <Button onClick={handleManualTrade} className="w-full">
            Execute Trade
          </Button>
        </div>
      </Modal>
    </div>
  );
};

interface StatusRowProps {
  label: string;
  value: string;
  highlight?: string;
}

const StatusRow: React.FC<StatusRowProps> = ({ label, value, highlight }) => (
  <div className="flex items-center justify-between">
    <span className="text-muted-foreground">{label}</span>
    <span className={`font-medium ${highlight || ''}`}>{value}</span>
  </div>
);

