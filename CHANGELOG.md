# CHANGELOG

本项目的版本与发布记录。发布约定：先本地积累并验证 → 更新本文件 → 头儿确认后推送。

## [1.0.0] - 2026-09-28

首个正式版本。按 `SPEC.md` 完整实现并通过全部验收（§19 十二条逐项核对通过）。

### 新增

**底座（`core/` + `basePlugin.py`）**

- 一级/二级分流：5 个 `db_*` 工具统一入口，底座零数据库类型判断（grep 验证）
- 三级权限（readonly / write / danger），MySQL 用 sqlglot AST 判级，Redis 查命令表，Mongo 查方法表，未知操作默认按 DANGER 拒绝
- 规则包四层合成：只能收紧；连接内联 `level` 为授权声明直接生效；`unsafe: true` 是规则包放宽的唯一开关；`deny` 并集不可撤销
- 审计：SQLite + JSONL 按天双写，成功/被拒/出错三种状态全覆盖，参数值与密码不落盘
- 连接管理：`${env:}` 密码引用（缺失报变量名）、未知字段警告、惰性建连、实例缓存、断线自动重连并重试一次
- 切面链插槽（限流/脱敏/缓存将来挂入，不改执行链路）、错误回调机制（回调异常吞掉不影响主流程）

**插件（`plugins/`）**

- `mysqlPlugin`：5.7/8.0 同一类；LIMIT 自动注入/收敛（notice 告知）；CTE/窗口函数在 5.7 明确报错不改写；错误码归一（2003/2006/2013 → 可重连）
- `redisPlugin`：命令分级表（约 130 条）；KEYS 判 danger 附 SCAN 建议且不实际执行；结果规整为标量/列表/哈希三种二维表
- `mongoPlugin`：JSON 操作协议；deleteMany 空 filter 升 DANGER；aggregate 含 $out/$merge 升 WRITE；坏 JSON 拒绝不回显原文；驼峰方法名自动映射 pymongo

**工具面**

- `db_list_connections`（含版本探测/能力列表/operation_hint）、`db_list_tables`、`db_describe_table`（按能力门控，不支持的类型给出建议而非空列表）、`db_query`（硬上限只读）、`db_exec`

**测试与脚本**

- 61 项离线测试：底座 23（含 SPEC §17 十五条必测）+ MySQL 22 + Redis/Mongo 分级 16，全程不依赖真库
- 三方言真库冒烟脚本 + MCP stdio 端到端检查（`scripts/`）

### 性能（2026-09-29 实测补充）

- 审计 SQLite 开启 `journal_mode=WAL` + `synchronous=NORMAL` + `busy_timeout=5000`，
  消除逐条 commit 造成的全局串行点：单次工具调用 p50 从 ~80ms 降至 ~1ms，
  MCP stdio 往返 p50 2.5ms，链路吞吐 12 → 500+ QPS（`scripts/bench.py` 可复现）
- 并发模型确认（mcp 2.x）：单会话内请求级并发（spawn + 线程池），多客户端即多进程
- 新增基准脚本 `scripts/bench.py`；已知边界：MySQL 插件为单连接，非线程安全，
  多线程并发打同一连接会出错（AI 会话内不会触发）；如需机器级并发，
  在 mysqlPlugin 连接层接 DBUtils 连接池即可，底座零改动

### 审计修复（2026-09-29 · 代码审计发现，头儿批准执行）

**P0（已修复）**

- `EXPLAIN` / `DESCRIBE` 误判 DANGER：sqlglot 的 `exp.Describe` 节点此前无分支，
  落兜底默认拒绝，readonly 连接无法看执行计划。已修复：纯 EXPLAIN/DESCRIBE →
  READONLY；`EXPLAIN ANALYZE`（会真执行内部语句）仅对 SELECT 放行，DML 默认拒绝
- **写操作断线不再自动重试**（安全语义变更，偏离 §16.2 原文并已注记）：
  INSERT/INCR/SET 等非幂等操作首次请求可能已到达服务器，自动重试会造成静默
  重复写。现在仅 readonly 操作自动重连重试；写/危险操作断线返回明确错误，
  提示 AI 先确认首次执行是否生效
- README 接入 JSON 补 `cwd` 字段（缺失时客户端从其他目录启动会加载不到
  `.env`，所有 `${env:VAR}` 报"未设置"）

**P1（已修复）**

- Mongo 操作超时：pymongo `socketTimeoutMS` 默认 None（永不超时），已显式下发
  连接的 timeout（默认 30s），find/aggregate/countDocuments 另带查询级 maxTimeMS
- `db_list_connections` 版本探测并发化（线程池）：此前串行，N 个不可达连接
  最坏阻塞 N×connect_timeout；不可达连接返回占位串不阻塞整体

**挂账排期（审计发现，本轮不修）**

- 恒真 WHERE 绕过 without_where 检测（如 `WHERE 1=1`）——建议生产连接坚持
  只读账号兜底；可加 affected 行数阈值审计打标
- 元数据操作（list/describe/connections）不落审计——DESIGN §9 目标为全部操作
- LIMIT 注入/多语句执行使用 sqlglot 重建 SQL（round-trip 语义差异风险），
  审计记录的是原文——需文档标注或记录执行版
- Redis BLPOP/BRPOP 为阻塞命令，在 WRITE 表内（会占住连接至 timeout）
- `allow` 规则字段实际无效（内置 allow=空集 ∩ 任何值 = 空，§9.3 伪代码
  语义死锁）——待拍板：改语义或删字段
- `db_audit_query` 工具、审计库保留策略、事务支持、`params` 仅 MySQL 生效的
  文档说明——见 P2 清单

### 省 token 三件套（2026-09-29，头儿拍板范围）

- 新增 `core/aspects.py` · `TokenTrimAspect`：SPEC §8.5 切面插槽的首个正式切面，
  经 `register_aspect` 挂入，执行链路零改动
  - 单值截断：str 单元格 >300 字符截断并在 notice 说明（防 TEXT/JSON 大字段爆上下文）
  - `approx_tokens`：返回体 token 粗估（ASCII≈4 字符/token、CJK≈1 字符/token，偏保守），
    超 8000 时在 notice 追加分页建议
  - `trim_describe`：describe 的 DDL >2000 字符截断并置 `ddl_truncated=true`
- `db_list_tables` / `db_describe_table` 返回同样过裁剪与计量
- `db_query` / `db_describe_table` 工具描述内嵌省上下文使用策略
  （先 COUNT 摸量、小样本、分页、收窄 filter）
- 真库实测：5000 字符大字段返回 325 字符 + 截断说明；500 行宽结果估算 41319 tokens
  并提示分页；30 列大表 DDL 7680→2025 字符。新增测试 13 项（累计 74 项全绿）
- 内置默认 `max_rows=500` 经拍板保持不变，控量靠值截断 + token 计量提示

### 决策记录

- SPEC §9.3 与 §14.3 的放宽语义冲突，拍板为「`level` 授权直接生效、`unsafe` 只管规则包」——详见 [IMPLEMENTATION_NOTES.md](./IMPLEMENTATION_NOTES.md)（共 11 处偏差/决策，均已在代码注释标注）

### 已知边界

- MySQL 5.7 的 CTE/窗口函数拦截为离线单测覆盖（本机无 5.7 实例），8.0 路径已真库验证
- Mongo 的 `renameCollection` / `convertToCapped` / `shutdown` 只判级不执行
- 不支持时序 / 图 / 向量 / 异步数仓（SPEC §2.3 明确排除）
