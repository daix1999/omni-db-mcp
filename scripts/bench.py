"""omni-db-mcp 吞吐基准（scripts/bench.py）。

测三件事：
  1. 顺序吞吐：三方言各 200 次最小查询（app 层直调，排除 MCP 协议开销）
  2. 并发吞吐：Redis/Mongo（驱动线程安全）8 线程
  3. MySQL 并发探针：证明"单连接非线程安全"这一已知短板（预期出现错误）

另附 MCP stdio 往返延迟（协议开销），单独脚本输出。

用法：python scripts/bench.py
"""
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, ".env"))

import plugins.mongoPlugin   # noqa: F401,E402
import plugins.mysqlPlugin   # noqa: F401,E402
import plugins.redisPlugin   # noqa: F401,E402
from core import app  # noqa: E402

N_SEQ = 200
N_PAR = 200
THREADS = 8


def pctl(xs, p):
    xs = sorted(xs)
    return xs[max(0, min(len(xs) - 1, int(len(xs) * p / 100)))]


def bench(name, fn, n):
    lat = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        lat.append((time.perf_counter() - t0) * 1000)
    qps = n / (sum(lat) / 1000)
    print(f"  {name:<28} QPS {qps:8.0f} | p50 {pctl(lat,50):6.2f}ms "
          f"p95 {pctl(lat,95):6.2f}ms p99 {pctl(lat,99):6.2f}ms")
    return qps


def bench_parallel(name, fn, n, threads):
    lat = []
    def one(_):
        t0 = time.perf_counter()
        fn()
        return (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    with ThreadPoolExecutor(threads) as ex:
        lat = list(ex.map(one, range(n)))
    wall = time.perf_counter() - t0
    print(f"  {name:<28} QPS {n/wall:8.0f} | p50 {pctl(lat,50):6.2f}ms "
          f"p95 {pctl(lat,95):6.2f}ms ({threads} 线程)")


def main():
    app.bootstrap(conf_dir=os.path.join(ROOT, "conf"),
                  rules_dir=os.path.join(ROOT, "rules"),
                  logs_dir=os.path.join(ROOT, "logs"))
    q = app.impl_query
    x = app.impl_exec
    print(f"== 顺序基准（app 层直调，n={N_SEQ}）==")
    bench("MySQL  SELECT 1",
          lambda: q("local-test", "SELECT 1"), N_SEQ)
    bench("Redis  GET bench:key",
          lambda: (x("local-redis", "SET bench:key v") or
                   q("local-redis", "GET bench:key")), N_SEQ)
    bench("Mongo  countDocuments",
          lambda: q("local-mongo",
                    '{"collection":"bench","method":"countDocuments","filter":{}}'),
          N_SEQ)

    print(f"== 并发基准（{THREADS} 线程 × {N_PAR}）==")
    bench_parallel("Redis  GET（线程安全连接池）",
                   lambda: q("local-redis", "GET bench:key"), N_PAR, THREADS)
    bench_parallel("Mongo  countDocuments（内置连接池）",
                   lambda: q("local-mongo",
                             '{"collection":"bench","method":"countDocuments","filter":{}}'),
                   N_PAR, THREADS)

    print("== MySQL 并发探针（8 线程 × 20，预期出错：证明单连接非线程安全）==")
    errs = {"n": 0}
    def probe(_):
        try:
            r = q("local-test", "SELECT 1")
            if not r.get("ok"):
                errs["n"] += 1
        except Exception:
            errs["n"] += 1
    t0 = time.perf_counter()
    with ThreadPoolExecutor(THREADS) as ex:
        list(ex.map(probe, range(THREADS * 20)))
    wall = time.perf_counter() - t0
    total = THREADS * 20
    print(f"  {total} 次并发 SELECT：错误 {errs['n']} 次 "
          f"({errs['n']*100/total:.0f}%)，墙钟 {wall:.2f}s")

    print("== 清理 ==")
    try:
        x("local-redis", "DEL bench:key")
        app.impl_exec("local-mongo",
                      '{"collection":"bench","method":"drop"}')
    except Exception:
        pass
    app.get_manager().close_all()
    from core import audit
    audit.get_logger().close()
    print("完成")


if __name__ == "__main__":
    main()
