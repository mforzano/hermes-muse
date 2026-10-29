# Hermes Muse — 开发约定（v2）

本文件是所有开发 agent 的统一工程语言。开发前先读完；**只读本文件也必须能不犯架构错**。
权威顺序：本文件 > 代码里的单一事实表（`kernel/store/machines.py` 的 `MACHINES`、`kernel/packets.py` 的 `SCHEMAS`/`JOBS`、`surface.py` 的 `MAIN`/`JOB`、`host/probe.py` 的 `PROBES`）> `docs/`。
本文件写的是 v2 的目标状态。代码与目标不一致之处列在第 7 节，**第 7 节里的内容不是规范**。WP7 会产出 v2 的 `docs/DESIGN.md`；在那之前，`docs/` 下的文件描述的都是 0.3.16。

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

所有运营状态都是 Store v2 的 `entity`。状态分为两类（定义见第 4 节）：
- **计时状态**：`Machine.ttl` 的键，必须有 `deadline`；
- **不计时状态**：不在 `ttl` 中，没有 deadline，但可能仍有出边。

计时状态到期后有两种处理：exit 不为空的，由 `Store.tick()` 迁到 exit；exit 为“—”的，由所属模块处理。

| kind | 状态（→ 表示允许的迁移） | natural_key（代码派生） | 计时状态：TTL → exit | 谁能改 |
|---|---|---|---|---|
| `signal` | `new → disposed / parked`；`parked → disposed` | `<session_id>:<message_id>`（`signal_key`） | `new`：72h 或 attempts≥3 → `parked` | Intake 捕获；intake 判断层处置；forget |
| `goal` | `active ↔ waiting → done / cancelled`；`done/cancelled → active` | goal slug | `active`/`waiting`：deadline 即 next_check，exit —（由 nightly 和 `goals.fade` 处理） | **事实源是 GOAL.md**；权限见第 2.2 节 |
| `notice` | `drafted ↔ scheduled → sending → sent / failed / unknown`；`unknown → sent / failed / closed_unknown`；`drafted/scheduled → withdrawn / expired / superseded` | notice key：`src:<上游ID>`、`weak:<标题+日>`，或淡出询问的 `goal:<id>:fade:<episode>:<attempt>` | drafted/scheduled：到 expires（默认 3 天）→ `expired`；`sending` 2h → `unknown`；`unknown` 48h → `closed_unknown` | Notifier；patrol 给出 verdict；用户 feedback=stop |
| `watch` | `active → stopped / expired` | `goal:<id>:watch:<n>` | `active`：到 expires，或 30 天（goal 仍开放时自动续期）→ `expired` | 用户 watch_create/stop；goal 关闭时代码停止并暂停 job |
| `interest` / `idea` | `active → expired / stopped`，可回到 `active` | 标题摘要（title key）；复盘产生的 idea 用 `review:<ref>` | `active`：14 天或 expires → `expired` | 用户；复盘 `latent_need` 产生 idea |
| `feed` | `published → deleted` | 来源 key 集合的摘要 | 无 | feed 判断层发布；用户 feed_update |
| `source` | `ok / limited / failing / unavailable` 任意互转 | `source_id`（`<kind>:<account>`） | 无 | 只有 `observe`：同一 limitation_code 连续 5 次 → `limited`；error → `failing`，连续 3 次标记 health |
| `finding` | `open → resolved / dismissed` | `review:<session>:<message>` | `open`：14 天 → `dismissed` | 复盘判断层；用户 feedback |
| `memory_receipt` | `pending_notice → noticed`；`noticed → pending_notice`（staged 再提示一次） | `tool_call_id` | `pending_notice`：24h → `noticed` | 主会话 hook；只记 failed/staged |
| `packet` | `issued → submitted / expired` | `<job>:<周期序号>` | `issued`：租约（等于 job 周期）→ `expired` | `build_packet` 签发；`submit` 提交 |
| `run` | `running → ok / degraded / failed / abandoned` | 宿主 execution ID | `running`：2h，exit —（由 `runs.check` 依据宿主终态决定） | 只有代码；结果来自宿主执行状态和 packet 处置情况，**不来自模型自评** |
| `tombstone` | `forgotten`（终态） | 被遗忘对象的 natural_key | 无 | forget；之后的旧证据不得重建该对象 |

去重规则：
- `Store.create()` 遇到同一个 id，或命中该 machine 的 `dedupe` 规则时，返回已有记录（`existing=True`），不报错。
- **`dedupe` 的默认值 `(ANY, ANY)` 表示同一 kind 内的 natural_key 永久唯一**，不能靠同一个 natural_key 重建实体。需要“再来一次”的实体，必须把代次放进 natural_key（如淡出询问的 `attempt`），或者显式传入 dedupe 窗口。
- Notice 的强键：7 天内只要已有 `drafted/scheduled/sending/sent` 记录，就返回该记录。弱键只和未发送的记录去重。
- 同一个 key 想生成新 Notice（generation+1），只能用 `supersede`，并附带新的 observed 证据。

### 2.2 授权模型（可检验规则）

1. **“是不是用户”由同一个函数判定**：工具调用方是否算用户、Signal 是否算用户，都由 `host/` 中同一个函数、同一份 `USER_SOURCES` 白名单判定。来源不在白名单里就拒绝，空值和未分类值也一样。群聊（`GROUP_CHATS`）同样拒绝。
2. **私聊主会话**（满足规则 1）可以使用 `muse_manage` 的全部 14 个动作。Goal 的状态和设置只能在这里修改。
3. **Muse job run** 的定义是：cron 会话，并且持有本 run 的 packet ID。它只能做这些：
   - `muse_job` 的 read/observe/submit，ref 只能是本 packet 内的；
   - `muse_manage` 中只允许 `read`、`calendar_view`，以及用 `content_write` 写 note/dream/alignment；
   - `submit.goals.create` 必须引用 packet 里的用户 signal，每天最多 5 个；
   - 对 Goal 只能追加 progress 和 assessment。
