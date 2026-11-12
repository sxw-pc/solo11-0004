# 提交后回读核对（verify）设计讨论稿

- 状态：讨论稿（本轮不写代码）
- 日期：2026-11-12
- 范围：fillbot 填报机器人；全程离线，仅连本地 mock 站点；回读请求遵守既有限速

## 1. 背景与问题

现状：任务提交成功后，机器人只凭回执页判断成功，`tasks.status='success'` + `receipt_no` 落台账，`receipt.html` 存证据。上月事故：目标系统后台处理延迟，回执显示"已受理"但查询页查不到记录，一天后才发现，人工拿回执号逐条核对了一下午。

教训：**回执是目标系统"受理"的承诺，不是"落库"的证明**。需要第三方证据——提交后沿用同一会话去列表/详情查询页回读，把三方对齐：

1. 提交回执：`receipt.html` + `tasks.receipt_no`
2. 台账状态：`tasks.status` / `verify_status`
3. 回读结果：查询页提取的 `found_receipt_no` + 页面字段断言 + 回读快照

对不上的进差异清单（discrepancy list），由人处理；机器人永不自行"补救"。

## 2. 目标与非目标

**目标**

- 提交成功后自动回读，确认记录真实可查；结果与回执号、台账三方对齐。
- 回读脚本复用现有 YAML DSL（选择器、断言、提取、插值、证据管线全部复用）。
- 只读保证是**机制性**的：引擎与传输层硬约束，不依赖脚本作者自觉。
- 目标系统有延迟时：有界轮询 + 挂起（inconclusive），既不判 failed 诱发重提，也不无限挂账。
- 台账平滑演进，存量历史记录零改写兼容。
- 差异清单进入汇总报表与证据目录，机器可读、人工可用。

**非目标（v1）**

- 不支持 POST 查询表单（只允许 GET 导航式回读，见 §14 取舍 3）。
- 不做差异自动修复、永不自动重新提交。
- 不做并发回读（保持串行执行器语义）。
- mismatch 的人工核销不回写台账（线下流程，v1 只列示）。

## 3. 总体设计一览

```text
                 ┌───────────────────────── run ─────────────────────────┐
                 │                                                       │
 tasks ──> Runner._execute_task ──> DSLExecutor(提交脚本, mode=write)     │
                 │        browser: MechanicalBrowser                     │
                 │        成功 → finish_task(success, verify='pending')  │
                 │        browser.set_read_only()  ← 单向闩锁            │
                 │        DSLExecutor(回读脚本, mode=verify)  ← 内联回读 │
                 │        VerifyRunner.poll(): 有界窗口                  │
                 │        → verified / mismatch / inconclusive           │
                 └───────────────────────────────────────────────────────┘
                                       │
 fillbot verify <run_id>  ──> 补扫（sweep）：登录脚本(可写) → 闩锁 → 回读脚本(只读)
                                       │
                 Ledger: tasks + verify_* 列（当前态）/ verify_attempts 表（历史）
                 Evidence: <run>/<task>/verify/（快照、journal、result.json）
                 Report: summary.json/csv + discrepancies.json + report 命令
```

模块边界（新增/改动）：

| 模块 | 改动 | 职责 |
| --- | --- | --- |
| `dsl/schema.py` | 改 | 脚本顶层新增 `mode: write\|verify`（默认 write）；verify 脚本的结构校验（动作白名单 + 必须提取 `found_receipt_no`） |
| `dsl/engine.py` | 改 | verify 模式使用只读 handler 表；其余不变 |
| `browser/mechanical.py` | 改 | 新增只读闩锁 `set_read_only()`，`_request` 拒绝非 GET/HEAD |
| `runner/verify.py` | 新 | `VerifyPolicy`（窗口/退避/次数）、结果分类、轮询循环、内联相位与 sweep 的编排 |
| `runner/scheduler.py` | 改 | 任务成功后调用内联回读；把 verify 脚本路径透传并记入 run options |
| `ledger/db.py` | 改 | `user_version` 迁移、verify 列读写、`verify_attempts` 表、可回读行查询 |
| `ledger/report.py` | 改 | 汇总增加 verify 计数与差异清单；导出 `discrepancies.json` |
| `evidence/store.py` | 改 | `verify/` 子目录：轮询快照、终态快照、`verify-actions.jsonl`、`result.json` |
| `mocksite/` | 改 | 新增 `GET /submissions` 列表/查询页；故障注入新增"延迟可见"（persist-delay） |
| `cli.py` / `config.py` | 改 | `--verify-script`、`verify` 命令、新配置键 |

