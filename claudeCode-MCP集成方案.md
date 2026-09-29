# pg-mcp 与 Claude Code 集成方案

> 适用对象：`w5/pg-mcp`（PostgreSQL 自然语言查询 MCP 服务）
> 当前环境已完成集成并验证通过（`claude mcp list` → `pg-mcp ✔ Connected`，
> stdio 冒烟返回真实查询结果 + request_id + 真实 token 用量）。
> 本文既是本次集成的**实录复盘**，也是可复用的**标准操作手册**。

---

## 1. 集成原理（30 秒版）

```
┌──────────────┐  ①按注册配置拉起子进程   ┌────────────────────────────┐
│ Claude Code  │ ───────────────────────► │ start_server.sh            │
│ (MCP Client) │                          │  · 导出 .env 环境变量       │
│              │  ②stdio JSON-RPC 双向通信 │  · PYTHONPATH=src          │
│              │ ◄──────────────────────► │  · python -m pg_mcp        │
└──────────────┘                          └─────────────┬──────────────┘
        │ ③模型决定何时调用工具                          │
        ▼                                              ▼
  mcp__pg-mcp__query(question, database,    asyncpg 连接池 → PostgreSQL
  return_type)                 10.128.14.72:5433（只读事务）
                                                 │
                                        LLM 网关（SQL 生成/结果校验）
                                         http://10.11.203.232:3030/v1
```

关键点：
- **传输方式**：stdio。Claude Code 把注册的 `command + args` 作为子进程拉起，通过
  stdin/stdout 交换 JSON-RPC。因此**子进程的 stdout 必须只输出协议报文**——
  pg-mcp 的日志已定向到 stderr（`observability/logging.py`），符合该约束。
- **工具命名**：注册名 `pg-mcp` + 工具名 `query` → 在 Claude Code 内部为
  `mcp__pg-mcp__query`（权限控制、`--allowedTools` 均用此全名）。
- **环境变量优先级**：`start_server.sh` 中「客户端注入的 env > 项目 `.env` 文件」，
  因此可以在注册时按需覆盖单个变量（如 `DATABASE_NAME`），不必改 `.env`。

---

## 2. 前置条件清单

| # | 条件 | 本环境实测 |
|---|------|-----------|
| 1 | Python 3.12 + 依赖（mcp/fastmcp、asyncpg、sqlglot、openai、pydantic-settings、prometheus_client） | `/root/miniconda3/envs/peixun/bin/python`（3.12.13） |
| 2 | `.env` 配置完整（DB 连接、OPENAI_API_KEY/BASE_URL/MODEL、安全与韧性参数） | [w5/pg-mcp/.env](.env) |
| 3 | PostgreSQL 可达且账号可用 | 10.128.14.72:5433，postgres 库 |
| 4 | LLM 网关可达（SQL 生成/结果校验用） | 10.11.203.232:3030/v1，glm-5.3-flash |
| 5 | 服务可独立启动（**集成前必做**） | `bash start_server.sh` 手动跑一次无报错 |

> 经验：先手动启动验证，再注册到 Claude Code。stdio 服务的启动失败在客户端侧
> 只表现为「连接失败」，日志不可见，排查成本高。

---

## 3. 集成步骤

### 3.1 方式 A：Local 作用域注册（当前采用，推荐单人开发）

```bash
cd /usr/local/dbb/peixun          # 注意：在哪个目录执行，就注册到哪个项目
claude mcp add pg-mcp \
  --scope local \
  -- bash /usr/local/dbb/peixun/geektime-bootcamp-ai/w5/pg-mcp/start_server.sh
```

注册结果落盘在 `~/.claude.json`：

```json
// ~/.claude.json → projects["/usr/local/dbb/peixun"].mcpServers
"pg-mcp": {
  "type": "stdio",
  "command": "bash",
  "args": ["/usr/local/dbb/peixun/geektime-bootcamp-ai/w5/pg-mcp/start_server.sh"],
  "env": {}
}
```

特点：
- 仅在 `/usr/local/dbb/peixun` 目录下启动的 Claude Code 会话可见（private to you）；
- 若需注入覆盖变量，用 `--env KEY=VALUE`（写入上面 `"env"` 字段），例如强制只连
  yancheng 数据面：`--env SECURITY_ALLOWED_SCHEMAS='["yancheng"]'`。

### 3.2 方式 B：项目级 `.mcp.json`（团队共享，需人工审批）

在仓库根目录创建 `.mcp.json`（参照 geektime-bootcamp-ai 已有的 playwright 注册）：

