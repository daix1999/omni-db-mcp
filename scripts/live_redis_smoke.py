"""Step 5 · 真实 Redis 端到端冒烟（WSL Docker 实例 127.0.0.1:6379）。

覆盖：列连接 / GET 标量 / SET→GET 往返 / SCAN 前缀 / KEYS 被级别拦 /
FLUSHDB 被拦 / 能力门（db_list_tables 对 Redis 应给出 SCAN 提示）/ 审计。

用法：python scripts/live_redis_smoke.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, ".env"))

import redis  # noqa: E402
import plugins.redisPlugin  # noqa: E402,F401 触发注册
from core import app, audit  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def guard(fn):
    return app.tool_guard(fn)


def main():
    app.bootstrap(conf_dir=os.path.join(ROOT, "conf"),
                  rules_dir=os.path.join(ROOT, "rules"),
                  logs_dir=os.path.join(ROOT, "logs"))
    r = redis.Redis(host="127.0.0.1", port=6379)
    r.mset({"smoke:user:1": "alice", "smoke:user:2": "bob"})
    print("== Step 5 真实 Redis 冒烟 ==")
    try:
        # 1 列连接
        res = guard(app.impl_list_connections)()
        conn = next((c for c in res["connections"] if c["name"] == "local-redis"), None)
        check("列到 local-redis 7.x", conn and conn["version"].startswith("7"),
              conn["version"] if conn else "")
        check("能力不含 entity/describe",
              conn and "entity" not in conn["capabilities"]
              and "describe" not in conn["capabilities"],
              str(conn["capabilities"]) if conn else "")
        check("operation_hint 是 Redis 命令",
              conn and "Redis" in conn["operation_hint"])

        # 2 能力门：db_list_tables 应被拦并给通用探数建议
        res = guard(app.impl_list_tables)("local-redis")
        check("db_list_tables 能力门拦截+建议",
              res["ok"] is False and "不支持" in res["error"]
              and "db_query" in (res.get("suggestion") or ""), res.get("error"))

        # 3 GET 标量
        res = guard(app.impl_query)("local-redis", "GET smoke:user:1")
        check("GET 返回 alice",
              res.get("ok") and res["rows"] == [["alice"]], str(res))

        # 4 SET 往返（db_exec, write 级）
        res = guard(app.impl_exec)("local-redis", "SET smoke:cfg:hello world")
        check("SET 放行", res.get("ok"), str(res.get("error")))
        res = guard(app.impl_query)("local-redis", "GET smoke:cfg:hello")
        check("SET→GET 往返一致",
              res.get("ok") and res["rows"] == [["world"]], str(res.get("rows")))

        # 5 HGETALL 规整为 field/value
        guard(app.impl_exec)("local-redis", "HSET smoke:h1 f1 v1 f2 v2")
        res = guard(app.impl_query)("local-redis", "HGETALL smoke:h1")
        ok_hash = res.get("ok") and res["columns"] == ["field", "value"] \
            and sorted(map(tuple, res["rows"])) == [("f1", "v1"), ("f2", "v2")]
        check("HGETALL → field/value 表", ok_hash, str(res.get("rows")))

        # 6 KEYS 被拦（DANGER > write）
        res = guard(app.impl_exec)("local-redis", "KEYS *")
        check("KEYS 被拦且提示 SCAN",
              res["ok"] is False and "SCAN" in str(res), str(res.get("error")))

        # 7 FLUSHDB 被拦
        res = guard(app.impl_exec)("local-redis", "FLUSHDB")
        check("FLUSHDB 被拦", res["ok"] is False and "危险" in res.get("error", ""),
              res.get("error"))

        # 8 未知命令默认拒绝
        res = guard(app.impl_exec)("local-redis", "DOESNOTTTHING x")
        check("未知命令按 DANGER 拦", res["ok"] is False, res.get("error"))

        # 9 审计
        import sqlite3
        scon = sqlite3.connect(os.path.join(ROOT, "logs", "operation_log.db"))
        n = scon.execute("SELECT COUNT(*) FROM operation_log WHERE dialect='redis'"
                         " AND status='denied'").fetchone()[0]
        scon.close()
        check("redis denied 审计 >=3", n >= 3, f"n={n}")
    finally:
        for k in r.keys("smoke:*"):
            r.delete(k)
        app.get_manager().close_all()
        audit.get_logger().close()

    print("== 结果 ==")
    if FAILS:
        print(f"  {len(FAILS)} 项失败：{FAILS}")
        sys.exit(1)
    print("  全部通过 ✅")


if __name__ == "__main__":
    main()