4. **委派子代理**只读。
5. **内核代码**可以做：
   - 执行 TTL exit、推进 next_check；
   - 发淡出询问，并执行淡出取消；
   - GOAL.md 被删除时，把镜像置为 `cancelled`；
   - 停止和续期 watch；
   - 投递与对账。
6. **Health** 只读，不修复任何东西。

只有私聊用户信号构成授权。引用的邮件、网页和工具输出只是证据，不是指令。

### 2.3 端到端流程

调度：
- owned job 由 `surface.JOB_SCHEDULES` 定义：intake 每 1h，patrol 每 30m，nightly 02:30，feed 08:00，weekly 周一 09:00；
- health 每 30m 运行一次，是 no_agent cron；
- 每个 watch 是一个独立的 owned job。

每个 owned job 的 launcher 都执行同一条路径：
1. `Kernel.tick()`；
2. `build_packet(job)`；
3. 把 packet 打印为 Script Output。

Script Output 为空时，Hermes 不唤醒模型。

1. **信号 → Intake**：
   - 用户在私聊发出消息后，`host.sessions.user_turns()` 读取它：只收白名单来源，先脱敏，超过 8k 的只取头尾摘录；
   - `intake.capture()` 把它记成 `signal(new)`，并推进水位 `meta.intake_since`；
   - intake packet 最多 15 条，摘录合计不超过 24k。模型对每条 submit 一个 disposition，`intake.dispose()` 把 signal 置为 `disposed`；
   - `goal_hint` 指向某个开放目标时，触碰该目标；为 `new` 时，进入 nightly 的建目标提案；
   - 3 次 packet 或 72h 后仍未处置的 signal 转为 `parked`，计入 Health，**不阻塞水位**。
2. **Patrol → Notice → 投递**：
   - patrol 每轮都唤醒，因为外部来源只有模型能读。packet 包含 `context.sources_due`（已过最小检查间隔的来源）和到期的 Notice（证据超过 1 天，或可发送已超过 6h）；
   - 模型先 `observe`，得到 `o1, o2…` 这些 ref，再 `submit` verdict、text 和 priority；
   - `notifier.decide()` → `draft()` 用 notice key 去重 → `schedule()` 由 `gate()` 计算 `next_eligible`。门控规则：
     - ordinary 和 time_sensitive 受静默时段和各自的每日预算约束；
     - urgent 可以越过静默时段，但受 `urgent_per_day` 上限约束，超出后按 time_sensitive 处理；
     - blocked_topics 中的话题直接撤回。
   - `tick` 中的 `dispatch()` 在同一个事务里检查预算并认领为 `sending`，然后调用 `host.deliver.send(target, text, delivery_key)`，由宿主生成一个 one-shot no_agent cron；
   - `reconcile()` 读取 delivery receipt，把 Notice 推进到 `sent`、`failed` 或 `unknown`。
3. **Nightly**：
   - 对到期目标给出 assessment 和 progress_note；`done_suggested` 只是提示，不关闭目标；
   - 复盘候选由代码按规则挑选，每天最多 10 条。模型的判断中，`agent_gap` 生成 Finding，`latent_need` 生成 Idea；
   - signal 提案由模型决定是否 `create` 目标；
   - 当天有实质变化时，用 `content_write` 写 dream 和 alignment。
4. **Feed**：每次最多取 3 个最近 3 天没有文章的 active interest。模型写文章并附 `source_refs`，代码生成 `feed` 实体，正文写入 `feed/<key>.md`。静默发布。
5. **Weekly**：packet 只有 health 摘要和 7 天的计数。模型写一条周报，投到用户的路由。只提建议，不改任何东西。
6. **淡出**（`goals.fade`，在 tick 中运行）：
   - 目标超过 `goal_dormant_days` 没有活动时，发一次询问，键为 `goal:<id>:fade:<episode>:<attempt>`；
   - 询问被撤回、过期、失败、closed_unknown 或 superseded 时，attempt+1 重问，最多 3 次；
   - 淡出时间已到，并且询问送达已满 1 天时，由代码取消目标；
   - 模型不能撤回或 supersede 淡出询问。
7. **Health**：`health_entry.py` 调 `health.run()`，写出 `muse/health.json`。告警有 4 个出口，**都不经过 Notifier**：
   - 主会话注入；
   - Dashboard 的 `/health`；
   - cron stdout，只在告警集合变化时输出；
   - 可选的 `health_deliver` 备用路由。
8. **主会话 hooks**：
   - 只在有变化时注入一行摘要；
   - 失败或待批准的 memory_receipt 只提示一次；
   - 用户静默 5 分钟后唤醒 intake，每天最多 3 次（`intake.quiet_wake`）；
   - 在后台运行 tick。

## 3. 系统架构

### 3.1 分层与依赖方向

```
Hermes 宿主
   │ 入站：插件 API（register(ctx)、ctx.register_tool/hook、hook 参数、handler kwargs、launcher 注入的 MUSE_HOME/MUSE_JOB）
   ▼
入站入口（只接线）：__init__.py → runtime.py（register）· hooks.py · surface.register · job_entry.py / cron_entry.py → entry.py · health_entry.py · dashboard/plugin_api.py
   ▼
应用层：surface.py（模型工具）· hooks.py（主会话）· install.py（安装/升级/迁移）· views.py（Dashboard 只读）· health/（独立只读）
   ▼
kernel/：intake · notifier · goals · review · sources · packets · runs · Kernel.tick   ← 实体状态写入只在这里
   ▼
kernel/store/：Store v2 + keys + migrate + bridge（只依赖 stdlib）
共享基础：content_io.py（Files、revision、DomainError）· locks.py（file_lock）· calendar.py（来源归一化纯函数）
   ▼
host/：HostAdapter   ← 出站唯一通道
   │ 出站：Hermes 模块 import、Hermes state.db 只读、cron/deliver/delegation API
   ▼
Hermes 宿主
```

