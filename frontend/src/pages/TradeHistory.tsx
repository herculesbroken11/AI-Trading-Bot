import React, { useState, useEffect } from 'react';
import { Card } from '../components/Card';
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from '../components/Table';
import { tradingAPI, Trade } from '../services/api';
import { Button } from '../components/Button';

export const TradeHistory: React.FC = () => {
  const [trades, setTrades] = useState<Trade[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    loadTrades();
  }, []);

  const loadTrades = async () => {
    try {
      const data = await tradingAPI.getLogs(200);
      setTrades(data);
    } catch (error) {
      console.error('Failed to load trades:', error);
    } finally {
      setLoading(false);
    }
  };

  const handleCloseTrade = async (tradeId: number) => {
    try {
      await tradingAPI.closeTrade(tradeId, 'manual_close');
      loadTrades();
    } catch (error) {
      console.error('Failed to close trade:', error);
    }
  };

  if (loading) {
    return <div className="text-center py-8">Loading...</div>;
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-3xl font-bold">Trade History</h1>
        <Button onClick={loadTrades}>Refresh</Button>
      </div>

      <Card>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>ID</TableHead>
              <TableHead>Symbol</TableHead>
              <TableHead>Side</TableHead>
              <TableHead>Entry</TableHead>
              <TableHead>Exit</TableHead>
              <TableHead>Quantity</TableHead>
              <TableHead>P&L</TableHead>
              <TableHead>Confidence</TableHead>
              <TableHead>Targets</TableHead>
              <TableHead>Exit Reason</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {trades.map((trade) => (
              <TableRow key={trade.trade_id}>
                <TableCell>{trade.trade_id}</TableCell>
                <TableCell className="font-medium">{trade.symbol}</TableCell>
                <TableCell>
                  <span
                    className={`px-2 py-1 rounded text-xs ${
                      trade.side === 'buy' ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'
                    }`}
                  >
                    {trade.side.toUpperCase()}
                  </span>
                </TableCell>
                <TableCell>${trade.entry_price.toFixed(2)}</TableCell>
                <TableCell>{trade.exit_price ? `$${trade.exit_price.toFixed(2)}` : '-'}</TableCell>
                <TableCell>{trade.quantity}</TableCell>
                <TableCell className={trade.pnl >= 0 ? 'text-green-600' : 'text-red-600'}>
                  ${trade.pnl.toFixed(2)}
                </TableCell>
                <TableCell>{trade.confidence.toFixed(1)}%</TableCell>
                <TableCell className="text-xs text-muted-foreground">
                  TP {(trade.take_profit * 100).toFixed(1)}%<br />
                  SL {(trade.stop_loss * 100).toFixed(1)}%<br />
                  Early {(trade.early_exit_target * 100).toFixed(1)}%
                </TableCell>
                <TableCell className="text-xs">
                  {trade.exit_reason || '—'}
                  {trade.ai_reasoning && (
                    <div className="text-muted-foreground mt-1 max-w-xs truncate" title={trade.ai_reasoning}>
                      {trade.ai_reasoning}
                    </div>
                  )}
                </TableCell>
                <TableCell>
                  <span
                    className={`px-2 py-1 rounded text-xs ${
                      trade.status === 'open' ? 'bg-blue-100 text-blue-800' : 'bg-gray-100 text-gray-800'
                    }`}
                  >
                    {trade.status.toUpperCase()}
                  </span>
                </TableCell>
                <TableCell>
                  {trade.status === 'open' && (
                    <Button variant="outline" size="sm" onClick={() => handleCloseTrade(trade.trade_id)}>
                      Close
                    </Button>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </Card>
    </div>
  );
};

