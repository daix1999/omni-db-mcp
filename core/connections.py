"""配置加载 + 校验 + 连接管理 + 二级分流 + 保活重连（SPEC §8.2/§8.3/§14/§16.2）。

依赖方向：只 import basePlugin 与 core/errors，绝不 import 任何具体插件
（通过 PLUGINS 注册表取类，SPEC §4.2）。
"""
import os
import re

import yaml

from basePlugin import (PLUGINS, ConnectionFailed, ConnectorError, QueryError,
                        level_from_name)
from core.errors import log_to_stderr

# 别名规则：只允许 [A-Za-z0-9_-]（§8.3 / §14.1）
_ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_ENV_RE = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}")

# 底座通用字段（§14.2）+ 规则内联字段（§9.2 ④）
COMMON_FIELDS = {
    "host", "port", "user", "password", "database", "path", "charset", "uri",
    "level", "rules", "unsafe", "timeout", "connect_timeout",
    "max_level", "max_rows", "deny", "allow",
}

class ConnectionConfig:
    """一个连接的配置（§6.5）。raw 为已完成 ${env:} 解析的字段字典。"""
    def __init__(self, name, dialect, level, rules, unsafe, timeout,
                 connect_timeout, raw, inline):
        self.name = name
        self.dialect = dialect
        self.level = level
        self.rules = rules
        self.unsafe = unsafe
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.raw = raw
        self.inline = inline     # ④ 内联规则字段 {max_level, max_rows, deny, allow}

def resolve_env(value: str, conn_name: str) -> str:
    """替换字符串中的 ${env:VAR}；变量未设置时报出变量名（§8.3 硬性要求）。"""
    def sub(m):
        var = m.group(1)
        val = os.environ.get(var)
        if val is None:
            raise ConnectorError(
                f"连接 {conn_name} 引用的环境变量 {var} 未设置",
                suggestion=f"请在 .env 中添加 {var}=... 后重启")
        return val
    return _ENV_RE.sub(sub, value)

