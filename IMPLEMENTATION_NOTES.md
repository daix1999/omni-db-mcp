# IMPLEMENTATION_NOTES —— 实现偏差与决策记录

> 实现按 `SPEC.md` 执行；以下为全部有意偏离/补充之处，均已在代码注释中标注。
> 日期：2026-09-28

## 设计层偏差（源于 SPEC 内部冲突，已拍板）

1. **规则合成语义（§9.3 vs §14.3 冲突）** —— 头儿拍板：连接内联 `level`
   是属主授权声明，**直接生效**（未写则回落默认 readonly）；`unsafe: true`
   控制的是「挂载规则包能否放宽」。SPEC §9.3 伪代码若照抄，`level: write`
   的连接会被 min() 压回 readonly，§14.3 全部示例失效、§17 用例 6 也不成立；
   现语义下用例 5/6 同时通过。`deny` 并集、`allow` 交集不变。

2. **异常体系定义位置（§7）** —— `ConnectorError` 四件套定义在
   `basePlugin.py`，`core/errors.py` re-export 保持 §7 导入路径兼容。
   原因：插件必须抛 `QueryError/ConnectionFailed`，而验收标准 #2 要求
   插件只 import basePlugin；两处规则无法同时满足，选择保住解耦红线。
   依赖方向无回环（errors → basePlugin 单向）。

3. **BasePlugin 新增两个普通类属性（非抽象方法）** ——
   `REQUIRED_FIELDS`（§8.3 缺字段报错需要插件声明必填项）、
   `EXTRA_FIELDS`（避免插件私有字段被 §8.3 未知字段警告误伤）。
   §5.2 只禁止添加 `probe()` 方法，此改动为契约携带元数据，不扩方法面。

## 运行时决策

4. **`db_list_connections` 惰性探测版本** —— §15.1 要求返回 version，
   只能在列连接时建连探测；单连接不可达返回「不可达（原因）」，
   不阻塞整体列表（connect_timeout 兜底）。

5. **拒绝消息携带分级判定依据** —— policy 拦截消息附加
   `（操作说明：<classify reasons>）`，落实 §12.4「KEYS 须给出 SCAN 建议」
   与 §15.3「说清为什么不行」。

6. **deny 判定先于级别判定** —— 显式规则命中比泛化的「需要 X 级别」
   信息量更大（例：readonly 连接跑无 WHERE DELETE 报 `delete_without_where`）。

7. **MCP SDK 适配** —— 本机 mcp 为 2.x（`FastMCP` 已改名 `MCPServer`），
   `core/app.py` 用 try/except 双兼容 import，与 §3 的 `from
   mcp.server.fastmcp import FastMCP` 等价。

8. **prod-safe.yaml 未设 `global: true`** —— §9.1 示例带 global，但
   deny 是不可撤销并集，全局套用会连本机开发库一起禁 DDL。默认按连接
   挂载（文件头注释说明如何切全局）。

9. **MongoDB 三个只判不跑的方法** —— `renameCollection` / `convertToCapped`
   / `shutdown` 保留 §13.4 分级识别（DANGER），执行面明确未开放
   （SPEC 未定义其参数透传格式，遵循「不擅自发明接口」）。

10. **Redis KEYS 双保险** —— 即使连接放开 danger，execute 也不实际执行
    KEYS，返回空结果 + 改用 SCAN 的 notice。

11. **MySQL autocommit=True** —— 首版无 begin/commit 工具面，写入自动落盘；
    `transaction` 能力仅声明在 capabilities。

## 版本差异处理实测说明

- §11.6 的 CTE / 窗口函数 5.7 门控：本机只有 MySQL 8.0.46，版本门以
  离线单测覆盖（注入假 server_info 模拟 5.7，见 tests/test_mysql_plugin.py）；
  8.0 路径已真实联调。
- sqlglot 30.x 对 FLUSH / SHUTDOWN 无法结构化解析 → 按 §16.4 默认拒绝
  判 DANGER；GRANT / RENAME 回退为 Command 节点，按首关键字归入
  grant / DDL 类别（均有单测断言）。

## 2026-09-29 代码审计（用户+审计双视角）结论与处置

**已修复（P0/P1，头儿批准）**：EXPLAIN/DESCRIBE 误判 DANGER（`exp.Describe`
无分支落兜底，readonly 连接看不了执行计划）；写操作断线不自动重试
（§16.2 语义修订，见 SPEC 注记）；README 接入 JSON 补 cwd；Mongo 操作超时
（socketTimeoutMS+maxTimeMS）；列连接探测并发化。

**挂账（见 CHANGELOG「挂账排期」）**：恒真 WHERE 绕过、元数据操作不落审计、
sqlglot round-trip 与审计原文不一致风险、BLPOP 阻塞命令、`allow` 字段死锁
（规格缺陷待拍板）、db_audit_query 工具、审计保留策略、事务支持等。

## 验收自查（SPEC §19）

| # | 标准 | 结果 |
|---|---|---|
| 1 | 底座无类型判断 | ✅ grep 证实 base/core 无 mysql/redis/mongo 分支 |
| 2 | 插件只 import basePlugin | ✅ |
| 3 | 依赖方向无回环 | ✅ |
| 4 | 单文件 ≤350 行 | ✅ 最大 327（mysqlPlugin） |
| 5 | 加库不改底座 | ✅ Step4 只加 conf；mongo 插件新增时 core/ 零改动 |
| 6 | 拦截有审计 | ✅ denied 记录实测落库 |
| 7 | 无 traceback 外泄 | ✅ |
| 8 | env 缺失报变量名 | ✅ 测试 13 |
| 9 | 未知字段警告 | ✅ 测试 14 |
| 10 | 5 工具可被发现 | ✅ MCP 客户端实测 |
| 11 | 脱离真库测试 | ✅ 61 个离线用例全绿 |
| 12 | 三件待补事项 | ✅ 字段全可选 / timeout / kind 字段 |
