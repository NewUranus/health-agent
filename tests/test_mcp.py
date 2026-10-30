"""直接用 MCP 客户端连 mcp_server，确认工具真的通过 MCP 暴露。"""

import asyncio
import json
import sys

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from conftest import ROOT


def _payload(result):
    data = json.loads(result.content[0].text)
    if isinstance(data, dict) and set(data) == {"result"}:
        data = data["result"]
    return data


async def _session_run(fn):
    params = StdioServerParameters(command=sys.executable, args=["-m", "mcp_server.server"], cwd=str(ROOT))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return await fn(s)


def test_tools_listed():
    async def fn(s):
        return {t.name for t in (await s.list_tools()).tools}

    assert {"cvd_risk", "bmi", "bp_category"} <= asyncio.run(_session_run(fn))


def test_cvd_risk_via_mcp():
    args = {
        "sex": "male",
        "age": 55,
        "total_chol_mmol": 5.5,
        "hdl_mmol": 1.3,
        "sbp": 140,
        "bp_treated": False,
        "smoker": False,
        "diabetes": False,
    }

    async def fn(s):
        return await s.call_tool("cvd_risk", args)

    res = asyncio.run(_session_run(fn))
    assert not res.is_error
    data = _payload(res)
    assert data["risk_percent"] == pytest.approx(13.45, abs=0.01)
    assert data["level"] == "intermediate"


def test_bmi_and_bp_via_mcp():
    async def fn(s):
        return (
            await s.call_tool("bmi", {"height_cm": 172, "weight_kg": 80}),
            await s.call_tool("bp_category", {"sbp": 140, "dbp": 90}),
        )

    b, p = asyncio.run(_session_run(fn))
    assert _payload(b)["category"] == "超重"
    assert _payload(p)["category"] == "1级高血压"


def test_invalid_args_is_error_not_crash():
    args = {
        "sex": "male",
        "age": 80,
        "total_chol_mmol": 5.5,
        "hdl_mmol": 1.3,
        "sbp": 140,
        "bp_treated": False,
        "smoker": False,
        "diabetes": False,
    }

    async def fn(s):
        bad = await s.call_tool("cvd_risk", args)
        good = await s.call_tool("bp_category", {"sbp": 110, "dbp": 70})
        return bad, good

    bad, good = asyncio.run(_session_run(fn))
    assert bad.is_error
    assert _payload(good)["category"] == "正常"
