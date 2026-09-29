"""插件契约：能力常量 + 数据结构 + 异常体系 + 抽象基类 + 注册表。

插件作者只需 import 本文件（SPEC §5、§6、验收标准 #2）。

实现说明（对 SPEC 的两处受控偏差，均不引入依赖回环）：
  1) 异常体系定义在本文件而非 core/errors.py —— 因为插件必须抛
     ConnectionFailed/QueryError，而验收标准要求插件只 import basePlugin；
     core/errors.py 从本文件 re-export 并补充错误回调机制。
  2) BasePlugin 增加两个普通类属性 REQUIRED_FIELDS / EXTRA_FIELDS（非抽象方法），
     供底座在配置加载期做 §8.3 校验（缺字段报错、未知字段警告）。SPEC §5.2
     只禁止添加 probe() 方法，未禁止契约携带元数据声明。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import IntEnum

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
# ② 级别枚举（SPEC §6.1）
# ─────────────────────────────────────────────
class Level(IntEnum):
    READONLY = 1    # 元数据读取与数据查询
    WRITE    = 2    # INSERT / UPDATE / DELETE
    DANGER   = 3    # DDL / DROP / 无条件的写入 / 危险命令

LEVEL_NAMES = {Level.READONLY: "readonly", Level.WRITE: "write", Level.DANGER: "danger"}

def level_from_name(name: str) -> "Level":
    """'readonly'/'write'/'danger'（大小写不敏感）→ Level；非法值抛 ValueError。"""
    try:
        return Level[str(name).upper()]
    except KeyError:
        raise ValueError(f"非法级别 {name!r}，合法取值：readonly / write / danger") from None

# ─────────────────────────────────────────────
# ③ 数据结构（SPEC §6.2–§6.4）
# ─────────────────────────────────────────────
@dataclass(frozen=True)
class ServerInfo:
    dialect: str                    # "mysql" / "redis" / "mongo"
    version: str                    # 原始版本字符串，如 "5.7.43"
    version_tuple: tuple            # 解析后的元组，如 (5, 7, 43)
    extra: dict = field(default_factory=dict)

@dataclass(frozen=True)
class OperationSpec:
    level: "Level"
    action: str                     # "SELECT" / "DROP" / "GET" / "find" ...
    targets: tuple = ()             # 涉及的表 / 集合 / key 模式
    reasons: tuple = ()             # 判定理由，供上层匹配 deny 类别

@dataclass
class QueryResult:
    kind: str = "table"             # 预留字段。首版只使用 "table"（§6.4）
    columns: list = None            # type: ignore[assignment]
    rows: list = None               # type: ignore[assignment]
    row_count: int = 0
    affected: int = 0
    elapsed_ms: int = 0
    truncated: bool = False         # 是否因超过上限被截断
    notice: str = None              # type: ignore[assignment]

# ─────────────────────────────────────────────
# ④ 异常体系（SPEC §7；定义位置偏差见模块头注释）
# ─────────────────────────────────────────────
class ConnectorError(Exception):
    """所有本系统异常的基类。message 给 AI 看，suggestion 可选。"""
    def __init__(self, message: str, suggestion: str | None = None):
        super().__init__(message)
        self.message = message
        self.suggestion = suggestion

class ConnectionFailed(ConnectorError):
    """连接建立失败、连接不可用（含驱动报"连接已断开"类错误）。"""

class PolicyDenied(ConnectorError):
    """被规则或级别拦截。必须带 suggestion，说明如何调整配置。"""

class QueryError(ConnectorError):
    """执行出错（语法错误、超时、权限不足等）。"""

# ─────────────────────────────────────────────
# ⑤ 插件契约（SPEC §5.1，9 个抽象方法）
# ─────────────────────────────────────────────
class BasePlugin(ABC):
    """所有数据库插件的抽象基类。

    子类必须设置：
        TYPE           插件类型标识，与 @register 的参数一致
        CONFIG_FILE    该插件的配置文件，位于 conf/ 目录下
        CAPABILITIES   能力集合，取值来自本文件常量
    子类可选设置：
        REQUIRED_FIELDS  配置必须携带的顶层字段（缺失则启动期报错）
        EXTRA_FIELDS     除底座通用字段外，本插件认识的额外字段
                         （避免被 §8.3 的"未知字段警告"误伤）
    """

    TYPE: str = ""
    CONFIG_FILE: str = ""
    CAPABILITIES: frozenset = frozenset()
    REQUIRED_FIELDS: tuple = ()
    EXTRA_FIELDS: tuple = ()

    def __init__(self, name: str, raw: dict):
        self.name = name          # 连接别名
        self.raw = raw            # 已完成 ${env:} 解析的原始配置

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
    def server_info(self) -> ServerInfo:
        """探测服务端信息（版本等）。实现内必须缓存，同一实例只探测一次。"""

    # ── 元数据 ──
    @abstractmethod
    def list_namespaces(self) -> list:
        """列出命名空间。不支持该能力时返回空列表。"""

    @abstractmethod
    def list_entities(self, namespace: str | None = None) -> list:
        """列出实体（表 / 集合 / key 前缀）。不支持该能力时返回空列表。"""

    @abstractmethod
    def describe(self, entity: str, namespace: str | None = None) -> dict:
        """描述实体结构。不支持该能力时抛 ConnectorError。"""

    # ── 执行 ──
    @abstractmethod
    def execute(self, operation: str, params: list | None = None,
                limit: int | None = None) -> QueryResult:
        """执行一次操作。operation 为不透明字符串，语义由插件解释。

        实现内必须注入 rows 上限（取 limit 与自身上限的较小值）；
        失败必须抛 QueryError；连接失效必须抛 ConnectionFailed（底座据此重连重试）。
        """

    @abstractmethod
    def classify(self, operation: str) -> OperationSpec:
        """判定操作所需级别。只判定，不执行。必须基于结构化解析。"""

    # ── 生命周期钩子（可选覆盖）──
    def on_error(self, exc: Exception, context: dict) -> None:
        """插件级错误钩子。默认空实现。实现内抛出的异常由底座吞掉。"""

# ─────────────────────────────────────────────
# ⑥ 注册表（SPEC §5.1 ③）
# ─────────────────────────────────────────────
PLUGINS: dict[str, type[BasePlugin]] = {}

def register(type_name: str):
    """插件注册装饰器。校验失败一律抛 ValueError（Step 1 验收项）。"""
    def deco(cls: type[BasePlugin]):
        if type_name in PLUGINS:
            raise ValueError(f"插件类型 {type_name!r} 重复注册："
                             f"{PLUGINS[type_name].__name__} 与 {cls.__name__}")
        if not getattr(cls, "CAPABILITIES", frozenset()):
            raise ValueError(f"{cls.__name__} 必须声明非空 CAPABILITIES")
        if not getattr(cls, "CONFIG_FILE", ""):
            raise ValueError(f"{cls.__name__} 必须声明 CONFIG_FILE")
        unknown = set(cls.CAPABILITIES) - ALL_CAPABILITIES
        if unknown:
            raise ValueError(f"{cls.__name__} 的 CAPABILITIES 含未知能力 "
                             f"{sorted(unknown)}，合法取值：{sorted(ALL_CAPABILITIES)}")
        cls.TYPE = cls.TYPE or type_name
        PLUGINS[type_name] = cls
        return cls
    return deco

def parse_version(text: str) -> tuple:
    """从版本字符串提取前导数字段，如 '5.7.43-log' → (5, 7, 43)。"""
    import re
    nums = re.findall(r"\d+", text or "")
    return tuple(int(n) for n in nums[:4]) or (0,)
