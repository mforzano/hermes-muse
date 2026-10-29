# Hermes Muse — 开发约定（v2）

本文件是所有开发 agent 的统一工程语言。开发前先读完；**只读本文件也必须能不犯架构错**。
权威顺序：本文件 > 代码里的单一事实表（`kernel/store/machines.py` 的 `MACHINES`、`kernel/packets.py` 的 `SCHEMAS`/`JOBS`、`surface.py` 的 `MAIN`/`JOB`、`host/probe.py` 的 `PROBES`）> `docs/`。
v2 设计依据是“Muse 全局重新设计方案（修订版 v2）”第 2、3、7 章；该文档不在仓库内，WP7 会把它落成 `docs/DESIGN.md`。在那之前，`docs/` 下的文件描述的都是 0.3.16，不是规范（见第 7 节）。

---

## 1. 项目是什么

Hermes Muse 是一个独立的 Hermes 插件（standalone plugin，MIT）。它把 Hermes 已有的会话、cron、投递、委派和原生 memory 组装成一个长期伴随助手，装进当前 profile：
- 记住用户接受的工作（Goal）和短期兴趣（Interest/Idea）；
- 按证据主动提醒（Notice），并做到不重复、守静默时段和每日预算；
- 夜间对目标做复盘、做对话复盘、写反思；
- 生成静默的个性化 Feed。

使用对象是任何用 Hermes 的个人用户，它不依赖特定的模型、memory provider、笔记应用或消息平台。v2 要解决的是 0.3.x 的结构性问题，做法是“确定性内核 + 薄判断层”：
- 记账交给代码：水位、去重、状态机与 TTL、投递对账，都不再交给模型；
- 宿主耦合收进唯一适配层，并用语义 probe 自检；
- 健康检查独立运行，失败不再静默；
- 契约只维护一份，由代码生成。

**非目标**（不要做，也不要“顺手”加）：
- 修改 Hermes 核心，或依赖 Hermes 的私有行为却不经过 `hermes_muse/host/`；
- 替代长期 memory 服务。原生 `memories/USER.md`、`MEMORY.md` 和 SOUL 归用户和 Hermes 所有；
- 自带数据源连接器。外部来源（邮件、日历等）只由模型用用户已授权的工具读，Muse 只接收 `observe` 上报的条目；
- 写外部来源（回复邮件、标记已读、改事件）；
- 通用操作审计、模型自评完成度、模型自拟 request_id 或幂等账本，这些在 v2 中已被删除；
- 跨 profile 互相读写或监控；
- 遥测、自更新、强制 provider、功能开关式的“选装”；
- 浏览器暂停/恢复、手机/设备服务；
- 即时推送对话复盘结果（只进入 nightly 摘要和简报）。

## 2. 业务模型

### 2.1 实体与状态机（单一事实表：`kernel/store/machines.py`）

所有运营状态都是 Store v2 的 `entity`。表中“非终态”一栏的每个状态都必须有 `deadline`，到期后 `Store.tick()` 执行唯一的退出动作；exit 为“—”的，由所属模块决定退出。

| kind | 状态（→ 表示允许的迁移） | natural_key（代码派生） | 非终态 TTL / 退出 | 谁能改 |
|---|---|---|---|---|
| `signal` | `new → disposed / parked`；`parked → disposed` | `<session_id>:<message_id>`（`signal_key`） | `new` 72h 或 attempts≥3 → `parked` | Intake 捕获；intake 判断层处置；forget |
| `goal` | `active ↔ waiting → done / cancelled`；`done/cancelled → active` | goal slug | `active`/`waiting`：deadline = `next_check_at`，exit —（由 Goals 决定） | **事实源是 GOAL.md**。状态只能由用户（主会话）改；cron 只能追加 progress/assessment；代码执行淡出取消 |
| `notice` | `drafted → scheduled → sending → sent / failed / unknown`；`unknown → sent / failed / closed_unknown`；终态 `withdrawn / expired / superseded` | `src:<上游ID>`（强键）或 `weak:<标题+日>`（弱键）；淡出询问为 `goal:<id>:fade:<episode>:<attempt>` | drafted/scheduled：到 expires → `expired`；`sending` 2h → `unknown`；`unknown` 48h → `closed_unknown` | Notifier；patrol 给出 verdict；用户 feedback=stop |
| `watch` | `active → stopped / expired` | `goal:<id>:watch:<n>` | 到 expires，或 30 天（goal 仍开放时自动续期） | 用户 watch_create/stop；goal 关闭时代码停止并暂停 job |
| `interest` / `idea` | `active → expired / stopped`，可回到 `active` | 标题摘要（`topic_key`）；复盘产生的 idea 用 `review:<ref>` | 14 天或 `expires` | 用户；复盘 `latent_need` 产生 idea |
| `feed` | `published → deleted` | 来源 key 集合的摘要 | 无 | feed 判断层发布；用户 feed_update |
| `source` | `ok / limited / failing / unavailable` 任意互转 | `source_id`（`<kind>:<account>`） | 无 | 仅 `observe`：同一 limitation_code 连续 5 次 → `limited`；error → `failing`，连续 3 次标记 health |
| `finding` | `open → resolved / dismissed` | `review:<session>:<message>` | `open` 14 天 → `dismissed` | 复盘判断层；用户 feedback |
| `receipt`（MemoryReceipt） | `pending_notice → noticed`；`noticed → pending_notice`（staged 再提示一次） | `tool_call_id` | 24h → `noticed` | 主会话 hook（WP6）；只记 failed/staged |
| `packet` | `issued → submitted / expired` | `<job>:<period 序号>` | 租约 = job 周期 → `expired` | `build_packet` 签发；`submit` 提交 |
| `run` | `running → ok / degraded / failed / abandoned` | 宿主 execution ID | 2h，exit —（`runs.check` 依据宿主终态决定，宿主无记录 → `abandoned`） | 仅代码；结果来自宿主执行状态和 packet 处置情况，**不来自模型自评** |
| `tombstone` | `forgotten`（终态） | 被遗忘对象的 natural_key | 无 | forget；之后的旧证据不得重建该对象 |

