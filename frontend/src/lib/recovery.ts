/** 失败后的恢复方式：连接未确认则续看原任务，未落账可重发，已落账或不确定则只转人工。 */

export type RecoveryMode = "reconnect" | "resubmit" | "hold";

const TERMINAL_FAILURE = new Set(["failed", "timed_out", "interrupted", "cancelled"]);

export function recoveryMode(task: { status: string; health_committed?: boolean | null } | null): RecoveryMode {
  if (!task) return "reconnect";
  if (task.status === "succeeded" || task.status === "waiting_human") return "reconnect";
  if (task.health_committed === false && TERMINAL_FAILURE.has(task.status)) return "resubmit";
  return "hold";
}

export function recoveryNote(mode: RecoveryMode, healthCommitted: boolean | null | undefined): string {
  if (mode === "reconnect") return "提交结果还不确定。继续查看会使用同一幂等键，不会新建第二个任务。";
  if (mode === "resubmit") return "这次没有保存新的健康记录，可以重新提交。请先看失败原因。";
  if (healthCommitted) return "健康记录已保存，请先核对随访记录，不要直接重发。";
  return "还不能确认健康记录是否已保存，请先核对，不要直接重发。";
}
