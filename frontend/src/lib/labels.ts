export const profileLabels: Record<string, string> = {
  sex: "性别", age: "年龄", sbp: "收缩压", dbp: "舒张压", total_chol: "总胆固醇",
  hdl: "HDL", smoker: "吸烟", diabetes: "糖尿病", bp_treated: "服降压药",
  height_cm: "身高", weight_kg: "体重",
};

export const nodeLabels: Record<string, string> = {
  safety_check: "急症筛查", emergency: "紧急就医指引", intake: "整理健康档案",
  supervisor: "规划下一步", ask_missing: "补充问诊", risk_assess: "计算健康风险",
  retrieve: "检索指南依据", advise: "生成干预建议", verify: "校验建议与引用",
  followup: "制定随访计划", simulate: "模拟干预效果", checkin: "对比复查结果", answer: "回答健康问题",
};

export const agents: Record<string, [string, string]> = {
  orchestrator: ["协调 Agent", "#82988b"], intake_agent: ["问诊 Agent", "#82a1ac"],
  assessment_agent: ["评估 Agent", "#6484ac"], intervention_agent: ["干预 Agent", "#6ba28b"],
  followup_agent: ["随访 Agent", "#ad986c"], simulation_agent: ["模拟 Agent", "#9784b2"],
  checkin_agent: ["复查 Agent", "#b08c74"], qa_agent: ["问答 Agent", "#7c9aaf"],
};

export const fieldNames = (keys: string[]) => keys.map((key) => profileLabels[key] ?? key).join("、");