去重：
- `Store.create()` 遇到同 id，或命中该 machine 的 `dedupe` 规则时，返回已有记录（`existing=True`），不报错。
- Notice 强键在 7 天内只要已有 `drafted/scheduled/sending/sent` 记录，就返回该记录；弱键只和未发送候选去重。
- 只有 `supersede` 并附带新的 observed 证据，才会为同一 key 生成新 Notice（generation+1）。

### 2.2 授权模型

| 调用方（`HostAdapter.caller()`） | 可以做 |
|---|---|
| 私聊主会话（用户） | `muse_manage` 全部 14 个动作；Goal 状态和设置只能由这里修改 |
| Muse job run（cron） | `muse_job` 的 read/observe/submit；`muse_manage` 只能用 `read`、`calendar_view`，以及 `content_write` 写 note/dream/alignment；`submit.goals.create` 必须引用 packet 里的用户 signal，每天最多 5 个 |
| 委派子代理（delegated） | 只读 |
| 群聊、未知来源 | 拒绝（fail closed） |
| 内核代码 | TTL 退出、`next_check` 推进、淡出询问与取消、watch 停止与续期、投递与对账 |
| Health | 只读，不修复 |

**只有私聊用户信号构成授权**。“用户信号”指 `sessions.source ∈ USER_SOURCES`、非群聊、且不是后台回传前缀的消息。引用的邮件、网页和工具输出只是证据，不是指令。

### 2.3 端到端流程

job 与调度（`surface.JOB_SCHEDULES`）：
- intake 每 1h，patrol 每 30m，nightly 02:30，feed 08:00，weekly 周一 09:00；
- health 每 30m 运行，形式是 no_agent cron；
- 每个 watch 是一个独立 job。

每个 job 的 launcher 运行 `job_entry.py` → `surface.job_output()`，依次执行：`Kernel.tick()` → `build_packet(job)`，把 packet 打印为 Script Output。输出为空时 Hermes 不唤醒模型。

1. **信号 → Intake**：
   - 用户在私聊发消息 → `host.sessions.user_turns()`（来源白名单、脱敏、8k 以上只取头尾摘录）→ `intake.capture()` 生成 `signal(new)`，并推进水位 `meta.intake_since`；
   - intake packet 最多 15 条、摘录合计 ≤24k → 模型 `submit {ref, disposition: saved|ignored|context, facts, goal_hint}` → `intake.dispose()` 置为 `disposed`；
   - `goal_hint` 指向某个开放目标时，触碰该目标（next_check 推进到 +1 天）；为 `new` 时，进入 nightly 的建目标提案；
   - 一条 signal 若在 3 次 packet 或 72h 内仍未处置，转为 `parked`，计入 Health，**不阻塞水位**。
2. **Patrol → Notice → 投递**：
   - patrol 每轮都唤醒，因为外部来源只有模型能读。packet 带 `context.sources_due`（只列已过最小间隔的来源）和到期 Notice（证据超过 1 天，或可发送已超 6h）；
   - 模型 `observe(source_id, items[{upstream_id,title,ts,url}])`，得到 `o1, o2…` 这些 ref → `submit {candidate_ref, verdict: send|skip|withdraw|supersede, text, priority}`；
   - `notifier.decide()` → `draft()`：用 `notice_key` 去重 → `schedule()`：`gate()` 依据静默时段、每日预算和 blocked_topics 计算 `next_eligible`；
   - `tick` 中的 `dispatch()`：先在同一事务里检查预算并认领为 `sending`，再 `host.deliver.send(target, text, delivery_key)`（一个 one-shot no_agent cron）；
   - `reconcile()` 读投递回执，把 Notice 推进到 `sent/failed/unknown`。
3. **Nightly**：
   - 到期目标给出 `assessment`（`on_track|stalled|blocked|done_suggested`）和 progress_note；`done_suggested` 只是提示，不关闭目标；
   - 复盘候选由代码按规则挑选（每天最多 10 条），模型给出 `agent_gap|latent_need|fine` 和一句理由：`agent_gap` 生成 Finding，`latent_need` 生成 Idea；
   - signal 提案由模型决定是否 `create` 目标；
   - 当天有实质变化时，用 `content_write` 写 dream 和 alignment。