依赖规则（违反即为架构错误）：
- **出站只经 `host/`**：
  - 只有 `hermes_muse/host/` 可以 import Hermes 模块（`hermes_*`、`cron.*`、`gateway.*`、`tools.*`、`agent.*`、`hermes_cli.*`）；
  - 只有它可以读 Hermes 的 `state.db`；
  - 只有它可以依赖 Hermes 的路径、命名、session 前缀或 outcome 值。
- **入站只在入口文件**：
  - Hermes 插件 API 的对象和参数语义只能出现在入站入口，包括 ctx、`register_tool`/`register_hook`、hook 和 handler 的参数（例如 `session_id` kwarg）、launcher 注入的全局变量；
  - 入口文件把这些翻译成 Muse 调用，ctx 不往下层传；
  - 入口文件不写业务规则。
- **实体状态写入只在 `kernel/`**：
  - `Store.create/transition/attempt/tick`、`common.patch` 只能在 `kernel/` 内调用（含 `kernel/store` 的 migrate 和 bridge）；
  - surface、hooks、install 只把参数映射成 kernel 函数调用；
  - views、health 和 dashboard 只用 `Store(..., readonly=True)` 读。
  - 所有 natural_key 都在 `kernel/store/keys.py` 中派生。
- `kernel/store/` 只依赖 stdlib。
- `kernel/` 依赖 store、共享基础和 host：
  - 宿主调用经 `Kernel.adapter` 发出；
  - 可以引用 `host.sessions` 的纯函数和常量（`redact`、`excerpt`、`EXCERPT`、`USER_SOURCES`、`MACHINE_SOURCES`）；
  - 不 import 应用层和入口。
- 共享基础只依赖 stdlib 和 `host/`，不依赖 kernel 和应用层。所有内容文件只能通过 `content_io.Files`（O_NOFOLLOW 逐级打开 + revision CAS）和 `locks.file_lock`（`mutations.lock`）写入。
- `health/` 依赖 `host/` 和只读的 `kernel.store`。它**不调用 kernel 业务、不走 Notifier、不修复数据**，这样内核出 bug 时仍能被发现。
- 允许 surface 和 hooks 读取 `health.load`/`health.injection`，这两个函数只读 `health.json`。
- 模型面只有 `surface.py`：工具 schema、校验、授权和分发都由 `MAIN`/`JOB` 这一张表生成。

### 3.2 出站接入点（HostAdapter 接口，`host/__init__.py`）

- `sessions`：
  - `user_turns(since, roles, limit)`：按来源白名单、私聊、脱敏、摘录返回会话轮次；
  - `read(ref, offset, length)`：单次 ≤12k，已脱敏；
  - `job_of`、`cron_sessions`、`tool_results`、`sources`、`bot_chats`、`schema_gaps`。
- `cron`：`ensure(key, …)` 创建或刷新自有 job `muse-<key>`（launcher 为 `scripts/hermes-muse-<key>.py`）；`pause`、`wake`、`outcome`、`executions`、`jobs`。
- `deliver`：`send(target, text, key)`，同一个 key 只投递一次；`receipt(key)` 只读，返回 `sending|sent|failed(reason)|unknown`。
- `owners.status(handle)`：查原生委派的状态。
- `config.get()`：先读 `plugins.entries.hermes-muse.settings/config`，再回退旧的 `plugins.hermes-muse`；`config.enabled()`。
- `cwd()`、`now()`、`caller()`、`probe()`，以及 `faults`（最近 50 条宿主故障）。
- 7 个 probe：`symbols`、`cron_sessions`、`delivery_semantics`、`delivery_target`、`signal_purity`、`config_location`、`home`。每个返回 `{name, status: ok|degraded|missing, detail, fix}`，**按能力和语义判断，不看版本区间**。

### 3.3 数据与状态存放（相对于当前 profile home）

| 路径 | 内容 | 所有者 / 规则 |
|---|---|---|
| `muse/state-v2.db` | Store v2：`entity`、`event`（保留 30 天）、`meta`（`intake_since`、`migration`、每日计数） | 只有 kernel 写；Health、views、Dashboard 只读；禁止手改 |
| `muse/state.db` | 0.3.x 旧库，迁移后置为只读（0400） | 只有 `rollback-bridge` 可以写回 |
| `muse/health.json` | 健康报告，原子替换写入 | 只有 Health 写 |
| `muse/install.json` | 版本、安装身份、自有 job ID 和 launcher | 安装流程 |
| `muse/workspace/goals/<slug>/GOAL.md`（以及 `files/`） | Goal 事实源：首行 `<!-- hermes-muse {json} -->` 元数据，后接正文 | 只能经 `goals.write()`（file CAS + `mutations.lock`） |
| `muse/memory/`、`muse/dreams/`、`muse/memory/people|groups/`、`muse/workspace/your_files/feed/*.md` | 内容文件，格式与 0.3.x 相同 | 只能经 `Files.replace(rel, raw, expected_revision)` |
| `muse/AGENTS.md`、`TOOLS.md`、`PROACTIVE_PREFERENCES.md` | 工作经验、来源可用性、偏好（首行元数据，后接正文） | 用户，或 content_write/preferences；安装时不覆盖 |
| `muse/mutations.lock`、`muse/host.lock` | 文件写锁；cron 创建锁 | — |
| `scripts/hermes-muse-*.py` | 自有 launcher，带生成头 `# Generated by hermes-muse` | `host.cron.write_script` 只覆盖自己生成的文件 |
| Hermes 的 `state.db`、`cron/jobs.json`、`cron/executions.db`、`memories/`、`SOUL.md` | 宿主所有 | Muse 只读，或只经原生 API 访问；**永不手改** |

## 4. 统一架构词汇表

代码、文档、提示词和测试**必须**使用左列术语，不得同义异名。旧术语只允许出现在迁移代码和第 7 节。
枚举的取值（disposition、verdict、priority、assessment 等）以 `SCHEMAS`/`MAIN` 和生成的 `interface.md` 为准，本表不重复列出。