依赖方向不变：`runner → dsl → browser`，`runner → ledger/evidence`；verify 编排住在 runner 层，DSL 引擎保持通用，不感知"回读"业务语义。

## 4. 关键设计一：只读保证是三层机制，不是自觉

回读脚本也是 YAML，但"只读"由三道互相独立的防线保证，任何一道单独失效都不会放行写请求：

**第一层 · schema 校验（加载期，fail fast）**
`mode: verify` 的脚本只允许动作白名单：`open, wait, assert_text, assert_status, extract, set_var`——全部无副作用。`fill/select/check/upload/click` 在校验期直接报 `SCRIPT` 错，`fillbot validate` 离线即可发现。同时强制约定：必须含 `extract save_as: found_receipt_no`（对齐 §5 的三方比对约定），缺了也在加载期报错。

**第二层 · 引擎 handler 表（运行期）**
`DSLExecutor` 在 `script.mode == 'verify'` 时使用只读 `ACTION_HANDLERS` 子集。即使有人绕过 schema 构造出 `Step(action='click')`，引擎也只有 `UnregisteredActionError` 可走。这与现有"未知动作报错"的严格契约一致。

**第三层 · 传输闩锁（进程内最后防线）**
`MechanicalBrowser` 新增**单向**只读闩锁：`set_read_only()` 之后，`_request` 对任何非 GET/HEAD 方法抛 `ReadOnlyViolation`（`SCRIPT` 类），请求根本不会发出。闩锁只能由 runner 代码设置、不可复位，YAML 够不到它。即使未来新增动作实现写错了，这一层仍然兜底。

为什么闩锁是单向的：内联回读与提交共用同一会话（同一个 `MechanicalBrowser`，仍是登录态）。提交阶段必须能 POST，所以构造时不锁；提交成功、台账落 `success+pending` 之后，runner 上闩，再启动回读执行器。一个会话的生命周期被切成"可写段 → 只读段"，方向不可逆。

**登录与回读分离**：补扫（sweep）是新进程、新会话，需要重新登录，而登录本身是 POST。因此登录是**另一个脚本工件**（`--login-script`，普通 write 模式），sweep 流程：执行登录脚本 → 上闩 → 对该账号的所有待回读任务执行只读回读脚本。只读保证覆盖的是"回读阶段"，认证阶段的写权限是显式、分离、可审计的。

**可选加固（v1 不做，记录在案）**：只读段再加 URL 路径白名单（如仅允许 `/receipt/`、`/submissions`），防止 `open` 撞到会改变状态的 GET 端点（如 `/logout`）。mock 站点全部 GET 都是安全的，故默认不开。

## 5. 关键设计二：回读脚本如何复用 YAML DSL

脚本示例（详情页直连式）：

```yaml
name: quarterly-report-verify
version: 1
mode: verify
steps:
  - {action: open, url: "{{env.FILLBOT_BASE_URL}}/receipt/{{task.receipt_no}}"}
  - {action: assert_status, status: 200}
  - {action: assert_text, text: "报送回执"}
  - {action: extract, selector: "#receipt-no", save_as: found_receipt_no}
  - {action: assert_text, text: "{{task.company_name}}"}
  - {action: assert_text, text: "{{task.period}}"}
```

或列表查询式（mock 新增 `/submissions`，支持 `?company_id=&period=` 过滤）：

```yaml
  - {action: open, url: "{{env.FILLBOT_BASE_URL}}/submissions?company_id={{task.company_id}}&period={{task.period}}"}
  - {action: extract, selector: ".submission-row .receipt-no", save_as: found_receipt_no}
```

复用点与新增约定：

