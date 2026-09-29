# 插件开发手册（PLUGIN_GUIDE）

> 给"要接入一种新数据库"的开发者。读完本篇 + 参照任一现有插件，即可在不碰底座一行代码的前提下接入新库——这是本项目的核心验收标准，已被 MySQL/Redis/Mongo 三种完全不同范式的接入实测过。
>
> 前置阅读：[SPEC.md](../SPEC.md) §5（契约）、§16（健壮性七条）。

---

## 1. 你要写什么

一个插件 = **一个文件、一个类**，放在 `plugins/` 下，`from basePlugin import ...`，其余什么都不许 import（底座内部模块禁止触碰，验收红线）。

```
plugins/pgPlugin.py      ← 新增
conf/pgConfig.yaml       ← 新增
base.py                  ← 加一行 import（全项目唯一改动）
```

## 2. 契约速查

### 2.1 能力常量（CAPABILITIES 从中取值，禁止自造字符串）

| 常量 | 含义 | 你的库有就声明 |
|---|---|---|
| `NAMESPACE` | 有命名空间（库 / db / database） | |
| `ENTITY` | 有实体（表 / 集合） | 没有则 `db_list_tables` 会被工具层拦下并给出建议 |
| `DESCRIBE` | 能描述实体结构 | |
| `SQL` / `COMMAND` / `DOCUMENT` | operation 的形态（决定 operation_hint 文案） | 三选一必选其一 |
| `TRANSACTION` / `EXPLAIN` | 附加能力 | |

### 2.2 类属性

| 属性 | 必填 | 说明 |
|---|---|---|
| `TYPE` | ✅ | 类型标识，与 `@register("xxx")` 一致 |
| `CONFIG_FILE` | ✅ | 配置文件名，位于 `conf/`，缺失启动即报 `ValueError` |
| `CAPABILITIES` | ✅ | 非空，且每项必须在常量池内（注册时校验） |
| `REQUIRED_FIELDS` | 可选 | 配置必须携带的顶层字段（缺字段启动期报错并指明连接名） |
| `EXTRA_FIELDS` | 可选 | 插件私有字段，避免被"未知字段警告"误伤 |

### 2.3 九个方法（抽象方法一个不能少）

| 方法 | 契约 | 失败时 |
|---|---|---|
| `connect()` | 建连；从 `self.raw`（已解析 `${env:}`）读参数 | 抛 `ConnectionFailed` |
| `close()` | 关连接 | **不得抛异常** |
| `health_check()` | 存活检查 | 返回 False，**不得抛异常** |
| `server_info()` | 探测版本 → `ServerInfo`；**实例内缓存，只探测一次** | 抛 `ConnectionFailed` |
| `list_namespaces()` | 列命名空间 | 不支持返回 `[]` |
| `list_entities(namespace)` | 列实体 | 不支持返回 `[]` |
| `describe(entity, namespace)` | 实体结构 dict | 不支持抛 `ConnectorError` |
| `execute(operation, params, limit)` | 执行，返回 `QueryResult` | 抛 `QueryError`；**连接失效抛 `ConnectionFailed`**（底座据此自动重连重试一次） |
| `classify(operation)` | 只判级别不执行，返回 `OperationSpec` | 无法判定抛 `ConnectorError` |

另有可选钩子 `on_error(exc, context)`：插件级错误回调，底座会吞掉它内部的一切异常。

### 2.4 数据结构（全部在 `basePlugin.py`，import 即用）

```python
Level          # IntEnum：READONLY=1 / WRITE=2 / DANGER=3
ServerInfo     # dialect / version / version_tuple / extra
OperationSpec  # level / action / targets / reasons   ← reasons 会被底座带进拒绝消息
QueryResult    # kind="table" / columns / rows / row_count / affected / elapsed_ms / truncated / notice
```

异常四件套（同在 `basePlugin`，插件直接抛）：

```python
ConnectorError(message, suggestion=None)   # 基类
ConnectionFailed            # 建连失败 / 连接失效（触发自动重连）
PolicyDenied                # 只由底座抛，插件不用管
QueryError                  # 执行失败（语法/超时/权限等）
```

## 3. 六条硬规矩（违反 = 验收不过）

1. **分级必须结构化解析**：SQL 用 AST（sqlglot），命令/方法用查表。禁止正则、禁止字符串匹配——注释、大小写、多语句都能绕过字符串匹配。
2. **默认拒绝**：无法解析/未识别的操作一律判 `Level.DANGER` + reasons 说明。绝不默认放行。
3. **异常归一**：驱动原生异常必须在插件内转成 `QueryError`；连接失效类（断连/超时失联）转 `ConnectionFailed`。原生异常穿透到底座外属于事故。
4. **`execute` 必须注入行数上限**：`limit` 参数与插件自身上限取小值；截断要置 `truncated=True`，注入/收敛要在 `notice` 说明。
5. **不回显敏感值**：解析失败、参数错误的报错信息中不得携带 operation 原文（可能含密码/秘钥），给格式示例即可。
6. **版本差异集中一处**：`server_info()` 探测一次后缓存；需要分支时用一个 property（参照 mysqlPlugin 的 `_is_8plus`）。禁止为版本拆类、禁止散落 if-version。检测到语法不支持时**明确报错，不得擅自改写语句**。

