"""Redis 插件（SPEC §12）：键值型，仅声明 COMMAND / NAMESPACE 能力。

不声明 ENTITY / DESCRIBE：db_list_tables、db_describe_table 会在工具层
被能力门拦下并给出 SCAN 提示（§15.1、DESIGN §7）；这两个方法仍实现，
供直连调用与底座内部使用。classify 走命令名分级表（§12.4），未知命令
默认 DANGER；KEYS 会阻塞实例，判 DANGER 并建议 SCAN（§12.4 特别处理）。
"""
import shlex
import time

import redis

from basePlugin import (COMMAND, NAMESPACE, BasePlugin, ConnectionFailed,
                        ConnectorError, Level, OperationSpec, QueryError,
                        QueryResult, ServerInfo, parse_version, register)

REDIS_READ = {
    "GET", "MGET", "EXISTS", "TTL", "PTTL", "TYPE", "STRLEN", "DUMP",
    "HGET", "HMGET", "HGETALL", "HLEN", "HEXISTS", "HKEYS", "HVALS",
    "LRANGE", "LLEN", "LINDEX", "LPOS",
    "SMEMBERS", "SCARD", "SISMEMBER", "SRANDMEMBER",
    "ZRANGE", "ZREVRANGE", "ZSCORE", "ZCARD", "ZRANK", "ZCOUNT",
    "SCAN", "SSCAN", "HSCAN", "ZSCAN", "DBSIZE", "INFO", "PING", "ECHO",
    "GETRANGE", "BITCOUNT", "HRANDFIELD", "MEMORY", "LOLWUT",
}

REDIS_WRITE = {
    "SET", "SETEX", "PSETEX", "SETNX", "MSET", "MSETNX", "GETSET", "GETDEL",
    "GETEX", "SETBIT", "SETRANGE", "APPEND", "INCR", "DECR", "INCRBY",
    "DECRBY", "INCRBYFLOAT",
    "DEL", "UNLINK", "EXPIRE", "PEXPIRE", "EXPIREAT", "PERSIST", "RENAME",
    "RENAMENX",
    "HSET", "HMSET", "HSETNX", "HDEL", "HINCRBY", "HINCRBYFLOAT",
    "LPUSH", "RPUSH", "LPUSHX", "RPUSHX", "LPOP", "RPOP", "LSET", "LREM",
    "LTRIM", "LINSERT", "RPOPLPUSH", "LMOVE", "BLPOP", "BRPOP",
    "SADD", "SREM", "SPOP", "SMOVE", "SINTERSTORE", "SUNIONSTORE", "SDIFFSTORE",
    "ZADD", "ZREM", "ZINCRBY", "ZREMRANGEBYRANK", "ZREMRANGEBYSCORE",
    "PFADD", "PFMERGE", "GEOADD", "GEOSEARCHSTORE",
}

REDIS_DANGER = {
    "KEYS",          # 阻塞整个实例（§12.4）
    "FLUSHALL", "FLUSHDB",
    "CONFIG", "SHUTDOWN", "DEBUG", "SCRIPT",
    "CLUSTER", "SLAVEOF", "REPLICAOF", "MONITOR",
    "SAVE", "BGSAVE", "BGREWRITEAOF",
    "MIGRATE", "RESTORE", "SWAPDB", "RESET",
}

_SIZE_CMD = {"string": "STRLEN", "list": "LLEN", "hash": "HLEN",
             "set": "SCARD", "zset": "ZCARD"}