| 术语 | 定义 | 代码位置 |
|---|---|---|
| profile home | 当前 Hermes profile 的绝对目录。每次调用都显式传入，不依赖进程默认值 | `host.resolve_home`、`Kernel.home` |
| HostAdapter / probe / host fault | 出站唯一适配层 / 对宿主能力和语义的自检 / 一次被容错吞掉的宿主调用失败 | `host/`、`PROBES`、`@tolerant` |
| 入站入口 | 接收宿主插件 API 调用的文件 | 第 3.1 节 |
| Kernel / tick | 确定性内核门面 / 幂等、按依赖顺序执行所有到期处理 | `Kernel`、`Kernel.tick`、`Store.tick` |
| Store v2 / entity / kind / event | 存储 / 一条状态记录 / 记录的类型（第 2.1 节的 13 种）/ 一次状态迁移的日志 | `kernel/store/`、`MACHINES` |
| natural_key | 由代码派生的业务身份键。默认在同一 kind 内永久唯一 | `kernel/store/keys.py` |
| 计时状态 / 不计时状态 | 在 `Machine.ttl` 中、必须有 deadline 的状态 / 不在 ttl 中、没有 deadline、可能仍有出边的状态 | `Machine.ttl`。注意 `Machine.terminal()` 判断的是“不计时”，不能据此推断“没有出边” |
| 终态 | 没有出边的状态，例如 `notice.sent`、`tombstone.forgotten` | `Machine.edges` |
| deadline / exit | entity 计时状态的截止时间 / 到期后 `Store.tick` 的去向；exit 为空时由所属模块处理 | `entity.deadline`、`Machine.ttl` |
| CAS / version | 带 `state+version` 条件的迁移 | `Store.transition`、`common.patch` |
| Signal / watermark / excerpt / pointer | 一条私聊用户消息 / Intake 读到的时间点 / 脱敏后的头尾摘录 / 原文位置 | kind `signal`、`meta.intake_since`、`host.sessions.excerpt` |
| user surface / machine source | 构成授权的会话来源 / 不构成授权的会话来源 | `USER_SOURCES` / `MACHINE_SOURCES` |
| owned job / launcher | Muse 创建的所有 `muse-*` cron（5 个 job run、health、watch、deliver）/ 它们的 `hermes-muse-*.py` 脚本 | `host/cron.py` `owned()`、`surface.JOB_SCHEDULES` |
| packet job | 签发 Packet 的 owned job，即 intake、patrol、nightly、feed | `packets.JOBS` |
| Packet / item / ref / budget / lease | 一个 packet job 每个周期唯一的有界工作包 / 其中的一条工作 / 条目的引用（只在本 packet 内有效）/ 上限 / 租约 | kind `packet`、`kernel/packets.py` |
| result schema | 每种 item 的 submit 结果形状（共 5 种） | `packets.SCHEMAS` |
| observe / observation | patrol 上报一次来源检查 / 由此得到的条目 `o<n>` | `packets.observe` |
| upstream_id | 来源给出的原始 ID（message-id、事件 ID+revision、URL） | `keys.upstream` |
| notice key | Notice 的 natural_key，三种形态：`src:`（强键）、`weak:`（弱键）、`goal:<id>:fade:…`（淡出询问） | `keys.notice_key` 派生前两种；`goals.fade` 派生第三种 |
| title key | Interest/Idea 的 natural_key（规范化标题的摘要） | 现位于 `surface.topic_key`，见第 7 节 |
| Source / source_id / limitation_code | 外部来源状态 / `<kind>:<account>` / 只能部分读取时给出的稳定代码 | kind `source`、`kernel/sources.py` |
| Notice / verdict / priority / gate | 一条主动通知 / patrol 的判断 / 优先级 / 静默时段、每日预算和 urgent 上限，算出 `next_eligible` | `kernel/notifier.py` |
| delivery_key / delivery receipt | 投递幂等键 / 宿主侧的投递结果 | `notifier.delivery_key`、`Deliver.receipt` |
| Goal / mirror / next_check | 用户接受的工作（GOAL.md）/ 它在 Store 中的索引镜像 / 下次检查时间，即 goal entity 的 deadline | `kernel/goals.py` |
| Goal 的 `deadline` 字段 | GOAL.md 和镜像 payload 里的**用户截止日**，与 entity.deadline 无关 | `goals.USER_FIELDS` |
| dormancy / fade / episode / attempt | 无活动 / 淡出取消 / 一次休眠期 / 第几次询问 | `goals.fade` |
| Watch / Interest / Idea / FeedItem | 为开放 Goal 定时检查的 owned job / 短期兴趣 / 稍后再说的想法 / 一篇 Feed 文章 | kind `watch`/`interest`/`idea`/`feed` |
| review candidate / Finding | 由代码按规则挑选的复盘轮次 / 复盘中确认的 agent 失误 | `kernel/review.py`、kind `finding` |
| memory_receipt | 失败或待批准的记忆写入提示 | kind `memory_receipt` |
| Run / outcome | 一次被唤醒的 job 执行 / 由代码计算的执行结果 | kind `run`、`kernel/runs.py` |
| tombstone / forget | 遗忘标记 / 用户要求删除 | kind `tombstone` |
| Health / invariant / health item | 独立健康服务 / 不变量 / 一条检查结果（`health.json` 中，以及模型面 `read(kind=status).health.items[]`） | `hermes_muse/health/` |
| action table | 生成工具 schema 的唯一表 | `surface.MAIN`（muse_manage）、`surface.JOB`（muse_job） |
| content kind / revision | 内容文件的类别 / 文件内容的 sha256（新文件用 `absent`） | `surface.CONTENT`、`content_io.revision` |
| `[SILENT]` | job 无需告知用户时唯一的回答 | `prompts/jobs/*.md` |

