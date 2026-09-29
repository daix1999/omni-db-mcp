"""MySQL 插件离线测试（SPEC §18 Step 3 验收）：classify / 版本门 / LIMIT 注入。

不连任何真实数据库；连接级版本门用注入假 server_info 模拟 5.7。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from basePlugin import Level, QueryError, ServerInfo
from plugins.mysqlPlugin import MySQLPlugin  # noqa: E402 触发注册


def _plugin():
    return MySQLPlugin("offline", {"host": "127.0.0.1", "database": "test"})


class TestClassify(unittest.TestCase):

    def setUp(self):
        self.p = _plugin()

    def lvl(self, sql):
        return self.p.classify(sql)

    def test_select_readonly_with_targets(self):
        spec = self.lvl("SELECT id FROM orders o JOIN users u ON o.uid=u.id")
        self.assertEqual(spec.level, Level.READONLY)
        self.assertEqual(spec.action, "SELECT")
        self.assertEqual(set(spec.targets), {"orders", "users"})

    def test_show_use_readonly(self):
        self.assertEqual(self.lvl("SHOW TABLES").level, Level.READONLY)
        self.assertEqual(self.lvl("USE test").level, Level.READONLY)

    def test_explain_describe_readonly(self):
        """2026-09-29 审计修复回归：EXPLAIN/DESCRIBE 不再误判 DANGER。"""
        for sql in ("EXPLAIN SELECT * FROM t", "DESCRIBE t",
                    "EXPLAIN FORMAT=JSON SELECT 1"):
            spec = self.lvl(sql)
            self.assertEqual(spec.level, Level.READONLY, msg=sql)

    def test_explain_analyze_select_readonly_dml_danger(self):
        """EXPLAIN ANALYZE 会真执行内部语句：SELECT 放行、DML 默认拒绝。"""
        self.assertEqual(self.lvl("EXPLAIN ANALYZE SELECT 1").level,
                         Level.READONLY)
        spec = self.lvl("EXPLAIN ANALYZE UPDATE t SET a=1 WHERE id=1")
        self.assertEqual(spec.level, Level.DANGER)

    def test_insert_write(self):
        self.assertEqual(self.lvl("INSERT INTO t VALUES (1)").level, Level.WRITE)

    def test_update_without_where_flagged(self):
        spec = self.lvl("UPDATE t SET a=1")
        self.assertEqual(spec.level, Level.WRITE)
        self.assertIn("update_without_where", spec.reasons)
        self.assertNotIn("update_without_where",
                         self.lvl("UPDATE t SET a=1 WHERE id=2").reasons)

    def test_delete_without_where_flagged(self):
        self.assertIn("delete_without_where", self.lvl("DELETE FROM t").reasons)

    def test_ddl_statements_danger(self):
        for sql in ("DROP TABLE t", "CREATE TABLE t(a INT)",
                    "ALTER TABLE t ADD c INT", "TRUNCATE TABLE t"):
            spec = self.lvl(sql)
            self.assertEqual(spec.level, Level.DANGER, msg=sql)
            self.assertIn("DDL", spec.reasons, msg=sql)

    def test_multi_statement(self):
        spec = self.lvl("SELECT 1; SELECT 2")
        self.assertEqual(spec.level, Level.DANGER)
        self.assertIn("multi_statement", spec.reasons)

    def test_grant_and_rename_and_kill(self):
        self.assertIn("grant", self.lvl("GRANT SELECT ON db.* TO u").reasons)
        self.assertIn("DDL", self.lvl("RENAME TABLE a TO b").reasons)
        self.assertIn("shutdown", self.lvl("KILL 12").reasons)

    def test_set_is_write(self):
        self.assertEqual(self.lvl("SET @x = 1").level, Level.WRITE)

    def test_unrecognized_defaults_danger(self):
        # §16.4 默认拒绝：FLUSH/SHUTDOWN 不被 sqlglot 结构化 → 兜底 DANGER
        for sql in ("FLUSH PRIVILEGES", "SHUTDOWN", "LOAD DATA INFILE 'x' INTO t"):
            self.assertEqual(self.lvl(sql).level, Level.DANGER, msg=sql)

    def test_comment_bypass_attempt_still_caught(self):
        # 禁止字符串匹配的用例依据：注释/大小写绕不过 AST
        spec = self.lvl("/* hi */ dElEtE FROM t")
        self.assertEqual(spec.action, "DELETE")
        self.assertIn("delete_without_where", spec.reasons)

    def test_empty_operation_rejected(self):
        from basePlugin import ConnectorError
        with self.assertRaises(ConnectorError):
            self.lvl("   ")


class TestVersionGate(unittest.TestCase):

    def setUp(self):
        self.p = _plugin()
        self.p._info = ServerInfo("mysql", "5.7.43", (5, 7, 43))  # 模拟 5.7

    def test_cte_blocked_on_57(self):
        import sqlglot
        stmt = sqlglot.parse("WITH c AS (SELECT 1) SELECT * FROM c",
                             dialect="mysql")[0]
        with self.assertRaises(QueryError) as cm:
            self.p._guard_version_features(stmt)
        self.assertIn("MySQL 8.0", cm.exception.message)
        self.assertIn("5.7.43", cm.exception.message)

    def test_window_blocked_on_57(self):
        import sqlglot
        stmt = sqlglot.parse("SELECT ROW_NUMBER() OVER () FROM t",
                             dialect="mysql")[0]
        with self.assertRaises(QueryError):
            self.p._guard_version_features(stmt)

    def test_plain_select_ok_on_57(self):
        import sqlglot
        stmt = sqlglot.parse("SELECT a FROM t", dialect="mysql")[0]
        self.p._guard_version_features(stmt)   # 不抛即通过

    def test_passes_on_80(self):
        self.p._info = ServerInfo("mysql", "8.0.46", (8, 0, 46))
        import sqlglot
        stmt = sqlglot.parse("WITH c AS (SELECT 1) SELECT * FROM c",
                             dialect="mysql")[0]
        self.p._guard_version_features(stmt)


class TestLimitInjection(unittest.TestCase):

    def setUp(self):
        self.p = _plugin()

    def test_injects_notice(self):
        import sqlglot
        stmt = sqlglot.parse("SELECT id FROM t", dialect="mysql")[0]
        sql, notice = self.p._inject_limit(stmt, "SELECT id FROM t", 100)
        self.assertIn("LIMIT 100", sql.upper())
        self.assertIn("已自动注入 LIMIT 100", notice)

    def test_existing_smaller_limit_kept(self):
        import sqlglot
        original = "SELECT id FROM t LIMIT 10"
        stmt = sqlglot.parse(original, dialect="mysql")[0]
        sql, notice = self.p._inject_limit(stmt, original, 100)
        self.assertEqual(sql, original)      # 原样保留，不改写
        self.assertIsNone(notice)

    def test_existing_bigger_limit_shrunk(self):
        import sqlglot
        original = "SELECT id FROM t LIMIT 500"
        stmt = sqlglot.parse(original, dialect="mysql")[0]
        sql, notice = self.p._inject_limit(stmt, original, 100)
        self.assertIn("LIMIT 100", sql.upper())
        self.assertIn("收敛", notice)

    def test_no_limit_none_passthrough(self):
        import sqlglot
        stmt = sqlglot.parse("SELECT 1", dialect="mysql")[0]
        sql, notice = self.p._inject_limit(stmt, "SELECT 1", None)
        self.assertEqual(sql, "SELECT 1")
        self.assertIsNone(notice)


class TestIdentifierGuard(unittest.TestCase):

    def test_illegal_identifier_rejected(self):
        from basePlugin import QueryError
        with self.assertRaises(QueryError):
            _plugin().list_entities("test; DROP TABLE x")

    def test_legal_chinese_and_underscore(self):
        from plugins import mysqlPlugin
        self.assertEqual(mysqlPlugin._q_ident("my_db1"), "`my_db1`")
        self.assertEqual(mysqlPlugin._q_ident("订单表"), "`订单表`")


if __name__ == "__main__":
    unittest.main(verbosity=2)
