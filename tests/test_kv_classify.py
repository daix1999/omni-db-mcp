"""Redis / MongoDB classify 离线单测（Step 5 前置验收：分级判定正确，不连库）。"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from basePlugin import ConnectorError, Level
from plugins.redisPlugin import RedisPlugin
from plugins.mongoPlugin import MongoPlugin


class TestRedisClassify(unittest.TestCase):
    def setUp(self):
        self.p = RedisPlugin("r", {"host": "127.0.0.1"})

    def test_read_commands(self):
        for cmd in ("GET user:1", "HGETALL h", "TTL k", "SCAN 0", "INFO"):
            self.assertEqual(self.p.classify(cmd).level, Level.READONLY, msg=cmd)

    def test_write_commands(self):
        for cmd in ("SET k v", "DEL k", "EXPIRE k 10", "HSET h f v", "LPUSH l a"):
            self.assertEqual(self.p.classify(cmd).level, Level.WRITE, msg=cmd)

    def test_danger_commands(self):
        for cmd in ("FLUSHALL", "CONFIG GET *", "SHUTDOWN", "SAVE", "MONITOR"):
            self.assertEqual(self.p.classify(cmd).level, Level.DANGER, msg=cmd)

    def test_keys_danger_with_scan_hint(self):
        spec = self.p.classify("KEYS *")
        self.assertEqual(spec.level, Level.DANGER)
        self.assertTrue(any("SCAN" in r for r in spec.reasons))

    def test_unknown_command_defaults_danger(self):
        self.assertEqual(self.p.classify("TOTALLYNEWCMD x").level, Level.DANGER)

    def test_case_insensitive(self):
        self.assertEqual(self.p.classify("get k").level, Level.READONLY)

    def test_empty_rejected(self):
        with self.assertRaises(ConnectorError):
            self.p.classify("  ")

    def test_targets_capture_key(self):
        self.assertEqual(self.p.classify("GET user:1").targets, ("user:1",))


class TestMongoClassify(unittest.TestCase):
    def setUp(self):
        self.p = MongoPlugin("m", {"host": "127.0.0.1", "database": "test"})

    def op(self, **kw):
        return json.dumps(kw)

    def test_find_readonly(self):
        spec = self.p.classify(self.op(collection="users", method="find"))
        self.assertEqual(spec.level, Level.READONLY)
        self.assertEqual(spec.targets, ("users",))

    def test_writes(self):
        for m in ("insertOne", "updateMany", "deleteOne", "createIndex"):
            self.assertEqual(
                self.p.classify(self.op(collection="c", method=m)).level,
                Level.WRITE, msg=m)

    def test_dangers(self):
        for m in ("drop", "dropDatabase", "renameCollection"):
            self.assertEqual(
                self.p.classify(self.op(collection="c", method=m)).level,
                Level.DANGER, msg=m)

    def test_delete_many_empty_filter_upgraded(self):
        spec = self.p.classify(self.op(collection="c", method="deleteMany",
                                        filter={}))
        self.assertEqual(spec.level, Level.DANGER)
        self.assertIn("delete_without_where", spec.reasons)
        spec2 = self.p.classify(self.op(collection="c", method="deleteMany",
                                         filter={"a": 1}))
        self.assertEqual(spec2.level, Level.WRITE)

    def test_aggregate_out_upgraded_to_write(self):
        spec = self.p.classify(self.op(
            collection="c", method="aggregate",
            pipeline=[{"$match": {}}, {"$out": "backup"}]))
        self.assertEqual(spec.level, Level.WRITE)
        spec2 = self.p.classify(self.op(
            collection="c", method="aggregate",
            pipeline=[{"$group": {"_id": 1}}]))
        self.assertEqual(spec2.level, Level.READONLY)

    def test_unknown_method_default_danger(self):
        spec = self.p.classify(self.op(collection="c", method="dropHead"))
        self.assertEqual(spec.level, Level.DANGER)

    def test_bad_json_rejected_without_echo(self):
        secret = '{"method":"find","pass":"s3cr3ts3cr3ts3cr3t'
        with self.assertRaises(ConnectorError) as cm:
            self.p.classify(secret)
        self.assertNotIn("s3cr3ts3cr3ts3cr3t", str(cm.exception))  # §13.3 不回显

    def test_missing_method_rejected(self):
        with self.assertRaises(ConnectorError):
            self.p.classify('{"collection":"c"}')


if __name__ == "__main__":
    unittest.main(verbosity=2)