**同名冲突的规定**：
- “receipt”单独出现时，只指 **delivery receipt**；记忆写入回执一律写作 **memory_receipt**。
- “finding”单独出现时，只指复盘 **Finding**；Health 的输出一律叫 **health item**。
- “deadline”单独出现时，只指 entity.deadline；用户截止日写作“Goal 的 `deadline` 字段”。
- “job”单独出现时，指 owned job；需要特指签发 packet 的那 4 个时写作 packet job。
- “topic”只指 Notice 的话题（`topic`、`blocked_topics`）；Interest/Idea 的键叫 title key。
- “source_key”这个名称不再使用：Notice 的键叫 notice key；`calendar.py` 里的 `source_key` 是日历来源标识，与 Notice 无关。

**禁用的旧术语**（v1）：cursor/游标（改用 watermark）、review_window、context section、operation audit/snapshot/token、run_complete、review_complete、request_id、mutation ledger、wake gate、notification candidate（改用 drafted Notice）、research_start。`event_key`、`issue_key` 只作为迁移来的 payload 或展示标签存在，**不参与去重**。

## 5. 工程规范与约束

### 5.1 命名与目录
- Python 3.11+，只用 stdlib。模块名用小写名词，与第 4 节的术语一致（`notifier`、`intake`、`goals`）。新业务模块放进 `kernel/`，新宿主调用放进 `host/`，新共享工具放进共享基础层（见第 3.1 节）。
- 自有 job 名为 `muse-<key>`，key 满足 `[a-z0-9][a-z0-9_-]{0,63}`；launcher 为 `scripts/hermes-muse-<key>.py`；投递 job 为 `muse-deliver-<delivery_key>`。
- `source_id` 满足 `[a-z0-9][a-z0-9_.:@-]{0,63}`，形如 `mail:work`。
- 时间一律以 epoch 秒（float）存储。面向模型的输入输出用 ISO 8601，没有时区时按 profile 时区解释。
- 测试命名：
  - `tests/test_*.py`：stdlib 单测；
  - `tests/*_acceptance.py`：按工作包写的验收脚本，输出 `PASS/FAIL/SKIP`；
  - `tests/native_*.py`：需要真实 Hermes。

### 5.2 错误处理
- **宿主调用逐条容错**：用 `@tolerant(default)` 包装，记一条 fault 供 Health 统计，不向 kernel 抛异常。
- **容错默认值必须让调用方能区分“调用失败”和“宿主说没有”**。如果调用结果会导致写入、重发或进入终态，失败时不得返回空答案，只能二选一：
  - 抛出异常，例如 `Cron.by_script`：返回空答案会造成重复建 job 或重发；
  - 返回带 `reason: "host_error"` 的结果，例如 `Deliver.send` 和 `Deliver.receipt`。调用方收到后，把实体留到下一次 tick 再处理。
- probe 永不抛异常。probe 期间出现宿主 fault 时，把 `ok` 降为 `degraded`。缺失的能力只降级对应功能，并写一条 high 级 health item。
- `Kernel.tick()` 的每一步互相独立：某一步失败时记入 `errors`，其余步骤继续执行。
- `submit` 逐条处理：单条结果的错误写进该条的返回值，不影响其他条目；未处理的条目留在原队列。
- 模型面的错误统一返回 `{ok:false, code, error[, field]}`。code 只能取以下值：`invalid_argument`、`permission_denied`、`not_found`、`revision_conflict`、`host_unavailable`、`outcome_unknown`。错误信息必须点名字段和合法值，不做猜测或强制转换。
- 偏好元数据无效时阻塞投递（`preferences()` 抛错），不猜默认值。

### 5.3 幂等、并发、TTL、跨模块契约
- Store 的每次写入都是 `BEGIN IMMEDIATE` 事务（`busy_timeout` 15s、WAL），状态迁移用 CAS（`WHERE id AND state AND version`）。
- **读改写必须在同一个 `store.atomic()` 里完成**。最简单的写法是 `common.patch`；`goals.mirror`、`notifier.supersede`、`Store.tick` 是在 atomic 内先 get 再 transition 的正确示例。
- `tick()` 幂等、可重入，多个进程同时进入也是安全的。新增定时行为只能有两种形式：`Kernel.tick` 里的一个 step，或某个计时状态的 exit。不要另起调度器或线程。
- **新增的计时状态必须有 deadline**（验收 A3；Health 的 `deadlines` 检查会拦）。到期后的处理只能二选一：
  - 写 exit，由 `Store.tick` 迁移（Health 的 `tick_stalled` 检查会拦）；
  - exit 为空，由所属模块在 tick 的某一步或某个 packet 中处理，同时补一条 Health 检查。现有的 `goal`、`run` 就是这种情况。
- packet：同一 job 同一周期只有一个 `issued`，并发调用拿到同一个 packet；ref 只在本 packet 内有效；submit 按条目幂等。
- 投递：先在事务内认领 `sending` 并写入 `delivery_key`，再调用宿主；同一个 key 重复 send 时返回已有回执。**结果未知的外部写入不得盲目重发**。
- 文件：GOAL.md 和内容文件必须持有 `mutations.lock`，并通过 `expected_revision` CAS 写入。冲突时返回 `revision_conflict`，调用方需重读并合并。
- **payload 字段以写入方为准**：读取别的模块写的 payload 字段时，字段名以写入方的代码为准；测试必须用写入方的函数产生数据，不得手写 payload。
- 共享文件和后台任务之间的竞争属于正确性问题，不能当作“偶发”问题处理。

### 5.4 代码与模型的分工（可检验规则）
1. **模型的输出只能是三类**：
   - 引用：本 packet 内的 ref，或 Muse 返回过的 ID；
   - 枚举值：`SCHEMAS`/`MAIN` 中定义的取值；
   - 自由文本：措辞、理由、事实。