```json
{
  "mcpServers": {
    "pg-mcp": {
      "type": "stdio",
      "command": "bash",
      "args": ["/usr/local/dbb/peixun/geektime-bootcamp-ai/w5/pg-mcp/start_server.sh"]
    }
  }
}
```

- 提交进 git 后，团队每个成员首次使用时 Claude Code 会提示
  **「.mcp.json servers are awaiting approval」**，批准后才生效（防供应链投毒机制）。
- 注意 `start_server.sh` 是绝对路径，团队共用时建议改为仓库内相对路径 +
  约定好 conda 环境名，或在脚本内做成可配置。

### 3.3 方式 C：User 作用域（个人全局可用）

```bash
claude mcp add pg-mcp --scope user -- bash .../start_server.sh
```

任何目录启动 Claude Code 都可见。仅当你把它当**个人通用数据库助手**时使用；
多项目环境不建议（避免无关项目获得数据库访问面）。

### 3.4 方式 D：VSCode 扩展形态（本会话的运行形态）

Claude Code VSCode 扩展同样读取上述三类注册。区别在于：
- 扩展侧还会注入自己的 MCP（如本会话的 `web-reader`），与 pg-mcp 并存互不影响；
- 会话内用 `/mcp` 命令查看连接状态与审批。

---

## 4. 注册后验证（三级验证法）

```bash
# ① 配置层：注册是否存在、参数是否正确
claude mcp get pg-mcp        # 在注册时所在的项目目录执行！

# ② 连接层：健康检查（实际拉起子进程完成握手）
MCP_TIMEOUT=120000 claude mcp list
# 期望输出：pg-mcp: bash .../start_server.sh - ✔ Connected

# ③ 业务层：真实查询冒烟（stdio JSON-RPC 直连，不依赖 Claude Code）
#    见附录 A 脚本，期望 success=true、tokens_used>0、request_id 非空
```

> **作用域陷阱（本会话实测踩过）**：`mcp list` 的结果与**执行时所在目录**绑定。
> 在 `/usr/local/dbb/peixun` 下执行能看到 pg-mcp；cd 到 `geektime-bootcamp-ai`
> 子目录后再执行，会读那个目录的 `.mcp.json`（playwright），显示「No MCP server
> named "pg-mcp"」。排查时先确认 cwd。

---

## 5. 会话内使用

### 5.1 自然语言触发（自动选择工具）

用户提问涉及数据库时，模型会自主调用 `query` 工具。实际会话示例：

> 用户：postgres 库中，schema 为 yancheng，请查询 t_chat_message 表数据，limit 10 条
> （Claude Code → `mcp__pg-mcp__query` → SQL 生成 → 安全校验 → 只读执行 → 返回 10 行）

带库名参数的多库路由由服务端完成（`database` 参数 → `executors[db]`）。

### 5.2 无头模式 / 脚本化集成

```bash
CLAUDE_CLI=/root/.vscode-server/extensions/anthropic.claude-code-2.1.195-linux-x64/resources/native-binary/claude
cd /usr/local/dbb/peixun    # 必须在注册作用域目录下
MCP_TIMEOUT=120000 $CLAUDE_CLI -p "查询 yancheng.t_chat_message 有多少行" \
  --allowedTools "mcp__pg-mcp__query"
```

适用：CI 冒烟、定时巡检、批处理。注意 `--allowedTools` 用工具全名做白名单可免人工审批。

### 5.3 斜杠/审批相关

- 会话内 `/mcp` 查看状态；`playwright` 等项目级服务首次使用需批准；
- 工具级权限可在 `.claude/settings.json` 的 `permissions.allow` 中固化
  `"mcp__pg-mcp__query"`，减少打断。

---

## 6. 安全边界（集成即生效，无需额外动作）

| 机制 | 配置项（.env） | 默认 |
|------|----------------|------|
| 只读执行 | 所有 SQL 在 `READ ONLY` 事务中运行；`SECURITY_ALLOW_WRITE_OPERATIONS` | false |
| 危险函数黑名单 | `SECURITY_BLOCKED_FUNCTIONS`（pg_sleep/lo_import/dblink…） | 10+ 项 |
| 表/列黑名单 | `SECURITY_BLOCKED_TABLES`（支持 `schema.table`）/ `SECURITY_BLOCKED_COLUMNS` | 空 |
| schema 白名单 | `SECURITY_ALLOWED_SCHEMAS`（空 = 不限制；限定名按部署启用） | 空 |
| EXPLAIN 策略 | `SECURITY_ALLOW_EXPLAIN` / `SECURITY_ALLOW_EXPLAIN_ANALYZE` | false / false |
| CTE/子查询深扫 | 禁止写操作藏在 CTE（`WITH x AS (DELETE...) SELECT`） | 恒开 |
| 纵深防御 | 执行前对黑名单表二次复核（防解析器漂移） | 恒开 |
| 限流 | `RESILIENCE_MAX_CONCURRENT_QUERIES/LLM` + `RATE_LIMIT_TIMEOUT` | 10 / 5 / 5s |
| 熔断 | `RESILIENCE_CIRCUIT_BREAKER_THRESHOLD/TIMEOUT` | 5 次 / 60s |
| 行数/超时 | `SECURITY_MAX_ROWS` / `MAX_EXECUTION_TIME` | 10000 / 30s |