- **上下文注入**：回读相位的 `{{task.*}}` 在原有 payload/task_id/biz_key 之外，由 runner 从台账注入 `receipt_no`（提交阶段提取、经格式与全局唯一校验过的那个）。存量历史记录的回灌也因此可行——台账里就有回执号。
- **`found_receipt_no` 约定**：与提交脚本必须提取 `receipt_no` 的既有约定对称。回读脚本必须把查询页上看到的回执号提取到 `found_receipt_no`。
- **三方比对在脚本外**：runner 比较 `found_receipt_no == tasks.receipt_no`；字段级比对（企业名、季度……）用现成的 `assert_text` + 插值写在脚本里。引擎不需要新的"比对动作"。
- **先定位、后比对**：脚本结构上先 `extract found_receipt_no`（定位记录），再做字段断言。这个顺序是 §6 结果分类的判定支点。
- **变量隔离**：回读用独立的 `DSLExecutor` 实例，其 `variables` 不污染提交阶段；两阶段只通过台账与 `task_context` 通信。
- 脚本与提交脚本的配对关系由 `--verify-script` / 配置键 `verify_script` 指定，并写入 `runs.options_json`，`resume` 与 `verify` 命令从台账读回，保证可复现。

## 6. 关键设计三：轮询、退避与挂起

### 6.1 两级重试，职责分开

- **内层（既有）**：回读脚本每个步骤的瞬时故障（NETWORK/SERVER/429）走现有 `run_with_retry` + `Retry-After` + 指数退避 + 抖动。配置不变。
- **外层（新增）**：整份回读脚本执行一次的"结果"若是"记录还不可见"，进入轮询循环——间隔后整体重跑回读脚本。外层循环由 `VerifyRunner` 持有，不写进 DSL（脚本里不放长 `wait`，节奏控制是编排层的职责）。

### 6.2 单次回读的结果分类（闭合五值）

| 结果 | 判定规则 | 后续 |
| --- | --- | --- |
| `verified` | 脚本完成且 `found_receipt_no == tasks.receipt_no` | 终态 |
| `absent` | 打开详情页 404；或 `found_receipt_no` **尚未绑定**前 ASSERT 失败（记录还不可见） | 轮询 |
| `mismatch` | `found_receipt_no` **已绑定**后与台账不等，或绑定后字段断言失败 | 终态，进差异清单 |
| `transient` | NETWORK/SERVER/429 经内层重试仍失败 | 轮询（消耗窗口） |
| `fatal` | SCRIPT 错误（脚本本身有 bug）、非 404 的 4xx（如会话失效） | 停止轮询 → `inconclusive`，错误入差异清单 |

"绑定前/绑定后"是 absent 与 mismatch 的分界线：**定位失败=可能还没落库（可轮询）；定位成功后的比对失败=确定性差异（再轮询也不会变好，立即终态）**。404 单独归类为 absent——在回读语境下它不是"客户端错误"，而是"记录尚未可见"。

已知误判角落：会话在内联回读中途失效时，mock 会 302 到登录页，表现为"定位前 ASSERT 失败"→ 按 absent 轮询到 inconclusive。方向是安全的（不会误判成功），且 sweep 会重新登录自愈；记录在案，不为此引入站点特判逻辑。

### 6.3 窗口、退避与次数（默认值与演算）

| 配置键 | 默认 | 作用 |
| --- | --- | --- |
| `verify_window_seconds` | 120 | 单次回读相位的墙钟窗口（内联与 sweep 同一剖面） |
| `verify_max_attempts` | 5 | 窗口内最大轮询次数 |
| `verify_poll_base_seconds` | 5 | 外层退避基数（指数，复用 `RetryPolicy.compute_delay` 的抖动机制） |
| `verify_poll_max_seconds` | 60 | 外层退避上限 |
| `verify_deadline_hours` | 72 | sweep 总闸：任务成功超过此时长后不再发起新轮询，差异永久列示 |

演算（默认剖面）：t≈0、+5s、+15s、+35s、+75s 共 5 次尝试，≈75s 内自然结束，窗口 120s 兜底；每任务最多 5 个请求。次数上限约束**请求量**（保护目标站点），窗口约束**墙钟**（保护批次时长），谁先触发谁生效。