2. **新的值由代码派生**：凡是会成为新 ID、键、时间、计数或状态名的值，都由代码派生。主会话可以转述用户明确说出的值（例如状态、Goal 的 `deadline` 字段、`next_check_at`），但模型不得自己生成这些值。
3. **模型给的 priority 和 verdict 不能绕过代码门控**：
   - 静默时段、每日预算、`urgent_per_day`、blocked_topics、每个 packet 的发送上限和去重，都由代码执行；
   - urgent 只能越过静默时段，并且受 `urgent_per_day` 上限约束；
   - 淡出询问只能由代码撤回。
4. **必须由代码完成的事**：
   - 调度与门控（packet 为空就保持静默）；
   - 信号的捕获与分类；水位；
   - packet 的组装与预算；
   - 去重与幂等；状态迁移与 TTL；
   - 投递与对账；next_check；淡出询问与取消；
   - 复盘候选的挑选；run 结果；
   - 健康检查与告警；脱敏与截断。
5. **允许交给模型的事**：
   - 信号中的事实与决定，以及它属于哪个目标；
   - 来源条目是否值得打扰用户，以及措辞；
   - 通知的 verdict；
   - 复盘结论；
   - 目标进展解读与完成建议；
   - 反思、人物页、Ideas 和 Feed 写作。
6. **prompt 只写行为**，每个 job 不超过 15 行，不写字段表。字段只存在于 action table 和 `packets.SCHEMAS` 中，`skills/companion/references/interface.md` 由代码生成。

### 5.5 日志与可观测
- 用 `logging.getLogger(__name__)`；宿主故障记 `warning`；不要用 `print` 输出调试信息。**launcher 的 stdout 就是投递内容**，非空就会发给用户。
- 每次状态迁移的 `reason` 用短代码，例如 `ttl`、`receipt:sent:`、`intake:saved`、`update:user`，供 event 日志和 Health 追溯。
- 可观测性只来自 `event` 表、`health.json` 和 `HostAdapter.faults`。发现新的失败模式时补一条 invariant，不要加遥测。

### 5.6 隐私与安全红线
- 所有交给模型的文本（摘录、`read`、复盘候选、会话摘要、列表标题）都必须先经过 `host.sessions.redact()`；迁移来的 v1 文本在交出前也要再脱敏一次。
- 长度限制：单次 `read` 不超过 12k；同一条目在同一 packet 内累计不超过 24k；超过 8k 的文本只给头尾摘录。
- 只读私聊用户来源。群聊和不在白名单里的来源一律不读、不授权。
- 代码、测试、文档、fixture 和提交里，不得出现个人数据、私有导出、真实会话、机器名、profile 名或凭据；测试只用合成数据。
- 不扩大来源访问范围；子代理只读；profile 之间相互隔离。
- forget 必须删除或清空 payload，并写入 tombstone；同时向用户说明哪些内容仍然存在。

### 5.7 依赖与外部调用
- 不加第三方依赖。Hermes API 只经 HostAdapter 调用；Hermes 的数据库只用 `mode=ro` 打开。
- 插件注册期间不调用模型、不访问网络。
- 安装时不改 SOUL、原生 memory 和 `agent.system_prompt`，不覆盖用户编辑过的模板，也不重复创建 job。
- job 不设 `workdir`，因为在 Docker 或远程终端上，宿主路径会让命令失败。宿主 API 需要 cwd 时用 `HostAdapter.cwd()`，不读也不改 `terminal.cwd`。
- 所有安装产生的状态都留在当前 profile 内。

### 5.8 明确禁止（及原因）

| 禁止 | 原因 |
|---|---|
| 在 `host/` 之外 import Hermes、读 Hermes 的 state.db、写死 `cron_` 前缀或 outcome 值 | 宿主语义一变，功能就会静默失效 |
| 在 `kernel/` 之外写实体状态，或在 `keys.py` 之外派生 natural_key | 写入方一分散，状态规则就会分叉 |
| 让模型生成 ID、键、时间、计数或状态名（违反第 5.4 节第 1、2 条） | 记账交给模型，会导致参数错误反复出现、状态卡死 |
| 用模型自拟的 `issue_key`、`event_key` 去重；去重时不比对 7 天内的已发历史 | 这是 0.3.x 重复通知的主因 |
| 让模型给的 priority 或 verdict 越过代码门控 | 静默时段和预算会形同虚设 |
| 新增没有 deadline 的计时状态；新增没有 exit、也没有 Health 检查的计时状态 | 状态机卡死、通知积压 |
| 用 `partial` 一类含义过载的完成态；让模型自评 run 结果 | 真故障被淹没 |
| 宿主调用失败时，在会导致写入、重发或终态的路径上返回空答案 | 一次瞬时故障就会关掉实体或造成重复 |
| Health 走 Notifier、写 Store 或修复数据 | 告警和故障共用通道后，内核出 bug 无人发现 |
| 要求全文读完才能处置；把大文本或未脱敏文本交给模型 | 隐私泄露、预算超限 |
| 在 prompt、skill 或文档里手写字段表；手改 `interface.md` | 契约多处维护，必然不一致 |
| 测试中手写别的模块的 payload | 测试自己编造契约，读写两端失配却仍然全绿 |
| 给 Hermes 核心打补丁；手改 `cron/jobs.json` 或 Muse 的 SQLite | 违背独立插件定位，还会破坏宿主账本 |
| 新增 LLM 唤醒门、通用操作审计、回执 review/defer 等已删除的机制 | v2 已删除这些机制 |
| 从 detached checkout 发布；宿主升级后不跑 native 契约测试 | 交付没有门禁 |

## 6. 如何做改动

### 6.1 改动落点（按顺序）

**新增或修改实体、状态**：
1. 在 `kernel/store/machines.py` 定义状态、边、TTL、exit 和 dedupe；natural_key 的派生写进 `keys.py`。
2. 补 `tests/test_store_v2.py` 的表驱动用例。
3. 涉及 v1 数据时，同步修改 `migrate.py` 的映射和 reason code。如果回滚后旧版会因此重复处理，还要改 `bridge.py`。
4. exit 为空，或需要告警时，在 `health/invariants.py` 加检查，测试数据用 kernel 函数产生。
5. 更新第 2.1 节的表。

