"""TokenTrimAspect 单测（省 token 三件套）：值截断 / token 计量 / DDL 截断 / 端到端。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.app import ASPECTS  # noqa: E402
from core.aspects import (MAX_CELL_CHARS, approx_tokens, trim_describe,  # noqa: E402
                          trim_payload)


class TestTrimPayload(unittest.TestCase):

    def test_long_cell_truncated_with_notice(self):
        big = "x" * 1000
        payload = {"ok": True, "columns": ["c"], "rows": [[big]],
                   "row_count": 1, "notice": None}
        out = trim_payload(payload)
        cell = out["rows"][0][0]
        self.assertLessEqual(len(cell), MAX_CELL_CHARS + 40)
        self.assertIn("chars truncated", cell)
        self.assertIn("超长已截断", out["notice"])

    def test_small_payload_untouched(self):
        payload = {"ok": True, "rows": [["a", "b"]], "row_count": 2,
                   "notice": "已自动注入 LIMIT 100"}
        out = trim_payload(payload)
        self.assertEqual(out["rows"], [["a", "b"]])
        self.assertEqual(out["notice"], "已自动注入 LIMIT 100")   # 不追加
        self.assertGreater(out["approx_tokens"], 0)

    def test_notice_appended_not_replaced(self):
        payload = {"ok": True, "rows": [["x" * 500]], "notice": "已自动注入 LIMIT 3"}
        out = trim_payload(payload)
        self.assertTrue(out["notice"].startswith("已自动注入 LIMIT 3"))
        self.assertIn("超长已截断", out["notice"])

    def test_non_string_cells_untouched(self):
        payload = {"ok": True, "rows": [[1, None, 3.14, ["x"]]], "row_count": 1}
        out = trim_payload(payload)
        self.assertEqual(out["rows"], [[1, None, 3.14, ["x"]]])

    def test_large_result_suggests_paging(self):
        payload = {"ok": True, "rows": [["word " * 80] for _ in range(300)],
                   "row_count": 300, "notice": None}
        out = trim_payload(payload)
        self.assertGreater(out["approx_tokens"], 8000)
        self.assertIn("分页", out["notice"])


class TestApproxTokens(unittest.TestCase):

    def test_ascii_ratio(self):
        text = "a" * 4000                       # 纯 ASCII → ~1000 tokens
        self.assertAlmostEqual(approx_tokens(text), 1000, delta=50)

    def test_cjk_counts_more(self):
        ascii_tokens = approx_tokens("a" * 1000)
        cjk_tokens = approx_tokens("字" * 1000)
        self.assertGreater(cjk_tokens, ascii_tokens * 2)  # 中文按 ~1 char/token

    def test_empty(self):
        self.assertEqual(approx_tokens(""), 0)
        self.assertEqual(approx_tokens({}), 0)


class TestTrimDescribe(unittest.TestCase):

    def test_ddl_truncated_flagged(self):
        info = {"columns": [{"name": "id"}], "indexes": [],
                "ddl": "CREATE TABLE t (" + "c INT," * 400 + ")"}
        out = trim_describe(info)
        self.assertTrue(out["ddl_truncated"])
        self.assertLess(len(out["ddl"]), 2100)
        self.assertIn("chars truncated", out["ddl"])

    def test_small_ddl_untouched(self):
        info = {"ddl": "CREATE TABLE t (id INT)"}
        out = trim_describe(info)
        self.assertNotIn("ddl_truncated", out)
        self.assertEqual(out["ddl"], "CREATE TABLE t (id INT)")

    def test_non_dict_safe(self):
        self.assertEqual(trim_describe({"ddl": None}), {"ddl": None})


class TestAspectWiring(unittest.TestCase):
    """端到端：切面已挂载，db_query 返回带 approx_tokens。"""

    def test_aspect_registered(self):
        names = [type(a).__name__ for a in ASPECTS]
        self.assertIn("TokenTrimAspect", names)

    def test_query_payload_has_approx_tokens(self):
        import tests.mockPlugin  # noqa: F401 触发 mock 注册
        from core import app, audit
        from core.connections import ConnectionManager
        from tests.mockPlugin import MockPlugin, MockKvPlugin
        import tempfile
        import yaml as _yaml

        with tempfile.TemporaryDirectory() as tmp:
            conf = os.path.join(tmp, "conf")
            rules = os.path.join(tmp, "rules")
            logs = os.path.join(tmp, "logs")
            os.makedirs(conf); os.makedirs(rules)
            with open(os.path.join(conf, "mockConfig.yaml"), "w",
                      encoding="utf-8") as f:
                _yaml.safe_dump({"rw": {"level": "write"}}, f)
            mgr = ConnectionManager(conf, plugins={"mock": MockPlugin,
                                                   "mockkv": MockKvPlugin})
            from core import audit
            app.bootstrap(conf_dir=conf, rules_dir=rules, logs_dir=logs,
                          manager=mgr)
            try:
                res = app.impl_query("rw", "SELECT id FROM t")
                self.assertTrue(res["ok"])
                self.assertIn("approx_tokens", res)
                self.assertGreater(res["approx_tokens"], 0)
            finally:
                mgr.close_all()
                audit.get_logger().close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
