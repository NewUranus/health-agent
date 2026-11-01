import { useEffect, useState } from "react";
import { formatPercent, formatPoints } from "../lib/format";
import type { Profile } from "../types";

const REQUIRED = ["sex", "age", "total_chol", "hdl", "sbp", "bp_treated", "smoker", "diabetes"];
const DEBOUNCE_MS = 200;

interface SimulationResult {
  baseline: number;
  simulated: number;
  absolute_reduction: number;
  relative_reduction_percent: number;
  changes: string[];
  note: string;
}

function Bar({ label, value, tone }: { label: string; value: number; tone: string }) {
  return <div className="sim-bar">
    <div className="sim-bar-label"><span>{label}</span><b>{formatPercent(value)}</b></div>
    <div className="sim-bar-track" aria-hidden="true"><i className={tone} style={{ width: `${Math.min(100, value)}%` }} /></div>
  </div>;
}

function SimulationForm({ profile }: { profile: Profile }) {
  const sbp = Number(profile.sbp);
  const chol = Number(profile.total_chol);
  const [sbpTarget, setSbpTarget] = useState(sbp);
  const [cholTarget, setCholTarget] = useState(chol);
  const [quit, setQuit] = useState(false);
  const [result, setResult] = useState<SimulationResult | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => {
      fetch("/api/tools/simulate_intervention", {
        method: "POST", headers: { "Content-Type": "application/json" }, signal: controller.signal,
        body: JSON.stringify({
          sex: profile.sex, age: profile.age, total_chol_mmol: chol, hdl_mmol: profile.hdl, sbp,
          bp_treated: profile.bp_treated, smoker: profile.smoker, diabetes: profile.diabetes,
          ...(sbpTarget !== sbp && { sbp_target: sbpTarget }),
          ...(cholTarget !== chol && { total_chol_target: cholTarget }),
          ...(quit && { quit_smoking: true }),
        }),
      })
        .then((response) => {
          if (!response.ok) throw new Error();
          return response.json();
        })
        .then((value: SimulationResult) => {
          if (controller.signal.aborted) return;
          setResult(value);
          setFailed(false);
        })
        .catch(() => { if (!controller.signal.aborted) setFailed(true); });
    }, DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [profile, sbp, chol, sbpTarget, cholTarget, quit]);

  return <div className="simulation">
    <label className="sim-control">
      <span>目标收缩压 <b>{sbpTarget} mmHg</b></span>
      <input type="range" aria-label="目标收缩压" min={Math.min(100, sbp)} max={sbp} step={1} value={sbpTarget}
        onChange={(event) => setSbpTarget(Number(event.target.value))} />
    </label>
    <label className="sim-control">
      <span>目标总胆固醇 <b>{cholTarget.toFixed(1)} mmol/L</b></span>
      <input type="range" aria-label="目标总胆固醇" min={Math.min(3, chol)} max={chol} step={0.1} value={cholTarget}
        onChange={(event) => setCholTarget(Number(event.target.value))} />
    </label>
    {profile.smoker === true && <label className="sim-check">
      <input type="checkbox" checked={quit} onChange={(event) => setQuit(event.target.checked)} /> 假设戒烟
    </label>}
    {failed && <p className="warning">模拟暂不可用，请稍后重试。</p>}
    {result && !failed && <div className="sim-result" aria-live="polite">
      <Bar label="当前风险" value={result.baseline} tone="current" />
      <Bar label="模拟后风险" value={result.simulated} tone="simulated" />
      <p className="sim-delta">绝对下降 {formatPoints(result.absolute_reduction)} 个百分点（相对下降 {result.relative_reduction_percent}%）</p>
      <p className="sim-note">{result.note}</p>
    </div>}
    <p className="sim-note">拖动滑块只做公式估算，不会修改你的健康档案。</p>
  </div>;
}

export function SimulationPanel({ profile }: { profile?: Profile }) {
  const complete = !!profile && REQUIRED.every((key) => profile[key] !== undefined && profile[key] !== null);
  if (!complete) return <div className="trace-empty"><span aria-hidden="true">⇅</span><h3>看看改变能带来什么</h3><p>先完成一次风险评估，再拖动滑块模拟血压、血脂和戒烟的效果。</p></div>;
  const age = Number(profile.age);
  if (age < 30 || age > 74) return <div className="trace-empty"><h3>暂时无法模拟</h3><p>风险模型只适用于 30–74 岁。</p></div>;
  // 档案变了就重新挂载，滑块回到新的当前值
  return <SimulationForm key={JSON.stringify(profile)} profile={profile} />;
}
