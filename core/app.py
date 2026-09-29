"""一级分流 + 切面链插槽 + MCP 工具注册 + 统一异常包装（SPEC §8、§15、§16.6）。

本层不得出现任何数据库类型判断（验收标准 #1）：所有方言差异由
capabilities / OperationSpec / 插件方法承载。
"""
import json
from datetime import datetime, timezone
from functools import wraps

from basePlugin import LEVEL_NAMES, Level
from core import audit, policy
from core.aspects import TokenTrimAspect, trim_describe, trim_payload
from core.connections import ConnectionManager, manager as default_manager
from core.errors import (ConnectorError, ErrorContext, PolicyDenied,
                         QueryError, emit_error, log_to_stderr)

# ── MCP SDK 适配：v1 为 FastMCP，本机 mcp 2.x 已改名 MCPServer（API 兼容）──
try:  # pragma: no cover
    from mcp.server.fastmcp import FastMCP as _MCPServer
except ImportError:  # pragma: no cover
    from mcp.server.mcpserver import MCPServer as _MCPServer

# ── 全局运行期状态（bootstrap 填充）──
_pack_store = {"packs": []}
_manager = default_manager

def get_manager() -> ConnectionManager:
    return _manager

def bootstrap(conf_dir: str = "conf", rules_dir: str = "rules",
              logs_dir: str = "logs", manager: ConnectionManager | None = None):
    """启动装配：加载规则包 + 加载连接 + 提前校验挂载（§8.3）。"""
    global _manager
    if manager is not None:
        _manager = manager
    _pack_store["packs"] = policy.load_packs(rules_dir)
    _manager.load()
    audit.init(logs_dir)
    for cfg in _manager.configs().values():
        policy.resolve_rules(cfg, _pack_store["packs"])   # 规则包引用不存在 → 启动即报错
    return _manager

# ─────────────────────────────────────────────
# 切面链（§8.5，首版为空实现，仅保留结构）
# ─────────────────────────────────────────────
class Aspect:
    """切面接口。首版不实现任何切面，仅保留结构。"""
    def before(self, ctx: dict) -> dict:
        return ctx
    def after(self, ctx: dict, result):
        return result

ASPECTS: list[Aspect] = []   # 首版为空

def register_aspect(aspect: Aspect) -> None:
    ASPECTS.append(aspect)

# v1.0.1：首个正式切面——结果裁剪与 token 计量（省 agent 上下文，§8.5 正统挂入）
register_aspect(TokenTrimAspect())

