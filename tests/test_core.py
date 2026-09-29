"""底座测试（SPEC §17）：15 条必测用例 + 契约校验，全程不依赖任何真实数据库。

运行：python -m unittest tests.test_core -v   （或 pytest tests/test_core.py）
"""
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import basePlugin  # noqa: E402
from basePlugin import (Level, PolicyDenied, register, ConnectorError)  # noqa: E402
from tests.mockPlugin import MockPlugin, MockKvPlugin  # noqa: F401,E402 触发注册
from core import app, audit, policy  # noqa: E402
from core.connections import ConnectionManager  # noqa: E402
from core.errors import (clear_error_callbacks, on_error, ErrorContext)


def _guard(fn):
    return app.tool_guard(fn)


class CoreTestBase(unittest.TestCase):
    """每个用例独立的临时 conf/ rules/ logs/ 环境。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conf = os.path.join(self._tmp.name, "conf")
        self.rules = os.path.join(self._tmp.name, "rules")
        self.logs = os.path.join(self._tmp.name, "logs")
        os.makedirs(self.conf)
        os.makedirs(self.rules)
        clear_error_callbacks()

    def tearDown(self):
        audit.set_backend(None)
        audit.get_logger().close()          # Windows：先释放 SQLite 句柄再删临时目录
        clear_error_callbacks()
        self._tmp.cleanup()

    def boot(self, mocks, mockkvs=(), rules_files=None, env=None):
        for k, v in (env or {}).items():
            os.environ[k] = v
        if mocks:
            self._write(os.path.join(self.conf, "mockConfig.yaml"), mocks)
        if mockkvs:
            self._write(os.path.join(self.conf, "mockkvConfig.yaml"), mockkvs)
        for fname, body in (rules_files or {}).items():
            self._write(os.path.join(self.rules, fname), body)
        mgr = ConnectionManager(self.conf,
                                plugins={"mock": MockPlugin, "mockkv": MockKvPlugin})
        app.bootstrap(conf_dir=self.conf, rules_dir=self.rules,
                      logs_dir=self.logs, manager=mgr)
        self.mgr = mgr
        return mgr

    @staticmethod
    def _write(path, body):
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(body, f, allow_unicode=True)

    def audit_rows(self, status=None):
        sql = "SELECT * FROM operation_log"
        args = ()
        if status:
            sql += " WHERE status=?"
            args = (status,)
        con = sqlite3.connect(os.path.join(self.logs, "operation_log.db"))
        try:
            cols = [d[0] for d in con.execute(sql, args).description]
            return [dict(zip(cols, r)) for r in con.execute(sql, args).fetchall()]
        finally:
            con.close()


class TestSpec17(CoreTestBase):

    def test_01_capability_missing_returns_error(self):
        """①一级分流：调用不存在的能力 → 明确错误，不抛异常。"""
        self.boot({}, mockkvs={"kv1": {"level": "readonly"}})
        guarded = _guard(app.impl_list_tables)
        with redirect_stderr(io.StringIO()):
            res = guarded("kv1")
        self.assertFalse(res["ok"])
        self.assertIn("不支持列出实体", res["error"])
        self.assertTrue(res.get("suggestion"))

    def test_02_unknown_connection(self):
        """②不存在的连接别名 → 明确错误 + 可用列表。"""
        self.boot({"ro": {}})
        res = _guard(app.impl_query)("nope", "SELECT 1")
        self.assertFalse(res["ok"])
        self.assertIn("不存在", res["error"])
        self.assertIn("ro", res["suggestion"])

    def test_03_builtin_defaults_readonly(self):
        """③内置默认合成 → max_level == READONLY。"""
        self.boot({"plain": {}})
        cfg = self.mgr.get_config("plain")
        rules = policy.resolve_rules(cfg, policy.load_packs(self.rules))
        self.assertEqual(rules["max_level"], Level.READONLY)
        self.assertEqual(rules["max_rows"], policy.BUILTIN_DEFAULTS["max_rows"])

    def test_04_global_pack_tightens(self):
        """④全局包收紧 → 比内置更严。"""
        self.boot({"plain": {}}, rules_files={
            "a-tight.yaml": {"name": "tight", "global": True, "max_rows": 100}})
        cfg = self.mgr.get_config("plain")
        rules = policy.resolve_rules(cfg, policy.load_packs(self.rules))
        self.assertEqual(rules["max_rows"], 100)
        self.assertLess(rules["max_rows"], policy.BUILTIN_DEFAULTS["max_rows"])

    def test_05_pack_relax_ignored_without_unsafe(self):
        """⑤连接包尝试放宽但无 unsafe → 被忽略。"""
        self.boot({"plain": {"rules": ["relaxer"]}}, rules_files={
            "r.yaml": {"name": "relaxer", "max_level": "danger", "max_rows": 9999}})
        cfg = self.mgr.get_config("plain")
        rules = policy.resolve_rules(cfg, policy.load_packs(self.rules))
        self.assertEqual(rules["max_level"], Level.READONLY)
        self.assertEqual(rules["max_rows"], policy.BUILTIN_DEFAULTS["max_rows"])

    def test_06_pack_relax_with_unsafe(self):
        """⑥连接包放宽且 unsafe: true → 生效（全系统唯一放宽开关之一）。"""
        self.boot({"wild": {"rules": ["relaxer"], "unsafe": True}}, rules_files={
            "r.yaml": {"name": "relaxer", "max_level": "danger", "max_rows": 9999}})
        cfg = self.mgr.get_config("wild")
        rules = policy.resolve_rules(cfg, policy.load_packs(self.rules))
        self.assertEqual(rules["max_level"], Level.DANGER)
        self.assertEqual(rules["max_rows"], 9999)

    def test_07_deny_is_union_only(self):
        """⑦deny 并集：层层只能加不能删。"""
        self.boot({"p": {"rules": ["connpack"]}}, rules_files={
            "a_global.yaml": {"name": "g", "global": True, "deny": ["DDL"]},
            "b.yaml": {"name": "connpack", "deny": ["multi_statement"],
                       "allow": ["DDL"]}})
        cfg = self.mgr.get_config("p")
        rules = policy.resolve_rules(cfg, policy.load_packs(self.rules))
        self.assertIn("DDL", rules["deny"])
        self.assertIn("multi_statement", rules["deny"])
        # 内置 allow 为空 → 交集仍为空：allow 无法凭空扩大
        self.assertEqual(rules["allow"], set())

    def test_08_readonly_exec_delete_denied(self):
        """⑧readonly 连接执行 DELETE → PolicyDenied 且带 suggestion。"""
        self.boot({"ro": {"level": "readonly"}})
        with self.assertRaises(PolicyDenied) as cm:
            app.impl_exec("ro", "DELETE FROM t WHERE id=1")
        self.assertIn("write", str(cm.exception))
        self.assertTrue(cm.exception.suggestion)

    def test_09_denied_operation_audited(self):
        """⑨被拦截操作必须落审计（status=denied）。"""
        self.boot({"ro": {"level": "readonly"}})
        with self.assertRaises(PolicyDenied):
            app.impl_exec("ro", "DELETE FROM t WHERE id=1")
        rows = self.audit_rows("denied")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["connection"], "ro")
        self.assertEqual(rows[0]["action"], "DELETE")

    def test_10_audit_failure_does_not_break_flow(self):
        """⑩审计写入失败 → 主流程照常返回。"""
        self.boot({"rw": {"level": "write"}})

        class Broken:
            def write(self, record):
                raise OSError("磁盘坏了")

        audit.set_backend(Broken())
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            res = app.impl_query("rw", "SELECT id FROM t")
        self.assertTrue(res["ok"])
        self.assertEqual(res["row_count"], 5)
        self.assertIn("审计写入失败", stderr.getvalue())

    def test_11_callback_exception_swallowed(self):
        """⑪错误回调内抛异常 → 主流程不受影响。"""
        self.boot({"ro": {"level": "readonly"}})

        def bad_cb(ctx):
            raise RuntimeError("回调炸了")

        on_error(bad_cb)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            res = _guard(app.impl_exec)("ro", "DELETE FROM t WHERE id=1")
        self.assertFalse(res["ok"])
        self.assertIn("write 级别", res["error"])       # 拒绝语义仍正确送达
        self.assertIn("回调", stderr.getvalue())        # 但被吞掉并留痕

    def test_12_tool_guard_no_traceback_leak(self):
        """⑫插件抛任意异常 → ok:false，无 traceback / 本机路径。"""
        self.boot({"boom": {"crash": True, "level": "write"}})
        res = _guard(app.impl_exec)("boom", "INSERT INTO t VALUES (1)")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "内部错误，请查看服务端日志")
        self.assertNotIn("Traceback", str(res))
        self.assertNotIn(self._tmp.name, str(res))

    def test_13_missing_env_var_named(self):
        """⑬${env:X} 未设置 → 报错信息含变量名。"""
        os.environ.pop("OMNI_TEST_MISSING_X", None)
        with self.assertRaises(ConnectorError) as cm:
            self.boot({"p": {"password": "${env:OMNI_TEST_MISSING_X}"}})
        self.assertIn("OMNI_TEST_MISSING_X", cm.exception.message)

    def test_14_unknown_field_warns_not_silent(self):
        """⑭未知字段（hots）→ 警告且不中断。"""
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.boot({"p": {"hots": "1.2.3.4"}})
        out = stderr.getvalue()
        self.assertIn("hots", out)
        self.assertIn("p", out)
        self.assertIn("p", self.mgr.configs())          # 连接仍然可用

    def test_15_disconnect_reconnects_and_retries_once(self):
        """⑮模拟断连 → 自动重连并重试一次。"""
        self.boot({"flaky": {"lost_first": True, "level": "write"}})
        res = _guard(app.impl_query)("flaky", "SELECT id FROM t")
        self.assertTrue(res["ok"], msg=str(res))
        self.assertEqual(res["row_count"], 5)
        from tests.mockPlugin import _MockBase
        self.assertEqual(_MockBase._connect_total.get("flaky"), 2)  # 建连→断→重连
        ok_rows = self.audit_rows("ok")
        self.assertEqual(len(ok_rows), 1)     # 重试成功只落一条 ok 审计


    def test_write_not_auto_retried_on_disconnect(self):
        """⑯审计修订（2026-09-29）：写操作断线不自动重试，提示先确认是否生效。"""
        self.boot({"flakyw": {"lost_first": True, "level": "write"}})
        res = _guard(app.impl_exec)("flakyw", "INSERT INTO t VALUES (1)")
        self.assertFalse(res["ok"])
        self.assertIn("未自动重试", res["error"])
        self.assertIn("是否已生效", res.get("suggestion") or "")
        from tests.mockPlugin import _MockBase
        self.assertEqual(_MockBase._connect_total.get("flakyw"), 1)  # 未重连重试


    def test_list_connections_probes_concurrently_and_tolerates_failures(self):
        """⑰审计修复（2026-09-29）：列连接并发探测，不可达连接返回占位不阻塞。"""
        self.boot({"good": {}, "dead": {"fail_connect": True}})
        res = app.impl_list_connections()
        self.assertTrue(res["ok"])
        by_name = {c["name"]: c for c in res["connections"]}
        self.assertIn("good", by_name)
        self.assertEqual(by_name["good"]["version"], "9.9.9-mock")
        self.assertTrue(by_name["dead"]["version"].startswith("不可达"),
                        by_name["dead"]["version"])


class TestContract(CoreTestBase):
    """Step 1 验收：@register 校验（SPEC §5.1 ③）。"""

    def test_duplicate_registration_rejected(self):
        with self.assertRaises(ValueError):
            @register("mock")
            class Dup(MockPlugin):
                pass

    def test_missing_capabilities_rejected(self):
        with self.assertRaises(ValueError):
            @register("bad1")
            class Bad(basePlugin.BasePlugin):
                CONFIG_FILE = "x.yaml"
                CAPABILITIES = frozenset()
                def connect(self): pass
                def close(self): pass
                def health_check(self): return True
                def server_info(self): pass
                def list_namespaces(self): return []
                def list_entities(self, namespace=None): return []
                def describe(self, entity, namespace=None): return {}
                def execute(self, operation, params=None, limit=None): pass
                def classify(self, operation): pass

    def test_unknown_capability_rejected(self):
        with self.assertRaises(ValueError):
            @register("bad2")
            class Bad2(MockPlugin):
                CAPABILITIES = frozenset({"sql", "telepathy"})

    def test_missing_config_file_rejected(self):
        with self.assertRaises(ValueError):
            @register("bad3")
            class Bad3(MockPlugin):
                CONFIG_FILE = ""


class TestRobustnessExtras(CoreTestBase):
    """§8.6 超时参数下发 / §11.5 上限注入的底座侧行为。"""

    def test_limit_injection_respects_rules(self):
        self.boot({"rw": {"level": "write", "max_rows": 3}})
        res = app.impl_query("rw", "SELECT id FROM t")
        self.assertTrue(res["ok"])
        self.assertEqual(res["row_count"], 3)
        self.assertTrue(res["truncated"])
        self.assertIn("LIMIT 3", res["notice"])

    def test_user_limit_smaller_than_cap(self):
        self.boot({"rw": {"level": "write"}})
        res = app.impl_query("rw", "SELECT id FROM t", limit=2)
        self.assertEqual(res["row_count"], 2)

    def test_db_query_caps_at_readonly(self):
        self.boot({"rw": {"level": "write"}})
        with self.assertRaises(PolicyDenied):
            app.impl_query("rw", "INSERT INTO t VALUES (1)")
        # 但 db_exec 放行（连接本身就是 write）
        res = _guard(app.impl_exec)("rw", "INSERT INTO t VALUES (1)")
        self.assertTrue(res["ok"], msg=str(res))

    def test_error_callback_receives_denied(self):
        self.boot({"ro": {"level": "readonly"}})
        seen = []
        on_error(seen.append)
        _guard(app.impl_exec)("ro", "DELETE FROM t WHERE id=1")
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0], ErrorContext)
        self.assertIsInstance(seen[0].error, PolicyDenied)


if __name__ == "__main__":
    unittest.main(verbosity=2)
