"""假插件（SPEC §16.7）：不连任何真实数据库，供底座全链路测试。

通过连接配置里的行为开关模拟各类失败路径：
  fail_connect: true   —— connect() 抛 ConnectionFailed
  lost_first:   true   —— 首次 execute 抛 ConnectionFailed（触发 §16.2 重连重试）
  crash:        true   —— execute 抛非本系统异常（触发 §16.6 工具兜底）
  fail_query:   true   —— execute 抛 QueryError
"""
from basePlugin import (COMMAND, DESCRIBE, ENTITY, NAMESPACE, SQL,
                        BasePlugin, ConnectionFailed, Level, OperationSpec,
                        QueryError, QueryResult, ServerInfo, register)

class _MockBase(BasePlugin):
    EXTRA_FIELDS = ("fail_connect", "lost_first", "crash", "fail_query")
    _already_lost = set()   # 类级：每个别名只"断一次"，模拟真实的一次性断连
    _connect_total = {}     # 类级：按别名统计累计建连次数（重连测试用）

    def __init__(self, name, raw):
        super().__init__(name, raw)
        self.connected = False
        self.connect_calls = 0
        self.exec_count = 0
        self._info = None

    def connect(self):
        self.connect_calls += 1
        _MockBase._connect_total[self.name] = _MockBase._connect_total.get(self.name, 0) + 1
        if self.raw.get("fail_connect"):
            raise ConnectionFailed(f"mock 连接 {self.name} 按配置拒绝建立")
        self.connected = True

    def close(self):
        self.connected = False

    def health_check(self):
        return self.connected

    def server_info(self):
        if self._info is None:
            self._info = ServerInfo(self.TYPE, "9.9.9-mock", (9, 9, 9))
        return self._info

    def describe(self, entity, namespace=None):
        return {"columns": [{"name": "id", "type": "int"},
                            {"name": "name", "type": "varchar"}]}

    def execute(self, operation, params=None, limit=None):
        if self.raw.get("lost_first") and self.name not in self._already_lost:
            self._already_lost.add(self.name)
            self.exec_count += 1
            self.connected = False
            raise ConnectionFailed("mock: 连接已断开（模拟 wait_timeout）")
        self.exec_count += 1
        if self.raw.get("crash"):
            raise ValueError("mock 故意抛出的非本系统异常")
        if self.raw.get("fail_query"):
            raise QueryError("mock: 语句执行失败", suggestion="检查语句")
        cap = limit if limit is not None else 5
        rows = [(i, f"row{i}") for i in range(min(5, cap))]
        return QueryResult(kind="table", columns=["id", "name"], rows=rows,
                           row_count=len(rows), affected=0, elapsed_ms=1,
                           truncated=cap < 5,
                           notice=f"已按上限注入 LIMIT {cap}" if cap < 5 else None)

    def classify(self, operation):
        """测试用途的简单关键字判定（SPEC 允许 mock 复用关键字方式）。"""
        text = (operation or "").strip().lower()
        if not text:
            raise QueryError("operation 为空")
        if ";" in text[:-1]:
            return OperationSpec(Level.DANGER, "MULTI", (), ("multi_statement",))
        head = text.split(maxsplit=1)[0]
        if head in ("select", "show", "describe", "desc", "explain"):
            return OperationSpec(Level.READONLY, head.upper(), (), ())
        if head in ("insert", "update", "delete"):
            reasons = []
            if head in ("update", "delete") and " where " not in text:
                reasons.append(f"{head}_without_where")
            return OperationSpec(Level.WRITE, head.upper(), (), tuple(reasons))
        if head in ("create", "alter", "drop", "truncate", "rename"):
            return OperationSpec(Level.DANGER, head.upper(), (), ("DDL",))
        if head in ("grant", "revoke"):
            return OperationSpec(Level.DANGER, head.upper(), (), ("grant",))
        if head in ("shutdown", "kill", "flush"):
            return OperationSpec(Level.DANGER, head.upper(), (), ("shutdown",))
        return OperationSpec(Level.DANGER, "UNKNOWN", (), ("unrecognized_statement",))


@register("mock")
class MockPlugin(_MockBase):
    TYPE = "mock"
    CONFIG_FILE = "mockConfig.yaml"
    CAPABILITIES = frozenset({SQL, ENTITY, DESCRIBE, NAMESPACE})

    def list_namespaces(self):
        return ["ns1", "ns2"]

    def list_entities(self, namespace=None):
        return ["t1", "t2"]


@register("mockkv")
class MockKvPlugin(_MockBase):
    """无 ENTITY / DESCRIBE 能力的类型，用于测试能力拦截（用例 1）。"""
    TYPE = "mockkv"
    CONFIG_FILE = "mockkvConfig.yaml"
    CAPABILITIES = frozenset({COMMAND, NAMESPACE})

    def list_namespaces(self):
        return ["db0"]

    def list_entities(self, namespace=None):
        return []

    def describe(self, entity, namespace=None):
        from basePlugin import ConnectorError
        raise ConnectorError("mockkv 不支持 describe")
