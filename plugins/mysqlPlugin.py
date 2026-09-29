"""MySQL 插件（SPEC §11）：同一类服务 5.7 / 8.0，禁止按版本拆类。

方言细节全部关在本文件内；classify 基于 sqlglot AST（§11.4），
连接失效错误码（2003/2006/2013）归一为 ConnectionFailed 供底座重连（§16.2）。
"""
import re
import time
import logging

import pymysql
import sqlglot
from sqlglot import exp

logging.getLogger("sqlglot").setLevel(logging.ERROR)  # 屏蔽 GRANT 等回退解析的告噪声

from basePlugin import (DESCRIBE, ENTITY, EXPLAIN, NAMESPACE, SQL,
                        TRANSACTION, BasePlugin, ConnectionFailed,
                        ConnectorError, Level, OperationSpec, QueryError,
                        QueryResult, ServerInfo, parse_version, register)

_IDENT_RE = re.compile(r"^[A-Za-z0-9_$\u4e00-\u9fff]+$")

_DDL_TYPES = (exp.Create, exp.Drop, exp.Alter, exp.TruncateTable)
_QUERY_TYPES = (exp.Select, exp.Union, exp.Except, exp.Intersect, exp.Values)

# 无法被 sqlglot 结构化的语句：按首个关键字归类
_CMD_READONLY = {"SHOW", "USE", "DESC", "DESCRIBE", "EXPLAIN"}
_CMD_DDL = {"RENAME"}
_CMD_GRANT = {"GRANT", "REVOKE"}
_CMD_SHUTDOWN = {"SHUTDOWN", "KILL", "FLUSH", "RESET"}
_CMD_WRITE = {"SET", "BEGIN", "START", "COMMIT", "ROLLBACK", "LOAD", "DO",
              "ANALYZE", "OPTIMIZE", "REPAIR", "CACHE", "UNLOCK", "INSTALL",
              "UNINSTALL"}


def _q_ident(name: str) -> str:
    """反引号包裹标识符；非法字符直接拒绝（防注入）。"""
    if not _IDENT_RE.match(name or ""):
        raise QueryError(f"标识符 {name!r} 含非法字符",
                         suggestion="库/表名只允许字母数字下划线美元符与中文")
    return "`" + name.replace("`", "``") + "`"