## 4. 实战：从零接入 PostgreSQL

### Step 1 · 写插件

```python
"""PostgreSQL 插件。"""
import psycopg  # 或 psycopg2

from basePlugin import (DESCRIBE, ENTITY, EXPLAIN, NAMESPACE, SQL,
                        TRANSACTION, BasePlugin, ConnectionFailed,
                        Level, OperationSpec, QueryError, QueryResult,
                        ServerInfo, parse_version, register)

@register("postgres")
class PostgresPlugin(BasePlugin):
    TYPE = "postgres"
    CONFIG_FILE = "pgConfig.yaml"
    CAPABILITIES = frozenset({SQL, ENTITY, DESCRIBE, NAMESPACE, TRANSACTION, EXPLAIN})
    REQUIRED_FIELDS = ("host",)

    def __init__(self, name, raw):
        super().__init__(name, raw)
        self._conn = None
        self._info = None

    def connect(self):
        try:
            self._conn = psycopg.connect(
                host=self.raw.get("host"),
                port=int(self.raw.get("port", 5432)),
                user=self.raw.get("user", ""),
                password=self.raw.get("password", ""),
                dbname=self.raw.get("database") or "postgres",
                connect_timeout=int(self.raw.get("connect_timeout", 10)),
            )
        except Exception as e:                      # 归一为 ConnectionFailed
            raise ConnectionFailed(f"PostgreSQL 连接 {self.name} 建立失败：{e}") from e

    def close(self):
        try:
            if self._conn: self._conn.close()
        except Exception: pass
        finally:
            self._conn = None

    def health_check(self):
        try:
            with self._conn.cursor() as c: c.execute("SELECT 1")
            return True
        except Exception:
            return False

    def server_info(self):
        if self._info is None:                      # 只探测一次
            ...  # SELECT version() → ServerInfo("postgres", v, parse_version(v))
        return self._info

    def classify(self, operation):
        ...  # sqlglot.parse(operation, dialect="postgres")，参照 mysqlPlugin

    def execute(self, operation, params=None, limit=None):
        ...  # LIMIT 注入 + 异常归一，参照 mysqlPlugin

    # list_namespaces / list_entities / describe 参照 mysqlPlugin
```

### Step 2 · 写配置

```yaml
# conf/pgConfig.yaml
local-pg:
  host: 127.0.0.1
  port: 5432
  user: postgres
  password: ${env:OMNI_PG_LOCAL_PWD}
  database: testdb
  level: write
```

`.env` 补 `OMNI_PG_LOCAL_PWD=...`；`requirements.txt` 补驱动。

### Step 3 · 注册（唯一底座侧改动）

```python
# base.py 加一行
import plugins.pgPlugin   # noqa: F401
```

重启即生效：`db_list_connections` 自动出现新连接，capabilities / operation_hint / 版本全部自动带出。

### Step 4 · 测试（与代码一起交付）

- **离线分级单测**（必须）：参照 `tests/test_mysql_plugin.py`——不连真库，逐类断言 classify 的 level/action/reasons、版本门、LIMIT 注入、注释绕过。
- **真库冒烟**（有环境就写）：参照 `scripts/live_redis_smoke.py` 的结构——列连接 / 查询 / 写入 / 危险操作被拦 / 审计落库。

## 5. 三种范式的判定方式对照（选参考模板）

| 插件 | operation 形态 | classify 方式 | 特别处理 |
|---|---|---|---|
| mysqlPlugin | SQL 字符串 | sqlglot AST 解析 | 多语句检测、无 WHERE 标记、5.7 CTE/窗口函数门控、LIMIT 注入 |
| redisPlugin | 命令字符串 | 查命令名分级表 | KEYS 判 danger 且 execute 不实际执行（SCAN 建议） |
| mongoPlugin | JSON 字符串 | 查方法名分级表 | deleteMany 空 filter → danger；aggregate 含 $out/$merge → write；坏 JSON 不回显 |

**选型建议**：关系型抄 mysqlPlugin（AST 路线）；命令型/键值型抄 redisPlugin（查表路线）；文档型抄 mongoPlugin（JSON 路线）。

## 6. 注册校验行为（`@register` 会抛 ValueError 的情况）

- type_name 重复注册
- 未声明 `CAPABILITIES` 或 `CONFIG_FILE`
- `CAPABILITIES` 含常量池之外的未知能力

底座对插件的全部认知**只有** `basePlugin.py` 这一个文件——守住这条，底座永远不需要为新库改一行。
