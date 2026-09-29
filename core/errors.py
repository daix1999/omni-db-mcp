"""异常体系 re-export + 错误回调机制（SPEC §7）。

异常类本体定义在 basePlugin.py（插件契约需要），此处保持 §7 规定的
`core/errors.py` 导入路径兼容，并提供 on_error / emit_error。
"""
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

# 兼容 SPEC §7 的导入路径：from core.errors import ConnectorError ...
from basePlugin import (  # noqa: F401
    ConnectorError, ConnectionFailed, PolicyDenied, QueryError,
    Level, LEVEL_NAMES, ServerInfo, OperationSpec, QueryResult,
)

def log_to_stderr(obj) -> None:
    """把不应影响主流程的异常留痕到 stderr（§16.6 兜底 / §7.1 回调吞异常）。"""
    try:
        print(f"[omni-db-mcp] {type(obj).__name__}: {obj}", file=sys.stderr)
    except Exception:
        pass

@dataclass
class ErrorContext:
    connection: str          # 连接别名
    operation: str | None    # 操作原文（可为 None，如建连失败时）
    level: str | None        # 判定级别名
    error: ConnectorError
    when: datetime

_CALLBACKS: list[Callable[[ErrorContext], None]] = []

def on_error(cb: Callable[[ErrorContext], None]) -> None:
    """注册错误回调，可注册多个，按注册顺序调用。"""
    _CALLBACKS.append(cb)

def clear_error_callbacks() -> None:
    """仅供测试：清空回调列表。"""
    _CALLBACKS.clear()

def emit_error(ctx: ErrorContext) -> None:
    """触发所有已注册回调。回调内异常必须被吞掉并记录到 stderr，
    不得影响主流程的返回值或异常传播（§7.1 硬性要求）。"""
    for cb in list(_CALLBACKS):
        try:
            cb(ctx)
        except Exception as e:  # noqa: BLE001 —— 规格明确要求吞掉
            log_to_stderr(f"错误回调异常（已吞掉）：{type(e).__name__}: {e}")