**新增 `muse_manage` 动作**：14 个动作是设计契约，动手前先确认设计（见下文“不能碰”）。确认后按顺序改：
1. 在 `kernel/` 中实现状态写入函数；
2. 在 `surface.MAIN` 加一行；
3. 实现 `Surface.muse_manage_<action>`，只做参数映射；
4. 如有必要，修改 `authorize`；
5. 运行 `python -m hermes_muse.surface`，重新生成 `interface.md`；
6. 更新 `surface_acceptance` S1 的计数；
7. 有判断原则的，写进 `skills/companion/references/*.md`，只写原则，不写字段。

**新增 packet job**：
1. 改 `packets.JOBS`、`BUILDERS`、`SCHEMAS`、`KIND_SCHEMA`、`TYPE_SCHEMAS`、`HANDLERS`；
2. 改 `surface.JOB_SCHEDULES`；
3. 写 `prompts/jobs/<job>.md`，不超过 15 行，结尾回答 `[SILENT]`；
4. 补 Health 的 packet 和 run 检查；
5. 补 `kernel_acceptance` 和 `surface_acceptance`。

**新增宿主调用**：
1. 只在 `host/` 中添加，用 `@tolerant`，默认值符合第 5.2 节；
2. 新读的列加进 `sessions.COLUMNS`；新增的 import 会被 `symbols` probe 通过 AST 自动覆盖；
3. 新的语义值加进相应的集合（`KNOWN_OUTCOMES`、`USER_SOURCES`、`MACHINE_SOURCES` 等）；
4. 跑 `tests/native_host_contract.py`。

**新增来源类型**：改 `sources.INTERVALS`，并新增 `skills/companion/references/source-<kind>.md`。

**新增 health invariant**：在 `health/invariants.py` 写一个函数并加入 `STORE_CHECKS`；每条 health item 都要给出 `fix`。

**不能碰**：
- Hermes 核心；用户的 SOUL、`memories/` 和用户编辑过的模板；
- `muse/state.db`：迁移后只读，只有 bridge 可写；
- 生成的 `interface.md`；
- 设计契约：14+3 个动作、5 种 submit schema、第 2.1 节的状态机与 TTL、第 2.2 节的授权规则、第 2.3 节的门控规则。要改契约，先提出，由维护者拍板，并同步更新本文件。

### 6.2 评审与验收清单
运行前先设置隔离目录：`export TMPDIR=$(mktemp -d)`。下表中 `<hermes-agent>` 表示 Hermes 源码路径。

| 检查 | 命令 |
|---|---|
| stdlib 单测（与 CI 相同） | `python -m unittest discover -s tests -v`；在隔离 home 中运行：`python tests/run_isolated.py` |
| 编译 | `python -m compileall -q hermes_muse tests` |
| Store v2、迁移、回滚（A1/A2/A3/A5/A12） | `python tests/store_v2_acceptance.py`（需要 git tag `v0.3.1`、`v0.3.16`） |
| 内核（A3/A5/A10） | `PYTHONPATH=<hermes-agent> python tests/kernel_acceptance.py`（没有 Hermes 时，native 部分为 SKIP） |
| 模型面（S1–S10） | `PYTHONPATH=<hermes-agent> python tests/surface_acceptance.py` |
| Health（A3/A4/A7–A10） | `PYTHONPATH=<hermes-agent> python tests/health_acceptance.py` |
| 宿主契约（A4/A13；宿主升级后必跑） | `HERMES_SOURCE=<hermes-agent> python tests/native_host_contract.py` |
| 生成物是否最新 | `python -m hermes_muse.surface && git diff --exit-code skills/companion/references/interface.md` |
| 出站边界 | 运行表下的命令，输出应为空（WP6 合入后改用 `scripts/check_boundaries.py`） |
| 官方插件校验 | `HERMES_SOURCE=<hermes-agent> python tests/official_validation.py`，或 `HERMES_HOME=$(mktemp -d) hermes plugins validate . --json` |
| 隐私扫描（发布前） | 在 diff 中搜索个人名、机器名、profile 名、真实 ID 和凭据模式，应当没有 |

```bash
grep -rnE '^\s*(from|import) (hermes_\w*|cron|gateway|tools|agent)\b' hermes_muse --include=*.py | grep -v '^hermes_muse/host/'
```

人工评审要点：
- 新增的计时状态是否有 deadline，是否有 exit 或 Health 检查；
- 读改写是否都在同一个 `store.atomic()` 内；
- 是否有 `kernel/` 之外的实体写入；
- 模型输出是否只有引用、枚举和自由文本；
- 交给模型的文本是否已脱敏、是否有长度上限；
- 宿主调用失败时的默认值能否区分“失败”和“没有”；
- 是否有 `host/` 之外的 Hermes 依赖；
- 是否新增了 stdout 输出；
- 测试是否只用临时 home（**绝不碰开发者正在使用的 Hermes**），读别的模块的 payload 时是否用写入方函数产生数据；
- 本文件、README 和 `docs/DESIGN.md` 是否与实现一致。

发布门禁：
- 单测、native 集成和官方插件校验全部通过，并附 A1–A13 的脚本输出；
- 只从 tag 安装；
- 发布后 7 天内只修 high 级问题。

### 6.3 提交与分支
- 提交信息用 `type(scope): 摘要`，摘要用英文祈使句：
  - type 取 `feat|fix|test|docs|refactor|chore`；
  - scope 用模块名，如 `host`、`kernel`、`kernel/store`、`health`、`surface`、`prompts`、`skill`、`dashboard`；
  - 一个提交只做一件事；验收脚本单独作为 `test(...)` 提交。
- 分支：
  - `redesign/v2` 是集成分支；
  - 工作包在 `redesign/v2-wpN` 上开发，验收通过后 merge 回集成分支；
  - v2 一次开发、一次发布（建议版本号 0.4.0），不做对外的阶段性发布。
- 未经维护者授权，不 push、不打 tag、不改远端。

