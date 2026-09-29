# omni-db-mcp

一个通用的数据库连接器 MCP 服务器。让 AI 用一个入口访问所有数据库，**加一种新类型的库，只需要加一个插件文件**。

- 已实现：MySQL（5.7 / 8.0 同一个类）· Redis · MongoDB
- 技术栈：Python 3.11+（本机 3.12.8 实测）
- 状态：**v1.0.0 已实现，验收全部通过**（61 项离线测试 + 三方言真库冒烟 + MCP 端到端）

---

## 给使用者（人或 AI）

**先调 `db_list_connections`**——它返回每个连接的类型、版本、生效权限级别、能力列表和 `operation_hint`（告诉你 operation 该传什么格式），照着 hint 传就行。

**架构一句话**：

> 底座是一个不认识任何数据库的分流器；数据库的一切细节都关在插件里，靠 `BasePlugin` 这一道门进出。

| 层 | 依据 | 是否认识数据库 |
|---|---|---|
| 一级分流 | 工具名 → 底座能力模块 | 完全不认识 |
| 二级分流 | 连接别名 → 插件实例 | 到这里才知道是 MySQL |

## 5 个工具

| 工具 | 用途 | operation 传什么 |
|---|---|---|
| `db_list_connections` | 列出所有连接（版本/级别/能力/hint） | — |
| `db_list_tables` | 列表 / 集合 / Redis key 前缀（按能力门控） | — |
| `db_describe_table` | 表结构 / 集合字段采样 / key 类型+TTL（按能力门控） | — |
| `db_query` | **只读**操作，超过 readonly 一律拒绝 | SQL / Redis 命令 / Mongo JSON |
| `db_exec` | 写入与结构变更（受级别+规则管控） | 同上 |

所有工具失败时返回 `{"ok": false, "error": "…", "suggestion": "…"}`，绝不抛 traceback。

## 快速开始

```bash
# 1. 安装依赖（本机已装可跳过）
python -m pip install -r requirements.txt

# 2. 配置密码
copy .env.example .env        # 填入真实密码（此文件不进 Git）

# 3. 按需编辑连接（一类库一个文件，顶层 key 就是连接别名）
#    conf/mysqlConfig.yaml / conf/redisConfig.yaml / conf/mongoConfig.yaml

# 4. 启动（stdio MCP server）
python base.py
```

### 接入 MCP 客户端

```json
{
  "mcpServers": {
    "omni-db": {
      "type": "stdio",
      "command": "python",
      "args": ["D:/QwenWork/myProjects/omni_db_mcp/base.py"],
      "cwd": "D:/QwenWork/myProjects/omni_db_mcp"
    }
  }
}
```

> **`cwd` 必须指向项目目录**：启动时要从该目录加载 `.env`（密码）与 `conf/`。
> 缺了它，所有 `${env:VAR}` 会报"未设置"。
>
> 千问办公：自定义 stdio 连接器需在「设置 → 连接器」页面手动粘贴以上 JSON。

## 权限与安全模型

**三级权限**（默认 `readonly`）：

| 级别 | 允许 | 判定方式 |
|---|---|---|
| `readonly` | 元数据、查询 | MySQL 用 sqlglot 解析 AST；Redis 查命令表；Mongo 查方法表 |
| `write` | + INSERT / UPDATE / DELETE | 未知命令/语句**默认按 danger 拒绝** |
| `danger` | + DDL / DROP / 危险命令 | |

**规则包**（`rules/*.yaml`）四层合成：内置默认 → 全局包 → 连接挂载包 → 连接内联设置。
只能**收紧**（max_level 调低 / max_rows 调小 / deny 并集）；放宽有且只有两个口：

- 连接内联写 `level: write` —— 属主授权声明，直接生效；
- 连接写 `unsafe: true` —— 允许其挂载的规则包放宽（全系统唯一包级放宽开关）。

**双保险**：生产连接建议同时 `level: readonly` + 数据库只读账号（见 conf 示例注释）。

**审计**：每次操作（含被拒绝的）双写 `logs/operation_log.db`（SQLite，可查统计）与
`logs/audit-YYYY-MM-DD.jsonl`（按天轮转）。日志含真实 SQL，**不进 Git**。

## 测试与验证

```bash
python -m unittest discover -s tests -v        # 61 项离线测试（不依赖任何真库）
python scripts/live_mysql_smoke.py             # 真库端到端（本机 MySQL 8.0）
python scripts/live_redis_smoke.py             # （WSL Docker Redis 7）
python scripts/live_mongo_smoke.py             # （WSL Docker Mongo 7）
python scripts/mcp_client_check.py             # 起 base.py + 官方客户端走 MCP 协议
```

## 加一个新库（如 PostgreSQL）

```
plugins/pgPlugin.py        ← 新增（实现 BasePlugin 契约，见 docs/PLUGIN_GUIDE.md）
conf/pgConfig.yaml         ← 新增
base.py                    ← 加一行 import（唯一改动）
```

底座（`core/`）与已有插件**零改动**——本项目验收标准之一，已实测。

## 目录结构

```
omni_db_mcp/
├─ base.py                启动入口（~30 行）
├─ basePlugin.py          插件契约：能力常量 + 数据结构 + 异常 + 抽象基类 + 注册表
├─ core/                  底座：errors / policy / audit / connections / app
├─ plugins/               mysqlPlugin / redisPlugin / mongoPlugin
├─ conf/                  每类库一个配置（密码走 ${env:}）
├─ rules/                 规则包（prod-safe / dev-loose）
├─ tests/                 mockPlugin + 离线测试（61 项）
├─ scripts/               真库冒烟 + MCP 端到端检查
├─ docs/                  DESIGN.html（设计意图）+ PLUGIN_GUIDE.md（插件手册）
├─ logs/                  审计日志（运行后自动创建，不进 Git）
└─ .env                   密码（不进 Git）
```

## 文档导航

| 文档 | 内容 |
|---|---|
| [SPEC.md](./SPEC.md) | 实施规格书（与代码同步，含实现注记） |
| [docs/DESIGN.html](./docs/DESIGN.html) | 设计意图：为什么这样设计 |
| [docs/PLUGIN_GUIDE.md](./docs/PLUGIN_GUIDE.md) | 插件开发手册：从零加一种新库 |
| [FAQ.md](./FAQ.md) | 常见问题（被拦了怎么办 / 日志在哪 / 怎么放开写） |
| [IMPLEMENTATION_NOTES.md](./IMPLEMENTATION_NOTES.md) | 实现偏差与决策记录（11 处） |
| [CHANGELOG.md](./CHANGELOG.md) | 版本记录 |

## 安全须知

| 文件 | 是否可提交 Git |
|---|---|
| `conf/*.yaml` | ✅ 可以（密码走 `${env:}`；含内网地址，公开仓库注意） |
| `rules/*.yaml` | ✅ 可以（纯规则） |
| `.env` | ❌ **不可**（真实密码） |
| `logs/` | ❌ **不可**（含真实 SQL） |
| `backup/` | ❌ 不进 Git |

## 已知边界

- 明确不支持：时序 / 图 / 向量 / 异步数仓（会破坏二维表契约，见 SPEC §2.3）
- MySQL 5.7 的 CTE / 窗口函数拦截已离线单测覆盖（本机无 5.7 实例）；8.0 路径已真库验证
- Redis `KEYS` 即使连接为 danger 级也**不实际执行**（返回空结果 + SCAN 建议），双保险
- Mongo 的 `renameCollection` / `convertToCapped` / `shutdown` 只做分级识别，执行面未开放