# ─────────────────────────────────────────────
# 统一异常包装（§16.6）
# ─────────────────────────────────────────────
def tool_guard(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ConnectorError as e:
            return {"ok": False, "error": e.message, "suggestion": e.suggestion}
        except Exception as e:  # noqa: BLE001 —— 兜底：不外抛、不回传 traceback
            log_to_stderr(e)
            return {"ok": False, "error": "内部错误，请查看服务端日志"}
    return wrapper

def _json_safe(obj):
    """rows 中 Decimal/datetime/ObjectId 等非原生类型 → 字符串化。"""
    try:
        return json.loads(json.dumps(obj, ensure_ascii=False, default=str))
    except Exception:  # noqa: BLE001
        return str(obj)

# ─────────────────────────────────────────────
# 执行链路（§8.4，顺序不得调整）
# ─────────────────────────────────────────────
def _run_operation(conn_name: str, operation: str, params, limit, *,
                   cap_readonly: bool, tool: str) -> dict:
    if not operation or not str(operation).strip():
        raise QueryError("operation 不能为空",
                         suggestion="查询传语句/命令字符串；参考 db_list_connections 的 operation_hint")
    cfg = _manager.get_config(conn_name)
    rules = policy.resolve_rules(cfg, _pack_store["packs"])          # ② 规则合成
    plugin = _manager.get_instance(conn_name)                        # 未建连实例，仅判定
    spec = plugin.classify(operation)                                # ③ 分级判定

    level_allowed_name = LEVEL_NAMES[rules["max_level"]]
    ctx = {"connection": conn_name, "operation": operation,
           "spec": spec, "rules": rules, "tool": tool, "dialect": cfg.dialect}
    for aspect in ASPECTS:                                           # ⑤ 切面链
        ctx = aspect.before(ctx)

    try:
        if cap_readonly and spec.level > Level.READONLY:
            raise PolicyDenied(
                f"{tool} 仅接受只读操作，该操作为 {LEVEL_NAMES[spec.level]} 级别",
                suggestion="写入/结构变更请改用 db_exec（受连接 level 与规则包管控）")
        policy.check(spec, rules, conn_name)                         # ④ 级别校验（执行前最后一刻，§16.4）

        eff_limit = rules["max_rows"] if limit is None else min(int(limit), rules["max_rows"])
        # 只读操作断线可安全重试；写/危险操作非幂等，不自动重试（2026-09-29 审计修订）
        result = _manager.execute_with_retry(
            conn_name,
            lambda p: p.execute(ctx["operation"], params=params, limit=eff_limit),
            retryable=(spec.level == Level.READONLY))        # ⑥⑦ 取连接实例 + 执行

        record = {"status": "ok", "elapsed_ms": result.elapsed_ms,
                  "affected": result.affected}                       # ⑧ 审计
        return _wrap_result(ctx, result, level_allowed_name, record)
    except PolicyDenied as e:
        audit.log(connection=conn_name, dialect=cfg.dialect,
                  level_needed=LEVEL_NAMES[spec.level],
                  level_allowed=level_allowed_name,
                  operation=operation, action=spec.action, status="denied",
                  error=e.message)
        _notify(ctx, e)
        raise
    except (QueryError, ConnectorError) as e:
        audit.log(connection=conn_name, dialect=cfg.dialect,
                  level_needed=LEVEL_NAMES[spec.level],
                  level_allowed=level_allowed_name,
                  operation=operation, action=spec.action, status="error",
                  error=e.message)
        _notify(ctx, e)
        raise
    except Exception as e:  # 未归一异常也须留审计痕迹，随后原样上抛给 tool_guard
        audit.log(connection=conn_name, dialect=cfg.dialect,
                  level_needed=LEVEL_NAMES[spec.level],
                  level_allowed=level_allowed_name,
                  operation=operation, action=spec.action, status="error",
                  error=f"{type(e).__name__}: {e}")
        _notify(ctx, QueryError(f"执行异常：{type(e).__name__}"))
        raise

def _wrap_result(ctx, result, level_allowed_name, record) -> dict:
    audit.log(connection=ctx["connection"], dialect=ctx["dialect"],      # §8.4 步骤 8
              level_needed=LEVEL_NAMES[ctx["spec"].level],
              level_allowed=level_allowed_name,
              operation=ctx["operation"], action=ctx["spec"].action,
              status=record["status"], elapsed_ms=record["elapsed_ms"],
              affected=record["affected"])
    payload = {
        "ok": True,
        "kind": result.kind,
        "connection": ctx["connection"],
        "level": LEVEL_NAMES[ctx["spec"].level] + " / allowed " + level_allowed_name,
        "action": ctx["spec"].action,
        "columns": result.columns,
        "rows": [list(r) for r in (result.rows or [])],
        "row_count": result.row_count,
        "affected": result.affected,
        "elapsed_ms": result.elapsed_ms,
        "truncated": result.truncated,
        "notice": result.notice,
    }
    for aspect in ASPECTS:
        payload = aspect.after(ctx, payload)
    return _json_safe(payload)

def _notify(ctx, err: ConnectorError) -> None:
    """触发错误回调（§7.1）+ 插件 on_error 钩子（异常吞掉，§5.1）。"""
    emit_error(ErrorContext(connection=ctx["connection"], operation=ctx.get("operation"),
                            level=None, error=err, when=datetime.now(timezone.utc)))
    try:
        plugin = ctx.get("plugin") or _manager.peek_instance(ctx["connection"])
        if plugin is not None:
            plugin.on_error(err, {k: v for k, v in ctx.items() if k != "plugin"})
    except Exception as e:  # noqa: BLE001 —— 钩子异常必须被底座吞掉
        log_to_stderr(e)

# ─────────────────────────────────────────────
# 能力检查辅助（元数据工具用）
# ─────────────────────────────────────────────
def _require_capability(conn_name: str, cap: str, lacking_msg: str, suggestion: str):
    cfg = _manager.get_config(conn_name)
    cls = _manager.plugins()[cfg.dialect]
    if cap not in cls.CAPABILITIES:
        raise ConnectorError(lacking_msg.format(conn=conn_name), suggestion=suggestion)
    return cfg

# ─────────────────────────────────────────────
# 5 个 MCP 工具的实现（一级分流的"去向"，§8.1）
# ─────────────────────────────────────────────
def impl_list_connections() -> dict:
    """2026-09-29 审计修复：版本探测并发化（此前串行，N 个不可达连接
    最坏阻塞 N×connect_timeout）。各连接独立线程探测，互不阻塞。"""
    from concurrent.futures import ThreadPoolExecutor

    names = sorted(_manager.configs().items())
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(names)))) as ex:
        versions = dict(zip([n for n, _ in names],
                            ex.map(lambda kv: _manager.probe_version(kv[0]),
                                   names)))
    conns = []
    for name, cfg in names:
        cls = _manager.plugins()[cfg.dialect]
        rules = policy.resolve_rules(cfg, _pack_store["packs"])
        conns.append({
            "name": name,
            "dialect": cfg.dialect,
            "version": versions.get(name) or "unknown",
            "level": LEVEL_NAMES[rules["max_level"]],
            "capabilities": sorted(cls.CAPABILITIES),
            "operation_hint": _hint_for(cls.CAPABILITIES),
            "rules": cfg.rules,
        })
    return {"ok": True, "connections": conns}

