# FAQ · 常见问题

> 按实际使用场景整理。被 AI 拒绝操作时，先看「权限与拦截」一节——多数"为什么不让做"的答案都在那里。

## 安装与启动

### Q：启动报「未找到 conf/xxxConfig.yaml」？
这是**警告不是错误**，表示该类型暂无连接，忽略即可。若确实需要，参考 `.env.example` 和 `conf/` 下现有文件创建。

### Q：启动报「连接 xxx 引用的环境变量 OMNI_MYSQL_LOCAL_PWD 未设置」？
密码走 `${env:VAR}` 引用。在项目根目录的 `.env` 里补上 `VAR=真实密码` 后重启（`.env` 已被 load_dotenv 自动加载，已列入 `.gitignore`，不会提交）。

### Q：Python 版本要求？
3.11+。本机 3.12.8 实测通过。MCP SDK 兼容 v1（FastMCP）与 2.x（MCPServer），代码里做了双版本适配。

## 权限与拦截

### Q：`db_exec` 被拒：「该操作需要 write 级别，连接 xxx 的上限是 readonly」？
该连接 `level: readonly`。放开方式：编辑 `conf/` 对应文件，把该连接的 `level` 改为 `write`（或 `danger`），重启生效。生产连接不建议放开——正确姿势是**连数据库账号本身也用只读账号**，双保险。

### Q：想执行 DROP / CREATE 等 DDL？
需要 `level: danger`。`write` 不含 DDL。本地测试库可设 `danger`；生产库强烈不建议。

### Q：被拒时消息里的「（操作说明：…）」是什么？
是分级判定给出的**理由**。例如 Redis `KEYS` 会附「会阻塞整个实例，建议改用 SCAN」；无 WHERE 的 DELETE 会附「delete_without_where」。照着说明调整即可。

### Q：`db_query` 和 `db_exec` 什么区别？
`db_query` 硬上限 readonly，SELECT/GET/find 走它；INSERT/UPDATE/DELETE/SET/DDL 走 `db_exec`（用 `db_query` 传写操作会被直接拒绝并提示改用 `db_exec`）。

### Q：规则包里写的 `max_rows: 1000` / `max_level: danger` 不生效？
规则包默认**只能收紧不能放宽**。要让挂载包的放宽生效，必须在连接上显式写 `unsafe: true`（全系统唯一包级放宽开关，方便事后 `grep unsafe` 审计）。注意连接里直接写的 `level` 是授权声明，不需要 unsafe 即可生效。

### Q：怎么临时禁掉某类操作？
挂一个 deny 列表更宽的规则包即可，例如 `deny: [DDL, multi_statement]`。deny 是**并集**，一旦挂上无法通过任何配置撤销（只能改规则包文件本身）。

## 操作格式

### Q：operation 到底传什么格式？
看 `db_list_connections` 返回的 `operation_hint`：
- MySQL → SQL 字符串：`"SELECT id FROM orders LIMIT 10"`
- Redis → 命令字符串：`"GET user:1"`、`"HGETALL cart:1001"`
- Mongo → JSON 字符串：`'{"collection":"users","method":"find","filter":{"vip":true},"limit":20}'`

### Q：为什么 Redis 连接调 `db_list_tables` 报「不支持列出实体」？
Redis 没声明 `entity` 能力（键值型无实体概念），工具层会拦下并给建议。查 key 用 `db_query` 传 `SCAN 0 MATCH user:* COUNT 100`。

### Q：返回里的 `notice: "已自动注入 LIMIT 100"` 是什么？
查询返回行数有上限（内置默认 500，可被规则包/连接配置收紧）。原语句没有 LIMIT 或 LIMIT 超上限时会自动注入/收敛，并如实告知；`truncated: true` 表示结果被截断，需要更多数据请分页或收紧 filter。

### Q：MySQL 5.7 连接上报「该语法需要 MySQL 8.0 及以上」？
CTE（WITH）和窗口函数是 8.0 特性。系统**只报错不改写**（自动改写有语义漂移风险）。按建议改用子查询，或换 8.0 连接测试。

## 审计

### Q：操作日志在哪？怎么查"AI 被拒了几次"？
```
logs/operation_log.db                  -- SQLite 主存储
logs/audit-2026-09-28.jsonl            -- 按天轮转的旁路文件
```
```sql
sqlite3 logs/operation_log.db "SELECT ts,connection,action,status,error FROM operation_log WHERE status='denied' ORDER BY ts DESC"
```

### Q：日志里能看到参数值吗？
不能。参数化查询只记语句原文不记参数值；超长语句截断到 4000 字符，连续 21 位以上的疑似敏感串会被 `***` 替代，密码/连接串永不落盘。

## 运维

### Q：连接闲置后再查询报「MySQL server has gone away」？
不用处理，底座会自动重连并重试原操作一次（对 Redis `ConnectionError`、Mongo `AutoReconnect` 同样生效）。重试仍失败才会把错误返回给 AI。

### Q：新增一个数据库环境要改代码吗？
不用。在对应 `conf/xxxConfig.yaml` 里加一段（顶层 key 是新别名）+ 在 `.env` 补密码，重启即可。改代码才说明抽象出了问题。

### Q：怎么临时下线一个连接？
注释掉 conf 里对应段落即可。别名被 AI 引用时会给出行不通的明确报错和剩余可用列表。

## 安全

### Q：为什么 Redis `KEYS` 直接被拒？
`KEYS *` 会阻塞整个实例（所有客户端一起等）。它被判 danger，且即使连接放开到 danger 也不实际执行，返回空结果 + SCAN 建议。

### Q：能用在生产库吗？
可以，这正是设计目标：应用层 `level: readonly` + 挂 `prod-safe` 规则包 + 数据库只读账号，三层防护；所有尝试（含被拒的）都有审计。参考 `conf/mysqlConfig.yaml` 里 `crm-prod` 的注释示例。

## 调试与接入

### Q：怎么本地调试 / 看工具列表？
`python scripts/mcp_client_check.py` 会起一个真实 server 并用官方客户端走完整协议；也可以用 MCP Inspector 连 `python base.py`。

### Q：怎么接入千问办公？
「设置 → 连接器」手动粘贴 README 里的 stdio JSON（自动注册会被安全策略拦截，属预期）。

### Q：错误信息出现「内部错误，请查看服务端日志」？
插件抛了未归一的异常（理论上不应发生）。去启动 server 的终端看 stderr——那里有完整异常，且不会泄露给 AI 侧。