@register("redis")
class RedisPlugin(BasePlugin):
    TYPE = "redis"
    CONFIG_FILE = "redisConfig.yaml"
    CAPABILITIES = frozenset({COMMAND, NAMESPACE})
    EXTRA_FIELDS = ("db",)     # 兼容 db 作为 database 别名

    def __init__(self, name, raw):
        super().__init__(name, raw)
        self._client = None
        self._info_cache = None

    def _db_index(self):
        v = self.raw.get("database", self.raw.get("db", 0))
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    # ── 生命周期 ──
    def connect(self):
        try:
            self._client = redis.Redis(
                host=self.raw.get("host", "127.0.0.1"),
                port=int(self.raw.get("port", 6379)),
                password=self.raw.get("password") or None,
                db=self._db_index(),
                socket_connect_timeout=int(self.raw.get("connect_timeout", 10)),
                socket_timeout=int(self.raw.get("timeout", 30)),
                decode_responses=True,
            )
            self._client.ping()
        except redis.RedisError as e:
            self._client = None
            raise ConnectionFailed(f"Redis 连接 {self.name} 建立失败：{e}",
                                   suggestion="检查 host/port/密码/实例可达性") from e

    def close(self):
        try:
            if self._client:
                self._client.close()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._client = None

    def health_check(self) -> bool:
        try:
            return bool(self._client.ping())
        except Exception:  # noqa: BLE001
            return False

    def server_info(self) -> ServerInfo:
        if self._info_cache is None:
            ver = str(self._raw_info("server").get("redis_version", "unknown"))
            self._info_cache = ServerInfo("redis", ver, parse_version(ver))
        return self._info_cache

    def _raw_info(self, section):
        try:
            return self._client.info(section)
        except redis.ConnectionError as e:
            raise ConnectionFailed(f"Redis 连接失效：{e}") from e
        except redis.RedisError as e:
            raise QueryError(f"Redis INFO 失败：{e}") from e

    # ── 元数据 ──
    def list_namespaces(self):
        try:
            n = int(self._client.config_get("databases").get("databases", 1))
        except redis.RedisError:
            n = 1
        return [f"db{i}" for i in range(n)]

    def list_entities(self, namespace=None):
        """SCAN 采样并按 ':' 前缀分组（§12.2）。"""
        counts: dict = {}
        cursor = 0
        sampled = 0
        while sampled < 500:
            try:
                cursor, keys = self._client.scan(cursor=cursor, count=200)
            except redis.ConnectionError as e:
                raise ConnectionFailed(f"Redis 连接失效：{e}") from e
            for k in keys:
                prefix = k.split(":", 1)[0] + ":" if ":" in k else k
                counts[prefix] = counts.get(prefix, 0) + 1
            sampled += len(keys)
            if cursor == 0:
                break
        return sorted(counts)

    def describe(self, entity, namespace=None):
        try:
            t = self._client.type(entity)
            ttl = self._client.ttl(entity)
            size = None
            if t in _SIZE_CMD:
                size = getattr(self._client, _SIZE_CMD[t].lower())(entity)
            return {"type": t, "ttl": ttl, "size": size}
        except redis.ConnectionError as e:
            raise ConnectionFailed(f"Redis 连接失效：{e}") from e
        except redis.RedisError as e:
            raise QueryError(f"Redis describe 失败：{e}") from e

    # ── 分级判定（§12.4：命令名表，未知默认 DANGER）──
    def classify(self, operation):
        parts = (operation or "").strip().split(maxsplit=1)
        if not parts:
            raise ConnectorError("operation 为空，无法判定级别")
        cmd = parts[0].upper()
        key = parts[1].split(maxsplit=1)[0] if len(parts) > 1 else ""
        if cmd == "KEYS":
            return OperationSpec(Level.DANGER, cmd, (key,),
                                 ("命令 KEYS 会阻塞整个实例，建议改用 SCAN 分批遍历",))
        if cmd in REDIS_DANGER:
            return OperationSpec(Level.DANGER, cmd, (key,), (f"命令 {cmd} 属于危险操作",))
        if cmd in REDIS_WRITE:
            return OperationSpec(Level.WRITE, cmd, (key,), ())
        if cmd in REDIS_READ:
            return OperationSpec(Level.READONLY, cmd, (key,), ())
        return OperationSpec(Level.DANGER, cmd, (key,), (f"未识别的命令 {cmd}",))

    # ── 执行（§12.3：命令字符串，规整为二维结果）──
    def execute(self, operation, params=None, limit=None):
        parts = shlex.split((operation or "").strip())
        if not parts:
            raise QueryError("operation 为空")
        cmd = parts[0].upper()
        args = parts[1:]
        if cmd == "KEYS":
            return QueryResult(kind="table", columns=["key"], rows=[],
                               notice="KEYS 已按策略禁用于线上，请改用 SCAN")
        start = time.monotonic()
        try:
            result = self._client.execute_command(cmd, *args)
        except redis.ConnectionError as e:
            raise ConnectionFailed(f"Redis 连接失效：{e}") from e
        except redis.ResponseError as e:
            raise QueryError(f"Redis 命令错误：{e}",
                             suggestion=f"{cmd} 参数或用法有误，见 db_list_connections 提示") from e
        rows, columns = _normalize(result)
        truncated = False
        if limit is not None and len(rows) > limit:
            rows = rows[:limit]
            truncated = True
        out = QueryResult(kind="table", columns=columns, rows=rows,
                          row_count=len(rows), affected=0,
                          elapsed_ms=int((time.monotonic() - start) * 1000),
                          truncated=truncated)
        if cmd == "KEYS":
            out.notice = "KEYS 可能阻塞实例"
        return out


def _normalize(result):
    """把 Redis 返回规整为 (rows[list], columns)。"""
    if isinstance(result, dict):
        return [[k, _scalar(v)] for k, v in result.items()], ["field", "value"]
    if isinstance(result, (list, tuple)):
        return [[i, _scalar(v)] for i, v in enumerate(result)], ["index", "value"]
    return [[_scalar(result)]], ["value"]


def _scalar(v):
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v