⚠️ 集成约定：`.env` 里的 schema 白名单**不要**照抄示例值——本库数据分散在
`yancheng/xzwa/group_message` 等多个 schema，配 `["public"]` 会拦截全部真实查询
（实测教训，见《作业2设计.md》实施记录）。

---

## 7. 排障手册（按症状查）

| 症状 | 根因 | 处置 |
|------|------|------|
| `mcp list` 显示 ✘ Failed to connect | 子进程启动失败或握手超时 | 先手动 `bash start_server.sh` 看报错；加大 `MCP_TIMEOUT=120000` |
| 会话里看不到 pg-mcp 工具 | cwd 不在注册作用域 / 服务被禁用 | `claude mcp get pg-mcp` 确认；回注册目录启动会话 |
| 工具返回 `database_error: Database 'x' not found` | 请求的库未在 `DATABASES` 中注册 | `.env` 增加 `DATABASES` JSON 或使用已注册库名 |
| 返回 `rate_limit_exceeded` | 并发超过 `MAX_CONCURRENT_QUERIES` 且排队超时 | 调大配额或 `RATE_LIMIT_TIMEOUT` |
| 返回 `llm_error: empty message content` | LLM 网关思考耗尽 `OPENAI_MAX_TOKENS`（finish_reason=length） | 调大 max_tokens；确认 `enable_thinking=false` 生效 |
| 协议流被日志污染、握手失败 | 有组件往 stdout 打日志 | 保持日志走 stderr（当前实现已保证） |
| 指标端口冲突 | `OBSERVABILITY_METRICS_PORT` 被占 | 改端口或 `METRICS_ENABLED=false` |

**终极兜底**：绕过 Claude Code 直连服务定位问题——附录 A 脚本可独立完成
「拉起服务 → 调工具 → 打印结果」，本会话即用它在 Claude Code 连接抖动时完成验证。

---

## 8. 附录 A：stdio 直连冒烟脚本

```python
# /tmp/pg_mcp_call.py —— 不依赖 Claude Code 的最小验证工具
import json, subprocess

proc = subprocess.Popen(
    ["bash", "/usr/local/dbb/peixun/geektime-bootcamp-ai/w5/pg-mcp/start_server.sh"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL, text=True, bufsize=1)

def send(o): proc.stdin.write(json.dumps(o) + "\n"); proc.stdin.flush()
def recv():
    for line in proc.stdout:
        if line.strip(): return json.loads(line)

send({"jsonrpc":"2.0","id":1,"method":"initialize",
      "params":{"protocolVersion":"2024-11-05","capabilities":{},
                "clientInfo":{"name":"smoke","version":"1.0"}}})
recv()
send({"jsonrpc":"2.0","method":"notifications/initialized"})
send({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"query",
      "arguments":{"question":"数据库里一共有多少张表？排除系统 schema"}}})

payload = json.loads(recv()["result"]["content"][0]["text"])
print("success:", payload["success"])
print("sql:", payload.get("generated_sql"))
print("rows:", payload.get("data", {}).get("rows"))
print("tokens_used:", payload.get("tokens_used"), "| request_id:", payload.get("request_id"))
proc.terminate()
```

验收断言：`success=true`；`tokens_used>0`（真实 token 统计）；`request_id` 非空（全链路追踪）。

## 9. 附录 B：常用运维命令速查

```bash
claude mcp list                      # 健康检查（注意在注册作用域目录执行）
claude mcp get pg-mcp                # 查看注册详情
claude mcp remove pg-mcp -s local    # 注销
claude mcp add pg-mcp -s local --env KEY=V -- bash <脚本路径>   # 注册+覆盖变量
MCP_TIMEOUT=120000 claude -p "..." --allowedTools "mcp__pg-mcp__query"   # 无头调用
PYTHONPATH=src python -m pytest tests/unit tests/security   # 服务端回归（315 例）
curl -s localhost:9090/metrics | grep pg_mcp_query_requests_total   # 运行指标
```
