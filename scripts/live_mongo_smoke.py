"""Step 5 · 真实 MongoDB 端到端冒烟（WSL Docker 实例 127.0.0.1:27017）。

覆盖：列连接 / find+LIMIT / insertOne→find 往返 / updateMany 计数 /
drop 被级别拦 / deleteMany 空 filter 升 DANGER 被拦 / describe 字段采样 /
坏 JSON 不回显 / 审计。用法：python scripts/live_mongo_smoke.py
"""
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, ".env"))

import pymongo  # noqa: E402
import plugins.mongoPlugin  # noqa: E402,F401 触发注册
from core import app, audit  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def guard(fn):
    return app.tool_guard(fn)


def op(**kw):
    return json.dumps(kw)


def main():
    app.bootstrap(conf_dir=os.path.join(ROOT, "conf"),
                  rules_dir=os.path.join(ROOT, "rules"),
                  logs_dir=os.path.join(ROOT, "logs"))
    client = pymongo.MongoClient("mongodb://127.0.0.1:27017")
    coll = client["smokedb"]["users"]
    coll.drop()
    coll.insert_many([{"name": "alice", "age": 30, "tags": ["a", "b"]},
                      {"name": "bob", "age": 25, "addr": {"city": "BJ"}}])
    print("== Step 5 真实 MongoDB 冒烟 ==")
    try:
        # 1 列连接（dialect 无关底座自动识别新注册插件）
        res = guard(app.impl_list_connections)()
        conn = next((c for c in res["connections"] if c["dialect"] == "mongo"), None)
        check("列到 mongo 连接 7.x", conn and conn["version"].startswith("7"),
              conn["version"] if conn else "")
        check("能力含 document/entity/describe",
              conn and {"document", "entity", "describe"} <= set(conn["capabilities"]))
        check("operation_hint 提示 JSON", conn and "JSON" in conn["operation_hint"])

        name = conn["name"]

        # 2 find + limit
        res = guard(app.impl_query)(name, op(collection="users", method="find"),
                                    None, 1)
        check("find ok 且 LIMIT 生效",
              res.get("ok") and res["row_count"] == 1, str(res.get("rows")))
        check("嵌套数组/对象序列化为 JSON 串",
              res.get("ok") and any(isinstance(c, str) and c[:1] in ("{", "[")
                                    for r in res["rows"] for c in r))

        # 3 insertOne → find 往返
        res = guard(app.impl_exec)(name, op(
            collection="users", method="insertOne",
            document={"name": "carol", "age": 40}))
        check("insertOne 放行 affected=1",
              res.get("ok") and res["affected"] == 1, str(res))
        res = guard(app.impl_query)(name, op(
            collection="users", method="find", filter={"name": "carol"}))
        check("insertOne→find 往返", res.get("ok") and res["row_count"] == 1)

        # 4 updateMany 计数
        res = guard(app.impl_exec)(name, op(
            collection="users", method="updateMany",
            filter={"age": {"$gte": 30}}, update={"$set": {"vip": True}}))
        check("updateMany affected=2",
              res.get("ok") and res["affected"] == 2, str(res.get("affected")))

        # 5 能力门：list/describe 走通（mongo 声明 entity/describe）
        res = guard(app.impl_list_tables)(name, "smokedb")
        check("list_tables 含 users", res.get("ok") and "users" in res["entities"])
        res = guard(app.impl_describe_table)(name, "users", "smokedb")
        fnames = [f["name"] for f in res.get("describe", {}).get("fields", [])] \
            if res.get("ok") else []
        check("describe 采样字段", {"name", "age", "tags", "addr", "vip"} <= set(fnames),
              str(fnames))

        # 6 drop 被级别拦（write 连接，drop 属 DANGER）
        res = guard(app.impl_exec)(name, op(collection="users", method="drop"))
        check("drop 被拦", res["ok"] is False and "danger" in res["error"],
              res.get("error"))

        # 7 deleteMany 空 filter → DANGER 被拦（特殊判定）
        res = guard(app.impl_exec)(name, op(collection="users", method="deleteMany"))
        check("deleteMany 空 filter 升 DANGER 被拦",
              res["ok"] is False and "danger" in res["error"], res.get("error"))

        # 8 坏 JSON 不回显
        res = guard(app.impl_exec)(name, '{"method":"insertOne","pass":"TOPSECRET123')
        check("坏 JSON 拒绝且无回显",
              res["ok"] is False and "TOPSECRET123" not in str(res), str(res))

        # 9 countDocuments 只读
        res = guard(app.impl_query)(name, op(collection="users",
                                             method="countDocuments", filter={}))
        check("countDocuments=3", res.get("ok") and res["rows"][0][0] == 3,
              str(res.get("rows")))

        # 10 审计
        scon = sqlite3.connect(os.path.join(ROOT, "logs", "operation_log.db"))
        n = scon.execute("SELECT COUNT(*) FROM operation_log WHERE dialect='mongo'"
                         " AND status='denied'").fetchone()[0]
        scon.close()
        check("mongo denied 审计 >=2", n >= 2, f"n={n}")
    finally:
        coll.drop()
        app.get_manager().close_all()
        audit.get_logger().close()

    print("== 结果 ==")
    if FAILS:
        print(f"  {len(FAILS)} 项失败：{FAILS}")
        sys.exit(1)
    print("  全部通过 ✅")


if __name__ == "__main__":
    main()