4. **Feed**：最近 3 天没有文章的 active interest，每次最多 3 个 → 模型写文章并附 `source_refs` → 生成 `feed` 实体，正文写入 `feed/<key>.md`。静默发布。
5. **Weekly**：packet 只有 health 摘要和 7 天计数 → 模型写一条给用户的周报，投到用户的路由。只做建议，不改任何东西。
6. **淡出**（`goals.fade`，在 tick 中运行）：
   - 目标超过 `goal_dormant_days` 无活动时，发一次询问，键为 `goal:<id>:fade:<episode>:<attempt>`；
   - 询问被撤回、过期或失败时，attempt+1 重问，最多 3 次；
   - 淡出时间（休眠开始后 `goal_fade_days`）已到，且询问送达已满 1 天时，代码执行取消；
   - 模型不能撤回或 supersede 淡出询问。
7. **Health**：`health_entry.py` → `health.run()` 写 `muse/health.json`。告警出口有 4 个，**都不经过 Notifier**：主会话注入、Dashboard `/health`、cron stdout（只在告警集合变化时输出）、可选的 `health_deliver` 备用路由。
8. **主会话**（WP6 hooks）：只在有变化时注入一行摘要；失败或待批准的记忆写入只提示一次；用户静默 5 分钟后触发 intake，每天最多 3 次（`intake.quiet_wake`）；后台 tick。

## 3. 系统架构

### 3.1 分层与依赖方向

```
Hermes 宿主（hooks · cron · 会话库 state.db · 投递 · delegate · 原生 memory · 模型）
        ▲  唯一出入口
hermes_muse/host/        HostAdapter：sessions · cron · deliver · owners · config · probe
        ▲
hermes_muse/kernel/store/   Store v2（entity/event/meta）+ migrate + rollback bridge（纯 stdlib）
        ▲
hermes_muse/kernel/      Intake · Notifier · Goals · Review · sources · packets · runs · Kernel.tick
        ▲                                    ▲
hermes_muse/surface.py   muse_manage / muse_job 动作表、校验、分发、job_output
hermes_muse/health/      独立只读：probe + 不变量 → health.json（依赖 host 与 kernel.store，只读）
        ▲
入口：__init__.register → runtime/hooks（WP6）；job_entry.py；health_entry.py；dashboard/plugin_api.py（只读）
```

依赖规则（违反即为架构错误）：
- **只有 `hermes_muse/host/` 可以 import Hermes 模块**（`hermes_*`、`cron.*`、`gateway.*`、`tools.*`、`agent.*`、`hermes_cli.*`），也只有它能读 Hermes 的 `state.db`、依赖 Hermes 的路径、命名、session 前缀或 outcome 值。
- `kernel/store/` 只依赖 stdlib，不依赖 kernel 业务模块和 host。
- `kernel/` 通过 `Kernel.adapter` 调宿主；它可以引用 `host.sessions` 的纯函数和常量（`redact`、`excerpt`、`USER_SOURCES`），但不 import `surface`、`health` 或任何入口模块。
- `health/` 只读 Store（`Store(..., readonly=True)`），**不调用 kernel 业务、不走 Notifier、不修复数据**，这样内核出 bug 时仍能被发现。
- `surface.py` 是模型面的唯一入口：工具 schema、校验、授权和分发都在这里，由 `MAIN`/`JOB` 一张表生成。
- 入口文件（`job_entry.py`、`health_entry.py`、hooks、dashboard）只做接线，不写业务逻辑。
- `content_io.Files`（按 O_NOFOLLOW 逐级打开、带 revision CAS）和 `locks.file_lock`（`mutations.lock`）是所有内容文件写入的唯一通道。

### 3.2 宿主接入点（HostAdapter 对外接口，`host/__init__.py`）

- `sessions`：
  - `user_turns(since, roles, limit)`：按来源白名单、私聊、脱敏、摘录返回会话轮次；
  - `read(ref, offset, length)`：单次 ≤12k，已脱敏；
  - `job_of`、`cron_sessions`、`tool_results`、`sources`、`bot_chats`、`schema_gaps`。
- `cron`：`ensure(key, …)` 创建或刷新自有 job `muse-<key>`（launcher 为 `scripts/hermes-muse-<key>.py`）；`pause`、`wake`、`outcome`、`executions`、`jobs`。
- `deliver`：`send(target, text, key)`，同一 key 只投递一次；`receipt(key)` 只读，返回 `sending|sent|failed(reason)|unknown`。
- `owners.status(handle)`：查原生委派的状态；`config.get()`：优先读 `plugins.entries.hermes-muse.settings/config`，再回退旧的 `plugins.hermes-muse`；`config.enabled()`。
- `cwd()`、`now()`、`caller()`、`probe()`，以及 `faults`（最近 50 条宿主故障）。
- 7 个 probe：`symbols`、`cron_sessions`、`delivery_semantics`、`delivery_target`、`signal_purity`、`config_location`、`home`。每个返回 `{name, status: ok|degraded|missing, detail, fix}`，**用能力和语义判断，不用版本区间**。

### 3.3 数据与状态存放（相对于当前 profile home）

