# fillbot｜表单填报 RPA 机器人
面向**表单填报（写入）**场景的本地 RPA：读取任务清单，按 YAML 动作脚本登录、分步填表、上传附件、提交并提取回执；全过程有校验、重试、幂等、断点续跑、证据快照、SQLite 台账与汇总报表。技术栈为 requests + BeautifulSoup + Flask，不使用浏览器二进制。
## 合规声明（必读）
- 本机器人只能用于**自己拥有或已获书面授权**的系统。
- 仓库内置且唯一默认可连的目标是本地 mock 站点（`127.0.0.1`）；mock 站点只绑定 `127.0.0.1`。
- 运行与全部测试不访问任何外网地址；代码、fixtures、文档中不含真实可访问目标、真实公司名或真实人名。
- 仓库不含真实密钥/口令/token。mock 账号口令统一以 `mock-only-` 开头，**仅本地 mock 使用**；真实敏感配置只从环境变量读取，参见 `.env.example`。
- 使用者应在授权范围内配置目标，并自行承担合规责任；禁止用于未授权填报、刷单、绕过风控或大规模攻击。
## 定位：写，而不是采
| 是 | 不是 |
| --- | --- |
| 在授权系统中按既有流程填写并提交表单 | 通用爬虫、数据采集器或搜索引擎 |
| 处理多步骤表单、附件、CSRF、回执 | 绕过验证码、风控、登录保护的工具 |
| 批次校验、失败定位、重试、断点续跑 | 分布式任务平台或消息队列系统 |
| 生成可审计的证据、台账和 JSON/CSV 报表 | 真实电子政务/税务系统的官方客户端 |
本机器人强调“可核对、可恢复、可追溯”：宁可在提交前失败，也不在不确定状态下重复写入。
## 快速开始
```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest                                               # 离线；覆盖率门槛 85%
python -m fillbot mock-serve                         # http://127.0.0.1:5000
python -m fillbot validate --tasks tasks/example_batch.json --script scripts/quarterly_report.yaml
python -m fillbot run --tasks tasks/example_batch.json --script scripts/quarterly_report.yaml
python -m fillbot run --tasks tasks/example_batch.json --script scripts/quarterly_report.yaml --dry-run
python -m fillbot resume <run_id> --config configs/bot.example.yaml
python -m fillbot report <run_id> --config configs/bot.example.yaml
# 人工确认门（高敏表单）：门点暂停 -> 人过目 -> 凭一次性令牌批准/拒绝
python -m fillbot run --tasks tasks/example_batch.json --script scripts/quarterly_report_gated.yaml
python -m fillbot confirmations list --config configs/bot.example.yaml
python -m fillbot confirmations approve --token <token> --tasks <tasks> --script <script>   # --token 可重复实现批量批准
python -m fillbot confirmations reject  --token <token> --tasks <tasks> --note "数据不符"
```
`validate` 完全离线，仅检查任务与脚本；`run` 默认串行执行。mock 账号（虚构）：`parkco-b / mock-only-beta`（C002，正常）、`parkco-c / mock-only-gamma`（C003，可注入提交故障）。
## 架构
```text
tasks (JSON/CSV) ──> intake loader ──> validator (row + field diagnostics)
                                            │
scripts (YAML) ──> DSL schema ──> DSL engine ──> MechanicalBrowser (requests/BeautifulSoup)
                                            │              │
                                            │              ├─ login / multi-step forms / CSRF / upload
                                            │              └─ submit / receipt extraction
                                            ├─ retry policy (backoff + jitter + Retry-After)
                                            ├─ evidence store (HTML, actions.jsonl, receipt.html)
                                            └─ ledger (SQLite: runs / tasks / steps) ──> summary.json / summary.csv
```
## 目录
```text
src/fillbot/browser/     requests+BeautifulSoup 适配器、页面模型、脱敏
src/fillbot/dsl/         脚本 schema、动作实现、选择器、执行引擎
src/fillbot/intake/      JSON/CSV 载入、字段规范化与聚合校验
src/fillbot/runner/      串行调度、重试、状态机、限速、续跑
src/fillbot/ledger/      SQLite 台账与汇总报表
src/fillbot/evidence/    证据目录、HTML 脱敏、保留策略
src/fillbot/mocksite/    仅回环的 Flask 虚构报送站点
scripts/ tasks/ tests/   示例脚本、任务清单、离线测试
```
## 任务清单
JSON 既可是对象也可是数组；CSV 第一行为表头，附件列用 `;` 或 `|` 分隔。必填：`task_id`、`biz_key` 与 `payload` 中的用户名、口令、企业编号、企业名称、报送季度、联系人、报送日期、能耗与用工字段；`attachments` 可选。
```json
{"task_id":"T-1001","biz_key":"C002:2025-Q4","attachments":["attachments/quarterly-notes.pdf"],
 "payload":{"username":"parkco-b","password":"mock-only-beta","company_id":"C002",
 "company_name":"青岚谷生态科技（虚构）","period":"2025-Q4","contact_name":"林小岚（虚构）",
 "report_date":"2025-11-10","electricity":12800.5,"water":340.2,"gas":88.1,"employees":42,"hires":3}}
```
```csv
task_id,biz_key,attachments,company_id,period
T-9002,C002:2025-Q1,attachments/quarterly-notes.pdf,C002,2025-Q1
```
校验失败会一次性聚合所有问题，输出 `文件:row N [field] message`，便于逐行修正。
## DSL 参考
顶层字段：`name`（必填）、`version: 1`、`steps`（非空列表）。每个步骤是映射，支持 `selector` 或 `label` 二选一作为定位器；公共选项：`timeout`（秒，>0）、`retry`（整数或 `{attempts, base_seconds, max_seconds}`）、`on_error`（`fail|skip|continue`）、`final`（仅 `click` 可用，标记提交步骤）。
| 动作 | 参数 | 说明 |
| --- | --- | --- |
| `open` | `url` | 打开 URL；`timeout` 可覆盖 |
| `fill` | `value` | 向输入框/文本域写入，值会字符串化 |
| `select` | `value` | 按可见文本或 value 选择下拉项 |
| `check` | `checked`（默认 true） | 勾选/取消复选框 |
| `upload` | `path` | multipart 上传本地文件，必须存在 |
| `click` | — | 提交所在表单或访问链接，沿用当前会话；`final: true` 标记提交步骤，可再叠加 `require_confirmation: true` 加入人工确认门 |
| `wait` | `seconds` | 显式等待，≤300 秒 |
| `assert_text` | `text`、可选定位器 | 页内/元素内必须包含文本 |
| `assert_status` | `status`（100–599） | 断言 HTTP 状态码 |
| `extract` | `save_as`、可选 `attr` | 提取文本/属性到 `{{var.*}}`，空值报错 |
| `set_var` | `name`、`value` | 设置脚本变量，值支持插值 |
插值命名空间：`{{task.*}}`（任务 payload 与 task_id/biz_key/attachment）、`{{env.*}}`（环境变量，另注入 `FILLBOT_BASE_URL`）、`{{var.*}}`（步骤变量）、`{{run.id}}`。整串插值保留原始类型（数字仍为数字）；嵌入文本时转字符串。未定义变量、未知命名空间、未知动作都会以 `SCRIPT` 错误定位到步骤。
```yaml
name: example
version: 1
steps:
  - {action: open, url: "{{env.FILLBOT_BASE_URL}}/login"}
  - {action: fill, label: "账号", value: "{{task.username}}"}
  - {action: click, selector: "form#login-form button[type=submit]", retry: {attempts: 3, base_seconds: 0.05}}
```
## 配置与环境变量
优先级：CLI > 环境变量 > YAML > 默认值。配置文件示例见 `configs/bot.example.yaml`；未知键会报错。
| 键 / 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `base_url` / `FILLBOT_BASE_URL` | `http://127.0.0.1:5000` | 目标根地址，必须 http(s) |
| `timeout_seconds` / `FILLBOT_TIMEOUT_SECONDS` | 10 | 单请求超时，≥0.01 |
| `max_retries` / `FILLBOT_MAX_RETRIES` | 2 | 最大重试次数 |
| `backoff_base_seconds` / `..._BASE_SECONDS` | 0.4 | 指数退避基数 |
| `backoff_max_seconds` / `..._MAX_SECONDS` | 8.0 | 退避上限 |
| `jitter` / `FILLBOT_JITTER` | true | 是否加抖动 |
| `rate_limit_per_minute` / `..._RATE_LIMIT_PER_MINUTE` | 60 | 全局令牌桶速率 |
| `follow_redirects` / `FILLBOT_FOLLOW_REDIRECTS` | true | 是否跟随重定向 |
| `evidence_dir` / `FILLBOT_EVIDENCE_DIR` | `evidence` | 证据根目录 |
| `evidence_keep_runs` / `FILLBOT_EVIDENCE_KEEP_RUNS` | 10 | 保留最近运行数 |
| `ledger_path` / `FILLBOT_LEDGER_PATH` | `data/ledger.db` | SQLite 台账路径 |
| `runs_dir` / `FILLBOT_RUNS_DIR` | `runs` | run.log 与汇总目录 |
| `mask_fields` | password/passwd/secret/token/csrf_token | 证据脱敏字段 |
## 错误、重试与状态
错误类别：`NETWORK`、`SERVER`（均含 429/5xx）、`CLIENT`（4xx）、`ASSERT`、`SCRIPT`。只有 NETWORK/SERVER 自动重试；`Retry-After` 优先，其次指数退避 + 抖动。任务状态：`pending → running → success|failed|skipped|dry_run|awaiting_confirmation →（批准）running → success|failed /（拒绝）abandoned`；运行状态：`running → success|failed|interrupted|dry_run|awaiting_confirmation`，`interrupted/failed` 可续跑，`abandoned` 为人工放弃终态不参与重试。未捕获异常（含 Ctrl+C/SystemExit）会把运行标记为 `interrupted` 后继续抛出。
## 提交前人工确认门
高敏表单要求“机器填好、人过目后再提交”。在提交所在的 `final` 点击上叠加 `require_confirmation: true` 即可启用；**不标记的旧脚本行为与之前完全一致**（默认全自动，零感知）。每个脚本至多一个确认门。
```yaml
- {action: click, selector: "form#confirm-form button[type=submit]", final: true, require_confirmation: true}
```
生命周期与安全保证：
1. **暂停即持久化**：执行到门点（提交点击之前）时，任务置为 `awaiting_confirmation` 并写入 SQLite；进程被 kill、隔天重启都能通过 `confirmations list` 找回。
2. **脱敏快照与证据**：把确认页脱敏后存为证据目录下的 `confirmation-snapshot.html`，并写一份不含填写值的 `confirmation.json` 字段清单（密码/令牌等仍按 `mask_fields` 脱敏）。
3. **一次性令牌**：暂停时生成 `secrets` 随机令牌，台账只存 SHA-256 哈希与短前缀，明文仅在运行输出中出现一次。`confirmations approve/reject --token` 凭令牌决策；令牌通过条件 UPDATE 原子消费，**错误令牌或已使用令牌一律拒绝（CLI 退出码 3）且写入审计**。
4. **会话不挂起**：暂停时立即关闭浏览器会话（不持有 cookie 干等）。批准后用**全新会话**从第一步安全重放到门点再提交——门点点击在暂停期间从未执行过，因此重放只会产生**恰好一次**提交；服务端会话过期也不受影响。
5. **拒绝即人工放弃终态**：拒绝后任务置为 `abandoned`（非失败），报表中单列 `human-abandoned`，`resume` 与后续重跑都不会把它当失败重试，且全程无提交。
6. **批量批准**：`approve --token A --token B ...` 可逐条/批量批准，每个令牌仍只生效一次。
任务状态新增 `awaiting_confirmation`、`abandoned`；运行状态新增 `awaiting_confirmation`。审计落在 `confirmation_events` 表（`paused/approved/rejected/invalid_token/spent_token`，含操作者、时间与备注）。旧台账文件首次打开时自动迁移（新增列与新表，历史数据保留）。
## 幂等、dry-run 与续跑
- 幂等键为 `biz_key`：台账中已有成功记录时，新任务直接记 `skipped`，不发起提交。
- 回执号必须匹配 `RCP-YYYYMMDD-NNNN` 且全局唯一（SQLite 部分唯一索引 + 运行时检查）。
- `--dry-run` 真实执行并留存步骤证据，但遇到 `final: true` 的点击前停止，状态为 `dry_run`，不产生回执。
- `resume <run_id>` 从台账读取原任务/脚本路径，跳过 `success|skipped`，只执行余下任务；已成功的 `biz_key` 不会重复提交。
## 证据与台账
证据位于 `evidence/<run_id>/<task_id>/`：每步 `step-NN.html`（密码、token、secret 字段及已知敏感值替换为 `***`）、`actions.jsonl`（动作、定位器、结果、耗时、错误，不含填写值）、成功提交后的 `receipt.html`。证据快照会强制保存，便于审计后复核；保留策略只清理超期运行目录，不动台账。
SQLite 五张表：`runs`（来源、状态、计数、时间，含 `abandoned`）、`tasks`（任务、业务键、回执、错误、证据目录）、`steps`（步骤、动作、状态、尝试次数）、`confirmations`（确认门、令牌哈希、决策，仅待确认状态每任务唯一）与 `confirmation_events`（暂停/批准/拒绝/无效或重用令牌的全量审计）。汇总写到 `runs/<run_id>/summary.json` 与 `summary.csv`，包含成功/失败/跳过/人工放弃/待确认数、失败原因分布、平均耗时与回执列表。
## 故障注入与 mock 站点
`python -m fillbot mock-serve --chaos --chaos-seed 7 --chaos-rate 0.2 --delay-ms 50 --flaky-submit` 可启用确定性故障；也可用 `FILLBOT_CHAOS*` 环境变量。`--flaky-submit` 只让指定企业（默认 C003）首次提交返回 500，用于验证重试与证据。mock 站点含登录、三步填报、确认、提交、回执路由以及 `/healthz`、`/__mock/receipts` 辅助端点。
## 设计取舍
- **requests 而非浏览器**：体积小、离线友好、可精确控制会话与 multipart；代价是不执行 JavaScript，目标站点需为服务端渲染表单。
- **串行执行**：避免并发提交造成重复写入，换取简单可靠的幂等与限速；吞吐通过任务批量和速率配置调节。
- **SQLite 作为唯一事实源**：便于恢复和审计；证据文件仅作旁证。唯一的回执索引是最后一道防线。
- **严格契约**：未知配置、动作、参数、变量一律报错，避免“静默成功”造成假报送。
- **mock 先行**：默认只允许回环目标，所有示例和测试在无外网环境可复现。
## 测试与离线保证
测试使用 pytest + pytest-cov；`tests/conftest.py` 的 autouse fixture 替换 `socket.socket.connect`，仅允许 `127.0.0.1`，任何外连立即失败。覆盖 intake、DSL、浏览器适配器、mock 站点、台账/报表、重试/状态机、证据脱敏、CLI 与端到端批次；门槛 85%。
## 路线图
- 增加 JSON Schema 导出与编辑器补全，降低脚本编写门槛。
- 支持可插拔的授权目标适配器和更细粒度的字段级重试。
- 增加运行指标（Prometheus 文本格式）与证据加密归档。
- 提供可选的并发执行器（按 biz_key 分区）；人工接管队列已支持提交前确认门，后续可扩展到任意步骤的人工介入。
- 持续补充离线契约测试；不计划加入验证码绕过、隐身或未授权采集能力。
