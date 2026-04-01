import React from 'react';
import { Card } from './Card';
import { Settings2, Clock, Percent, Shield } from 'lucide-react';
import type { ExecutionProfile } from '../services/api';

interface Props {
  profile: ExecutionProfile | null;
  loading?: boolean;
  compact?: boolean;
}

const Row: React.FC<{ label: string; value: string }> = ({ label, value }) => (
  <div className="flex justify-between gap-4 text-sm">
    <span className="text-muted-foreground">{label}</span>
    <span className="font-medium text-right tabular-nums">{value}</span>
  </div>
);

export const ExecutionProfileCard: React.FC<Props> = ({ profile, loading, compact }) => {
  if (loading) {
    return (
      <Card>
        <p className="text-sm text-muted-foreground">Loading execution profile…</p>
      </Card>
    );
  }
  if (!profile) {
    return (
      <Card>
        <p className="text-sm text-muted-foreground">Execution profile unavailable.</p>
      </Card>
    );
  }

  const reservePct = (profile.buying_power_reserve_pct ?? 0) * 100;
  const maxPosPct = (profile.max_position_pct_of_buying_power ?? 0) * 100;
  const pullbackMinPct = (profile.pullback_min_retrace_pct ?? 0) * 100;

  return (
    <Card>
      <div className="flex items-start justify-between gap-2 mb-4">
        <div>
          <h2 className="text-xl font-semibold flex items-center gap-2">
            <Settings2 className="h-5 w-5 text-primary" />
            Execution profile
          </h2>
          <p className="text-xs text-muted-foreground mt-1">
            Read-only view of <code className="text-xs bg-muted px-1 rounded">config.json</code> + computed morning band (ET).
          </p>
        </div>
      </div>

      <div className={`space-y-3 ${compact ? '' : 'md:grid md:grid-cols-2 md:gap-4 md:space-y-0'}`}>
        <div className="space-y-2 rounded-md border border-border/60 p-3 bg-muted/20">
          <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            <Clock className="h-3.5 w-3.5" />
            Timing
          </div>
          <Row label="Timezone" value={profile.timezone} />
          <Row label="Morning entry band" value={`${profile.entry_band_start} – ${profile.entry_band_end}`} />
          {!compact && (
            <p className="text-xs text-muted-foreground leading-snug pt-1">{profile.entry_band_description}</p>
          )}
          <Row label="Forced exit (ET)" value={profile.forced_exit_time} />
          {compact && (
            <p className="text-xs text-muted-foreground pt-1 border-t border-border/40 mt-2">
              Pullback filter: {profile.pullback_entry_enabled ? 'On' : 'Off'} · min{' '}
              {pullbackMinPct.toFixed(2)}% · reserve {reservePct.toFixed(0)}% BP
            </p>
          )}
        </div>

        <div className="space-y-2 rounded-md border border-border/60 p-3 bg-muted/20">
          <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            <Percent className="h-3.5 w-3.5" />
            Orders & sizing
          </div>
          <Row label="Order type" value={profile.order_type} />
          <Row label="Buying power reserve" value={`${reservePct.toFixed(1)}%`} />
          <Row label="Max position (after reserve)" value={`${maxPosPct.toFixed(0)}% of effective BP`} />
          <Row label="Default quantity" value={String(profile.default_quantity)} />
          <Row label="Min AI confidence" value={`${profile.min_confidence}%`} />
        </div>

        {!compact && (
          <div className="space-y-2 rounded-md border border-border/60 p-3 bg-muted/20 md:col-span-2">
            <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              <Shield className="h-3.5 w-3.5" />
              Pullback gate
            </div>
            <Row label="Pullback filter" value={profile.pullback_entry_enabled ? 'On' : 'Off'} />
            <Row label="Min retrace / bounce" value={`${pullbackMinPct.toFixed(2)}%`} />
            <Row label="Min bars in window" value={String(profile.pullback_lookback_bars)} />
          </div>
        )}
      </div>
    </Card>
  );
};