为什么内联窗口要短：执行器是串行的，窗口直接拉长批次时长。大多数记录是即时可见的（第一次尝试即 verified）；真正延迟的少数任务，长尾巴交给 sweep（按需触发、不阻塞主流程），而不是让整批任务陪着等。

为什么不无限轮询、也不判 failed：

- **永不判 failed**：本系统里 `failed` 的语义是"可安全重提"（`FAILED→RUNNING` 合法、resume 会重跑）。回执已发的记录重提就是重复写入（mock 会 409，真实系统未必）。所以回读阴性结果住在独立的回读轴上，没有任何边指回 `running`。
- **不无限挂账**：窗口+次数+deadline 三重有界；`inconclusive` 不是沉默状态——它进差异清单、进每次汇总报表、`verify` 命令以非零退出码提示，直到人工处理。挂账必须"挂着且吵"，不能"挂着且隐身"。

### 6.4 限速合规

回读请求复用 Runner 持有的**同一个** `RateLimiter` 令牌桶（`MechanicalBrowser(limiter=self.limiter.wait)` 不变），与提交流量共享同一额度——全局速率语义不变。外层轮询的 sleep 是节奏控制，与令牌桶互补：桶管"瞬时速率不超"，轮询管"总量与间隔像样"。429 仍由内层重试按 `Retry-After` 处理。

## 7. 关键设计四：状态模型——双轴，提交轴不动

### 7.1 设计决定

`tasks.status`（提交轴）**一字不改**：`pending → running → success|failed|skipped|dry_run`，幂等键 `has_successful_biz_key` 语义不变。回读是独立的第二轴 `tasks.verify_status`。两轴正交：回读轴的任何状态都**不会**反过来修改提交轴——这是"永不自动重提"不变量的结构保证。

### 7.2 回读轴状态机（闭合）

状态：`none / pending / verified / mismatch / inconclusive`

| 当前 | 事件 | 下一状态 | 触发者 |
| --- | --- | --- | --- |
| `none` | 提交成功落台账 | `pending` | 内联（`finish_task` 同事务） |
| `none` | `verify --include-legacy` 命中历史 success 行 | `pending` | sweep |
| `pending` | 三方一致 | `verified`（终态） | 内联 / sweep |
| `pending` | 定位后比对失败 | `mismatch`（终态） | 内联 / sweep |
| `pending` | absent/transient 且窗口未满 | `pending` | 轮询循环 |
| `pending` | 窗口/次数耗尽，或 fatal | `inconclusive` | 内联 / sweep |
| `pending` | 进程中断（Ctrl+C 等） | `pending`（不变） | — |
| `inconclusive` | sweep 开启新窗口（未过 deadline） | `pending` | `verify` 命令 |
| `inconclusive` | 已过 deadline | `inconclusive`（拒绝再试，持续列示） | `verify` 命令 |
| `none`（非 success 行） | — | 永远 `none`（不适用） | — |

闭合性论证：每个非终态的出边都列出了唯一触发者；`verified`/`mismatch` 为终态（mismatch 的核销是线下人工流程，机器人侧无出边）；`inconclusive→pending` 只能由显式命令触发，不存在自动循环；崩溃恢复路径明确——任何时刻进程死亡，行只可能停在 `pending`/`inconclusive`，sweep 都可续。`none` 对非 success 行是"不适用"而非"未做"，报表分开渲染（§9）。

顺序保证：先 `finish_task(success, verify='pending')` 落台账，**再**开始内联回读。崩溃窗口内台账永远记得"这笔成功但回读未完"，不会丢。

### 7.3 与既有机制的咬合

- `resume`：语义不变（只重跑非 success/skipped 的任务）。回读尾巴不走 resume，走 `verify` 命令——职责分离。
- `dry_run`：无回执，不回读（`none`）。
- 幂等跳过（`skipped`）：该 biz_key 的成功记录自有其回读轴；本次 skipped 行记 `none`。
- `start_task` 重跑时的 ON CONFLICT 重置清单要同步清空 verify 列（只有 failed 任务会被重跑，其回读轴本就为 none，这里只是保持重置语义完整）。

## 8. 关键设计五：台账演进与存量兼容

### 8.1 Schema 演进（`user_version` 迁移，纯追加）

