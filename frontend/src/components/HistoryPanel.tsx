import { useEffect, useState, type CSSProperties } from "react";
import { apiFetch } from "../lib/api";
import { formatPercent } from "../lib/format";
import { adherenceLabels } from "../lib/labels";
import type { FollowupStatus, HistoryRecord } from "../types";

const WIDTH = 320;
const HEIGHT = 170;
const MARGIN = { top: 12, right: 42, bottom: 24, left: 26 };
const THRESHOLDS = [10, 20]; // 与后端风险分级一致：<10 低，10–20 中，>=20 高
const levelLabels: Record<string, string> = { low: "低风险", intermediate: "中风险", high: "高风险" };

const dateFormat = new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit" });
export const formatRecordDate = (at: string) => dateFormat.format(new Date(at));

export function RiskTrend({ records }: { records: HistoryRecord[] }) {
  const [active, setActive] = useState<number | null>(null);
  const plotWidth = WIDTH - MARGIN.left - MARGIN.right;
  const plotHeight = HEIGHT - MARGIN.top - MARGIN.bottom;
  const yMax = Math.max(30, Math.ceil(Math.max(...records.map((r) => r.risk_percent)) / 10) * 10);
  // 按次数等距排列：复查可能在同一天，按真实时间排会叠在一起
  const x = (index: number) => MARGIN.left + (records.length === 1 ? plotWidth / 2 : (index / (records.length - 1)) * plotWidth);
  const y = (value: number) => MARGIN.top + (1 - value / yMax) * plotHeight;
  const slot = plotWidth / Math.max(1, records.length - 1);
  const line = records.map((r, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(r.risk_percent).toFixed(1)}`).join(" ");
  const last = records.length - 1;
  const current = active === null ? null : records[active];

  return <div className="trend">
    <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label={`10年风险趋势，共 ${records.length} 次记录，最新 ${formatPercent(records[last].risk_percent)}`}>
      <line className="trend-axis" x1={MARGIN.left} x2={WIDTH - MARGIN.right} y1={y(0)} y2={y(0)} />
      <text className="trend-tick" x={MARGIN.left - 6} y={y(0) + 3} textAnchor="end">0</text>
      {THRESHOLDS.map((value) => <g key={value}>
        <line className="trend-threshold" x1={MARGIN.left} x2={WIDTH - MARGIN.right} y1={y(value)} y2={y(value)} />
        <text className="trend-tick" x={MARGIN.left - 6} y={y(value) + 3} textAnchor="end">{value}</text>
      </g>)}
      <text className="trend-tick" x={MARGIN.left - 6} y={y(yMax) + 3} textAnchor="end">{yMax}</text>
      <text className="trend-tick" x={x(0)} y={HEIGHT - 6} textAnchor="start">{formatRecordDate(records[0].at).slice(5)}</text>
      <text className="trend-tick" x={x(last)} y={HEIGHT - 6} textAnchor="end">{formatRecordDate(records[last].at).slice(5)}</text>
      {active !== null && <line className="trend-crosshair" x1={x(active)} x2={x(active)} y1={MARGIN.top} y2={y(0)} />}
      <path className="trend-line" d={line} />
      {records.map((r, i) => <circle key={i} className="trend-point" cx={x(i)} cy={y(r.risk_percent)} r={active === i ? 5.5 : 4} />)}
      <text className="trend-end-label" x={x(last) + 8} y={y(records[last].risk_percent) + 4}>{formatPercent(records[last].risk_percent)}</text>
      {records.map((r, i) => <rect key={i} className="trend-hit" x={x(i) - slot / 2} y={0} width={slot} height={HEIGHT} tabIndex={0}
        aria-label={`${formatRecordDate(r.at)}，风险 ${formatPercent(r.risk_percent)}`}
        onPointerEnter={() => setActive(i)} onPointerLeave={() => setActive(null)}
        onFocus={() => setActive(i)} onBlur={() => setActive(null)} />)}
    </svg>
    {current && active !== null && <div className="trend-tooltip" role="tooltip"
      style={{
        left: `${(x(active) / WIDTH) * 100}%`, top: `${(y(current.risk_percent) / HEIGHT) * 100}%`,
        "--tip-x": x(active) < WIDTH * 0.3 ? "-20%" : x(active) > WIDTH * 0.7 ? "-80%" : "-50%",
      } as CSSProperties}>
      <b>{formatPercent(current.risk_percent)}</b><span>{formatRecordDate(current.at)}</span>
    </div>}
    <p className="trend-caption">10年风险（%）。低于 10 为低风险，10–20 为中风险，20 以上为高风险。横轴按记录次数等距排列。</p>
  </div>;
}

function HistoryItem({ record }: { record: HistoryRecord }) {
  const pressure = record.sbp ? `血压 ${record.sbp}${record.dbp ? `/${record.dbp}` : ""}` : null;
  return <li className="history-item">
    <div className="history-head">
      <span className="history-date">{formatRecordDate(record.at)}</span>
      <span className="history-kind">{record.kind === "checkin" ? "复查" : "首次评估"}</span>
    </div>
    <div className="history-risk"><b>{formatPercent(record.risk_percent)}</b><span>{levelLabels[record.level] ?? ""}</span></div>
    <p>
      {pressure && <span>{pressure}</span>}
      {record.adherence && <span>依从：{adherenceLabels[record.adherence] ?? "未明确"}</span>}
      {record.symptoms === true && <span className="warning">有不适症状</span>}
      {record.symptoms === false && <span>无不适症状</span>}
    </p>
  </li>;
}

export function HistoryPanel({ userId, refreshKey, followup }: { userId: string; refreshKey: string; followup: FollowupStatus }) {
  const [records, setRecords] = useState<HistoryRecord[] | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    apiFetch(`/api/history?user_id=${encodeURIComponent(userId)}`, { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error();
        return response.json();
      })
      .then((value: { records: HistoryRecord[] }) => {
        if (controller.signal.aborted) return;
        setRecords(value.records);
        setFailed(false);
      })
      .catch(() => { if (!controller.signal.aborted) setFailed(true); });
    return () => controller.abort();
  }, [userId, refreshKey]);

  if (failed) return <div className="history"><p className="warning">暂时无法读取随访记录，请稍后重试。</p></div>;
  if (!records) return <div className="history"><p className="sim-note">正在读取…</p></div>;
  if (!records.length) return <div className="trace-empty"><span aria-hidden="true">↗</span><h3>还没有记录</h3><p>完成一次风险评估后，这里会显示风险变化和复查记录。</p></div>;

  return <div className="history">
    {followup.status !== "none" && <p className="history-next">下次复查 {followup.due_date}</p>}
    {records.length > 1 ? <RiskTrend records={records} /> : <p className="sim-note">再复查一次，就能看到风险趋势。</p>}
    <ol className="history-list">
      {[...records].reverse().map((record, index) => <HistoryItem key={`${record.at}:${index}`} record={record} />)}
    </ol>
  </div>;
}
