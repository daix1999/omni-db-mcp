"""切面实现（SPEC §8.5 预留插槽的首个正式切面）。

TokenTrimAspect：结果裁剪与 token 计量，目的是控制 agent 上下文用量。
- 单值截断：str 单元格超过上限截断并在 notice 说明（防 TEXT/JSON 大字段爆上下文）
- approx_tokens：对返回体做 token 粗估（ASCII 按 4 字符/token、CJK 按 1 字符/token，
  对中文场景偏保守高估，促使 agent 谨慎），大结果追加分页建议
- trim_describe：describe 的 DDL 截断（通用值截断对 DDL 太狠，单独策略）

本模块不 import 其他 core 模块（Aspect 用同构占位接口，app 侧 duck-type
挂载），保持切面可独立测试且依赖无回环。
"""
import json


class Aspect:
    """与 core.app.Aspect 同构的最小接口占位（避免回环 import）。"""
    def before(self, ctx: dict) -> dict:
        return ctx

    def after(self, ctx: dict, result):
        return result


MAX_CELL_CHARS = 300        # 单元格字符上限（TEXT/JSON 大字段防爆）
DDL_MAX_CHARS = 2000        # describe 返回 DDL 上限
LARGE_RESULT_TOKENS = 8000  # 超过该估算值时在 notice 里建议分页


def approx_tokens(obj) -> int:
    """粗估 token 数。ASCII≈4 字符/token，CJK≈1 字符/token；大文本按
    前 4K 字符的字符构成比例外推（混合文本比例稳定，避免逐字符扫大结果）。"""
    text = obj if isinstance(obj, str) else json.dumps(
        obj, ensure_ascii=False, default=str)
    sample = text[:4096]
    if not sample:
        return 0
    ascii_ratio = sum(1 for ch in sample if ord(ch) < 128) / len(sample)
    return int(len(text) * (ascii_ratio / 4 + (1 - ascii_ratio) * 1.0))


def _truncate(text: str, limit: int) -> str:
    return text[:limit] + f"...[{len(text) - limit} chars truncated]"


def trim_payload(payload: dict, max_cell: int = MAX_CELL_CHARS,
                 large_tokens: int = LARGE_RESULT_TOKENS) -> dict:
    """返回体裁剪：rows 单元格截断 + approx_tokens 计量 + 大结果提示。
    兼容任意返回结构（无 rows 时仅做计量）。"""
    rows = payload.get("rows")
    trimmed = False
    if isinstance(rows, list):
        new_rows = []
        for r in rows:
            if isinstance(r, (list, tuple)):
                nr = [_truncate(c, max_cell)
                      if isinstance(c, str) and len(c) > max_cell else c
                      for c in r]
                if nr != list(r):
                    trimmed = True
                new_rows.append(nr)
            else:
                new_rows.append(r)
        if trimmed:
            payload["rows"] = new_rows
            note = (f"部分字段超长已截断（单值上限 {max_cell} 字符），"
                    f"需要完整值请用 SUBSTRING/字段裁剪精确取")
            payload["notice"] = (payload["notice"] + "；" + note
                                 if payload.get("notice") else note)
    payload["approx_tokens"] = approx_tokens(payload)
    if payload["approx_tokens"] > large_tokens:
        note = (f"结果较大（约 {payload['approx_tokens']} tokens），"
                f"建议用 limit 分页或收窄 filter 只取需要的列")
        payload["notice"] = (payload["notice"] + "；" + note
                             if payload.get("notice") else note)
    return payload


def trim_describe(info: dict, ddl_max: int = DDL_MAX_CHARS) -> dict:
    """describe 结果瘦身：DDL 超长截断（完整 DDL 可用 SHOW CREATE TABLE 单独取）。"""
    if isinstance(info, dict):
        ddl = info.get("ddl")
        if isinstance(ddl, str) and len(ddl) > ddl_max:
            info["ddl"] = _truncate(ddl, ddl_max)
            info["ddl_truncated"] = True
    return info


class TokenTrimAspect(Aspect):
    """执行链路的结果裁剪切面（before 无操作，after 裁剪成功 payload）。"""

    def before(self, ctx: dict) -> dict:
        return ctx

    def after(self, ctx: dict, result):
        if isinstance(result, dict) and result.get("ok"):
            return trim_payload(result)
        return result