```sql
-- user_version 0 → 1，打开台账时自动、幂等执行
ALTER TABLE tasks ADD COLUMN verify_status TEXT NOT NULL DEFAULT 'none';
ALTER TABLE tasks ADD COLUMN verify_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN verified_at TEXT;
ALTER TABLE tasks ADD COLUMN verify_receipt_no TEXT;   -- 回读页面上看到的回执号
ALTER TABLE tasks ADD COLUMN verify_error TEXT;

CREATE TABLE IF NOT EXISTS verify_attempts (           -- 每次轮询的审计历史
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL, task_id TEXT NOT NULL,
  attempted_at TEXT NOT NULL,
  source TEXT NOT NULL,              -- inline | sweep
  outcome TEXT NOT NULL,             -- verified | absent | mismatch | transient | fatal
  http_status INTEGER, found_receipt_no TEXT,
  error_class TEXT, error_message TEXT, snapshot_path TEXT,
  FOREIGN KEY(run_id) REFERENCES runs(run_id));
CREATE INDEX IF NOT EXISTS idx_verify_attempts_task ON verify_attempts(run_id, task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_verify_pending ON tasks(verify_status)
  WHERE verify_status IN ('pending','inconclusive');
```

分层与既有 `runs/tasks/steps` 同构：`tasks` 上的 verify 列是**当前态**（快查、报表 join），`verify_attempts` 是**只追加历史**（审计"第几次轮询看到了什么"）。

### 8.2 存量兼容

- 迁移零数据改写：历史行靠列默认值落在 `verify_status='none'`。
- 报表对 `status='success' AND verify_status='none'` 渲染为"未回读（历史）"计数——**不进差异清单**，避免升级当天差异清单被历史记录刷屏。`none` 对非 success 行渲染为"—"。
- 历史回灌：`fillbot verify <run_id> --include-legacy` 把历史 success 行纳入 sweep（回执号取自台账，任务 payload 取自 `runs.task_source` 指向的原始任务文件；文件已删的行报告为"不可回读"并列出，不静默跳过）。
- 旧库新代码：自动迁移。新库旧代码：不支持（单工具演进），但新列全有默认值，既有显式列名的 SQL 不受影响。
- 既有 `summary.json` 的字段只增不改，下游消费者不破。

## 9. 关键设计六：差异清单、报表与证据

### 9.1 差异清单（机器可读 + 人可读）

差异类型：`not_confirmed`（窗口耗尽仍未见记录）/ `receipt_mismatch`（页面回执号≠台账）/ `field_mismatch`（记录存在但字段断言失败）/ `verify_error`（回读脚本或会话 fatal）/ `legacy_unverified`（历史未回读，仅回灌报告）。

每次 run 收尾与每次 sweep 后，刷新 `runs/<run_id>/discrepancies.json`：

```json
{"run_id": "20261112T…", "generated_at": "…", "count": 1,
 "items": [{"task_id": "T-1007", "biz_key": "C003:2025-Q4",
            "receipt_no": "RCP-20261112-0007", "kind": "not_confirmed",
            "verify_status": "inconclusive", "attempts": 5,
            "detail": "回读窗口 120s 内未在查询页找到记录",
            "evidence": "evidence/<run_id>/T-1007/verify/"}]}
```

`evidence` 字段直接指到该任务的回读证据目录——人工核对的起点就是上次事故里"拿回执号逐条查"的自动化替身。

### 9.2 汇总报表

`RunSummary` 增加：`verified / inconclusive / mismatch / legacy_unverified` 计数与 `discrepancies` 列表。`summary.json` 加字段、`summary.csv` 加行、`fillbot report` 文本输出增加"回读核对"与"差异清单"两节（差异为 0 也显式打印 `差异清单: 0 条`——上次的教训就是"绿色报表掩盖了未确认"，报表必须让回读轴无法被忽略）。run 的成败状态语义不变（提交轴），回读轴独立呈现。

### 9.3 证据目录

```text
evidence/<run_id>/<task_id>/
  step-NN.html  actions.jsonl  receipt.html     # 提交段（既有，不动）
  verify/
    attempt-01.html …                            # 每次轮询的查询页快照（走既有脱敏管线）
    verified.html | mismatch.html | last-absent.html   # 终态页
    verify-actions.jsonl                         # 回读 journal，与提交 journal 分文件
    result.json                                  # {status, found_receipt_no, attempts, 起止时间, error}
```

