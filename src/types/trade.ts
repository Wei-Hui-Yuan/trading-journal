export type TradeStatus = 'pending_review' | 'completed';
export type TradeDirection = 'LONG' | 'SHORT';

export interface Trade {
  id: string;
  ibkr_exec_id?: string | null;
  ticker: string;
  direction: TradeDirection;
  style: string; // e.g., 'Breakout', 'Mean Reversion'
  status: TradeStatus;
  entry_date: string;
  exit_date?: string | null;
  actual_entry: number;
  exit_price?: number | null;
  quantity: number;
  
  // Manual Plan Elements
  planned_entry?: number | null;
  stop_loss?: number | null;
  target?: number | null;
  risk_percent: number;
  strategy_id?: string | null;
  
  // Qualitative & Discipline Checkboxes
  grade?: string | null; // 'A', 'B', 'C', 'F'
  market_regime?: string | null;
  source_tag: string;
  screenshot_url?: string | null;
  lessons_comments?: string | null;
  hard_sl_set: boolean;
  waited_retest: boolean;
  followed_plan: boolean;
  created_at: string;
}

export interface KPIStats {
  netPnl: number;
  winRate: number;
  totalTrades: number;
  /** null when there are no losing trades - the ratio is unbounded. */
  profitFactor: number | null;
  avgRoi: number;
  pendingCount: number;
}

export interface HeatmapCell {
  dayOfWeek: number; // 1 (Mon) - 5 (Fri)
  timeSlot: 'Morning' | 'Midday' | 'Afternoon' | 'After-Hours';
  pnl: number;
  tradeCount: number;
}