def _load_yaml(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except yaml.YAMLError as e:
        loc = ""
        mark = getattr(e, "problem_mark", None)
        if mark is not None:
            loc = f"（第 {mark.line + 1} 行第 {mark.column + 1} 列）"
        raise ConnectorError(f"配置文件 {os.path.basename(path)} YAML 语法错误{loc}：{e}") from e

class ConnectionManager:
    """扫描 conf/、校验、惰性建连、实例缓存、断线重试。"""

    def __init__(self, conf_dir: str = "conf", plugins: dict | None = None):
        self._conf_dir = conf_dir
        self._plugins = PLUGINS if plugins is None else plugins
        self._configs: dict[str, ConnectionConfig] = {}
        self._instances: dict[str, object] = {}
        self._connected: set[str] = set()
        self.loaded = False

    def plugins(self) -> dict:
        """只读访问注册表（供 app 层查 capabilities，不暴露可变引用）。"""
        return dict(self._plugins)

    # ── 启动期：加载 + 校验（§8.2 1/4）──
    def load(self) -> None:
        self._configs.clear()
        self._instances.clear()
        for type_name, cls in self._plugins.items():
            path = os.path.join(self._conf_dir, cls.CONFIG_FILE)
            if not os.path.isfile(path):
                log_to_stderr(f"未找到 {path}，请参考 .env.example 创建"
                              f"（类型 {type_name} 暂无可用连接）")
                continue
            raw_doc = _load_yaml(path)
            if not isinstance(raw_doc, dict):
                raise ConnectorError(f"{cls.CONFIG_FILE} 顶层必须是"
                                     f"「连接别名: 字段映射」结构")
            for alias, fields in raw_doc.items():
                cfg = self._build_config(type_name, cls, str(alias), fields)
                if cfg.name in self._configs:
                    raise ConnectorError(
                        f"连接别名 {cfg.name!r} 重复（{cls.CONFIG_FILE} 与其他文件冲突）")
                self._configs[cfg.name] = cfg
        self.loaded = True

    def _build_config(self, type_name, cls, alias, fields) -> ConnectionConfig:
        if not _ALIAS_RE.match(alias):
            raise ConnectorError(
                f"连接别名 {alias!r} 非法：只允许字母、数字、下划线与连字符")
        if not isinstance(fields, dict):
            raise ConnectorError(f"连接 {alias} 的配置必须是字段映射")

        known = COMMON_FIELDS | set(cls.EXTRA_FIELDS)
        for key in fields:
            if key not in known:
                # §8.3：未知字段必须警告，不得静默忽略
                log_to_stderr(f"警告：连接 {alias}（{cls.CONFIG_FILE}）含未知字段 "
                              f"{key!r}，请检查是否拼写错误（该字段将被插件忽略）")

        raw = {}
        for key, val in fields.items():
            raw[key] = resolve_env(val, alias) if isinstance(val, str) else val

        for req in cls.REQUIRED_FIELDS:
            if req not in raw or raw[req] in (None, ""):
                raise ConnectorError(
                    f"连接 {alias} 缺少必须字段 {req!r}（类型 {type_name} 需要）",
                    suggestion=f"请在 {cls.CONFIG_FILE} 的连接 {alias} 下补充 {req}")

        try:
            level = level_from_name(raw.get("level", "readonly"))
        except ValueError as e:
            raise ConnectorError(f"连接 {alias} 的 level 非法：{e}") from None

        # 内联规则层（§9.2 ④）：只有 YAML 里显式写了才参与合成；
        # 未写 level 时留空，由合成链回落「内置默认 readonly」。
        inline = {"max_level": None,
                  "max_rows": raw.get("max_rows"),
                  "deny": set(raw["deny"]) if raw.get("deny") else None,
                  "allow": set(raw["allow"]) if raw.get("allow") else None}
        if "max_level" in raw:
            try:
                inline["max_level"] = level_from_name(raw["max_level"])
            except ValueError as e:
                raise ConnectorError(f"连接 {alias} 的 max_level 非法：{e}") from None
        elif "level" in raw:
            inline["max_level"] = level_from_name(raw["level"])

        return ConnectionConfig(
            name=alias, dialect=type_name, level=level,
            rules=list(raw.get("rules") or []), unsafe=bool(raw.get("unsafe", False)),
            timeout=int(raw.get("timeout", 30)),
            connect_timeout=int(raw.get("connect_timeout", 10)),
            raw=raw, inline=inline)

    # ── 查询 ──
    def configs(self) -> dict:
        if not self.loaded:
            self.load()
        return dict(self._configs)

    def get_config(self, name: str) -> ConnectionConfig:
        cfg = self.configs().get(name)
        if cfg is None:
            raise ConnectorError(
                f"连接 {name!r} 不存在",
                suggestion=f"可用连接：{sorted(self._configs) or '（无，请检查 conf/ 配置）'}")
        return cfg

    # ── 二级分流 + 惰性建连（§8.2 2/3，§8.4 步骤 6）──
    def get_instance(self, name: str):
        """取（或构造）插件实例，不建连。用于执行链路步骤 3 的 classify。"""
        cfg = self.get_config(name)
        inst = self._instances.get(name)
        if inst is None:
            cls = self._plugins[cfg.dialect]
            inst = cls(name, cfg.raw)
            self._instances[name] = inst
        return inst

    def get_plugin(self, name: str):
        """取已连接、可用的插件实例（首次使用时才建连）。"""
        inst = self.get_instance(name)
        if name not in self._connected:
            try:
                inst.connect()
            except ConnectorError:
                raise
            except Exception as e:  # 驱动原生建连异常归一（§16.1）
                raise ConnectionFailed(f"连接 {name} 建立失败：{e}") from e
            self._connected.add(name)
        return inst

    def cached_version(self, name: str):
        """仅当已建连时返回探测版本；不为列连接而主动建连（§8.2 惰性原则）。"""
        if name in self._connected:
            try:
                return self._instances[name].server_info().version
            except Exception as e:  # noqa: BLE001
                log_to_stderr(e)
        return None

    def probe_version(self, name: str):
        """db_list_connections 专用：惰性建连并探测版本（SPEC §15.1 要求返回版本）。
        单连接不可达不影响整体列出，返回带原因的占位串。"""
        try:
            return self.get_plugin(name).server_info().version
        except ConnectorError as e:
            log_to_stderr(f"探测连接 {name} 版本失败：{e.message}")
            return f"不可达（{e.message.splitlines()[0][:60]}）"

    def drop(self, name: str) -> None:
        """丢弃实例（重连路径内部使用）。"""
        self._connected.discard(name)
        inst = self._instances.pop(name, None)
        if inst is not None:
            try:
                inst.close()
            except Exception as e:  # noqa: BLE001
                log_to_stderr(e)

    def execute_with_retry(self, name: str, fn, *, retryable: bool = True):
        """执行 fn(plugin)；连接失效时的处理（§16.2，2026-09-29 审计修订）：

        - 只读操作（retryable=True）：重连并重试原操作一次——只读幂等，安全；
        - 写/危险操作（retryable=False）：**不自动重试**——INSERT/INCR/SET 等
          非幂等，首次请求可能已到达服务器（仅响应丢失），自动重试会造成
          静默重复写。改为抛 QueryError 并明确提示 AI 先确认首次是否生效。
        """
        plugin = self.get_plugin(name)
        try:
            return fn(plugin)
        except ConnectionFailed as first:
            if not retryable:
                raise QueryError(
                    f"连接 {name} 断开：{first.message}；本次为写/危险操作，"
                    f"未自动重试",
                    suggestion="写操作重复执行可能造成重复数据——请先确认首次执行"
                               "是否已生效（如查目标行），确认未生效后再重试") from first
            self.drop(name)
            try:
                plugin = self.get_plugin(name)      # 重新建连
                return fn(plugin)                    # 重试原操作一次
            except ConnectionFailed as second:
                raise QueryError(
                    f"连接 {name} 断线且重连后重试仍失败：{second.message}",
                    second.suggestion or "请检查数据库服务状态后重试") from second

    def peek_instance(self, name: str):
        """取已构造的实例（可能未建连），不存在则返回 None。错误钩子用。"""
        return self._instances.get(name)

    def close_all(self) -> None:
        for name in list(self._instances):
            self.drop(name)

manager = ConnectionManager()