每次轮询都存快照（审计优先，体积由既有 `evidence_keep_runs` 保留策略兜底）。sweep 的证据写回**原 run 目录**下，一个任务的全部证据始终同处、同生命周期。已知时序风险：若保留策略已清理原 run 目录，延迟 sweep 会重建只含 verify 证据的目录——建议需要长核对窗口的批次调大 `evidence_keep_runs`，记录在开放问题（§15）。

## 10. CLI 与配置

- `run --verify-script PATH`（或配置 `verify_script`）：启用内联回读；路径记入 `runs.options_json`。不配置则完全无回读行为——零配置向后兼容，现有用户无感知。
- `fillbot verify <run_id> [--include-legacy] [--login-script PATH]`：补扫 `pending`+`inconclusive`（`--include-legacy` 时含历史 `none` 的 success 行）。按账号分组复用会话：登录脚本（可写）→ 上闩 → 逐任务只读回读。刷新台账、证据与 `discrepancies.json`；仍有未清差异时退出码非零。sweep 不新建 runs 行（runs 表保持"提交批次"语义），日志追加到 `runs/<run_id>/verify-sweep.log`。
- `resume`：从 options_json 读回 verify 脚本路径，行为与 `run` 一致。
- 新配置键（均遵循 CLI > 环境变量 > YAML > 默认）：`verify_script`、`verify_login_script`、`verify_window_seconds`、`verify_max_attempts`、`verify_poll_base_seconds`、`verify_poll_max_seconds`、`verify_deadline_hours`。

## 11. mock 站点与故障注入扩展

- 新增 `GET /submissions`：登录态列表页，支持 `?company_id=&period=` 过滤，行内含回执号链接到既有 `/receipt/<receipt_no>` 详情页。纯 GET。
- 故障注入新增**延迟可见**：`--persist-delay-ms N`（或 `FILLBOT_CHAOS_PERSIST_DELAY_MS`）。提交正常发回执，但记录在列表/详情页延迟 N 毫秒才可见；`Submission` 增加 `visible_at`，查询类路由按可见性过滤。**注意**：提交去重（`find_submission`）必须仍能看到不可见记录——后端"已受理未入索引"正是事故原型，且能防止机器人自己重复提交。
- 全部确定性可复现（seed 注入、`MockStore` 已有可注入 clock），离线测试覆盖"前 N 次 absent 后 verified"的完整轮询路径。

## 12. 失败与重试路径全集

| # | 路径 | 结果 |
| --- | --- | --- |
| 1 | 提交成功，首次回读即命中且一致 | `verified` |
| 2 | 记录延迟可见（404/定位前断言失败） | `absent` → 退避轮询 → 窗口内 `verified` |
| 3 | 窗口/次数耗尽仍不可见 | `inconclusive` → 差异清单 → sweep 新窗口 → `verified`/`mismatch`/仍 `inconclusive` |
| 4 | 查询页 5xx/429/网络错误 | 内层重试（Retry-After 优先）→ 仍败记一次 `transient` → 外层轮询 → 耗尽则 `inconclusive` |
| 5 | 回读脚本 bug（选择器、变量、语法） | `fatal` → 立即 `inconclusive`，差异类型 `verify_error`（轮询修不好 bug，不浪费请求） |
| 6 | 页面回执号≠台账，或定位后字段断言失败 | `mismatch` 终态 → 差异清单 + 现场快照；**永不重提** |
| 7 | 内联回读中途进程被杀 | 行停 `pending` → sweep 续 |
| 8 | 会话中途失效（302 到登录页） | 表现如 absent → `inconclusive` → sweep 重新登录自愈（§6.2 已记录） |
| 9 | 超过 `verify_deadline_hours` | sweep 拒绝再试，差异永久列示，人工收口 |
| 10 | dry_run / skipped / failed 任务 | 回读轴 `none`，不进入任何回读路径 |
| 11 | 历史记录（升级前） | `none`，报表单列"未回读（历史）"；`--include-legacy` 回灌 |
| 12 | 全部回读流量 | 过同一令牌桶；429 按 Retry-After；离线 socket 守卫不变 |

