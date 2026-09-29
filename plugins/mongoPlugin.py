"""MongoDB 插件（SPEC §13）：文档型，operation 为 JSON 字符串。

classify 解析 JSON 查方法名分级表 + 两条特殊判定（deleteMany 空 filter →
DANGER；aggregate 含 $out/$merge → WRITE），未知方法默认拒绝（§13.4）。
JSON 解析失败不回显原始内容（可能含敏感值，§13.3）。
"""
import json
import time
from collections import OrderedDict

import pymongo
from pymongo import errors as mongo_errors

from basePlugin import (DESCRIBE, DOCUMENT, ENTITY, NAMESPACE, BasePlugin,
                        ConnectionFailed, ConnectorError, Level,
                        OperationSpec, QueryError, QueryResult, ServerInfo,
                        parse_version, register)

MONGO_READ = {
    "find", "findOne", "countDocuments", "estimatedDocumentCount",
    "distinct", "listCollections", "listIndexes", "dbStats", "collStats",
    "aggregate",          # 含 $out / $merge 时升级为 WRITE
}

MONGO_WRITE = {
    "insertOne", "insertMany",
    "updateOne", "updateMany", "replaceOne",
    "deleteOne", "deleteMany",
    "findOneAndUpdate", "findOneAndReplace", "findOneAndDelete",
    "createIndex", "dropIndex", "dropIndexes",
}

MONGO_DANGER = {
    "drop", "dropDatabase", "renameCollection", "createCollection",
    "convertToCapped", "shutdown",
}

# operation 里的驼峰方法名 → pymongo 下划线方法名
_SNAKE = {
    "insertOne": "insert_one", "insertMany": "insert_many",
    "updateOne": "update_one", "updateMany": "update_many",
    "replaceOne": "replace_one",
    "deleteOne": "delete_one", "deleteMany": "delete_many",
    "findOne": "find_one",
    "findOneAndUpdate": "find_one_and_update",
    "findOneAndReplace": "find_one_and_replace",
    "findOneAndDelete": "find_one_and_delete",
    "countDocuments": "count_documents",
    "estimatedDocumentCount": "estimated_document_count",
    "createIndex": "create_index",
    "dropIndex": "drop_index", "dropIndexes": "drop_indexes",
    "listIndexes": "list_indexes",
}


def _cell(v):
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, default=str)
    if isinstance(v, bytes):
        return v.hex()
    return v


