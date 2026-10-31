export type Profile = Record<string, string | number | boolean | null>;

export interface Risk {
  risk_percent: number;
  level: string;
}

export interface Source {
  id: string;
  title: string;
  source: string;
  score: number;
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
  prompt_version?: string;
  tool_calls?: ToolCall[];
  chunks?: Source[];
  delta?: number;
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
  | { type: "error"; message: string }
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