| 路径 | 内容 | 所有者 / 规则 |
|---|---|---|
| `muse/state-v2.db` | Store v2：`entity`、`event`（保留 30 天）、`meta`（`intake_since`、`migration`、每日计数） | kernel 独占写；Health 和 Dashboard 只读；禁止手改 |
| `muse/state.db` | 0.3.x 旧库；迁移后置为只读（0400） | 只有 `rollback-bridge` 可以写回 |
| `muse/health.json` | 健康报告（原子替换写入） | 只有 Health 写 |
| `muse/install.json` | 版本、安装身份、自有 job ID 和 launcher | 安装流程 |
| `muse/workspace/goals/<slug>/GOAL.md`（+`files/`） | Goal 事实源：首行 `<!-- hermes-muse {json} -->` 元数据 + 正文 | 只能经 `goals.write()`（file CAS + `mutations.lock`） |
| `muse/memory/`、`muse/dreams/`、`muse/memory/people|groups/`、`muse/workspace/your_files/feed/*.md` | 内容文件，格式与 0.3.x 相同 | 只能经 `Files.replace(rel, raw, expected_revision)` |
| `muse/AGENTS.md`、`TOOLS.md`、`PROACTIVE_PREFERENCES.md` | 工作经验、来源可用性、偏好（首行元数据 + 正文） | 用户或 content_write/preferences；安装时不覆盖 |
| `muse/mutations.lock`、`muse/host.lock` | 文件写锁；cron 创建锁 | — |
| `scripts/hermes-muse-*.py` | 自有 launcher，带生成头 `# Generated by hermes-muse` | `host.cron.write_script` 只覆盖自己生成的文件 |
| Hermes 的 `state.db`、`cron/jobs.json`、`cron/executions.db`、`memories/`、`SOUL.md` | 宿主所有 | Muse 只读，或只经原生 API 访问；**永不手改** |

## 4. 统一架构词汇表

代码、文档、提示词和测试**必须**使用左列术语，不得同义异名。旧术语只允许出现在迁移代码和第 7 节。

| 术语 | 定义 | 代码位置 |
|---|---|---|
| profile home | 当前 Hermes profile 的绝对目录；每次调用都显式传入，绝不依赖进程默认值 | `host.resolve_home`、`Kernel.home` |
| HostAdapter | 唯一的宿主适配层 | `hermes_muse/host/` |
| probe | 对宿主能力和语义的自检 | `host/probe.py` `PROBES` |
| host fault | 一次被容错吞掉的宿主调用失败 | `HostAdapter.fault`、`@tolerant` |
| Kernel | 确定性内核门面 | `kernel/core.py` `Kernel` |
| Store v2 | 实体、事件、meta 存储 | `kernel/store/store.py` `Store` |
| entity / kind | 一条状态记录 / 它的类型（第 2.1 节的 13 种） | `MACHINES` |
| natural_key | 由代码派生的业务身份键 | `kernel/store/keys.py` |
| state machine | 某个 kind 的状态、边、TTL 和去重规则 | `machines.Machine` |
| deadline / TTL / exit | 非终态的截止时间 / 时长 / 到期后的唯一去向 | `Machine.ttl` |
| CAS / version | 带 `state+version` 条件的迁移 | `Store.transition`、`common.patch` |
| event | 一次状态迁移的日志 | `event` 表 |
| tick | 幂等、按依赖顺序执行所有到期退出 | `Kernel.tick`、`Store.tick` |
| Signal | 一条私聊用户消息 | kind `signal`、`kernel/intake.py` |
| user surface / machine source | 构成授权的会话来源 / 不构成授权的会话来源 | `USER_SOURCES` / `MACHINE_SOURCES` |
| disposition | Signal 的处置：`saved|ignored|context` | `intake.DISPOSITIONS` |
| excerpt / pointer | 脱敏后的头尾摘录 / 原文位置 | `host.sessions.excerpt`、`payload.pointer` |
| watermark | Intake 已读到的时间点 | `meta.intake_since` |
| job / job run | Muse 自有的定时任务 / 它的一次执行 | `packets.JOBS`、`surface.JOB_SCHEDULES` |
| Packet | 某个 job 在某个周期内唯一的有界工作包 | kind `packet`、`kernel/packets.py` |
| item / ref | packet 中的一条工作 / 它的引用（只接受本 packet 内的 ref） | `packet.payload.items` |
| budget / lease | packet 的条数、字符、秒数上限 / 租约（等于 job 周期） | `packets.build` |
| result schema | 每种 item 的 submit 结果形状（共 5 种） | `packets.SCHEMAS` |
| observe / observation | patrol 上报一次来源检查 / 由此得到的条目 `o<n>` | `packets.observe` |
| upstream_id | 来源给出的原始 ID（message-id、事件 ID+revision、URL） | `keys.upstream` |
| source_key（强/弱） | Notice 的 natural_key，`src:`（强键）或 `weak:`（弱键） | `keys.notice_key` |
| Source / source_id / limitation_code | 外部来源状态 / `<kind>:<account>` / 只能部分读取时的稳定代码 | kind `source`、`kernel/sources.py` |
| Notice | 一条主动通知 | kind `notice`、`kernel/notifier.py` |
| verdict | patrol 判断：`send|skip|withdraw|supersede` | `notifier.VERDICTS` |
| priority | `urgent|time_sensitive|ordinary` | `notifier.PRIORITIES` |
| gate | 静默时段 + 每日预算，计算出 `next_eligible` | `notifier.gate` |
| delivery_key / delivery receipt | 投递幂等键 / 宿主侧的投递结果 | `notifier.delivery_key`、`Deliver.receipt` |
| Goal / mirror | 用户接受的工作（GOAL.md）/ 它在 Store 中的索引镜像 | `kernel/goals.py` |
| next_check | Goal 的下次检查时间，即 goal entity 的 deadline | `goals.CHECK_EVERY` |
| assessment | nightly 对 Goal 的判断 | `goals.ASSESSMENTS` |
| dormancy / fade / episode / attempt | 无活动 / 淡出取消 / 一次休眠期 / 询问的第几次 | `goals.fade` |
| Watch | 为开放 Goal 定时检查的自有 job | kind `watch` |
| Interest / Idea | 短期兴趣 / 稍后再说的想法（14 天） | kind `interest` / `idea` |
| FeedItem | 一篇 Feed 文章（Store 记录 + md 正文） | kind `feed` |
| review candidate / rule | 由代码挑选的复盘轮次 / 挑选规则 | `kernel/review.py` |
| Finding | 对话复盘中确认的 agent 失误 | kind `finding` |
| MemoryReceipt | 失败或待批准的记忆写入提示 | kind `receipt` |
| Run / outcome | 一次被唤醒的 job 执行 / 由代码计算的结果 | kind `run`、`kernel/runs.py` |
| tombstone / forget | 遗忘标记 / 用户要求删除 | kind `tombstone`、`muse_manage forget` |
| Health / invariant / health item | 独立健康服务 / 不变量 / `health.json.invariants[]` 中的一项 | `hermes_muse/health/` |
| action / action table | 模型工具动作 / 生成 schema 的唯一表 | `surface.MAIN`、`surface.JOB` |
| content kind / revision | 内容文件类别 / 文件内容的 sha256（新文件用 `absent`） | `surface.CONTENT`、`content_io.revision` |
| owned job / launcher | Muse 创建的 cron job / 它的 `hermes-muse-*.py` 脚本 | `host/cron.py` `owned()` |
| `[SILENT]` | job 无需告知用户时的唯一回答 | `prompts/jobs/*.md` |

