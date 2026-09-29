"""Step 4 · 真实 MySQL 端到端冒烟（SPEC §18 Step 4：只加配置、零代码改动）。

直接驱动 app 层（经 tool_guard，等价于 MCP 工具收到/返回的形状），对
本机 MySQL 8.0 的 test 库跑通：列连接 / 元数据 / 查询+LIMIT 注入 /
写入 / DROP 被级别拦 / 多语句被规则拦 / 审计落库。运行前用裸连接建测试表。

用法：python scripts/live_mysql_smoke.py
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

import pymysql  # noqa: E402
import plugins.mysqlPlugin  # noqa: E402,F401 触发 @register("mysql")
from core import app, audit  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -> {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def guard(fn):
    return app.tool_guard(fn)


def setup_table():
    conn = pymysql.connect(host="127.0.0.1", port=3306, user="root",
                           password=os.environ["OMNI_MYSQL_LOCAL_PWD"],
                           database="test", charset="utf8mb4", autocommit=True)
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS omni_smoke")
        cur.execute("CREATE TABLE omni_smoke (id INT PRIMARY KEY,"
                    " name VARCHAR(32), amount DECIMAL(10,2))")
        cur.executemany("INSERT INTO omni_smoke VALUES (%s,%s,%s)",
                        [(1, "alpha", 10.5), (2, "beta", 20.0),
                         (3, "gamma", 30.25)])
    conn.close()


def teardown_table():
    conn = pymysql.connect(host="127.0.0.1", port=3306, user="root",
                           password=os.environ["OMNI_MYSQL_LOCAL_PWD"],
                           database="test", charset="utf8mb4", autocommit=True)
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS omni_smoke")
    conn.close()


def main():
    app.bootstrap(conf_dir=os.path.join(ROOT, "conf"),
                  rules_dir=os.path.join(ROOT, "rules"),
                  logs_dir=os.path.join(ROOT, "logs"))
    setup_table()
    print("== Step 4 真实 MySQL 端到端冒烟 ==")
    try:
        # 1 列连接
        r = guard(app.impl_list_connections)()
        conn = next((c for c in r["connections"] if c["name"] == "local-test"), None)
        check("list_connections 找到 local-test", conn is not None)
        check("版本探测 8.0.x", conn and conn["version"].startswith("8.0"),
              conn["version"] if conn else "")
        check("级别=write（内联授权生效）", conn and conn["level"] == "write",
              conn["level"] if conn else "")
        check("operation_hint 提示 SQL", conn and "SQL" in conn["operation_hint"])

        # 2 元数据
        r = guard(app.impl_list_tables)("local-test", "test")
        check("list_tables 含 omni_smoke",
              r.get("ok") and "omni_smoke" in r["entities"], str(r.get("entities")))
        r = guard(app.impl_describe_table)("local-test", "omni_smoke", "test")
        cols = [c["name"] for c in r.get("describe", {}).get("columns", [])] \
            if r.get("ok") else []
        check("describe 列齐全", r.get("ok") and set(cols) >= {"id", "name", "amount"},
              str(cols))

        # 3 查询 + LIMIT 注入
        r = guard(app.impl_query)("local-test", "SELECT id,name,amount FROM omni_smoke",
                                  None, 2)
        check("query ok", r.get("ok") is True, str(r.get("error")))
        check("row_count=2（limit 注入）", r.get("row_count") == 2)
        check("有 LIMIT 注入 notice", bool(r.get("notice")), str(r.get("notice")))
        check("Decimal 序列化为字符串",
              r.get("rows") and isinstance(r["rows"][0][2], (str, int, float)))

        # 4 写入（write 级别放行）
        r = guard(app.impl_exec)("local-test",
                                 "UPDATE omni_smoke SET name='ALPHA' WHERE id=1")
        check("UPDATE where 放行", r.get("ok") is True and r.get("affected") == 1,
              str(r.get("error")))

        # 5 DDL 被级别拦截（write 连接，DROP 需 danger）
        r = guard(app.impl_exec)("local-test", "DROP TABLE omni_smoke")
        check("DROP 被拦（需 danger）", r.get("ok") is False and "danger" in r.get("error", ""),
              r.get("error"))
        r = guard(app.impl_query)("local-test", "SELECT COUNT(*) FROM omni_smoke")
        check("DROP 未生效（表还在）", r.get("ok") and r["rows"][0][0] == 3,
              str(r.get("rows")))

        # 6 多语句被规则拦（dev-loose deny multi_statement）
        r = guard(app.impl_exec)("local-test", "SELECT 1; SELECT 2")
        check("多语句被 deny 拦截",
              r.get("ok") is False and "multi_statement" in r.get("error", ""),
              r.get("error"))

        # 7 错误信息不泄露 traceback / 本机路径
        r = guard(app.impl_query)("local-test", "SELECT * FROM no_such_table_xyz")
        check("错误友好且无 traceback",
              r.get("ok") is False and "Traceback" not in str(r) and ROOT not in str(r),
              r.get("error"))

        # 8 审计落库：ok / denied 都有
        import sqlite3
        scon = sqlite3.connect(os.path.join(ROOT, "logs", "operation_log.db"))
        denied = scon.execute("SELECT COUNT(*) FROM operation_log"
                              " WHERE status='denied'").fetchone()[0]
        ok = scon.execute("SELECT COUNT(*) FROM operation_log"
                          " WHERE status='ok'").fetchone()[0]
        scon.close()
        check("审计含 denied 记录(>=2)", denied >= 2, f"denied={denied}")
        check("审计含 ok 记录", ok >= 3, f"ok={ok}")
    finally:
        teardown_table()
        app.get_manager().close_all()
        audit.get_logger().close()

    print("== 结果 ==")
    if FAILS:
        print(f"  {len(FAILS)} 项失败：{FAILS}")
        sys.exit(1)
    print("  全部通过 ✅")


if __name__ == "__main__":
    main()
