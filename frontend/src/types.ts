export type Profile = Record<string, string | number | boolean | null>;

export interface Risk {
  risk_percent: number;
  level: string;
}

export type FollowupStatus =
  | { status: "none"; last_risk_percent?: number }
  | { status: "upcoming" | "due" | "overdue"; due_date: string; days_until: number; level: string; last_risk_percent?: number };

export interface HistoryRecord {
  at: string;
  kind?: string;
  risk_percent: number;
  level: string;
  sbp?: number | null;
  dbp?: number | null;
  total_chol?: number | null;
  hdl?: number | null;
  smoker?: boolean | null;
  adherence?: string;
  symptoms?: boolean;
  bp_updated?: boolean;
}

export interface Source {
  id: string;
  title: string;
  source: string;
  score: number;
  page?: number | null;
  url?: string;
  origin?: string;
  fetched_at?: string;
}

export interface ToolCall {
  tool: string;
  args: Record<string, unknown>;
  result: Record<string, unknown>;
  via?: string;
  cached?: boolean;
}

export interface NodeData extends Record<string, unknown> {
  profile?: Profile;
  missing?: string[];
  from_memory?: string[];
  red_flags?: string[];
  route?: string;
  reason?: string;
  ok?: boolean;
  problems?: string[];
  attempt?: number;
  followup?: string;
  degraded?: boolean;
  fallback?: boolean;
  no_evidence?: boolean;
  prompt_version?: string;
  tool_calls?: ToolCall[];
  chunks?: Source[];
  delta?: number;
  key?: string;
  step?: number;
  total?: number;
  adherence?: string;
  due_date?: string;
  cancelled?: boolean;
  web_search?: boolean;
  web_failed?: boolean;
  web_read_count?: number;
  web_sources_only?: boolean;
  web_results?: { title: string; url: string }[];
  web_errors?: string[];
}

export interface NodeEvent {
  type: "node";
  node: string;
  agent: string;
  ms: number;
  data?: NodeData;
}

export type ServerEvent =
  | NodeEvent
  | { type: "message"; content: string }
  | { type: "error"; message: string; code?: string; request_id?: string; retryable?: boolean; next_action?: string }
  | { type: "done" };

export interface Message {
  role: "assistant" | "user" | "error";
  text: string;
  turn: number;
}

export interface TraceTurn {
  turn: number;
  nodes: NodeEvent[];
}
