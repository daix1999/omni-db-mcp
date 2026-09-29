"""规则包加载 + 四层合成 + 分级拦截（SPEC §9）。

合成语义（头儿 2026-09-28 拍板，解决 SPEC §9.3 伪代码与 §14.3 示例的冲突）：
  ① 内置默认 → ② 全局规则包（只收紧）→ ③ 连接挂载包（放宽需 unsafe）
  → ④ 连接内联设置（level 视为属主授权声明，直接生效）
deny 永远是并集（只能加不能删）、allow 永远是交集（§9.3 原文）。
"""
import os

import yaml

from basePlugin import ConnectorError, PolicyDenied, Level, LEVEL_NAMES, level_from_name
from core.errors import log_to_stderr

BUILTIN_DEFAULTS = {
    "max_level": Level.READONLY,
    "max_rows": 500,
    "deny": set(),
    "allow": set(),
}

_META_KEYS = {"name", "global"}
_RULE_KEYS = {"max_level", "max_rows", "deny", "allow"}

class RulePack:
    """一个已加载并校验过的规则包。

    引用键 ref = 文件名去扩展名（如 prod-safe），与 SPEC §14.3
    `rules: [prod-safe]` 的挂载写法一致；name 仅用于展示。
    """
    def __init__(self, filename: str, name: str, is_global: bool, rules: dict):
        self.filename = filename
        self.ref = os.path.splitext(filename)[0]
        self.name = name
        self.global_ = is_global
        self.rules = rules

def _normalize_rules(raw: dict, filename: str) -> dict:
    out: dict = {}
    for key, val in raw.items():
        if key in _META_KEYS:
            continue
        if key not in _RULE_KEYS:
            # §9.1：既非元字段又非规则字段 → 警告，不得静默忽略
            log_to_stderr(f"规则包 {filename} 含未知字段 {key!r}（已忽略；"
                          f"合法规则字段：{sorted(_RULE_KEYS)}）")
            continue
        if key == "max_level":
            out["max_level"] = level_from_name(val)   # 非法取值抛 ValueError
        elif key == "max_rows":
            if not isinstance(val, int) or val <= 0:
                raise ConnectorError(f"规则包 {filename} 的 max_rows 必须是正整数，"
                                     f"当前为 {val!r}")
            out["max_rows"] = val
        elif key in ("deny", "allow"):
            if not isinstance(val, list):
                raise ConnectorError(f"规则包 {filename} 的 {key} 必须是列表")
            out[key] = {str(v) for v in val}
    return out

def load_packs(rules_dir: str) -> list:
    """扫描 rules/ 目录，返回按文件名字典序排列的规则包列表。"""
    packs = []
    if not os.path.isdir(rules_dir):
        return packs
    for fn in sorted(os.listdir(rules_dir)):
        if not fn.endswith((".yaml", ".yml")):
            continue
        path = os.path.join(rules_dir, fn)
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            raise ConnectorError(f"规则包 {fn} YAML 解析失败：{e}") from e
        if not isinstance(raw, dict):
            raise ConnectorError(f"规则包 {fn} 顶层必须是映射（key: value）结构")
        name = str(raw.get("name") or os.path.splitext(fn)[0])
        packs.append(RulePack(fn, name, bool(raw.get("global", False)),
                              _normalize_rules(raw, fn)))
    return packs

def merge(base: dict, incoming: dict, allow_relax: bool) -> dict:
    """§9.3 合成算法。max_level/max_rows 默认只收紧；deny 并集；allow 交集。"""
    out = {
        "max_level": base["max_level"],
        "max_rows": base["max_rows"],
        "deny": set(base["deny"]),
        "allow": set(base["allow"]),
    }
    if "max_level" in incoming:
        new_level = incoming["max_level"]
        out["max_level"] = new_level if allow_relax else min(out["max_level"], new_level)
    if "max_rows" in incoming:
        new_rows = incoming["max_rows"]
        out["max_rows"] = new_rows if allow_relax else min(out["max_rows"], new_rows)
    out["deny"] |= set(incoming.get("deny", set()))      # 只能加，不能删
    if "allow" in incoming:
        out["allow"] &= set(incoming["allow"])           # 只能减，不能加
    return out

def resolve_rules(conn, packs: list) -> dict:
    """对一个连接完成四层合成，返回生效规则 dict。

    conn 需提供：level、rules(挂载包引用列表)、unsafe、inline(dict)。
    挂载引用优先按文件名（ref，如 prod-safe），也兼容 name 字段。
    """
    known: dict[str, RulePack] = {}
    for p in packs:
        known[p.ref] = p
        known.setdefault(p.name, p)
    refs = sorted({p.ref for p in packs})
    mounted = []
    for pack_name in conn.rules:
        if pack_name not in known:
            raise ConnectorError(
                f"连接 {conn.name} 引用的规则包 {pack_name!r} 不存在",
                suggestion=f"rules/ 下实际可用的规则包：{refs or '（无）'}")
        mounted.append(known[pack_name])

    rules = dict(BUILTIN_DEFAULTS, deny=set(), allow=set())
    for pack in (p for p in packs if p.global_):
        rules = merge(rules, pack.rules, allow_relax=False)
    for pack in mounted:
        # 挂载包尝试放宽 → 仅当连接 unsafe: true 时生效
        rules = merge(rules, pack.rules, allow_relax=conn.unsafe)
    # ④ 连接内联设置：level 属授权声明，直接生效（已拍板语义）
    inline = {k: v for k, v in (conn.inline or {}).items() if v is not None}
    rules = merge(rules, inline, allow_relax=True)
    rules["mounted_pack_names"] = [p.name for p in mounted] + \
        [p.name for p in packs if p.global_]
    return rules

def check(spec, rules: dict, conn_name: str) -> None:
    """分级 + deny 拦截。通过则静默返回，违规抛 PolicyDenied（带 suggestion）。

    顺序：先查 deny（显式规则意图，信息更具体），再查级别上限。
    """
    hits = _deny_hits(spec, rules["deny"])
    if hits:
        raise PolicyDenied(
            f"该操作命中禁止类别 {sorted(hits)}，已被规则拦截{_note(spec)}",
            f"如确需执行，请在确认后调整 rules/ 规则包中该连接的 deny 列表，"
            f"或对该连接设置 unsafe: true 并挂载更宽松的规则包")

    if spec.level > rules["max_level"]:
        need = LEVEL_NAMES[spec.level]
        allowed = LEVEL_NAMES[rules["max_level"]]
        raise PolicyDenied(
            f"该操作需要 {need} 级别，连接 {conn_name} 的上限是 {allowed}{_note(spec)}",
            f"如需执行，请确认后在 conf/ 对应配置文件中调整该连接的 level"
            f"（当前挂载规则包：{rules.get('mounted_pack_names') or '无'}）")


def _note(spec) -> str:
    """把插件 classify 产出的判定理由带进拒绝消息（§12.4 KEYS→SCAN 提示等）。"""
    return f"（操作说明：{'；'.join(str(r) for r in spec.reasons)}）" if spec.reasons else ""

def _deny_hits(spec, deny: set) -> set:
    if not deny:
        return set()
    candidates = {str(r) for r in spec.reasons} | {spec.action.upper()}
    return {c for c in deny
            if c in candidates or c.upper() in {x.upper() for x in candidates}}
