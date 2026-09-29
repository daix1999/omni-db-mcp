# omni-db-mcp · 实施规格书

> 本文档面向**实现者**（人类或 AI 编码工具）。目标是：读完本文档即可完整实现本项目，无需再询问设计意图。
>
> 文档中「**必须**」= 硬性要求，不得省略或变通；「**建议**」= 推荐做法，可自主决定；「**不得**」= 禁止。
>
> **实现状态（2026-09-28）**：本规格已按 v1.0.0 完整实现并通过 §19 全部验收。
> 与初稿存在少量受控偏差/拍板修订，均已就地以「**实现注记**」标入正文，
> 汇总见 [IMPLEMENTATION_NOTES.md](./IMPLEMENTATION_NOTES.md)。阅读时以注记后的语义为准。

---

## 目录

1. [任务说明](#1-任务说明)
2. [范围与边界](#2-范围与边界)
3. [技术栈与依赖](#3-技术栈与依赖)
4. [目录结构](#4-目录结构)
5. [核心契约](#5-核心契约)
6. [数据结构定义](#6-数据结构定义)
7. [异常体系](#7-异常体系)
8. [底座实现规格](#8-底座实现规格)
9. [规则包与分级](#9-规则包与分级)
10. [审计日志](#10-审计日志)
11. [MySQL 插件规格](#11-mysql-插件规格)
12. [Redis 插件规格](#12-redis-插件规格)
13. [MongoDB 插件规格](#13-mongodb-插件规格)
14. [配置文件规格](#14-配置文件规格)
15. [MCP 工具规格](#15-mcp-工具规格)
16. [健壮性要求（7 条）](#16-健壮性要求7-条)
17. [测试要求](#17-测试要求)
18. [实施顺序](#18-实施顺序)
19. [验收标准](#19-验收标准)
20. [禁止事项](#20-禁止事项)

---

## 1. 任务说明

实现一个名为 `omni-db-mcp` 的 MCP（Model Context Protocol）服务器。

**它做什么**：为 AI 提供一个统一的数据库访问入口。AI 通过 5 个工具操作所有已配置的数据库，不关心数据库类型、部署位置、版本差异。

**核心设计**：底座 + 插件。

- **底座**：负责入口、分流、规则、分级、审计、错误处理。**底座代码中不得出现任何数据库类型判断**（不得有 `if mysql` / `if redis` 之类的分支）。
- **插件**：每种数据库类型一个插件类。插件负责连接、执行、版本探测、操作分级判定。
- **配置**：每种数据库类型一个 YAML 文件，声明该类库的所有连接。
- **规则包**：可复用的安全规则，可全局套用或挂到单个连接。

**首版覆盖**：MySQL（5.7 / 8.0）、Redis、MongoDB。

---

## 2. 范围与边界

### 2.1 首版必须实现

| 类型 | 库 | 说明 |
|---|---|---|
| 关系型 | MySQL 5.7 / 8.0 | 同一插件类服务两个版本，禁止按版本拆类 |
| 键值型 | Redis | |
| 文档型 | MongoDB | |

### 2.2 架构需支持但首版不实现

PostgreSQL、SQLite、SQL Server、达梦、金仓、OceanBase、GaussDB、Elasticsearch、ClickHouse。

**要求**：底座与契约必须让这些库**只需新增插件文件即可接入**，不得为了它们提前增加抽象层。

### 2.3 明确不支持

时序库（InfluxDB / Prometheus）、图数据库（Neo4j）、向量库（Milvus / Qdrant）、异步数仓（Hive / Trino / BigQuery）。

**原因**：这些库的返回结构不是二维表，或执行模型是"提交-轮询"异步模式，会破坏契约。**不得**为其改造 `QueryResult` 或 `execute` 签名。

---

## 3. 技术栈与依赖

**语言**：Python 3.11+

**依赖清单**（写入 `requirements.txt`）：

```
mcp>=1.2.0
pymysql>=1.1.0
redis>=5.0.0
pymongo>=4.6.0
sqlglot>=25.0.0
PyYAML>=6.0
python-dotenv>=1.0.0
```

**说明**：

- `mcp`：官方 MCP SDK，使用 `from mcp.server.fastmcp import FastMCP`
  > **实现注记**：本机安装的 mcp 为 2.x，`FastMCP` 已改名 `MCPServer`
  > （`from mcp.server.mcpserver import MCPServer`，装饰器与 `run()` API 兼容）。
  > `core/app.py` 以 try/except 双版本兼容 import，两种环境均可运行。
- `sqlglot`：SQL 解析，用于 MySQL 的分级判定与多语句检测
- `python-dotenv`：加载 `.env`

**建议**：为避免驱动版本冲突，可将数据库驱动分组到 `requirements-mysql.txt` / `requirements-redis.txt` / `requirements-mongo.txt`，按需安装。

---

## 4. 目录结构

```
omni-db-mcp/
├─ base.py                    启动入口（很薄）
├─ basePlugin.py              插件契约：能力常量 + 抽象基类 + 注册表
├─ core/
│  ├─ __init__.py
│  ├─ errors.py               异常体系 + 错误回调
│  ├─ policy.py               规则包加载 + 合成 + 分级判定
│  ├─ audit.py                审计日志
│  ├─ connections.py          连接管理 + 二级分流 + 保活重连
│  └─ app.py                  一级分流 + 切面链 + MCP 工具注册
├─ plugins/
│  ├─ __init__.py
│  ├─ mysqlPlugin.py
│  ├─ redisPlugin.py
│  └─ mongoPlugin.py
├─ conf/
│  ├─ mysqlConfig.yaml
│  ├─ redisConfig.yaml
│  └─ mongoConfig.yaml
├─ rules/
│  ├─ prod-safe.yaml
│  └─ dev-loose.yaml
├─ tests/
│  ├─ mockPlugin.py
│  └─ test_core.py
├─ logs/                      运行后自动创建
├─ .env                       不进 Git
├─ .env.example
├─ .gitignore
├─ README.md
├─ SPEC.md                    本文档
└─ requirements.txt
```

### 4.1 各文件职责

| 文件 | 职责 | 预估行数 |
|---|---|---|
| `base.py` | 加载 `.env`、import 各插件触发注册、启动 MCP server | ~20 |
| `basePlugin.py` | 能力常量池、`BasePlugin` 抽象类、`PLUGINS` 注册表、`@register` 装饰器 | ~90 |
| `core/errors.py` | 异常体系、错误回调注册与触发 | ~70 |
| `core/policy.py` | 加载规则包、四层合成、操作分级判定 | ~140 |
| `core/audit.py` | SQLite + JSONL 双写审计日志 | ~90 |
| `core/connections.py` | 加载配置、连接实例管理、二级分流、保活重连 | ~130 |
| `core/app.py` | 一级分流、切面链、5 个 MCP 工具、统一异常包装 | ~220 |
| `plugins/mysqlPlugin.py` | MySQL 插件 | ~220 |
| `plugins/redisPlugin.py` | Redis 插件 | ~160 |
| `plugins/mongoPlugin.py` | MongoDB 插件 | ~170 |

**约束**：任何单文件不得超过 350 行。超过说明职责划分有问题，应拆分。

### 4.2 依赖方向（必须严格遵守，不得回环）

```
basePlugin.py        core/errors.py          ← 最底层，不 import 任何本地模块
        ↑
core/policy.py   core/audit.py   core/connections.py
        ↑
core/app.py
        ↑
base.py
```

- `basePlugin.py` 和 `core/errors.py` **不得** import 其他本地模块
- `core/connections.py` 从 `basePlugin.PLUGINS` 读注册表，**不得**直接 import 任何插件
- 仅 `base.py` 负责 import 插件以触发注册

---

## 5. 核心契约

### 5.1 `basePlugin.py` 完整内容

```python
"""插件契约：能力常量 + 抽象基类 + 注册表。插件作者只需 import 本文件。"""
from abc import ABC, abstractmethod

# ─────────────────────────────────────────────
# ① 能力名常量池（唯一真相来源，禁止在别处硬编码字符串）
# ─────────────────────────────────────────────
NAMESPACE   = "namespace"     # 有命名空间概念（MySQL 库 / Redis db / Mongo 库）
ENTITY      = "entity"        # 有实体概念（MySQL 表 / Mongo 集合）
DESCRIBE    = "describe"      # 能描述实体结构
SQL         = "sql"           # 操作是 SQL 语句
COMMAND     = "command"       # 操作是命令字符串（Redis）
DOCUMENT    = "document"      # 操作是文档查询（Mongo）
TRANSACTION = "transaction"   # 支持事务
EXPLAIN     = "explain"       # 支持执行计划预演

ALL_CAPABILITIES = frozenset({
    NAMESPACE, ENTITY, DESCRIBE, SQL, COMMAND, DOCUMENT, TRANSACTION, EXPLAIN,
})

# ─────────────────────────────────────────────
# ② 插件契约
# ─────────────────────────────────────────────
class BasePlugin(ABC):
    """所有数据库插件的抽象基类。

    子类必须设置：
        TYPE         插件类型标识，与 @register 的参数一致
        CONFIG_FILE  该插件的配置文件，位于 conf/ 目录下
        CAPABILITIES 能力集合，取值来自上方常量
    子类可选设置（普通类属性，非抽象方法，不属于方法面扩充）：
        REQUIRED_FIELDS  配置必须携带的顶层字段（缺失则启动期报错并指明连接名，
                         支撑 §8.3「缺少必须字段」检查）
        EXTRA_FIELDS     插件私有字段白名单（避免被 §8.3 的未知字段警告误伤）
    """

    TYPE: str = ""
    CONFIG_FILE: str = ""
    CAPABILITIES: frozenset[str] = frozenset()

    # ── 生命周期 ──
    @abstractmethod
    def connect(self) -> None:
        """建立连接。失败必须抛 ConnectionFailed。"""

    @abstractmethod
    def close(self) -> None:
        """关闭连接。不得抛异常。"""

    @abstractmethod
    def health_check(self) -> bool:
        """检查连接是否存活。返回 False 表示不可用。不得抛异常。"""

    # ── 元信息 ──
    @abstractmethod
    def server_info(self) -> "ServerInfo":
        """探测服务端信息（版本等）。实现内必须缓存结果，同一实例只探测一次。"""

    # ── 元数据 ──
    @abstractmethod
    def list_namespaces(self) -> list[str]:
        """列出命名空间。不支持该能力时返回空列表。"""

    @abstractmethod
    def list_entities(self, namespace: str | None = None) -> list[str]:
        """列出实体。
        - MySQL: 表名列表
        - Mongo: 集合名列表
        - Redis: key 前缀分组（如 "user:" / "session:"）
        不支持该能力时返回空列表。
        """

    @abstractmethod
    def describe(self, entity: str, namespace: str | None = None) -> dict:
        """描述实体结构。
        - MySQL: {columns: [...], indexes: [...], ddl: str}
        - Mongo: {fields: [...], sample_size: int}
        - Redis: {type: str, ttl: int, size: int}
        不支持该能力时抛 ConnectorError。
        """

    # ── 执行 ──
    @abstractmethod
    def execute(
        self,
        operation: str,
        params: list | None = None,
        limit: int | None = None,
    ) -> "QueryResult":
        """执行一次操作。

        `operation` 是不透明字符串，语义由各插件自行解释：
        - MySQL: SQL 语句
        - Redis: 命令字符串，如 "GET user:1"
        - Mongo: JSON 字符串，如 '{"collection":"users","filter":{}}'

        实现内必须注入 rows 上限（取 limit 与自身上限的较小值）。
        失败必须抛 QueryError，不得抛驱动原生异常。
        """

    @abstractmethod
    def classify(self, operation: str) -> "OperationSpec":
        """判定操作所需级别。只判定，不执行。

        必须基于结构化解析，不得仅做字符串匹配：
        - MySQL: 用 sqlglot 解析
        - Redis: 查命令名分级表
        - Mongo: 查方法名分级表
        """

    # ── 生命周期钩子（可选覆盖）──
    def on_error(self, exc: Exception, context: dict) -> None:
        """插件级错误钩子。默认空实现。实现内抛出的异常必须被底座吞掉。"""


# ─────────────────────────────────────────────
# ③ 注册表
# ─────────────────────────────────────────────
PLUGINS: dict[str, type[BasePlugin]] = {}


def register(type_name: str):
    """插件注册装饰器。

    用法：
        @register("mysql")
        class MySQLPlugin(BasePlugin): ...

    校验（必须实现）：
      - type_name 不得重复注册，重复则抛 ValueError
      - 注册的类必须声明 CAPABILITIES 与 CONFIG_FILE，否则抛 ValueError
      - CAPABILITIES 中的每一项必须在 ALL_CAPABILITIES 内，否则抛 ValueError
    """
```

### 5.2 契约方法汇总（共 9 个抽象方法）

| # | 方法 | 输入 | 输出 | 失败时 |
|---|---|---|---|---|
| 1 | `connect` | — | None | 抛 `ConnectionFailed` |
| 2 | `close` | — | None | 不得抛异常 |
| 3 | `health_check` | — | bool | 返回 False，不得抛异常 |
| 4 | `server_info` | — | `ServerInfo`（缓存） | 抛 `ConnectionFailed` |
| 5 | `list_namespaces` | — | `list[str]` | 不支持返回空列表 |
| 6 | `list_entities` | `namespace` | `list[str]` | 不支持返回空列表 |
| 7 | `describe` | `entity`, `namespace` | `dict` | 不支持抛 `ConnectorError` |
| 8 | `execute` | `operation`, `params`, `limit` | `QueryResult` | 抛 `QueryError` |
| 9 | `classify` | `operation` | `OperationSpec` | 抛 `ConnectorError` |

**不得**添加 `probe()` 方法。插件与配置文件的匹配由 `CONFIG_FILE` 声明完成，不需要探测。

> **实现注记**：`ConnectionConfig`（§6.5）实际定义在 `core/connections.py`；
> 插件通过 `BasePlugin.__init__(name, raw)` 接收连接别名与已解析配置。

---

## 6. 数据结构定义

**建议**全部定义在 `basePlugin.py` 中（因为插件需要 import 它们）。若拆分，需保证插件仍只 import `basePlugin`。

### 6.1 级别枚举

```python
from enum import IntEnum

class Level(IntEnum):
    READONLY = 1    # 元数据读取与数据查询
    WRITE    = 2    # INSERT / UPDATE / DELETE
    DANGER   = 3    # DDL / DROP / 无条件的写入 / 危险命令

LEVEL_NAMES = {Level.READONLY: "readonly", Level.WRITE: "write", Level.DANGER: "danger"}
```

### 6.2 ServerInfo

```python
@dataclass(frozen=True)
class ServerInfo:
    dialect: str                    # "mysql" / "redis" / "mongo"
    version: str                    # 原始版本字符串，如 "5.7.43"
    version_tuple: tuple[int, ...]  # 解析后的元组，如 (5, 7, 43)
    extra: dict = field(default_factory=dict)   # 插件可放额外信息
```

### 6.3 OperationSpec

```python
@dataclass(frozen=True)
class OperationSpec:
    level: Level
    action: str                  # "SELECT" / "DROP" / "GET" / "find" ...
    targets: tuple[str, ...]     # 涉及的表 / 集合 / key 模式
    reasons: tuple[str, ...]     # 判定理由，用于向 AI 解释，如 ("语句包含 DDL",)
```

### 6.4 QueryResult

```python
@dataclass
class QueryResult:
    kind: str = "table"          # 预留字段。首版只使用 "table"
    columns: list[str] | None = None
    rows: list[tuple] | None = None
    row_count: int = 0
    affected: int = 0
    elapsed_ms: int = 0
    truncated: bool = False      # 是否因超过上限被截断
    notice: str | None = None    # 如 "已自动注入 LIMIT 100"
```

**`kind` 字段的用途**：将来支持文档型 / 标量型返回时扩展取值（`"documents"` / `"scalar"`）。**首版必须保留该字段，只赋 `"table"`**。

### 6.5 ConnectionConfig

由 `core/connections.py` 从 YAML 加载后构造：

```python
@dataclass
class ConnectionConfig:
    name: str                       # 连接别名（YAML 顶层 key）
    dialect: str                    # 来自插件的 TYPE
    level: Level = Level.READONLY   # 该连接允许的最高级别
    rules: list[str] = field(default_factory=list)   # 挂载的规则包名
    unsafe: bool = False            # 是否允许规则放宽
    timeout: int = 30               # 单次执行超时（秒）
    connect_timeout: int = 10       # 建连超时（秒）
    raw: dict = field(default_factory=dict)          # 原始字段，交给插件解释
```

### 6.6 统一返回结构

所有 MCP 工具返回统一结构：

```python
# 成功
{"ok": True, ...业务字段...}

# 失败（禁止抛出未捕获异常）
{"ok": False, "error": "人类可读的错误说明", "suggestion": "可选，给出如何修正"}
```

---

## 7. 异常体系

**文件**：`core/errors.py`

> **实现注记**：四个异常类**本体定义在 `basePlugin.py`**，`core/errors.py`
> re-export 以保持本节导入路径兼容，并在该文件内实现 `ErrorContext` /
> `on_error` / `emit_error`。原因：插件必须抛 `ConnectionFailed`/`QueryError`，
> 而验收标准 #2 要求插件只 import `basePlugin`——两条规则只有让异常进入
> 契约文件才能同时满足。依赖方向仍无回环（errors → basePlugin 单向）。

```python
class ConnectorError(Exception):
    """所有本系统异常的基类。
    属性：message（给 AI 看的原因）、suggestion（可选，如何修正）
    """
    def __init__(self, message: str, suggestion: str | None = None): ...

class ConnectionFailed(ConnectorError):
    """连接建立失败、连接不可用。"""

class PolicyDenied(ConnectorError):
    """被规则或级别拦截。必须带 suggestion，说明如何调整配置。"""

class QueryError(ConnectorError):
    """执行出错（语法错误、超时、权限不足等）。"""
```

### 7.1 错误回调

```python
@dataclass
class ErrorContext:
    connection: str          # 连接别名
    operation: str | None    # 操作原文（可为 None，如建连失败时）
    level: str | None        # 判定级别名
    error: ConnectorError
    when: datetime

def on_error(cb: Callable[[ErrorContext], None]) -> None:
    """注册错误回调，可注册多个，按注册顺序调用。"""

def emit_error(ctx: ErrorContext) -> None:
    """触发所有已注册回调。"""
```

**硬性要求**：

- 回调执行**必须**包在 `try/except Exception` 中，回调内抛出的异常**必须被吞掉并记录到 stderr**
- 回调**不得**影响主流程的返回值或异常传播

---

## 8. 底座实现规格

### 8.1 一级分流（`core/app.py`）

工具名到能力模块的映射。**此层不得出现任何数据库类型的判断**。

| MCP 工具 | 去向 |
|---|---|
| `db_list_connections` | 连接管理 |
| `db_list_tables` | 元数据（→ `list_entities`） |
| `db_describe_table` | 元数据（→ `describe`） |
| `db_query` | 执行链路，级别上限 `READONLY` |
| `db_exec` | 执行链路，级别上限不限（由连接与规则决定） |

### 8.2 二级分流（`core/connections.py`）

**输入**：连接别名
**输出**：已连接、可用的插件实例

**必须实现**：

1. 启动时扫描 `conf/*.yaml`，按插件声明的 `CONFIG_FILE` 匹配加载
2. 惰性建连（第一次使用时才连接），避免启动时全量建连
3. 实例缓存：同一别名复用同一插件实例
4. 配置校验（见 8.3）
5. 连接保活与断线重连（见 16.2）

### 8.3 配置校验（必须实现，逐项检查）

| 检查项 | 行为 |
|---|---|
| 配置文件不存在 | 明确提示「未找到 conf/xxxConfig.yaml，请参考 .env.example 创建」 |
| YAML 语法错误 | 报错时附上文件名与行列位置 |
| 连接别名为空或含非法字符 | 报错。别名只允许 `[A-Za-z0-9_-]` |
| 缺少必须字段（如 MySQL 缺 `host`） | 明确报出缺哪个字段、在哪个连接下 |
| `${env:VAR}` 引用的变量未设置 | **必须报出变量名**，如「连接 crm-prod 引用的环境变量 OMNI_MYSQL_CRM_PWD 未设置」 |
| 出现未知字段（如把 `host` 写成 `hots`） | **输出警告，不得静默忽略**。警告内容含字段名与所在连接 |
| `level` 取值非法 | 报错，列出合法取值 |
| `rules` 引用的规则包不存在 | 报错，列出 `rules/` 下实际可用的规则包 |

**绝对禁止**：用 `None` 或缺省值静默填充失败字段后继续建连。

### 8.4 执行链路（`core/app.py`）

顺序**必须**如下，不得调整：

```
1. 参数校验（连接是否存在、operation 是否为空）
2. 规则合成（policy.resolve_rules(connection)）→ 得到生效规则
3. 分级判定（plugin.classify(operation)）→ 得到 OperationSpec
4. 级别校验：spec.level 是否超过生效规则与连接级别
   - 超过 → 抛 PolicyDenied（带 suggestion），审计记 denied，触发错误回调
5. 切面链（预留插槽，首版为空实现，原样透传）
6. 二级分流取插件实例（含保活检查）
7. 插件执行 execute()
8. 写审计日志（status = ok）
9. 规整为统一返回结构
```

**关键**：级别校验必须在**执行前最后一刻**完成，校验与执行之间不得插入任何可被外部影响的步骤。

### 8.5 切面链（预留插槽）

**必须**在 `core/app.py` 中留出切面链结构，首版为空：

```python
class Aspect:
    """切面接口。首版不实现任何切面，仅保留结构。"""
    def before(self, ctx: dict) -> dict: return ctx
    def after(self, ctx: dict, result) -> any: return result

ASPECTS: list[Aspect] = []   # 首版为空

def register_aspect(aspect: Aspect) -> None:
    ASPECTS.append(aspect)
```

**将来的切面**（限流、脱敏、缓存）应通过 `register_aspect` 挂入，**不得**为此修改执行链路代码。

### 8.6 超时控制

**必须**为 `execute` 增加超时保护：

- 超时值优先取连接的 `timeout` 字段，默认 30 秒
- 超时后抛 `QueryError("执行超时（30秒）", suggestion="可缩小查询范围或调整连接的 timeout")`
- MySQL 实现可用 `pymysql` 的连接级 `read_timeout` 参数；Redis/Mongo 用各自的超时参数

---

## 9. 规则包与分级

### 9.1 规则包格式

```yaml
# rules/prod-safe.yaml
name: 生产保护            # 必填，人类可读名称
global: true              # 可选，默认 false。true 表示所有连接自动套用
max_level: readonly       # 可选。合法值 readonly / write / danger
max_rows: 100             # 可选。单次返回的最大行数
deny:                     # 可选。禁止的操作类别
  - DDL
  - multi_statement
  - update_without_where
  - delete_without_where
allow: []                 # 可选。显式放行的类别（优先级低于 deny）
```

**元字段与规则字段分开处理**：

- 元字段：`name`、`global`
- 规则字段：`max_level`、`max_rows`、`deny`、`allow`
- 出现**既不是元字段也不是规则字段**的 key → **报警告，不得静默忽略**

### 9.2 四层合成顺序

```
① 内置默认（代码内硬编码）
② 全局规则包（global: true 的，按文件名字典序，只收紧）
③ 连接挂载的规则包（按连接配置中 rules 列表的顺序；放宽需连接 unsafe: true）
④ 连接内联设置（YAML 中直接写的 level / max_rows / deny 等；level 为属主授权声明，直接生效）
```

后一层覆盖前一层，**但方向受 9.3 约束**。

### 9.3 收紧规则（核心安全设计）

合成算法的伪代码：

```python
def merge(base: dict, incoming: dict, allow_relax: bool) -> dict:
    out = dict(base)

    if "max_level" in incoming:
        new_level = Level[incoming["max_level"].upper()]
        if allow_relax:
            out["max_level"] = new_level
        else:
            out["max_level"] = min(out["max_level"], new_level)   # 取更严的

    if "max_rows" in incoming:
        if allow_relax:
            out["max_rows"] = incoming["max_rows"]
        else:
            out["max_rows"] = min(out["max_rows"], incoming["max_rows"])   # 取更小的

    # deny 永远是并集：只能加，不能删
    out["deny"] = set(out["deny"]) | set(incoming.get("deny", []))

    # allow 永远是交集：只能减，不能加
    out["allow"] = set(out["allow"]) & set(incoming.get("allow", set()))

    return out
```

**合成调用**（v1.0.0 拍板语义，解决初稿伪代码与 §14.3 示例的冲突）：

```python
rules = BUILTIN_DEFAULTS                                    # max_level=readonly
for pack in global_packs:                                   # allow_relax=False，只收紧
    rules = merge(rules, pack, allow_relax=False)
for pack in connection_packs:                               # 放宽需 unsafe
    rules = merge(rules, pack, allow_relax=connection.unsafe)
rules = merge(rules, connection_inline, allow_relax=True)   # ④ level 属授权声明，直接生效
```

**关键约束（拍板）**：

- 连接内联的 `level` 是**属主授权声明**，直接生效（未写 `level` 时回落内置默认
  readonly）——否则 §14.3 中所有 `level: write` 示例都会被 min() 压回只读，
  write 级别永远不可用；
- `unsafe: true` 控制的是**挂载规则包**能否放宽，是规则包维度的唯一放宽开关，
  便于事后 `grep unsafe` 审计；
- `deny` 并集、`allow` 交集不受 allow_relax 影响（见上方 merge 伪代码）。

该语义下 §17 用例 5（无 unsafe 放宽被忽略）与用例 6（unsafe 放宽生效）同时成立。

### 9.4 内置默认值

```python
BUILTIN_DEFAULTS = {
    "max_level": Level.READONLY,
    "max_rows": 500,
    "deny": set(),
    "allow": set(),
}
```

### 9.5 内置 deny 类别

| 类别 | 含义 | 判定方式 |
|---|---|---|
| `DDL` | 结构变更语句 | 语句类型属于 CREATE / ALTER / DROP / TRUNCATE / RENAME |
| `multi_statement` | 一次提交多条语句 | 解析后语句数量 > 1 |
| `update_without_where` | 无条件的 UPDATE | UPDATE 且无 WHERE 子句 |
| `delete_without_where` | 无条件的 DELETE | DELETE 且无 WHERE 子句 |
| `grant` | 权限变更 | GRANT / REVOKE |
| `shutdown` | 关停服务 | SHUTDOWN / KILL / FLUSH |

**各插件在 `classify` 中必须产出 `reasons`**，供上层匹配 deny 类别。

### 9.6 三级权限

| 级别 | 允许 |
|---|---|
| `readonly` | 元数据读取、数据查询 |
| `write` | 以上 + INSERT / UPDATE / DELETE |
| `danger` | 以上 + DDL / DROP / 危险命令 |

**默认级别为 `readonly`**。

---

## 10. 审计日志

**文件**：`core/audit.py`

### 10.1 存储

- **主存储**：SQLite 数据库，路径 `logs/operation_log.db`（`logs/` 不存在时自动创建）
- **旁路存储**：JSONL 文件，路径 `logs/audit-YYYY-MM-DD.jsonl`，按天轮转

### 10.2 表结构

```sql
CREATE TABLE IF NOT EXISTS operation_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,   -- ISO8601 UTC，如 2026-09-28T09:30:00Z
    connection    TEXT    NOT NULL,   -- 连接别名
    dialect       TEXT    NOT NULL,   -- mysql / redis / mongo
    level_needed  TEXT    NOT NULL,   -- 该操作判定出的级别
    level_allowed TEXT    NOT NULL,   -- 该连接生效的最高级别
    operation     TEXT    NOT NULL,   -- 操作原文，超过 4000 字符截断
    action        TEXT,               -- 操作类型，如 SELECT / SET / find
    status        TEXT    NOT NULL,   -- ok / denied / error
    elapsed_ms    INTEGER,
    affected      INTEGER,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_ts ON operation_log(ts);
CREATE INDEX IF NOT EXISTS idx_conn ON operation_log(connection);
```

### 10.3 记录要求

**必须记录的情形**（三种都要记）：

1. 执行成功 → `status = "ok"`
2. 被级别或规则拦截 → `status = "denied"`（**不得遗漏**）
3. 执行出错 → `status = "error"`，`error` 字段填错误原因

**脱敏要求**：

- `operation` 中若使用参数化查询，**只记录语句原文，不记录参数值**
- 若参数中含疑似敏感值（长度 > 20 的连续字母数字串），以 `***` 替代
- **不得**记录密码、连接串

### 10.4 可靠性要求

- 写日志**必须**包在 `try/except` 中
- 写入失败时降级写 stderr，**不得**影响主流程返回结果
- 写入失败**不得**静默丢弃：至少要在 stderr 留下痕迹

### 10.5 扩展点

```python
def set_backend(backend) -> None:
    """替换审计后端。backend 需实现 write(record: dict) -> None。"""
```

---

## 11. MySQL 插件规格

**文件**：`plugins/mysqlPlugin.py`

### 11.1 基本信息

```python
@register("mysql")
class MySQLPlugin(BasePlugin):
    TYPE = "mysql"
    CONFIG_FILE = "mysqlConfig.yaml"
    CAPABILITIES = frozenset({SQL, ENTITY, DESCRIBE, NAMESPACE, TRANSACTION, EXPLAIN})
```

**约束**：全项目只有这一个 MySQL 类。**不得**创建 `MySQL57Plugin` / `MySQL80Plugin` 之类的版本子类。

### 11.2 依赖

`pymysql`

### 11.3 方法实现要点

| 方法 | 实现要点 |
|---|---|
| `connect` | 用配置的 host/port/user/password/database/charset 建连；设置 `read_timeout`；失败抛 `ConnectionFailed` |
| `close` | 关闭连接，吞掉异常 |
| `health_check` | 执行 `SELECT 1`，异常则返回 False |
| `server_info` | 执行 `SELECT VERSION()`；解析为 `(major, minor, patch)`；**结果缓存到实例属性**，只探测一次 |
| `list_namespaces` | `SHOW DATABASES` |
| `list_entities` | `SHOW TABLES FROM <namespace>` |
| `describe` | 查 `information_schema.columns` + `SHOW INDEX`，拼接出 `{columns, indexes, ddl}` |
| `execute` | 注入 LIMIT（见 11.5）后执行；结果规整为 `QueryResult` |
| `classify` | 用 sqlglot 解析（见 11.4） |

### 11.4 `classify` 实现（用 sqlglot）

```python
import sqlglot
from sqlglot import exp

def classify(self, operation: str) -> OperationSpec:
    # 1. 多语句检测
    statements = sqlglot.parse(operation, dialect="mysql")
    if len(statements) > 1:
        return OperationSpec(Level.DANGER, "MULTI", (), ("multi_statement",))

    stmt = statements[0]

    # 2. 语句类型判定
    if isinstance(stmt, (exp.Drop, exp.Create, exp.Alter, exp.TruncateTable, ...)):
        return OperationSpec(Level.DANGER, type(stmt).__name__.upper(), tables, ("DDL",))

    if isinstance(stmt, exp.Select):
        return OperationSpec(Level.READONLY, "SELECT", tables, ())

    if isinstance(stmt, (exp.Insert, exp.Update, exp.Delete)):
        reasons = []
        if not stmt.args.get("where"):
            reasons.append(f"{action.lower()}_without_where")
        return OperationSpec(Level.WRITE, action, tables, tuple(reasons))

    # 3. 兜底：未知语句类型按最高级别处理
    return OperationSpec(Level.DANGER, "UNKNOWN", (), ("unrecognized_statement",))
```

**硬性要求**：

- **必须**基于 AST 判定，**不得**用正则或字符串匹配
- **必须**有兜底分支：无法识别的语句按 `DANGER` 处理（默认拒绝，不是默认放行）
- 提取 `targets` 时须遍历 AST 中的表节点，产出表名元组

> **实现注记（sqlglot 30.x 实测）**：`SHOW`/`USE`/`SET`/`KILL` 是独立 AST 节点
> （非 `Command`），分别归 READONLY / WRITE / DANGER(shutdown)；`GRANT`/`RENAME`
> 等回退解析为 `Command`，按首关键字归入 grant / DDL 类别；`FLUSH`/`SHUTDOWN`
> 无法结构化解析，直接落兜底分支判 DANGER。拒绝消息会附带 reasons
> （如「操作说明：命令 KEYS 会阻塞整个实例…」），使 AI 能看到拒绝理由。

### 11.5 LIMIT 注入

```python
def _effective_limit(self, requested: int | None, allowed: int) -> int:
    if requested is None:
        return allowed
    return min(requested, allowed)
```

仅对 `SELECT` 类语句注入；`INSERT` / `UPDATE` / `DELETE` 不注入。
若原语句已有 LIMIT 且小于生效上限，保留原值。
注入后须在 `QueryResult.notice` 中说明，如 `"已自动注入 LIMIT 100"`。

### 11.6 版本差异处理

**不得**为 5.7 / 8.0 分别写代码分支散落各处。改为：

1. `server_info()` 探测一次版本后缓存
2. 需要版本差异时，集中在一处判断，例如：

```python
@property
def _supports_cte(self) -> bool:
    return self._info.version_tuple >= (8, 0)
```

3. 已知需要处理的差异点（首版至少覆盖第 1、2 项）：

| 差异 | 5.7 | 8.0 | 处理 |
|---|---|---|---|
| CTE（`WITH` 子句） | 不支持 | 支持 | 执行前拦截，抛 `QueryError` 并说明需要 8.0+ |
| 窗口函数 | 不支持 | 支持 | 同上 |
| 认证插件 | `mysql_native_password` | `caching_sha2_password` | 建连参数适配 |
| 默认字符集 | `utf8mb4_general_ci` | `utf8mb4_0900_ai_ci` | 显式指定 charset，不依赖默认值 |

**重要**：检测到语法不支持时，**必须明确报错，不得擅自改写 SQL**。改写存在语义漂移风险。

报错示例：

```python
raise QueryError(
    "该语法需要 MySQL 8.0 及以上，当前连接 crm-prod 的版本是 5.7.43",
    suggestion="可改用子查询，或在支持 8.0 的连接上测试该语句"
)
```

---

## 12. Redis 插件规格

**文件**：`plugins/redisPlugin.py`

### 12.1 基本信息

```python
@register("redis")
class RedisPlugin(BasePlugin):
    TYPE = "redis"
    CONFIG_FILE = "redisConfig.yaml"
    CAPABILITIES = frozenset({COMMAND, NAMESPACE})
```

**注意**：Redis **不声明** `ENTITY` 与 `DESCRIBE` 能力。

### 12.2 方法实现要点

| 方法 | 实现要点 |
|---|---|
| `connect` | `redis.Redis(host, port, password, db, socket_timeout=timeout)` |
| `server_info` | `INFO server` → `redis_version` |
| `list_namespaces` | 返回 `["db0", "db1", ...]`（按 `CONFIG GET databases` 结果） |
| `list_entities` | 用 `SCAN` 采样，按 `:` 前缀分组，返回前缀列表（如 `["user:", "session:"]`） |
| `describe` | 对给定 key：`TYPE` + `TTL` + 对应类型的长度（`STRLEN` / `LLEN` / `HLEN` / `SCARD` / `ZCARD`） |
| `execute` | 见 12.3 |
| `classify` | 查命令名分级表（见 12.4） |

### 12.3 `execute` 实现

`operation` 是命令字符串，如 `"GET user:1"` 或 `"SET user:1 value"`。

```python
parts = operation.strip().split(maxsplit=1)
cmd = parts[0].upper()
args = shlex.split(parts[1]) if len(parts) > 1 else []
result = self._client.execute_command(cmd, *args)
```

**结果规整**：

- 标量结果 → `QueryResult(kind="table", columns=["value"], rows=[(result,)])`
- 列表结果 → `QueryResult(columns=["index", "value"], rows=[...])`
- 哈希结果 → `QueryResult(columns=["field", "value"], rows=[...])`

### 12.4 命令分级表（必须实现）

```python
REDIS_READ = {
    "GET", "MGET", "EXISTS", "TTL", "PTTL", "TYPE", "STRLEN", "DUMP",
    "HGET", "HMGET", "HGETALL", "HLEN", "HEXISTS", "HKEYS", "HVALS",
    "LRANGE", "LLEN", "LINDEX", "LPOS",
    "SMEMBERS", "SCARD", "SISMEMBER", "SRANDMEMBER",
    "ZRANGE", "ZREVRANGE", "ZSCORE", "ZCARD", "ZRANK", "ZCOUNT",
    "SCAN", "SSCAN", "HSCAN", "ZSCAN", "DBSIZE", "INFO", "PING", "ECHO",
}

REDIS_WRITE = {
    "SET", "SETEX", "PSETEX", "SETNX", "MSET", "MSETNX", "GETSET", "GETDEL",
    "DEL", "UNLINK", "EXPIRE", "PEXPIRE", "EXPIREAT", "PERSIST", "RENAME",
    "INCR", "DECR", "INCRBY", "DECRBY", "INCRBYFLOAT", "APPEND", "SETRANGE",
    "HSET", "HMSET", "HSETNX", "HDEL", "HINCRBY",
    "LPUSH", "RPUSH", "LPUSHX", "RPUSHX", "LPOP", "RPOP", "LSET", "LREM",
    "LTRIM", "LINSERT", "RPOPLPUSH", "LMOVE",
    "SADD", "SREM", "SPOP", "SMOVE", "SINTERSTORE", "SUNIONSTORE", "SDIFFSTORE",
    "ZADD", "ZREM", "ZINCRBY", "ZREMRANGEBYRANK", "ZREMRANGEBYSCORE",
    "PFADD", "PFMERGE", "GEOADD",
}

REDIS_DANGER = {
    "KEYS",          # 会阻塞整个实例，必须禁用
    "FLUSHALL", "FLUSHDB",
    "CONFIG", "SHUTDOWN", "DEBUG", "SCRIPT",
    "CLUSTER", "SLAVEOF", "REPLICAOF", "MONITOR",
    "SAVE", "BGSAVE", "BGREWRITEAOF",
    "MIGRATE", "RESTORE", "SWAPDB", "RESET",
}
```

判定逻辑：

```python
def classify(self, operation: str) -> OperationSpec:
    cmd = operation.strip().split(maxsplit=1)[0].upper()

    if cmd in REDIS_DANGER:
        return OperationSpec(Level.DANGER, cmd, (), (f"命令 {cmd} 属于危险操作",))
    if cmd in REDIS_WRITE:
        return OperationSpec(Level.WRITE, cmd, (), ())
    if cmd in REDIS_READ:
        return OperationSpec(Level.READONLY, cmd, (), ())
    # 兜底：未知命令按 DANGER 处理
    return OperationSpec(Level.DANGER, cmd, (), (f"未识别的命令 {cmd}",))
```

**必须特别处理 `KEYS`**：该命令会阻塞整个 Redis 实例。除了判为 `DANGER`，还应在其被调用时给出 suggestion：`"建议改用 SCAN 分批遍历"`。

> **实现注记（双保险）**：KEYS 的 SCAN 建议放进 classify 的 reasons（底座会将其
> 带进拒绝消息）；且即使连接放开到 danger 级，`execute` 也不实际执行 KEYS，
> 返回空结果 + notice。实测见 `scripts/live_redis_smoke.py`。

### 12.5 不得实现的能力

Redis **不得**实现 `describe` 返回表结构之类的语义；其 `describe` 只返回 key 的类型、TTL、长度。

---

## 13. MongoDB 插件规格

**文件**：`plugins/mongoPlugin.py`

### 13.1 基本信息

```python
@register("mongo")
class MongoPlugin(BasePlugin):
    TYPE = "mongo"
    CONFIG_FILE = "mongoConfig.yaml"
    CAPABILITIES = frozenset({DOCUMENT, ENTITY, DESCRIBE, NAMESPACE})
```

### 13.2 方法实现要点

| 方法 | 实现要点 |
|---|---|
| `connect` | `pymongo.MongoClient(uri, serverSelectionTimeoutMS=connect_timeout*1000)`；显式 `ping` 验证 |
| `server_info` | `client.server_info()["version"]` |
| `list_namespaces` | `client.list_database_names()` |
| `list_entities` | `client[ns].list_collection_names()` |
| `describe` | 采样若干文档（默认 100 条），汇总出现过的字段名与类型 |
| `execute` | 解析 JSON 形式的 `operation`（见 13.3） |
| `classify` | 查方法名分级表（见 13.4） |

### 13.3 `execute` 的 operation 格式

`operation` 是 JSON 字符串：

```json
{"collection": "users", "method": "find", "filter": {"status": "active"}, "limit": 50}
```

字段说明：

- `collection`：必填，集合名
- `method`：必填，操作方法
- 其余字段按方法透传

**解析失败必须**抛 `QueryError`，并给出格式示例。**不得**回显原始内容（可能含敏感值）。

### 13.4 方法分级表

```python
MONGO_READ = {
    "find", "findOne", "countDocuments", "estimatedDocumentCount",
    "distinct", "listCollections", "listIndexes", "dbStats", "collStats",
    "aggregate",          # 含 $out / $merge 时升级为 WRITE，见下
}

MONGO_WRITE = {
    "insertOne", "insertMany",
    "updateOne", "updateMany", "replaceOne",
    "deleteOne", "deleteMany",
    "findOneAndUpdate", "findOneAndReplace", "findOneAndDelete",
    "createIndex", "dropIndex", "dropIndexes",
}

MONGO_DANGER = {
    "drop", "dropDatabase", "renameCollection", "createCollection",
    "convertToCapped", "shutdown",
}
```

**两个特殊判定（必须实现）**：

1. `deleteMany` 的 `filter` 为空 `{}` → 升级为 **DANGER**，reason 为 `delete_without_where`
2. `aggregate` 的 pipeline 中含 `$out` 或 `$merge` 阶段 → 升级为 **WRITE**

**兜底**：未识别的方法名按 `DANGER` 处理。

> **实现注记**：operation JSON 中的驼峰方法名（updateMany / findOneAndUpdate /
> dropIndex 等）在执行层自动映射为 pymongo 的下划线方法；`renameCollection` /
> `convertToCapped` / `shutdown` 三个方法保留分级识别（DANGER）但执行面明确
> 未开放（SPEC 未定义其参数透传格式，不擅自发明接口，调用返回 QueryError）。

---

## 14. 配置文件规格

### 14.1 组织方式

- 位置：`conf/` 目录
- **一种数据库类型一个文件**，文件名由插件的 `CONFIG_FILE` 声明
- **文件顶层 key 即连接别名**，AI 调用工具时使用该名称
- 别名只允许 `[A-Za-z0-9_-]`

### 14.2 通用字段

所有类型共用的字段（插件按需读取，未用到的忽略）：

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `host` | str | 视类型 | — | 主机地址 |
| `port` | int | 否 | 由插件定 | 端口 |
| `user` | str | 否 | — | 用户名 |
| `password` | str | 否 | — | 密码，**建议写 `${env:VAR}`** |
| `database` | str | 否 | — | 默认库 |
| `path` | str | 否 | — | 文件路径（SQLite 等文件型库用） |
| `charset` | str | 否 | `utf8mb4` | 字符集（MySQL） |
| `level` | str | 否 | `readonly` | 最高权限级别 |
| `rules` | list[str] | 否 | `[]` | 挂载的规则包名 |
| `unsafe` | bool | 否 | `false` | 是否允许规则放宽 |
| `timeout` | int | 否 | `30` | 单次执行超时（秒） |
| `connect_timeout` | int | 否 | `10` | 建连超时（秒） |

**要求**：所有字段均为**可选**。SQLite 这类文件库只用 `path`；账号密码为空时插件不得报错。

### 14.3 示例

```yaml
# conf/mysqlConfig.yaml
local-test:
  host: 127.0.0.1
  user: root
  password: ${env:OMNI_MYSQL_LOCAL_PWD}
  database: testdb
  level: write

vm-docker:
  host: 192.168.56.10
  port: 13306
  user: root
  password: ${env:OMNI_MYSQL_DOCKER_PWD}
  database: dev
  level: write

crm-prod:
  host: 10.20.30.40
  user: readonly
  password: ${env:OMNI_MYSQL_CRM_PWD}
  database: crm
  level: readonly
  rules: [prod-safe]
  timeout: 20
```

```yaml
# conf/redisConfig.yaml
vm-docker-redis:
  host: 192.168.56.10
  port: 6379
  password: ${env:OMNI_REDIS_PWD}
  database: 0
  level: write
```

```yaml
# conf/mongoConfig.yaml
vm-docker-mongo:
  host: 192.168.56.10
  port: 27017
  user: admin
  password: ${env:OMNI_MONGO_PWD}
  database: dev
  level: write
```

### 14.4 敏感信息边界（必须遵守）

| 文件 | 是否进 Git | 原因 |
|---|---|---|
| `conf/*.yaml` | ✅ 可以 | 密码已走 `${env:}`，不含真实凭据，但含内网地址，公开仓库需注意 |
| `.env` | ❌ **不得** | 含真实密码 |
| `logs/` | ❌ **不得** | 含真实 SQL 语句 |
| `rules/*.yaml` | ✅ 可以 | 纯规则，可分享 |

`.gitignore` **必须**包含：

```
.env
logs/
*.db
__pycache__/
*.pyc
.venv/
venv/
```

---

## 15. MCP 工具规格

**共 5 个工具**，命名带 `db_` 前缀以防与其他 MCP 工具冲突。

### 15.1 工具清单

#### `db_list_connections()`

无参数。返回所有可用连接及其元信息。

```json
{
  "ok": true,
  "connections": [
    {
      "name": "crm-prod",
      "dialect": "mysql",
      "version": "5.7.43",
      "level": "readonly",
      "capabilities": ["sql", "entity", "describe", "namespace", "transaction"],
      "operation_hint": "operation 传 SQL 字符串",
      "rules": ["prod-safe"]
    }
  ]
}
```

**要求**：`operation_hint` 必须按 `capabilities` 生成，让 AI 知道该传什么格式：

| capabilities 含 | operation_hint |
|---|---|
| `sql` | `"operation 传 SQL 字符串"` |
| `command` | `"operation 传 Redis 命令字符串，如 GET user:1"` |
| `document` | `"operation 传 JSON 字符串，如 {\"collection\":\"users\",\"method\":\"find\"}"` |

**不得**返回密码。

> **实现注记**：`version` 通过惰性建连探测（§8.2 惰性原则的例外：列连接本身
> 即"首次使用"）；单个连接不可达时返回「不可达（原因）」占位串，不阻塞整体
> 列表，也不抛错。

#### `db_list_tables(connection: str, namespace: str | None = None)`

内部调用 `list_entities`。

**要求**：若目标连接不具备 `entity` 能力，**必须**返回：

```json
{"ok": false, "error": "连接 vm-docker-redis 不支持列出实体", "suggestion": "该库为键值型，可用 db_query 执行 SCAN 查看 key"}
```

**不得**返回空列表代替。

#### `db_describe_table(connection: str, table: str, namespace: str | None = None)`

内部调用 `describe`。不具备 `describe` 能力时同上报错。

#### `db_query(connection: str, operation: str, params: list | None = None, limit: int | None = None)`

只读查询。执行级别不得超过 `READONLY`。

#### `db_exec(connection: str, operation: str, params: list | None = None)`

写入或结构变更。级别由连接的 `level` 与生效规则共同决定。

### 15.2 返回约定

**所有工具必须**：

- 成功返回 `{"ok": true, ...}`
- 失败返回 `{"ok": false, "error": "...", "suggestion": "..."}`
- **绝不**抛出未捕获异常，**绝不**返回 traceback 或本机路径

### 15.3 错误信息要求

错误信息面向 AI，必须做到：

1. **说清是什么问题**：如「该操作需要 write 级别，连接 crm-prod 的上限是 readonly」
2. **给出修正建议**：如「如需执行，请确认后在 conf/mysqlConfig.yaml 调整该连接的 level」
3. **不回显敏感内容**：涉及操作的错误消息中，若原文可能含敏感值，须脱敏

---

## 16. 健壮性要求（7 条）

**以下 7 条为硬性要求，逐条实现并逐条测试。**

### 16.1 异常归一

所有插件的 `execute` 必须捕获驱动原生异常，转换为本系统异常：

```python
try:
    ...
except pymysql.err.OperationalError as e:
    raise QueryError(f"MySQL 操作失败：{e.args[1] if len(e.args) > 1 else e}") from e
except pymysql.err.ProgrammingError as e:
    raise QueryError(...) from e
except pymysql.MySQLError as e:
    raise QueryError(...) from e
```

**不得**让驱动原生异常穿透到 `core/app.py` 之外。

### 16.2 连接保活与断线重连

> **实现注记（2026-09-29 审计修订）**：自动重试**仅限只读操作**——只读幂等，
> 重试安全；写/危险操作（INSERT/UPDATE/DELETE/SET 等）非幂等，首次请求可能
> 已到达服务器（仅响应丢失），自动重试会造成静默重复写。此类操作断线时
> 抛 QueryError 并提示 AI 先确认首次执行是否生效。SPEC 原文的"重试原操作
> 一次"按操作级别区分执行（`execute_with_retry(retryable=)`）。

**触发场景**：MySQL `wait_timeout` 默认 8 小时，连接空闲超时后被服务端关闭，再查询会报 `MySQL server has gone away`。

**实现要求**：

- 每次 `execute` 前**不需要**主动 ping（有性能开销）
- 捕获连接失效类异常（MySQL `2006` / `2013` 错误码、Redis `ConnectionError`、Mongo `AutoReconnect`）后：
  1. 重新建立连接
  2. **重试原操作一次**
  3. 若重试仍失败，才抛 `QueryError`
- 重连成功后，`server_info` 缓存可保留（版本不会因重连改变）

### 16.3 配置校验与未知字段警告

见 8.3。核心：**写错的字段名必须报警告，不得静默忽略**。

### 16.4 分级校验的位置

级别校验必须在**执行前最后一刻**完成。校验通过与调用 `execute` 之间**不得**插入任何可能改变判定结果的步骤。

判定必须基于结构化解析（AST / 命令表），**不得**用字符串匹配。

**默认拒绝**：无法识别的操作按 `DANGER` 处理。

### 16.5 日志不影响主流程

见 10.4。

### 16.6 工具层兜底异常处理

`core/app.py` 中**每个** MCP 工具入口必须包统一装饰器：

```python
def tool_guard(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ConnectorError as e:
            return {"ok": False, "error": str(e), "suggestion": e.suggestion}
        except Exception as e:
            log_to_stderr(e)
            return {"ok": False, "error": "内部错误，请查看服务端日志"}
    return wrapper
```

**不得**把 `traceback` 内容返回给 AI。

### 16.7 可脱离真实数据库测试

**必须**提供 `tests/mockPlugin.py`：

```python
@register("mock")
class MockPlugin(BasePlugin):
    """假插件：不连任何真实数据库，返回构造好的数据。"""
    TYPE = "mock"
    CONFIG_FILE = "mockConfig.yaml"
    CAPABILITIES = frozenset({SQL, ENTITY, DESCRIBE, NAMESPACE})

    # classify 复用简单的 SQL 关键字判定
    # execute 返回固定的 QueryResult
    # 可被配置为"抛指定异常"，用于测试错误路径
```

**要求**：底座的测试（`tests/test_core.py`）**不得**依赖任何真实数据库。

---

## 17. 测试要求

**文件**：`tests/test_core.py`

**必须覆盖的用例**：

| # | 用例 | 断言 |
|---|---|---|
| 1 | 一级分流：调用不存在的能力 | 返回明确错误，不抛异常 |
| 2 | 二级分流：不存在的连接别名 | 返回明确错误，提示可用连接列表 |
| 3 | 规则合成：内置默认 | `max_level == READONLY` |
| 4 | 规则合成：全局包收紧 | 结果比内置更严 |
| 5 | 规则合成：连接包尝试放宽但无 `unsafe` | **放宽被忽略，仍为更严值** |
| 6 | 规则合成：连接包放宽且有 `unsafe: true` | 放宽生效 |
| 7 | 规则合成：`deny` 并集 | 只能加不能减 |
| 8 | 分级拦截：`readonly` 连接执行 `DELETE` | 抛 `PolicyDenied`，带 suggestion |
| 9 | 审计：被拦截的操作 | 写入一条 `status == "denied"` 记录 |
| 10 | 审计：日志写入失败 | 主流程仍返回结果，不抛异常 |
| 11 | 错误回调：回调内抛异常 | 主流程不受影响 |
| 12 | 工具兜底：插件抛任意异常 | 返回 `{"ok": false}`，无 traceback |
| 13 | 配置：`${env:X}` 未设置 | 报错信息含变量名 `X` |
| 14 | 配置：未知字段 | 产生警告，不中断 |
| 15 | 连接保活：模拟断连 | 自动重连并重试一次 |

> **实现注记（v1.0.0 实际分布，共 61 项全部通过）**：上表 15 条必测全部实现于
> `tests/test_core.py`（另含契约校验与加固项，共 23 条）；MySQL 插件离线分级
> /版本门/LIMIT 注入 22 条（`tests/test_mysql_plugin.py`）；Redis/Mongo 分级
> 16 条（`tests/test_kv_classify.py`）。运行：`python -m unittest discover -s tests`。
> 真库冒烟与 MCP 端到端见 `scripts/`。

---

## 18. 实施顺序

**必须**按以下顺序实施，每步完成后先自测再进入下一步。

### Step 1 · 契约层

产出：`basePlugin.py`（能力常量 + `BasePlugin` + 注册表 + 数据结构）、`core/errors.py`

验收：能 import 成功；`@register` 对非法声明的校验生效。

### Step 2 · 底座

产出：`core/policy.py`、`core/audit.py`、`core/connections.py`、`core/app.py`、`base.py`、`tests/mockPlugin.py`、`tests/test_core.py`

验收：**用 MockPlugin 跑通全链路**（分流 / 规则合成 / 分级拦截 / 审计 / 错误回调 / 工具返回），17 节全部用例通过。**此步不连任何真实数据库**。

### Step 3 · MySQL 插件

产出：`plugins/mysqlPlugin.py`、`conf/mysqlConfig.yaml`、`rules/prod-safe.yaml`、`rules/dev-loose.yaml`

验收：用 MockPlugin 的测试全部仍通过；MySQL 插件的 `classify` 对各类语句判定正确（单元测试，可离线）。

### Step 4 · 接入三个环境

产出：实际可用的 `conf/mysqlConfig.yaml`（三个连接）、`.env`

**验收标准（本项目的终极验收）**：接入三个 MySQL 环境，**只增加配置，零代码改动**。若此步需要改代码，说明前三步的抽象有遗漏，须返回修正。

### Step 5 · Redis 与 MongoDB 插件

产出：`plugins/redisPlugin.py`、`plugins/mongoPlugin.py` 及对应配置文件

验收：同 Step 4 ——**只增加文件与配置，底座零改动**。

---

## 19. 验收标准

项目完成后，逐条核对：

| # | 标准 | 验证方式 |
|---|---|---|
| 1 | 底座代码中不含任何数据库类型判断 | 搜索 `base.py`、`core/*.py` 中的 `mysql` / `redis` / `mongo` 字面量，应只在错误提示与文档字符串中出现 |
| 2 | 插件不 import 底座内部 | 搜索 `plugins/*.py` 的 import，应只有 `from basePlugin import ...` |
| 3 | 依赖方向无回环 | 检查 4.2 节的层次，无反向 import |
| 4 | 单文件不超过 350 行 | `wc -l` 检查 |
| 5 | 加一个库不改底座 | 实际新增一个插件，验证 `core/` 下零改动 |
| 6 | 被拦截的操作有审计记录 | 制造一次拦截，查 `operation_log` 表 |
| 7 | 错误信息不含 traceback | 制造各类错误，检查返回值 |
| 8 | 环境变量缺失时报出变量名 | 删除一个 `.env` 变量后启动 |
| 9 | 配置写错字段名时有警告 | 改一个字段名后启动 |
| 10 | 5 个工具均可被 MCP 客户端发现 | 用 MCP Inspector 或客户端检查 |
| 11 | 底座可脱离真库测试 | 断开所有数据库，仅跑 `tests/test_core.py` |
| 12 | 三件待补事项已实现 | 配置字段全可选；有超时控制；`QueryResult` 含 `kind` 字段 |

> **验证结论（2026-09-28，v1.0.0）**：上表 12 项全部通过。
> - #1/#2/#3/#4 以 grep 与 `wc -l` 复核（底座零类型分支；插件仅 import
>   `basePlugin`；最大文件 327 行）；
> - #5 实测两次：接入 MySQL 只加 conf 零代码；新增 mongo/redis 插件时 `core/` 零改动；
> - #6–#11 由三方言冒烟脚本 + `scripts/mcp_client_check.py`（官方客户端
>   stdio 全链路）+ 61 项离线测试覆盖；
> - #12 三件待补事项全部落地。
> 唯一保留项：#11.6 中 5.7 的 CTE/窗口门控因本机无 5.7 实例，以假
> `server_info` 离线覆盖，8.0 真实路径已验证。

---

## 20. 禁止事项

以下行为**明确禁止**，实现时不得采用：

| # | 禁止 | 原因 |
|---|---|---|
| 1 | 在底座中写数据库类型分支 | 破坏解耦，加库要改底座 |
| 2 | 按数据库版本拆分类（如 `MySQL57Plugin`） | 类会随版本爆炸，与"一个库一个类"相悖 |
| 3 | 插件 import 底座内部模块 | 破坏解耦，无法独立测试 |
| 4 | 用字符串匹配或正则做分级判定 | 可被注释、大小写、多语句绕过 |
| 5 | 无法识别的操作默认为低级别 | 必须默认拒绝 |
| 6 | 擅自改写 SQL 以适配低版本 | 语义漂移风险，必须明确报错 |
| 7 | 在错误信息或日志中回显密码、参数值 | 泄密 |
| 8 | 把 traceback 返回给 AI | 泄露本机路径，且 AI 无法处理 |
| 9 | 日志写入失败时中断主流程 | 旁路不得影响主链 |
| 10 | 用 `None` 静默填充缺失的配置字段 | 会报出难以排查的认证错误 |
| 11 | 规则合成允许无 `unsafe` 的放宽 | 安全底线 |
| 12 | 为时序 / 图 / 向量 / 异步数仓改造契约 | 超出范围，会破坏设计 |
| 13 | 把日志或 `.env` 提交进 Git | 敏感信息泄露 |
| 14 | 为"将来可能"增加抽象层 | 过早抽象，本项目明确要求避免 |

---

## 附：项目定位一句话

> **底座是一个不认识任何数据库的分流器；数据库的一切细节都关在插件里，靠 `BasePlugin` 这一道门进出。**