## 7. 已知取舍与历史包袱（不要当成规范）

本节列出当前分支与上文目标之间的差异。

**与规则冲突、需要修的代码缺口**：
- **授权（违反第 2.2 节第 1 条）**：
  - `HostAdapter.caller()` 只用 chat_type 和 `platform != "curator"` 计算 `private`，没有查 `USER_SOURCES`；
  - `Surface.authorize` 也不查。结果是 webhook、api_server、kanban 或空 platform 都能调用写动作。
  - **待确认**：Hermes 在 CLI/TUI 下实际给出的 `HERMES_SESSION_PLATFORM` 值。修的时候不能把合法的主会话误拒，同时要在 `surface_acceptance` 加“机器来源被拒”的用例。
  - job run 目前只检查 `caller.cron`，没有核对它是哪个 job。
- **urgent 门控（违反第 2.3 节和第 5.4 节第 3 条）**：`notifier.gate()` 对 urgent 直接放行，偏好里也没有 `urgent_per_day`。
  - **待确认**：`urgent_per_day` 的默认值（评审建议 2）。
- **实体写入不在 kernel（违反第 3.1 节）**：
  - `surface.py` 直接用 `store.create`/`patch` 实现了 interest/idea 的记录与续期、forget、watch_create/stop、feedback、Feed 删除；
  - `topic_key` 写在 `surface.py`，没有放进 `keys.py`；
  - WP6 的 `hooks.py` 直接创建 memory_receipt。
  - 处理方式：把这些写入搬成 kernel 中的函数，不需要新抽象。
- **宿主容错（违反第 5.2 节）**：`runs.check` 在租约到期时调用 `cron.outcome`，该调用失败时返回 None，被当作“宿主没有记录”，run 因此被判为 `abandoned`。
- **Health 读的字段 kernel 从未写过（违反第 5.3 节“写入方为准”）**。裁定以 kernel 为准，具体改法：
  - `invariants.goals` 读 `fade_at`，kernel 从不写：改为由 `goals.fade` 写入 `fade_at`；
  - `invariants.findings` 读 `evidence`，kernel 写的是 `turn`：改为以 `turn` 作为证据；
  - `invariants.sources` 读 `failures`（默认值 3），kernel 写的是 `fail_streak` 和 `health`：改为读这两个字段；
  - `goals.fade` 写的 `fade_blocked` 目前没有任何 invariant 读，需要补检查。
  - `tests/health_acceptance.py` 手写了这些 payload，要改成用 kernel 函数产生。
- **命名尚未与词汇表一致**：
  - kind 现在叫 `receipt`，应改名为 `memory_receipt`（`machines.py`、`migrate.py`、hooks）；
  - 模型面 `read(kind=status)` 返回的是 `health.findings[]`，`health.invariants.finding()` 也应改名为 item；
  - `notifier.draft` 的参数叫 `source_key`，应改为 notice key；
  - 动作表 `surface.JOB` 建议改名为 `JOB_ACTIONS`，与 owned job 区分。
- **边界**：本分支的 `content_io.check_cancelled` 在 `host/` 之外 import 了 `tools.interrupt`。集成分支已改为 `host.interrupted()`。

**WP6 未合入造成的现状**：
- **0.3.x 模块仍在树中**：
  - 涉及：`service.py`、`runtime.py`（v1）、`host_v1.py`、`contracts.py`、`guidance.py`、`operations.py`、`wake_gate.py`、`context_pages.py`、`memory_feedback.py`、`fade.py`、`followup.py`、`research.py`、`mutations.py`、`store.py`、`views.py`（v1）等；`prompts/*.md` 顶层的 8 个 v1 prompt；大部分 `tests/test_*.py` 和 `tests/native_*.py`。
  - 不要扩展或引用它们。本分支的 `calendar.py` 还依赖 v1 的 `guidance.py`，WP6 会把它改成只依赖 `content_io`。
- **接线仍是 v1**：
  - `__init__.register` 调用的是 v1 的 `runtime.register`；
  - `cron_entry.py` 走的是 `host_v1`；
  - `plugin.yaml` 的 version 是 `0.3.16`，`provides_tools` 缺 `muse_job`；
  - `surface.register`、`ensure_jobs`、`health.ensure_cron`、`runs.start`、`intake.quiet_wake` 都还没有被调用。
- **单测**：`python -m unittest discover -s tests` 有 5 个 FAIL、1 个 ERROR，都是 v1 测试在断言 v1 的 prompt 和 skill 文本。v2 测试和验收脚本全部通过。
- **出站边界 grep**：命中 11 个文件，其中 10 个是 v1 模块，另 1 个是 `content_io.py`。
- **CI**（`.github/workflows/test.yml`）：只跑单测和 v1 的 `native_smoke.py`，v2 的 `*_acceptance.py` 不在 CI 中。
- **文档**：`docs/DESIGN.md`、`docs/INTERFACE.md`、`docs/ACCEPTANCE.md`、`docs/FEATURE-COVERAGE*.md`、`README*.md`、`after-install.md` 描述的都是 0.3.16，由 WP7 重写。在那之前，v2 的接口以生成的 `skills/companion/references/interface.md` 为准。

**刻意的取舍**：
- 强键是 `src:<上游ID>`，不带 `source_id` 前缀，这样才能与没有记录 source_id 的 0.3.x 历史互相匹配（见 `keys.notice_key` 的 docstring）。
- 复盘规则用正则匹配中英文关键词，属于启发式。
- 已标注的 shortcut：
  - 迁移来的 watch 如果没有 expires，给 30 天（`migrate._watches`）；
  - Feed 记录写入后、正文落盘前如果崩溃，正文会丢失，而且无法恢复：重新提交会返回 `existing`，不再写正文（`Surface.write_feed`）。
- 文档字符串过期：`surface.reference()` 的 docstring 提到的 `tests/surface_check.py` 不存在，实际的检查是 `surface_acceptance.py` 的 S3。