_HINTS = [
    ("sql", "operation 传 SQL 字符串"),
    ("command", "operation 传 Redis 命令字符串，如 GET user:1"),
    ("document", 'operation 传 JSON 字符串，如 {"collection":"users","method":"find"}'),
]

def _hint_for(caps) -> str:
    for cap, hint in _HINTS:
        if cap in caps:
            return hint
    return "operation 格式参见该类型插件说明"

def impl_list_tables(connection: str, namespace: str | None = None) -> dict:
    _require_capability(
        connection, "entity",
        "连接 {conn} 不支持列出实体",
        "该库无实体概念，可用 db_query 执行探查命令（见 db_list_connections 的 operation_hint）")
    entities = _manager.execute_with_retry(
        connection, lambda p: p.list_entities(namespace))
    return trim_payload(_json_safe({"ok": True, "connection": connection,
                                    "namespace": namespace, "entities": entities,
                                    "count": len(entities)}))

def impl_describe_table(connection: str, table: str,
                        namespace: str | None = None) -> dict:
    _require_capability(
        connection, "describe",
        "连接 {conn} 不支持描述实体结构",
        "该库无结构描述概念，可对单个 key 执行 db_query（如 TYPE/TTL）")
    info = _manager.execute_with_retry(
        connection, lambda p: p.describe(table, namespace))
    return trim_payload(_json_safe({
        "ok": True, "connection": connection, "entity": table,
        "describe": trim_describe(info)}))

def impl_query(connection: str, operation: str, params=None, limit=None) -> dict:
    return _run_operation(connection, operation, params, limit,
                          cap_readonly=True, tool="db_query")

def impl_exec(connection: str, operation: str, params=None) -> dict:
    return _run_operation(connection, operation, params, None,
                          cap_readonly=False, tool="db_exec")

# ─────────────────────────────────────────────
# MCP server 组装
# ─────────────────────────────────────────────
mcp = _MCPServer("omni-db-mcp")

@mcp.tool(name="db_list_connections",
          description="列出所有已配置的数据库连接：别名、类型、版本、生效权限级别、"
                      "能力列表与 operation 格式提示。做任何事之前先调它。")
@tool_guard
def db_list_connections() -> dict:
    """无参数。"""
    return impl_list_connections()

@mcp.tool(name="db_list_tables",
          description="列出某连接的实体（MySQL 表 / Mongo 集合 / Redis key 前缀分组）。")
@tool_guard
def db_list_tables(connection: str, namespace: str | None = None) -> dict:
    """connection 为连接别名；namespace 为库名/db 序号，缺省用默认库。"""
    return impl_list_tables(connection, namespace)

@mcp.tool(name="db_describe_table",
          description="描述单个实体的结构（MySQL 列+索引+DDL / Mongo 字段类型采样 / Redis key 类型+TTL+长度）。"
                      "大表的 DDL 超过 2000 字符会被截断（ddl_truncated=true），"
                      "完整 DDL 可用 db_query 执行 SHOW CREATE TABLE 单独获取。")
@tool_guard
def db_describe_table(connection: str, table: str, namespace: str | None = None) -> dict:
    """table 为实体名（表/集合/key 名）。"""
    return impl_describe_table(connection, table, namespace)

@mcp.tool(name="db_query",
          description="只读查询。操作会被解析分级，超过 readonly 的一律拒绝（改用 db_exec）。"
                      "省上下文策略：先用 COUNT 摸量、取小样本，需要更多再分页；"
                      "优先用 filter/namespace 收窄范围，避免 SELECT * 大表。"
                      "返回含 approx_tokens 粗估；超长字段值会被截断并在 notice 说明。")
@tool_guard
def db_query(connection: str, operation: str,
             params: list | None = None, limit: int | None = None) -> dict:
    """params 为参数化占位值列表（审计不记录参数值）；limit 为期望最大行数。"""
    return impl_query(connection, operation, params, limit)

@mcp.tool(name="db_exec",
          description="写入或结构变更（INSERT/UPDATE/DELETE/DDL/SET 等）。实际是否放行由连接 level 与规则包共同决定。")
@tool_guard
def db_exec(connection: str, operation: str, params: list | None = None) -> dict:
    """operation 语义与 db_query 相同，按 db_list_connections 的 operation_hint 传格式。"""
    return impl_exec(connection, operation, params)

def run() -> None:
    """stdio 方式启动 MCP server（base.py 调用）。"""
    mcp.run()
