import React from 'react';
import {
  ResponsiveContainer,
  ComposedChart,
  CartesianGrid,
  XAxis,
  YAxis,
  Tooltip,
  Area,
  Bar,
} from 'recharts';
import { CandlePoint } from '../services/api';

interface PriceChartProps {
  data: CandlePoint[];
  symbol: string;
}

const formatTime = (timestamp: string) => {
  const date = new Date(timestamp);
  return `${date.getHours()}:${String(date.getMinutes()).padStart(2, '0')}`;
};

export const PriceChart: React.FC<PriceChartProps> = ({ data, symbol }) => {
  const chartData = data.map((point) => ({
    ...point,
    time: formatTime(point.timestamp),
    range: [point.low, point.high],
    isUp: point.close >= point.open,
  }));

  return (
    <ResponsiveContainer width="100%" height={320}>
      <ComposedChart data={chartData} margin={{ top: 10, right: 20, bottom: 10, left: 0 }}>
        <CartesianGrid strokeDasharray="3 3" />
        <XAxis dataKey="time" minTickGap={24} />
        <YAxis yAxisId="price" domain={['dataMin', 'dataMax']} tickFormatter={(v) => `$${v.toFixed(2)}`} width={72} />
        <YAxis yAxisId="volume" orientation="right" hide domain={[0, 'dataMax']} />
        <Tooltip
          formatter={(value: number, name: string) => {
            if (name === 'close') {
              return [`$${value.toFixed(2)}`, 'Close'];
            }
            if (name === 'volume') {
              return [value.toLocaleString(), 'Volume'];
            }
            return [value, name];
          }}
          labelFormatter={(label) => `${symbol} ${label}`}
        />
        <Area
          yAxisId="price"
          type="monotone"
          dataKey="close"
          stroke="hsl(221.2 83.2% 53.3%)"
          fill="hsl(221.2 83.2% 53.3% / 0.2)"
          strokeWidth={2}
          name="close"
        />
        <Bar
          yAxisId="volume"
          dataKey="volume"
          fill="hsl(215.4 16.3% 46.9%)"
          barSize={6}
          radius={[2, 2, 0, 0]}
          opacity={0.4}
        />
      </ComposedChart>
    </ResponsiveContainer>
  );
};
