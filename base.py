"""omni-db-mcp 启动入口（SPEC §4：很薄，~20 行）。

职责：加载 .env → import 插件触发注册（仅此处 import 插件，§4.2）→
装配底座 → stdio 启动 MCP server。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")   # Windows 控制台 GBK 兜底
except Exception:
    pass

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import plugins.mysqlPlugin   # noqa: F401  ← 注册 @register("mysql")
import plugins.redisPlugin   # noqa: F401  ← 注册 @register("redis")
import plugins.mongoPlugin   # noqa: F401  ← 注册 @register("mongo")

from core import app

if __name__ == "__main__":
    root = os.path.dirname(os.path.abspath(__file__))
    app.bootstrap(conf_dir=os.path.join(root, "conf"),
                  rules_dir=os.path.join(root, "rules"),
                  logs_dir=os.path.join(root, "logs"))
    app.run()
