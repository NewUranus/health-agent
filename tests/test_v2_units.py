"""V2 新增模块的单元测试：模拟工具、轨迹、术语、RRF、Function Calling、提示词。"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.function_calling import mcp_tools_to_openai, run_tool_loop
from app.prompts import load_prompt
from app.rag import Retriever, rrf_merge
from app.terms import expand_query
from app.tools_core import project_trajectory, simulate_intervention
from conftest import ROOT

SMOKER = dict(sex="male", age=55, total_chol_mmol=5.5, hdl_mmol=1.3, sbp=140, bp_treated=False, smoker=True, diabetes=False)
NON_SMOKER = {**SMOKER, "smoker": False}


def test_simulate_quit_smoking():
    r = simulate_intervention(**SMOKER, quit_smoking=True)
    assert r["baseline"] == pytest.approx(24.27, abs=0.01)
    assert r["simulated"] == pytest.approx(13.45, abs=0.01)
    assert r["absolute_reduction"] == pytest.approx(10.82, abs=0.01)
    assert r["relative_reduction_percent"] == pytest.approx(44.6, abs=0.1)
    assert "戒烟" in r["changes"]
    assert "估算" in r["note"]


def test_simulate_combined():
    r = simulate_intervention(**SMOKER, quit_smoking=True, sbp_target=120)
    assert r["simulated"] == pytest.approx(10.17, abs=0.01)
    assert r["absolute_reduction"] == pytest.approx(14.10, abs=0.01)
    assert len(r["changes"]) == 2


def test_simulate_no_change():
    r = simulate_intervention(**NON_SMOKER)
    assert r["baseline"] == r["simulated"]
    assert r["absolute_reduction"] == 0


def test_trajectory_default_years():
    r = project_trajectory(**NON_SMOKER)
    assert [(p["years"], p["age"]) for p in r["points"]] == [(0, 55), (5, 60), (10, 65)]
    assert [p["risk_percent"] for p in r["points"]] == pytest.approx([13.45, 17.18, 21.41], abs=0.01)
    assert "其他因素不变" in r["note"]


def test_trajectory_skips_beyond_model_range():
    r = project_trajectory(**{**NON_SMOKER, "age": 72})
    assert [p["age"] for p in r["points"]] == [72]


@pytest.mark.parametrize(
    "text, term",
    [
        ("高压老是偏高", "收缩压"),
        ("好胆固醇低", "高密度脂蛋白"),
        ("心梗有什么症状", "心肌梗死"),
        ("中风怎么办", "脑卒中"),
    ],
)
def test_expand_query(text, term):
    out = expand_query(text)
    assert text in out and term in out


def test_expand_query_no_match():
    assert expand_query("今天天气不错") == "今天天气不错"


def test_rrf_merge():
    assert rrf_merge([["a", "b", "c"], ["c", "a", "d"]]) == ["a", "c", "b", "d"]


def test_hybrid_uses_embedder():
    # BM25 查不到 xyzzy，只有向量能把它带到戒烟那一段
    def embedder(texts):
        return [[1.0 if ("戒烟" in t or "xyzzy" in t) else 0.0, 0.1] for t in texts]

    r = Retriever.from_dir(ROOT / "corpus", embedder=embedder)
    assert r.search("xyzzy", k=3)[0].id == "smoking_cessation"


def test_mcp_tools_to_openai():
    tool = SimpleNamespace(name="bmi", description="算 BMI", input_schema={"type": "object", "properties": {}})
    assert mcp_tools_to_openai([tool]) == [
        {"type": "function", "function": {"name": "bmi", "description": "算 BMI", "parameters": {"type": "object", "properties": {}}}}
    ]


class ScriptedLLM:
    """按脚本依次返回消息，记录绑定情况和收到的消息。"""

    def __init__(self, script):
        self.script = list(script)
        self.bound = []
        self.unbound_calls = 0
        self.seen = []

    def bind_tools(self, tools):
        self.bound.append(tools)
        outer = self

        class Bound:
            async def ainvoke(self, messages):
                outer.seen.append(list(messages))
                return outer.script.pop(0)

        return Bound()

    async def ainvoke(self, messages):
        self.unbound_calls += 1
        self.seen.append(list(messages))
        return self.script.pop(0)


def _tool_call(name, args, id_="1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": id_, "type": "tool_call"}])


def test_tool_loop_executes_and_feeds_back():
    llm = ScriptedLLM([_tool_call("bmi", {"height_cm": 172, "weight_kg": 80}), AIMessage(content="BMI 是 27.0")])
    calls = []

    async def call_tool(name, args):
        calls.append((name, args))
        return {"bmi": 27.0, "category": "超重"}

    final, trace = asyncio.run(run_tool_loop(llm, [{"x": 1}], call_tool, [HumanMessage("算一下")]))
    assert final.content == "BMI 是 27.0"
    assert calls == [("bmi", {"height_cm": 172, "weight_kg": 80})]
    assert trace == [{"tool": "bmi", "args": {"height_cm": 172, "weight_kg": 80}, "result": {"bmi": 27.0, "category": "超重"}}]
    last = llm.seen[-1][-1]
    assert isinstance(last, ToolMessage) and last.tool_call_id == "1"
    assert json.loads(last.content)["category"] == "超重"


def test_tool_loop_error_goes_back_to_model():
    llm = ScriptedLLM([_tool_call("cvd_risk", {"age": 80}), AIMessage(content="年龄超出范围")])

    async def call_tool(name, args):
        raise ValueError("年龄超出 30-74")

    final, trace = asyncio.run(run_tool_loop(llm, [], call_tool, [HumanMessage("算风险")]))
    assert final.content == "年龄超出范围"
    assert "年龄超出 30-74" in trace[0]["result"]["error"]
    assert "年龄超出 30-74" in llm.seen[-1][-1].content


def test_tool_loop_stops_at_max_steps():
    llm = ScriptedLLM([_tool_call("bmi", {}, "1"), _tool_call("bmi", {}, "2"), AIMessage(content="到此为止")])

    async def call_tool(name, args):
        return {}

    final, trace = asyncio.run(run_tool_loop(llm, [], call_tool, [HumanMessage("x")], max_steps=2))
    assert final.content == "到此为止"
    assert len(trace) == 2
    assert llm.unbound_calls == 1


@pytest.mark.parametrize("name", ["advise", "answer", "extract", "intent"])
def test_prompts_are_versioned(name):
    p = load_prompt(name)
    assert p.version
    assert p.text.strip()
    assert "version:" not in p.text
