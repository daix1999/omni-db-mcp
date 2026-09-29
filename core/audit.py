"""审计日志：SQLite 主存储 + JSONL 旁路（SPEC §10）。

写入永远包在 try/except 中，失败降级 stderr，绝不影响主流程（§10.4）。
"""
import json
import os
import re
import sqlite3
import sys
import threading
from datetime import datetime, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS operation_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,
    connection    TEXT    NOT NULL,
    dialect       TEXT    NOT NULL,
    level_needed  TEXT    NOT NULL,
    level_allowed TEXT    NOT NULL,
    operation     TEXT    NOT NULL,
    action        TEXT,
    status        TEXT    NOT NULL,
    elapsed_ms    INTEGER,
    affected      INTEGER,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_ts ON operation_log(ts);
CREATE INDEX IF NOT EXISTS idx_conn ON operation_log(connection);
"""

_SENSITIVE = re.compile(r"[A-Za-z0-9]{21,}")   # §10.3 疑似敏感值

def sanitize_operation(text: str) -> str:
    """截断超长语句 + 连续 21 位以上字母数字串以 *** 替代。"""
    if text is None:
        return ""
    text = _SENSITIVE.sub("***", str(text))
    return text[:4000] + "...[truncated]" if len(text) > 4000 else text

class AuditLogger:
    def __init__(self, log_dir: str = "logs"):
        self._dir = log_dir
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        self._backend = None          # set_backend 替换后的外部后端

    # ── 内部 ──
    def _ensure_db(self) -> sqlite3.Connection:
        if self._db is None:
            os.makedirs(self._dir, exist_ok=True)
            self._db = sqlite3.connect(os.path.join(self._dir, "operation_log.db"),
                                       check_same_thread=False)
            self._db.executescript(_SCHEMA)
            # 吞吐优化（2026-09-29 实测：逐条默认 commit 仅 ~12 QPS 且成为
            # 全局串行点；WAL+NORMAL 提升至 ~48 QPS，多进程并发写由
            # busy_timeout 兜底。进一步提速可改后台批量刷盘，见 CHANGELOG）
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.commit()
        return self._db

    def _write_jsonl(self, record: dict) -> None:
        day = record["ts"][:10]
        path = os.path.join(self._dir, f"audit-{day}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ── 对外 ──
    def set_backend(self, backend) -> None:
        """替换审计后端。backend 需实现 write(record: dict) -> None。"""
        self._backend = backend

    def log(self, *, connection: str, dialect: str, level_needed: str,
            level_allowed: str, operation: str, action: str | None,
            status: str, elapsed_ms: int | None = None,
            affected: int | None = None, error: str | None = None) -> None:
        """记录一次操作。status ∈ ok / denied / error。任何异常都不得外抛。"""
        record = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "connection": connection,
            "dialect": dialect,
            "level_needed": level_needed,
            "level_allowed": level_allowed,
            "operation": sanitize_operation(operation),
            "action": action,
            "status": status,
            "elapsed_ms": elapsed_ms,
            "affected": affected,
            "error": (str(error)[:2000] if error is not None else None),
        }
        try:
            with self._lock:
                if self._backend is not None:
                    self._backend.write(record)
                else:
                    db = self._ensure_db()
                    db.execute(
                        "INSERT INTO operation_log (ts, connection, dialect,"
                        " level_needed, level_allowed, operation, action, status,"
                        " elapsed_ms, affected, error)"
                        " VALUES (:ts, :connection, :dialect, :level_needed,"
                        " :level_allowed, :operation, :action, :status,"
                        " :elapsed_ms, :affected, :error)", record)
                    db.commit()
                    self._write_jsonl(record)
        except Exception as e:  # noqa: BLE001 —— §10.4 降级 stderr，不得静默丢弃
            try:
                print(f"[omni-db-mcp] 审计写入失败（已降级）：{type(e).__name__}: {e}",
                      file=sys.stderr)
                print(json.dumps(record, ensure_ascii=False), file=sys.stderr)
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                try:
                    self._db.close()
                except Exception:
                    pass
                self._db = None

# 模块级默认实例；启动后可被 init() 换目录（测试用）
_default = AuditLogger()

def init(log_dir: str) -> AuditLogger:
    global _default
    _default.close()
    _default = AuditLogger(log_dir)
    return _default

def get_logger() -> AuditLogger:
    return _default

def log(**kwargs) -> None:
    _default.log(**kwargs)

def set_backend(backend) -> None:
    _default.set_backend(backend)