**同名冲突的规定**：
- “receipt”单独出现时，只指 **delivery receipt**；记忆写入回执一律写作 **MemoryReceipt**。
- “finding”单独出现时，只指复盘 **Finding**；Health 的输出叫 **health item** 或 invariant。代码里 `health.invariants.finding()` 这个函数名是既有命名，不要扩散。

**禁用的旧术语**（v1）：cursor/游标（改用 watermark）、review_window、context section、operation audit/snapshot/token、run_complete、review_complete、request_id、mutation ledger、wake gate、notification candidate（改用 drafted Notice）、research_start。另外，`event_key`、`issue_key` 只作为迁移来的 payload 或展示标签存在，**不参与去重**。

## 5. 工程规范与约束

### 5.1 命名与目录
- Python 3.11+，只用 stdlib。模块名用小写名词，与第 4 节术语一致（`notifier`、`intake`、`goals`）。新业务模块放进 `kernel/`，新宿主调用放进 `host/`。
- 自有 job 名为 `muse-<key>`，key 满足 `[a-z0-9][a-z0-9_-]{0,63}`；launcher 为 `scripts/hermes-muse-<key>.py`；投递 job 为 `muse-deliver-<delivery_key>`。
- `source_id` 满足 `[a-z0-9][a-z0-9_.:@-]{0,63}`，形如 `mail:work`。
- 时间一律用 epoch 秒（float）存储；面向模型的输入输出用 ISO 8601，无时区时按 profile 时区解释。
- 测试命名：`tests/test_*.py` 是 stdlib 单测；`tests/*_acceptance.py` 是按工作包写的验收脚本，输出 `PASS/FAIL/SKIP`；`tests/native_*.py` 需要真实 Hermes。

### 5.2 错误处理
- **宿主调用逐条容错**：用 `@tolerant(default)` 包装，失败时返回空答案，并记一条 fault 供 Health 统计，不向 kernel 抛异常。唯一例外是 `Cron.by_script`：它故意在失败时抛出，因为空答案会导致重复建 job 或重发。
- probe 永不抛异常；probe 期间出现宿主 fault 时，把 `ok` 降为 `degraded`。缺失的能力只降级对应功能，并写一条 high 级 health item。
- `Kernel.tick()` 每一步独立：某一步失败时记入 `errors`，其余步骤继续执行。
- `submit` 逐条处理：单条结果的错误写进该条返回值，不影响其他条目；未处理的条目留在原队列。
- 模型面错误统一返回 `{ok:false, code, error[, field]}`。code 只能取以下值：`invalid_argument`、`permission_denied`、`not_found`、`revision_conflict`、`host_unavailable`、`outcome_unknown`。错误信息必须点名字段和合法值，不做猜测或强制转换。
- 偏好元数据无效时阻塞投递（`preferences()` 抛错），不去猜默认值。