## 13. 测试策略（离线，覆盖率门槛不变）

- schema：`mode:verify` 白名单拒绝写动作；缺 `found_receipt_no` 报错；write 模式行为不变。
- 引擎：绕过 schema 构造的写动作 Step 在 verify 模式被 handler 表拒绝。
- 传输：上闩后 POST 必抛 `ReadOnlyViolation` 且**零网络请求发出**（假 session 计数断言）；闩锁不可复位。
- 轮询：mock persist-delay 下"N 次 absent 后 verified"；窗口耗尽 `inconclusive`；篡改页面回执号构造 `mismatch`；`Retry-After` 被遵守。
- 状态机：仿 `test_ledger_retry_state.py` 对 §7.2 全表做合法/非法迁移断言。
- 迁移：构造无新列的旧库，打开后自动迁移、历史行 `none`、报表渲染正确。
- sweep：pending/inconclusive 被续扫；legacy 仅带旗标才扫；deadline 后拒绝；`--include-legacy` 找不到任务文件时显式报告。
- 限速：统计 limiter 调用，验证回读请求同样过桶。
- e2e：run（带 verify 脚本 + persist-delay 故障）→ 部分 verified 部分 inconclusive → `verify` 命令 → 全部 verified；断言 summary/discrepancies/证据文件内容。
- 离线：`tests/conftest.py` 的 socket 守卫自动覆盖全部新测试。

## 14. 取舍记录（备选方案与否决理由）

1. **双轴状态 vs 扩展 TaskStatus**：扩展枚举（如 `verified` 做成 status）会污染提交语义与幂等判断，且存量 `success` 行需要数据迁移。双轴零迁移、语义正交；代价是报表要 join 两轴——可接受。
2. **复用 YAML DSL vs 专用回读 API**：复用让选择器/断言/提取/插值/证据/限速管线全部零成本继承，脚本作者无新学习成本；代价是必须建三层只读防线——防线本身也是资产（未来其他只读场景复用）。
3. **GET-only vs 允许 POST 查询表单**：v1 只允许 GET 导航，机制简单绝对（闩锁一行判断）。少数只能 POST 查询的目标站点暂不支持；未来可加"仅 GET 表单的安全 click"或专用 `query` 动作，模型不破坏。
4. **内联短窗口 + sweep 补扫 vs 内联长轮询**：串行执行器不能长堵；sweep 把长尾移出主流程且天然支持跨进程、跨天核对。代价是多一条命令——换来主流程时长可预期。
5. **回读阴性永不判 failed**：见 §6.3，这是安全不变量，不是口味问题。
6. **每次轮询都存快照 vs 只存终态**：审计优先；差异排查需要"当时页面长什么样"。体积由保留策略兜底。
7. **当前态列 + 历史表 vs 单表**：与 runs/tasks/steps 层次一致；报表查当前态不必扫历史。
8. **登录与回读分两个脚本 vs 回读脚本内放行登录动作**：只读保证不留口子；代价是多一个脚本工件。
9. **共用令牌桶 vs 独立回读限速**：用户明确要求遵守既有限速；共用桶语义最直白。轮询 sleep 是补充节奏，不是第二套限速。

## 15. 开放问题

- POST 查询表单的支持形态（安全 click / `query` 动作），待真实目标站点需求驱动。
- mismatch 人工核销是否回写台账（v1 线下；若回写，需新增"人工确认"通道与审计字段）。
- 证据保留策略与延迟 sweep 的时序冲突（keep_runs 清理早于 sweep 到达时，证据目录被重建、提交段证据已失）。
- 多账号 sweep 的会话复用粒度（当前按 username 分组；账号多时会话建立开销线性）。

## 16. 分阶段落地路线

- **P1（核心闭环）**：schema/mode + 三层只读 + mock `/submissions` 与 persist-delay + 内联回读 + 台账迁移 + 报表与差异清单 + 证据目录。
- **P2（补扫）**：`verify` 命令（sweep）、`--include-legacy` 回灌、deadline 总闸。
- **P3（加固）**：URL 路径白名单、运行指标、证据归档、人工核销回写。
