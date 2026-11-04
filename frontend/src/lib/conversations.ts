import type { ServerEvent } from "../types";
export type Conversation = { id: string; title: string; thread_id: string; archived: boolean; updated_at: number; turns: number; last_status: string };
export type ConversationTurn = { id: string; message: string; status: string; events: ServerEvent[]; truncated: boolean };
export type ConversationPage = { conversation: Conversation; turns: ConversationTurn[]; next_cursor: string | null };