### 5.3 幂等、并发、TTL
- Store 的每次写入都是 `BEGIN IMMEDIATE` 事务（`busy_timeout` 15s、WAL）；状态迁移用 CAS（`WHERE id AND state AND version`）。读改写走 `common.patch`，不要自己先 `get` 再 `transition`，否则会丢失并发竞争。
- `tick()` 幂等、可重入，多个进程同时进入也是安全的。新增的定时行为必须作为 `tick` 的一个 step，或作为某个状态的 TTL exit，不要另起调度器或线程。
- **任何新的非终态都必须有 deadline 和唯一 exit**（验收 A3；Health 的 `deadlines`/`tick_stalled` 会检查）。
- packet：同一 job 同一周期只有一个 `issued`，并发调用拿到同一个 packet；ref 只在本 packet 内有效；submit 按条目幂等。
- 投递：先在事务内认领 `sending` 并写入 `delivery_key`，再调用宿主；同一 key 的重复 send 返回已有回执。**结果未知的外部写入不得盲目重发**。
- 文件：GOAL.md 和内容文件必须持有 `mutations.lock`，并通过 `expected_revision` CAS 写入；冲突时返回 `revision_conflict`，调用方需重读并合并。
- 共享文件和后台任务之间的竞争属于正确性问题，不是“偶发”问题。

### 5.4 代码与模型的分工
- **必须由代码确定性完成**：调度与门控（packet 为空就静默）、信号捕获与分类、水位、packet 组装与预算、所有 ID、natural_key、去重和幂等、状态迁移与 TTL、静默时段、预算与投递对账、next_check、淡出询问与取消、复盘候选挑选、run 结果、健康检查与告警、脱敏与截断。
- **只允许交给模型**：信号中的事实与决定、归属哪个目标、来源条目是否值得打扰以及措辞、通知取舍（verdict）、复盘结论、目标进展解读与完成建议、反思、人物页、Ideas 和 Feed 写作。
- prompt 只写行为，每个 job 不超过 15 行；不写字段表，也不要求模型记账。字段只存在于 `surface.MAIN/JOB` 和 `packets.SCHEMAS` 中，`skills/companion/references/interface.md` 由代码生成。

### 5.5 日志与可观测
- 用 `logging.getLogger(__name__)`；宿主故障记 `warning`；不要 `print` 调试信息。**launcher 的 stdout 就是投递内容**，非空即会发给用户。
- 每次状态迁移的 `reason` 用短代码（如 `ttl`、`receipt:sent:`、`intake:saved`、`update:user`），供 event 日志和 Health 追溯。
- 可观测性只来自 `event` 表、`health.json` 和 `HostAdapter.faults`；新的失败模式应补一条 invariant，不要加遥测。

### 5.6 隐私与安全红线
- 所有交给模型的文本（摘录、`read`、复盘候选、会话摘要、列表标题）都必须先经过 `host.sessions.redact()`；迁移来的 v1 文本在交出前也要再次脱敏。单次 `read` ≤12k，同一条目在同一 packet 内累计 ≤24k；超过 8k 的文本只给头尾摘录。
- 只读私聊用户来源；群聊和未分类来源一律不读、不授权。
- 不在代码、测试、文档、fixture 或提交中放入个人数据、私有导出、真实会话、机器名、profile 名或凭据；测试只用合成数据。
- 不读取或复制 Hermes 的凭据和配置以外的私有文件；不扩大来源访问范围；子代理只读；profile 之间相互隔离。
- forget 必须删除或清空 payload，并写入 tombstone；向用户说明哪些内容仍然存在。

### 5.7 依赖与外部调用
- 不加第三方依赖。Hermes API 只经 HostAdapter 调用；Hermes 的数据库只用 `mode=ro` 打开。
- 插件注册期间不调用模型、不访问网络。安装不改 SOUL、原生 memory 和 `agent.system_prompt`，不覆盖用户已编辑的模板，也不重复创建 job。
- job 不设 `workdir`：在 Docker 或远程终端上，宿主路径会让命令失败。宿主 API 需要 cwd 时用 `HostAdapter.cwd()`，绝不读或改 `terminal.cwd`。
- 所有已安装的状态都留在当前 profile 内。

### 5.8 明确禁止（及原因）

| 禁止 | 原因 |
|---|---|
| 在 `host/` 之外 import Hermes、读 Hermes 的 state.db、写死 `cron_` 前缀或 outcome 值 | 宿主语义一变就静默失效（0.3.x 的 R5） |
| 让模型提供或记忆 ID、request_id、去重键、next_check、完成度 | 记账交给模型会导致参数错误反复出现、状态卡死（R1） |
| 用模型自拟的 `issue_key`、`event_key` 去重；去重时不比对 7 天内的已发历史 | 0.3.x 重复通知的主因（R2） |
| 新增没有 deadline 或 exit 的非终态 | 状态机卡死、通知积压（R3） |
| 用 `partial` 一类含义过载的完成态；让模型自评 run 结果 | 真故障被淹没（R4） |
| Health 走 Notifier、写 Store 或修复数据 | 告警和故障共用通道，内核出 bug 时无人发现 |
| 要求全文读完才能处置；把大文本或未脱敏文本交给模型 | 隐私泄露、预算超限（R9） |
| 在 prompt、skill 或文档里手写字段表；手改 `interface.md` | 契约三处维护导致不一致（R7） |
| 打补丁修改 Hermes 核心；手改 `cron/jobs.json` 或 Muse 的 SQLite | 违背独立插件定位，且会破坏宿主账本 |
| 新增 LLM 唤醒门、通用操作审计、回执 review/defer 等已删除机制 | v2 已删除，承接关系见设计第 3 章 |
| 从 detached checkout 发布；宿主升级后不跑 native 契约测试 | 交付无门禁（R8） |

