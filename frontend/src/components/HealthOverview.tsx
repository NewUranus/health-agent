import type { NodeData, Risk } from "../types";
import { fieldNames, profileLabels } from "../lib/labels";

export function HealthOverview({ intake, risk, placeholder, status }: {
  intake: NodeData | null;
  risk: Risk | null;
  placeholder: string;
  status: string;
}) {
  const entries = Object.entries(intake?.profile ?? {});
  const riskLevel = risk ? ({ low: "低", intermediate: "中", high: "高" }[risk.level] ?? "") + "风险" : "还未评估";
  return (
    <section className="overview" aria-label="健康概览">
      <div className="profile-card">
        <div className="card-heading">
          <h2>我的健康档案</h2>
          <span>{intake ? (intake.missing?.length ? `还需 ${intake.missing.length} 项信息` : "评估资料已齐全") : status}</span>
        </div>
        <div className="profile-fields">
          {entries.length ? entries.map(([key, value]) => (
            <div className="field" key={key}>
              <span>{profileLabels[key] ?? key}</span>
              <b>{typeof value === "boolean" ? (value ? "是" : "否") : value === "male" ? "男" : value === "female" ? "女" : String(value ?? "—")}</b>
            </div>
          )) : <p className="muted">{intake ? "等待补充健康信息" : placeholder}</p>}
        </div>
        {!!intake?.from_memory?.length && <p className="memory-note">来自长期记忆：{fieldNames(intake.from_memory)}</p>}
      </div>
      <div className={`risk-card ${risk?.level ?? ""}`}>
        <div className="risk-ring">
          <strong>{risk ? `${(Math.round((risk.risk_percent + Number.EPSILON) * 10) / 10).toFixed(1)}%` : "—"}</strong>
          <span>10年风险</span>
        </div>
        <div>
          <h2>{riskLevel}</h2>
          <p>{risk ? "Framingham 模型估算 · 仅供演示" : "完善资料后查看风险分层"}</p>
          <div className="risk-legend"><span>低</span><span>中</span><span>高</span></div>
        </div>
      </div>
    </section>
  );
}