@register("mysql")
class MySQLPlugin(BasePlugin):
    TYPE = "mysql"
    CONFIG_FILE = "mysqlConfig.yaml"
    CAPABILITIES = frozenset({SQL, ENTITY, DESCRIBE, NAMESPACE,
                              TRANSACTION, EXPLAIN})
    REQUIRED_FIELDS = ("host",)
    EXTRA_FIELDS = ("auth_plugin",)

    def __init__(self, name, raw):
        super().__init__(name, raw)
        self._conn = None
        self._info: ServerInfo | None = None

    # ── 生命周期 ──
    def connect(self):
        try:
            self._conn = pymysql.connect(
                host=self.raw.get("host"),
                port=int(self.raw.get("port", 3306)),
                user=self.raw.get("user", ""),
                password=self.raw.get("password", ""),
                database=self.raw.get("database") or None,
                charset=self.raw.get("charset", "utf8mb4"),   # §11.6：显式指定
                connect_timeout=int(self.raw.get("connect_timeout", 10)),
                read_timeout=int(self.raw.get("timeout", 30)),  # §8.6 超时下发
                write_timeout=int(self.raw.get("timeout", 30)),
                autocommit=True,
            )
        except pymysql.MySQLError as e:
            self._conn = None
            raise ConnectionFailed(
                f"MySQL 连接 {self.name} 建立失败：{e.args[1] if len(e.args) > 1 else e}",
                suggestion="检查 host/port/账号密码/网络可达性；生产库请确认只读账号") from e

    def close(self):
        try:
            if self._conn:
                self._conn.close()
        except Exception:  # noqa: BLE001 —— 契约：close 不得抛
            pass
        finally:
            self._conn = None

    def health_check(self) -> bool:
        try:
            with self._conn.cursor() as c:
                c.execute("SELECT 1")
            return True
        except Exception:  # noqa: BLE001
            return False

    # ── 元信息 ──
    def server_info(self) -> ServerInfo:
        if self._info is None:                       # §11.3 只探测一次
            row = self._query_one("SELECT VERSION()")
            ver = str(row[0]) if row else "unknown"
            self._info = ServerInfo("mysql", ver, parse_version(ver))
        return self._info

    @property
    def _is_8plus(self) -> bool:
        return self.server_info().version_tuple >= (8, 0)

    # ── 元数据 ──
    def list_namespaces(self):
        return [r[0] for r in self._query_all("SHOW DATABASES")]

    def list_entities(self, namespace=None):
        sql = "SHOW TABLES"
        if namespace:
            sql += " FROM " + _q_ident(namespace)
        return [r[0] for r in self._query_all(sql)]

    def describe(self, entity, namespace=None):
        tbl = _q_ident(entity)
        if namespace:
            tbl = _q_ident(namespace) + "." + tbl
        db = namespace or self.raw.get("database") or ""
        cols = self._query_all(
            "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_KEY, EXTRA,"
            " COLUMN_COMMENT FROM information_schema.columns"
            " WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
            (db, entity))
        idx_rows, idx_desc = self._query_all_with_cols("SHOW INDEX FROM " + tbl)
        indexes = [dict(zip(idx_desc, r)) for r in idx_rows]
        ddl = self._query_one("SHOW CREATE TABLE " + tbl)
        return {"columns": [
                    {"name": c[0], "type": c[1], "nullable": c[2] == "YES",
                     "key": c[3], "extra": c[4], "comment": c[5]}
                    for c in cols],
                "indexes": indexes,
                "ddl": ddl[1] if ddl and len(ddl) > 1 else None}

    # ── 分级判定（§11.4：必须 AST，兜底 DANGER）──
    def classify(self, operation):
        text = (operation or "").strip()
        if not text:
            raise ConnectorError("operation 为空，无法判定级别")
        try:
            statements = sqlglot.parse(text, dialect="mysql")
        except sqlglot.errors.ParseError:
            # §16.4 默认拒绝：解析不出来 → 按 DANGER 交策略层拦截，
            # 绝不猜测放行；execute() 同句会再解析失败并明确报错。
            return OperationSpec(Level.DANGER, "UNPARSEABLE", (),
                                 ("unrecognized_statement",))
        statements = [s for s in statements if s is not None]
        if len(statements) > 1:
            return OperationSpec(Level.DANGER, "MULTI", (), ("multi_statement",))
        if not statements:
            return OperationSpec(Level.DANGER, "EMPTY", (), ("unrecognized_statement",))

        stmt = statements[0]
        targets = tuple(dict.fromkeys(
            t.name for t in stmt.find_all(exp.Table) if t.name))

        if isinstance(stmt, _DDL_TYPES):
            return OperationSpec(Level.DANGER, "DDL", targets, ("DDL",))
        if isinstance(stmt, _QUERY_TYPES):
            return OperationSpec(Level.READONLY, "SELECT", targets, ())
        if isinstance(stmt, (exp.Insert, exp.Update, exp.Delete)):
            action = {exp.Insert: "INSERT", exp.Update: "UPDATE",
                      exp.Delete: "DELETE"}[type(stmt)]
            reasons = []
            if action in ("UPDATE", "DELETE") and not stmt.args.get("where"):
                reasons.append(f"{action.lower()}_without_where")
            return OperationSpec(Level.WRITE, action, targets, tuple(reasons))
        if isinstance(stmt, (exp.Show, exp.Use)):
            return OperationSpec(Level.READONLY, type(stmt).__name__.upper(),
                                 targets, ())
        if isinstance(stmt, exp.Describe):
            # DESCRIBE t / EXPLAIN <stmt>（2026-09-29 审计修复：此前落入
            # 兜底被误判 DANGER，readonly 连接无法看执行计划）。
            # 注意 EXPLAIN ANALYZE 会真执行内部语句：只对纯查询放行，
            # 内部为 DML 时默认拒绝（MySQL 本身也不支持 ANALYZE DML）。
            if stmt.args.get("style") == "ANALYZE":
                if isinstance(stmt.args.get("this"), _QUERY_TYPES):
                    return OperationSpec(Level.READONLY, "EXPLAIN ANALYZE",
                                         targets, ())
                return OperationSpec(Level.DANGER, "EXPLAIN ANALYZE", targets,
                                     ("unrecognized_statement",))
            action = "EXPLAIN" if isinstance(stmt.args.get("this"), _QUERY_TYPES) \
                else "DESCRIBE"
            return OperationSpec(Level.READONLY, action, targets, ())
        if isinstance(stmt, exp.Kill):
            return OperationSpec(Level.DANGER, "KILL", (), ("shutdown",))
        if isinstance(stmt, exp.Set):
            return OperationSpec(Level.WRITE, "SET", (), ())
        if isinstance(stmt, exp.Command):
            head = str(getattr(stmt, "this", "") or stmt.name).split(maxsplit=1)[0]
            head = head.upper()
            return self._classify_command(head, targets)
        # 其余可解析但未列举的语句类型 → 默认拒绝（§16.4）
        return OperationSpec(Level.DANGER, "UNKNOWN", targets,
                             ("unrecognized_statement",))

    def _classify_command(self, head, targets):
        if head in _CMD_READONLY:
            return OperationSpec(Level.READONLY, head, targets, ())
        if head in _CMD_DDL:
            return OperationSpec(Level.DANGER, head, targets, ("DDL",))
        if head in _CMD_GRANT:
            return OperationSpec(Level.DANGER, head, targets, ("grant",))
        if head in _CMD_SHUTDOWN:
            return OperationSpec(Level.DANGER, head, targets, ("shutdown",))
        if head in _CMD_WRITE:
            return OperationSpec(Level.WRITE, head, targets, ())
        return OperationSpec(Level.DANGER, head or "UNKNOWN", targets,
                             ("unrecognized_statement",))

    # ── 执行 ──
    def execute(self, operation, params=None, limit=None):
        text = (operation or "").strip()
        try:
            statements = [s for s in sqlglot.parse(text, dialect="mysql")
                          if s is not None]
        except sqlglot.errors.ParseError as e:
            raise QueryError(f"SQL 解析失败：{str(e).splitlines()[0]}",
                             suggestion="修正语法后重试") from e
        is_query = bool(statements) and isinstance(statements[0], _QUERY_TYPES)

        if is_query:
            self._guard_version_features(statements[0])
            sql, notice = self._inject_limit(statements[0], text, limit)
        elif len(statements) == 1:
            sql, notice = text, None
        else:  # 多语句且规则放行（danger + 无 deny）：逐条执行
            return self._execute_multi(statements, limit)

        start = time.monotonic()
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql, tuple(params) if params else None)
                if cur.description is not None:
                    columns = [d[0] for d in cur.description]
                    rows = cur.fetchall()
                    truncated = limit is not None and len(rows) >= limit
                    result = QueryResult(kind="table", columns=columns,
                                         rows=[tuple(r) for r in rows],
                                         row_count=len(rows),
                                         affected=cur.rowcount if cur.rowcount >= 0 else 0,
                                         truncated=bool(truncated), notice=notice)
                else:
                    if not self._conn.get_autocommit():
                        self._conn.commit()
                    result = QueryResult(kind="table", affected=cur.rowcount,
                                         notice=notice)
        except pymysql.err.OperationalError as e:
            raise self._map_operational(e) from e
        except pymysql.err.ProgrammingError as e:
            msg = e.args[1] if len(e.args) > 1 else str(e)
            if "syntax" in str(msg).lower() or e.args and e.args[0] in (1064, 1065):
                msg = f"{msg}（若为版本兼容问题，见 db_list_connections 版本列）"
            raise QueryError(f"MySQL 语句错误：{msg}",
                             suggestion="检查表名/列名/语法与目标库版本") from e
        except pymysql.MySQLError as e:
            raise QueryError(f"MySQL 操作失败：{e.args[1] if len(e.args) > 1 else e}") from e
        result.elapsed_ms = int((time.monotonic() - start) * 1000)
        return result

    def _execute_multi(self, statements, limit):
        affected = 0
        last = QueryResult(kind="table")
        for sub in statements:
            res = self.execute(sub.sql(dialect="mysql"), limit=limit)
            affected += res.affected
            last = res
        last.affected = affected
        last.notice = (last.notice or "") + f"；共执行 {len(statements)} 条语句"
        return last

    def _map_operational(self, e):
        code = e.args[0] if e.args else None
        msg = e.args[1] if len(e.args) > 1 else str(e)
        if code in (2003, 2006, 2013):     # 断连/失联 → 触发底座重连重试
            return ConnectionFailed(f"MySQL 连接失效（{code}）：{msg}")
        if code == 1062:
            return QueryError(f"唯一键冲突：{msg}",
                              suggestion="检查主键/唯一索引是否重复")
        if code == 1146:
            return QueryError(f"表不存在：{msg}",
                              suggestion="先用 db_list_tables 确认表名与 namespace")
        if code == 1305 or "timeout" in str(msg).lower():
            return QueryError(f"MySQL 执行超时/失败：{msg}",
                              suggestion="可缩小查询范围或调整连接的 timeout")
        return QueryError(f"MySQL 操作失败（{code}）：{msg}")

    def _guard_version_features(self, stmt):
        """§11.6：5.7 遇到 CTE / 窗口函数必须明确报错，不得擅自改写。"""
        if self._is_8plus:
            return
        feature = None
        if stmt.find(exp.CTE) is not None or isinstance(stmt.args.get("with"), exp.With):
            feature = "CTE（WITH 子句）"
        elif stmt.find(exp.Window) is not None:
            feature = "窗口函数（OVER）"
        if feature:
            raise QueryError(
                f"该语法需要 MySQL 8.0 及以上，当前连接 {self.name} 的版本是 "
                f"{self.server_info().version}",
                suggestion="可改用子查询，或在支持 8.0 的连接上测试该语句")

    def _inject_limit(self, stmt, original_sql, limit):
        """§11.5：仅 SELECT 注入；原 LIMIT 更小则保留。"""
        if limit is None:
            return original_sql, None
        cur_limit = stmt.args.get("limit")
        if cur_limit is not None:
            try:
                cur_val = int(cur_limit.expression.sql())
            except (ValueError, AttributeError):
                return original_sql, None      # 非常量 LIMIT：不动原句
            if cur_val <= limit:
                return original_sql, None
            new_sql = stmt.copy().limit(limit).sql(dialect="mysql")
            return new_sql, f"原 LIMIT {cur_val} 超过生效上限，已收敛为 {limit}"
        new_sql = stmt.copy().limit(limit).sql(dialect="mysql")
        return new_sql, f"已自动注入 LIMIT {limit}"

    # ── 内部查询助手 ──
    def _query_all(self, sql, args=None):
        rows, _ = self._query_all_with_cols(sql, args)
        return rows

    def _query_all_with_cols(self, sql, args=None):
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql, args or None)
                cols = [d[0] for d in cur.description] if cur.description else []
                return cur.fetchall(), cols
        except pymysql.err.OperationalError as e:
            raise self._map_operational(e) from e
        except pymysql.MySQLError as e:
            raise QueryError(f"MySQL 元数据查询失败：{e.args[1] if len(e.args) > 1 else e}") from e

    def _query_one(self, sql, args=None):
        rows, _ = self._query_all_with_cols(sql, args)
        return rows[0] if rows else None