## 6. 如何做改动

### 6.1 改动落点（按顺序）
- **新增或修改实体、状态**：
  1. `kernel/store/machines.py`：状态、边、TTL、exit、dedupe；
  2. `tests/test_store_v2.py` 的表驱动用例；
  3. 涉及 v1 数据时，同步修改 `migrate.py` 的映射和 reason code；如果回滚后旧版会因此重复处理，还要改 `bridge.py`；
  4. 需要告警的，在 `health/invariants.py` 加检查；
  5. 第 2.1 节的表。
- **新增 `muse_manage` 动作**：14 个动作是设计契约，先确认设计（见下文“不能碰”）。确认后依次改：
  1. `surface.MAIN` 加一行；
  2. 实现 `Surface.muse_manage_<action>`；
  3. 必要时修改 `authorize`；
  4. 运行 `python -m hermes_muse.surface` 重新生成 interface.md；
  5. 更新 `surface_acceptance` S1 的计数；
  6. 如有判断原则，写进 `skills/companion/references/*.md`（只写原则，不写字段）。
- **新增 job 或 packet 类型**：
  1. `packets.JOBS`、`BUILDERS`、`SCHEMAS`、`KIND_SCHEMA`、`TYPE_SCHEMAS`、`HANDLERS`；
  2. `surface.JOB_SCHEDULES`；
  3. `prompts/jobs/<job>.md`（≤15 行，结尾答 `[SILENT]`）；
  4. Health 的 packet 和 run 检查；
  5. `kernel_acceptance` 和 `surface_acceptance`。
- **新增宿主调用**：
  1. 只在 `host/` 中添加，用 `@tolerant`；
  2. 新读的列加进 `sessions.COLUMNS`（`symbols` probe 会通过 AST 自动覆盖新导入）；
  3. 新语义值加进相应集合，如 `KNOWN_OUTCOMES`、`USER_SOURCES`、`MACHINE_SOURCES`；
  4. 跑 `tests/native_host_contract.py`。
- **新增来源类型**：`sources.INTERVALS`，以及 `skills/companion/references/source-<kind>.md`。
- **新增 health invariant**：在 `health/invariants.py` 写一个函数，加入 `STORE_CHECKS`；每条 health item 都要给出 `fix`。

**不能碰**：
- Hermes 核心；用户的 SOUL、`memories/` 和用户编辑过的模板；
- `muse/state.db`（迁移后只读，只有 bridge 可写）；
- 生成的 `interface.md`；
- 设计契约：14+3 个动作、5 种 submit schema、第 2.1 节的状态机与 TTL、授权模型。修改契约前先提出，由维护者拍板，并同步本文件。

### 6.2 评审与验收清单
运行前设置隔离目录：`export TMPDIR=$(mktemp -d)`；Hermes 源码路径记作 `<hermes-agent>`。

| 检查 | 命令 |
|---|---|
| stdlib 单测（CI 同款） | `python -m unittest discover -s tests -v`；用隔离 home 运行：`python tests/run_isolated.py` |
| 编译 | `python -m compileall -q hermes_muse tests` |
| Store v2、迁移、回滚（A1/A2/A3/A5/A12） | `python tests/store_v2_acceptance.py`（需要 git tag `v0.3.1`、`v0.3.16`） |
| 内核（A3/A5/A10） | `PYTHONPATH=<hermes-agent> python tests/kernel_acceptance.py`（没有 Hermes 时 native 部分 SKIP） |
| 模型面（S1–S10） | `PYTHONPATH=<hermes-agent> python tests/surface_acceptance.py` |
| Health（A3/A4/A7–A10） | `PYTHONPATH=<hermes-agent> python tests/health_acceptance.py` |
| 宿主契约（A4/A13；宿主升级后必跑） | `HERMES_SOURCE=<hermes-agent> python tests/native_host_contract.py` |
| 生成物是最新的 | `python -m hermes_muse.surface && git diff --exit-code skills/companion/references/interface.md` |
| 宿主边界 | 见表下命令，输出应为空（WP6 合入后改用 `scripts/check_boundaries.py`） |
| 官方插件校验 | `HERMES_SOURCE=<hermes-agent> python tests/official_validation.py`，或 `HERMES_HOME=$(mktemp -d) hermes plugins validate . --json` |
| 隐私扫描（发布前） | 在 diff 中搜索个人名、机器名、profile 名、真实 ID 和凭据模式，确认没有 |

```bash
grep -rnE '^\s*(from|import) (hermes_\w*|cron|gateway|tools|agent)\b' hermes_muse --include=*.py | grep -v '^hermes_muse/host/'
```

