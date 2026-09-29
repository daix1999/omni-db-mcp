"""MCP 端到端验证（SPEC §19 验收项 10）：以 stdio 启动 base.py，
用官方客户端真实走一遍 MCP 协议：初始化 → 发现 5 个 db_* 工具 → 调用。
"""
import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


async def main():
    params = StdioServerParameters(
        command=sys.executable,
        args=[os.path.join(ROOT, "base.py")],
        cwd=ROOT,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            check("协议初始化", bool(info.server_info.name),
                  info.server_info.name)

            tools = (await session.list_tools()).tools
            names = sorted(t.name for t in tools)
            expect = sorted(["db_list_connections", "db_list_tables",
                             "db_describe_table", "db_query", "db_exec"])
            check("发现 5 个 db_* 工具", names == expect, str(names))

            res = await session.call_tool("db_list_connections", {})
            payload = json.loads(res.content[0].text)
            conns = {c["name"]: c for c in payload.get("connections", [])}
            check("列到 3 方言连接",
                  {"local-test", "local-redis", "local-mongo"} <= set(conns),
                  str(sorted(conns)))
            check("MySQL 版本 8.0.46",
                  conns.get("local-test", {}).get("version", "").startswith("8.0"))

            res = await session.call_tool("db_query", {
                "connection": "local-test",
                "operation": "SELECT 1 AS one, 'mcp链路' AS tag", "limit": 10})
            payload = json.loads(res.content[0].text)
            check("db_query 真库往返",
                  payload.get("ok") and payload["rows"][0][1] == "mcp链路",
                  str(payload.get("rows")))

            res = await session.call_tool("db_exec", {
                "connection": "local-test",
                "operation": "DROP DATABASE mysql"})      # 必须被级别拦
            payload = json.loads(res.content[0].text)
            check("危险操作被拦且无 traceback",
                  payload.get("ok") is False and "danger" in payload["error"],
                  payload.get("error"))

            res = await session.call_tool("db_query", {
                "connection": "local-redis", "operation": "PING"})
            payload = json.loads(res.content[0].text)
            check("redis PING 走同一工具面", payload.get("ok") is True, str(payload))

    print("== 结果 ==")
    if FAILS:
        print(f"  {len(FAILS)} 项失败：{FAILS}")
        sys.exit(1)
    print("  全部通过 ✅")


if __name__ == "__main__":
    asyncio.run(main())