@register("mongo")
class MongoPlugin(BasePlugin):
    TYPE = "mongo"
    CONFIG_FILE = "mongoConfig.yaml"
    CAPABILITIES = frozenset({DOCUMENT, ENTITY, DESCRIBE, NAMESPACE})
    EXTRA_FIELDS = ("auth_source",)

    def __init__(self, name, raw):
        super().__init__(name, raw)
        self._client = None
        self._info_cache = None

    # ── 生命周期 ──
    def connect(self):
        # 2026-09-29 审计修复：pymongo 的 socketTimeoutMS 默认 None（永不超时），
        # 慢查询会无限挂住工具调用；serverSelectionTimeoutMS 只管建连选服务器。
        # 此处显式下发 socket 级超时 + 查询级 maxTimeMS（见 _dispatch）。
        socket_ms = int(self.raw.get("timeout", 30)) * 1000
        kw = dict(serverSelectionTimeoutMS=int(
            self.raw.get("connect_timeout", 10)) * 1000,
            socketTimeoutMS=socket_ms)
        try:
            if self.raw.get("uri"):
                self._client = pymongo.MongoClient(self.raw["uri"], **kw)
            else:
                if self.raw.get("user"):
                    kw.update(username=self.raw["user"],
                              password=self.raw.get("password") or None,
                              authSource=self.raw.get("auth_source")
                              or self.raw.get("database") or "admin")
                self._client = pymongo.MongoClient(
                    host=self.raw.get("host", "127.0.0.1"),
                    port=int(self.raw.get("port", 27017)), **kw)
            self._client.admin.command("ping")          # §13.2 显式验证
        except mongo_errors.ConnectionFailure as e:
            self._client = None
            raise ConnectionFailed(f"MongoDB 连接 {self.name} 建立失败：{e}",
                                   suggestion="检查 host/port/认证与实例可达性") from e
        except Exception as e:  # noqa: BLE001
            self._client = None
            raise ConnectionFailed(f"MongoDB 连接 {self.name} 建立失败：{e}") from e

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
            self._client.admin.command("ping")
            return True
        except Exception:  # noqa: BLE001
            return False

    def server_info(self) -> ServerInfo:
        if self._info_cache is None:
            ver = str(self._call(self._client.server_info).get("version", "unknown"))
            self._info_cache = ServerInfo("mongo", ver, parse_version(ver))
        return self._info_cache

    # ── 元数据 ──
    def list_namespaces(self):
        return list(self._call(self._client.list_database_names))

    def list_entities(self, namespace=None):
        return list(self._call(self._db(namespace).list_collection_names))

    def describe(self, entity, namespace=None):
        coll = self._db(namespace)[entity]
        docs = list(self._call(lambda: list(coll.find({}).limit(100))))
        fields: OrderedDict = OrderedDict()
        for doc in docs:
            for k, v in doc.items():
                e = fields.setdefault(k, {"name": k, "types": set(), "count": 0})
                e["types"].update(_types_of(v))
                e["count"] += 1
        return {"fields": [{**f, "types": sorted(f["types"])} for f in fields.values()],
                "sample_size": len(docs)}

    # ── 分级判定（§13.4）──
    def classify(self, operation):
        obj = self._parse(operation)
        method = str(obj.get("method"))
        coll = str(obj.get("collection") or "")
        if method in MONGO_DANGER:
            return OperationSpec(Level.DANGER, method, (coll,),
                                 (f"方法 {method} 属于危险操作",))
        level = (Level.WRITE if method in MONGO_WRITE else
                 Level.READONLY if method in MONGO_READ else None)
        if level is None:
            return OperationSpec(Level.DANGER, method or "UNKNOWN", (coll,),
                                 (f"未识别的方法 {method!r}",))
        reasons = ()
        if method == "deleteMany" and not obj.get("filter"):
            level, reasons = Level.DANGER, ("delete_without_where",)
        if method == "aggregate" and not reasons:
            stages = [next(iter(p)) for p in (obj.get("pipeline") or [])
                      if isinstance(p, dict) and p]
            if "$out" in stages or "$merge" in stages:
                level, reasons = Level.WRITE, ("aggregate 含 $out/$merge 写出阶段",)
        return OperationSpec(level, method, (coll,), reasons)

    # ── 执行（§13.3）──
    def execute(self, operation, params=None, limit=None):
        obj = self._parse(operation)
        method = str(obj.get("method"))
        coll = self._db(obj.get("namespace"))[str(obj.get("collection") or "_")]
        flt = obj.get("filter") or {}
        start = time.monotonic()
        columns, rows, affected = self._call_or_map(
            lambda: self._dispatch(method, coll, obj, flt, limit))
        return QueryResult(kind="table", columns=columns or None, rows=rows,
                           row_count=len(rows), affected=affected,
                           elapsed_ms=int((time.monotonic() - start) * 1000))

    def _dispatch(self, method, coll, obj, flt, limit):
        hard = int(limit) if limit is not None else 1000
        db = coll.database
        max_ms = int(self.raw.get("timeout", 30)) * 1000   # 查询级超时（服务端终止）

        if method == "find":
            cur = coll.find(flt, obj.get("projection"))
            if obj.get("sort"):
                cur = cur.sort(obj["sort"])
            cur = cur.max_time_ms(max_ms)
            cur = cur.limit(min(int(obj.get("limit") or hard), hard))
            return self._docs_to_rows(list(cur)) + (0,)
        if method == "findOne":
            doc = coll.find_one(flt, obj.get("projection"))
            return self._docs_to_rows([doc] if doc else []) + (0,)
        if method == "insertOne":
            coll.insert_one(obj.get("document") or {})
            return [], [], 1
        if method == "insertMany":
            res = coll.insert_many(obj.get("documents") or [])
            return [], [], len(res.inserted_ids)
        if method in ("updateOne", "updateMany"):
            res = getattr(coll, _SNAKE[method])(flt, obj.get("update") or {},
                                                 upsert=bool(obj.get("upsert")))
            return [], [], res.modified_count
        if method == "replaceOne":
            res = coll.replace_one(flt, obj.get("replacement") or {},
                                   upsert=bool(obj.get("upsert")))
            return [], [], res.modified_count
        if method in ("findOneAndUpdate", "findOneAndReplace"):
            doc = getattr(coll, _SNAKE[method])(flt, obj.get("update")
                                                or obj.get("replacement") or {})
            return self._docs_to_rows([doc] if doc else []) + (1,)
        if method == "deleteOne":
            return [], [], coll.delete_one(flt).deleted_count
        if method == "deleteMany":
            return [], [], coll.delete_many(flt).deleted_count
        if method == "findOneAndDelete":
            doc = coll.find_one_and_delete(flt)
            return self._docs_to_rows([doc] if doc else []) + (1,)
        if method == "countDocuments":
            return ["count"], [[coll.count_documents(flt, maxTimeMS=max_ms)]], 0
        if method == "estimatedDocumentCount":
            return ["count"], [[coll.estimated_document_count()]], 0
        if method == "distinct":
            vals = coll.distinct(str(obj["key"]), flt)
            return ["value"], [[_cell(v)] for v in vals], 0
        if method == "aggregate":
            pipeline = list(obj.get("pipeline") or [])
            if not any(("$out" in p) or ("$merge" in p)
                       for p in pipeline if isinstance(p, dict)):
                pipeline = pipeline + [{"$limit": hard}]
            return self._docs_to_rows(
                list(coll.aggregate(pipeline, maxTimeMS=max_ms))) + (0,)
        if method == "listIndexes":
            return self._docs_to_rows(list(coll.list_indexes())) + (0,)
        if method == "createIndex":
            keys = obj.get("keys") or obj.get("index")
            if not keys:
                raise QueryError('createIndex 需要 keys 字段',
                                 suggestion='示例 keys：{"field": 1} 或 [["field", 1]]')
            coll.create_index(keys)
            return [], [], 1
        if method == "dropIndex":
            coll.drop_index(obj.get("name") or "*")
            return [], [], 1
        if method == "dropIndexes":
            coll.drop_indexes()
            return [], [], 1
        if method == "listCollections":
            names = db.list_collection_names()
            return ["collection"], [[n] for n in names], 0
        if method == "createCollection":
            db.create_collection(str(obj.get("collection") or "new"))
            return [], [], 1
        if method == "drop":
            coll.drop()
            return [], [], 1
        if method == "dropDatabase":
            db.drop_database()
            return [], [], 1
        if method == "dbStats":
            stats = db.command("dbstats")
            return ["stat", "value"], [[k, str(v)] for k, v in stats.items()], 0
        if method == "collStats":
            stats = db.command("collstats", coll.name)
            return ["stat", "value"], [[k, str(v)] for k, v in stats.items()], 0
        if method in ("renameCollection", "convertToCapped", "shutdown"):
            raise QueryError(f"方法 {method} 首版未开放执行",
                             suggestion="该危险方法仅保留分级识别，请通过运维通道操作")
        raise QueryError(f"未实现的方法 {method!r}",
                         suggestion='示例：{"collection":"users","method":"find","filter":{}}')

    # ── 助手 ──
    def _parse(self, operation):
        try:
            obj = json.loads(operation or "")
        except (json.JSONDecodeError, TypeError):
            # §13.3：不回显原始内容（可能含敏感值）
            raise ConnectorError(
                "MongoDB operation 必须是含 method 字段的 JSON 字符串",
                suggestion='示例：{"collection":"users","method":"find","filter":{}}') from None
        if not isinstance(obj, dict) or not obj.get("method"):
            raise ConnectorError(
                "MongoDB operation JSON 缺少 method 字段",
                suggestion='示例：{"collection":"users","method":"find","filter":{}}')
        return obj

    def _db(self, namespace=None):
        return self._client[namespace or self.raw.get("database") or "test"]

    @staticmethod
    def _docs_to_rows(docs):
        columns = []
        for d in docs:
            for k in d:
                if k not in columns:
                    columns.append(k)
        rows = [[_cell(d.get(c)) for c in columns] for d in docs]
        return columns, rows

    def _call(self, fn, *a, **kw):
        """统一驱动调用 + 连接失效归一（§16.1/§16.2）。"""
        try:
            return fn(*a, **kw) if callable(fn) else fn
        except mongo_errors.AutoReconnect as e:
            raise ConnectionFailed(f"MongoDB 连接失效：{e}") from e
        except mongo_errors.ConnectionFailure as e:
            raise ConnectionFailed(f"MongoDB 连接不可用：{e}") from e

    def _call_or_map(self, fn):
        try:
            return self._call(fn)
        except (QueryError, ConnectionFailed):
            raise
        except mongo_errors.ExecutionTimeout as e:
            raise QueryError(f"MongoDB 执行超时：{e}",
                             suggestion="缩小 filter 范围或调整连接 timeout") from e
        except mongo_errors.OperationFailure as e:
            raise QueryError(f"MongoDB 操作失败：{e}",
                             suggestion="检查集合/字段/权限；格式见 db_list_connections 提示") from e
        except Exception as e:  # noqa: BLE001 —— 驱动原生异常一律归一
            raise QueryError(f"MongoDB 操作失败：{e}") from e


def _types_of(value) -> list:
    if isinstance(value, dict):
        return ["object"]
    if isinstance(value, list):
        return ["array"]
    return ["null" if value is None else type(value).__name__]