人工评审要点：
- 有没有新的非终态缺 deadline；
- 读改写是否都走了 CAS；
- 交给模型的文本是否已脱敏、是否有界；
- 是否新增了模型记账；
- 是否有 `host/` 之外的 Hermes 依赖；
- 有没有新增 stdout 输出；
- 测试是否只用临时 home（**绝不碰开发者正在使用的 Hermes**）；
- 本文件、README 和 `docs/DESIGN.md` 是否与实现一致。

发布门禁：单测、native 集成和官方插件校验全部通过，并附 A1–A13 的脚本输出；只从 tag 安装；发布后 7 天内只修 high 级问题。

### 6.3 提交与分支
- 提交信息格式：`type(scope): 摘要`，用英文祈使句。type 取 `feat|fix|test|docs|refactor|chore`；scope 用模块名，如 `host`、`kernel`、`kernel/store`、`health`、`surface`、`prompts`、`skill`、`dashboard`。一个提交只做一件事；验收脚本单独作为 `test(...)` 提交。
- 分支：`redesign/v2` 是集成分支；工作包在 `redesign/v2-wpN` 上开发，验收通过后 merge 回集成分支。v2 一次开发、一次发布（建议版本号 0.4.0），不做对外的阶段性发布。
- 未经维护者授权，不 push、不打 tag、不改远端。

## 7. 已知取舍与历史包袱（不要当成规范）

按设计文档的目标状态写，下面是当前分支与目标的差异：
- **0.3.x 模块仍在树中**（WP6 在另一分支删除）：
  - 涉及的模块：`service.py`、`runtime.py`、`host_v1.py`、`contracts.py`、`guidance.py`、`operations.py`、`wake_gate.py`、`context_pages.py`、`memory_feedback.py`、`fade.py`、`followup.py`、`research.py`、`mutations.py`、`store.py`、`views.py` 等；`cron_entry.py`；`prompts/*.md` 顶层的 8 个 v1 prompt；大部分 `tests/test_*.py` 和 `tests/native_*.py`。
  - 不要扩展或引用它们；`calendar.py` 会保留为来源归一化的纯函数。
- **接线仍是 v1**：
  - `__init__.register` 调的仍是 `runtime.register`（v1 hooks）；
  - `plugin.yaml` 的 version 是 `0.3.16`，`provides_tools` 只有 `muse_manage`，缺 `muse_job`；
  - `surface.register`、`ensure_jobs`、`health.ensure_cron`、`runs.start`、`intake.quiet_wake` 和 MemoryReceipt 的写入方都还没有被调用（WP6 负责）。
- **文档描述的是 0.3.16**：`docs/DESIGN.md`、`docs/INTERFACE.md`、`docs/ACCEPTANCE.md`、`docs/FEATURE-COVERAGE*.md`、`README*.md`、`after-install.md` 都由 WP7 重写。在那之前，v2 的接口以 `skills/companion/references/interface.md`（生成物）为准。
- **CI**（`.github/workflows/test.yml`）只跑单测和 v1 的 `native_smoke.py`，v2 的 `*_acceptance.py` 不在 CI 中。
- **单测现状**：本分支上 `python -m unittest discover -s tests` 有 5 个 FAIL、1 个 ERROR，都是 v1 测试（`test_actionable_errors`、`test_coverage_scope`、`test_prompt`、`test_weather_proactivity`）在断言 v1 的 prompt 和 skill 文本，属于已知失败，WP6 删除 v1 测试后消失。v2 测试和验收脚本全部通过。
- **边界 grep 现状**：命中 11 个文件，其中 10 个是 v1 模块，另 1 个是 `content_io.py`（见下一条）。v1 模块删除后，命中应只剩 0 个。
- **边界违规**：`content_io.check_cancelled` 在 `host/` 之外 import 了 `tools.interrupt`。WP6 的提交说明里有 `host.interrupted()`，待合入后确认是否已修复。
- **Health 与 kernel 的 payload 字段不一致**：
  - `invariants.goals` 读 `goal.payload.fade_at`，但 kernel 从不写这个字段，所以 `goal_fade` 永远不会触发；
  - `invariants.findings` 读 `finding.payload.evidence`，而 `review.record` 写的是 `reason/turn`，所以每条 open Finding 都会被判为“无证据”；
  - `invariants.sources` 读 `payload.failures`，默认值为 3，而 kernel 写的是 `fail_streak`，所以任何 failing 来源都会立刻告警。
  - 以哪一边的字段名为准，**待确认**。
- **刻意偏离设计**：
  - 强键是 `src:<上游ID>`，不带 `source_id` 前缀，目的是与没有记录 source_id 的 0.3.x 历史互相匹配（`keys.notice_key` 的 docstring 有说明）；
  - 复盘规则用正则匹配中英文关键词，属于启发式。
- **已标注的 shortcut**：
  - 迁移来的 watch 若没有 expires，给 30 天（`migrate._watches`）；
  - Feed 记录写入成功后、正文落盘前崩溃，会丢失正文（`Surface.write_feed`）。
- **文档字符串过期**：`surface.reference()` 的 docstring 提到 `tests/surface_check.py`，该文件不存在，实际检查是 `surface_acceptance.py` 的 S3。
