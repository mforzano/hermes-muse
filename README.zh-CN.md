# Hermes Muse 0.3.16

[English](README.md) · [设计文档](docs/DESIGN.md) · [验收清单](docs/ACCEPTANCE.md)

助手只在你开口时才回应，很多事就会被落下：周一提过的行程、一直在等的回复、说好月底前做的决定。Hermes Muse 是 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 的插件，让 Hermes 能自己跟进这些事。它受 [Meta Muse](https://about.fb.com/news/2026/09/introducing-muse-personal-ai-agent/) 的主动交互启发。

实际用起来大概是这样：

- 你说这个周末去杭州。出发前它会查天气和你的日历，只有变化大到需要你知道时才开口。
- 你说想在月底前选好一辆自行车。它在后台持续比较候选，把结果带回来，直到你真的做了决定才收尾。
- 等的那封回复到了，或者会议改了时间。下一轮检查发现后，会先读原始邮件或日程，再在 Bot Chat 里告诉你发生了什么、为什么重要。
- “已处理”“晚点再说”“以后别提”各有各的效果；没有值得说的事，就保持安静。

它建立在 Hermes 已有的能力上：记忆、Cron、Skill 和子任务。模型和记忆后端沿用现有配置，提醒默认交给当前 profile 的 Bot Chat，不要求安装 Hindsight、Obsidian 或其他指定服务。本项目与 Meta/Muse 无官方关联，代码和提示词独立编写。

## 插件页面

Muse 在 Hermes 的 Dashboard / Desktop 页面里提供一个入口：选择 profile 后，就能浏览对应的 Feed、Ideas 和 Goals，样式跟随宿主主题。

![Muse 页面总览与 Feed 文章列表](docs/images/muse-dashboard-overview.png)
*选择 profile 后可浏览、搜索 Feed 文章，也可切换到 Ideas 或 Goals。所有截图均使用演示数据。*

![Muse Feed 文章正文](docs/images/muse-feed-article.png)
*打开一篇 Feed 文章，可阅读按 Markdown 渲染的正文，来源单独列在下方。*

![Muse Ideas](docs/images/muse-ideas.png)
*Ideas 按关联的目标分组展示建议，并可回到对应主题。*

![Muse Goals](docs/images/muse-goals.png)
*Goals 先列出进行中的目标，显示状态、下次检查和最新进展；已完成和已取消的折叠在下方。*

## 安装

```sh
hermes plugins install xiaohei-info/hermes-muse --enable
```

安装到当前选定的 profile。明确安装到 default：`hermes -p default plugins install xiaohei-info/hermes-muse --enable`；安装到其他 profile 时，将 `default` 换成 `architect` 等名称。每个启用插件的 profile 独立保存状态、创建 6 个任务并投递到自己的 Bot Chat；省略 `-p` 会沿用当前 profile 选择。

如果自行配置了 `platform_toolsets` 白名单，请在使用插件的平台列表中加入 `muse`，否则可能已加载提示词，却无法调用状态工具。

需要 Hermes 0.21.5 及相关插件接口，已测试版本见[验收清单](docs/ACCEPTANCE.md)。首次加载会自动创建工作区，注册 1 个 Skill、1 个状态工具（`muse_manage`）和 6 个钩子，追加 1 段系统提示词，并建立 6 个固定 Cron，无须选择功能或另行初始化。没有运行中的宿主时，下次启动 Hermes 才会加载。

新增系统规则在新会话生效。后台执行需要 Hermes 调度器在线、模型可用；定时检查、研究和内容生成均使用现有模型与工具，按相应服务计费。

## 功能对照

插件在首次加载时配好任务和规则，模型和信息源沿用现有 Hermes 配置，通知默认在当前 profile 的 Bot Chat 中处理。

| Muse 功能 | 原生 Hermes | 安装 Hermes Muse 后 |
| --- | --- | --- |
| 记住偏好与近况 | 原生支持长期记忆和用户资料；定期整理人物关系、夜间复盘等流程需要自行配置。 | 开箱即用的记忆维护：整理交流、查到的新事实和任务结果，更新人物关系；夜间独立回顾，形成下一次协作的依据。 |
| 主动提醒重要变化 | 原生支持 Cron、会话心跳和推送；关注范围、通知条件、去重与限频规则需要自行配置。 | 开箱即用的主动巡查：检查已连接的邮件、日历、提醒事项和设备信息，结合近况判断重要变化；天气、出行和兴趣新闻有实际帮助时也会提醒。 |
| 持续跟进目标 | 原生支持持续任务和任务看板；长期目标、进度、关联监控和结束条件需要自行组织。 | 聊天中确认的计划可以持续跟进，隔天也能凭核实后的结果收尾；父会话说明结果后标记目标完成，停止所属后续监控。 |
| 后台准备资料 | 原生支持异步子任务；定期研究哪些目标、如何保存和使用结果，需要自行配置。 | 多步任务优先交给原生异步子代理，主会话继续聊天；研究批次由后台完成后回传，无须反复催问。 |
| 主动提出建议 | 原生能根据对话与记忆给出建议；持续研究、筛选和保存建议需要自行安排。 | 结合目标、近况和已有信息准备建议，记录依据和反馈；新建议由用户决定是否执行，已有任务继续推进。 |
| 个性化 Feed | 原生支持搜索、写作和定时任务；兴趣选题、文章记录、反馈和更新流程需要自行搭建。 | 开箱即用的本地 Feed：结合已有记忆、兴趣和反馈选题，静默保存文章；没有登记目标也能选题，可在聊天中搜索、反馈和删除，也可在 Dashboard 页面浏览。 |
| 调整提醒与停止跟进 | 原生支持修改偏好、停用任务；反馈如何关联提醒、目标和监控，需要自行约定。 | “已处理”“晚点再说”和“以后别提”分别处理；事项完成、取消或改期后重新判断提醒，临时兴趣默认 14 天到期。 |
| 代办生活事务 | 原生可调用浏览器和外部工具；电话、预订、支付需自行接入服务并配置流程。 | 未提供这部分装配，继续使用用户已接入的工具和服务。 |

Feed 文章可在只读的 [Dashboard / Desktop 页面](#统一-dashboard)浏览；后台研究使用 Hermes 原生执行和回流，重启后的状态与恢复能力取决于宿主，未知结果不会自动重跑。记忆整理、事实核实和内容生成由现有模型与工具完成，具体限制见[详细实现对照](docs/FEATURE-COVERAGE.zh-CN.md)。

## Skill 与工具

| 名称 | 作用 |
| --- | --- |
| [hermes-muse:companion](skills/companion/SKILL.md) | 主对话和后台任务共用的操作规程，包括连接与设备检查、目标与兴趣、主动提醒与反馈、记忆与人物关系、Feed、后台委派、操作自查和周／月复盘。 |
| muse_manage | 一个工具、40 个动作（[接口](docs/INTERFACE.md)）。供助手记录和查询来源检查、目标、兴趣、提醒、反馈、Feed 与研究进度，按连接、账号和资源保存检查进度，区分完整、部分和失败的检查，维护到期、停止和投递状态，并查看实际工具执行结果、记录错误和修正进度。列表分页，请求和响应有大小上限；目标使用可选整数版本；其他受管修改保留对象版本与请求键重放。 |

Skill 随插件注册，文件保留在插件目录。主对话按需读取其中的规程，六个固定 Cron 都绑定这个 Skill。日常使用直接聊天即可，由助手调用工具。接到新要求或任务变更时，它会结合目的考虑通知、来源限制和退出条件，把安排落实到实际任务并核实，再自然地说明最有用的判断。例如提高检查频率，并不意味着每次检查都要发消息；明确订阅的定时报表仍照约定发送。语气沿用你的习惯，不套固定话术。

## 提示词

插件自带 8 份提示词：

| 文件 | 用途 |
| --- | --- |
| [receiving.md](prompts/receiving.md) | Bot Chat 继续自己的后台工作：先说清对象和发生了什么，再讲发现、结果和需要负责的错误，让回复本身就能读懂，不回复任务“收到”，不重复创建提醒。 |
| [system.md](prompts/system.md) | 主对话接手要求时考虑实际后果、落实安排并自然说明判断；多步任务优先异步委派，及时处理新消息并保留未完成责任。 |
| [proactive-watch.md](prompts/proactive-watch.md) | 主动巡查：发现可用连接与设备，检查变化及临近事项，记录各来源检查结果，核实后通知或静默。 |
| [memory-upkeep.md](prompts/memory-upkeep.md) | 记忆维护：整理新增交流与事实，检查自己的操作是否有误，验证修正并更新记忆和进度。 |
| [nightly-review.md](prompts/nightly-review.md) | 夜间复盘：独立回顾交流，更新对齐，研究目标与建议，检查收尾和未解决的问题，积累已验证的经验并复盘技能。 |
| [feed-pulse.md](prompts/feed-pulse.md) | Feed 写作：结合原生记忆、偏好、近期理解和真实来源选题，有新价值才静默生成文章。 |
| [weekly-governance-review.md](prompts/weekly-governance-review.md) | 周复盘：检查产出与投入、漏查来源、未收尾任务和重复提醒，提出下周调整建议。 |
| [monthly-system-audit.md](prompts/monthly-system-audit.md) | 月度审计：检查规则与定时任务是否仍有效，有无重复流程和该退出的工作，提出精简建议。 |

`system.md` 通过 Hermes 插件接口追加到系统提示词的记忆段之后，在新会话生效。`receiving.md` 随 Bot Chat 成果附带，并由接收会话钩子使用。六份任务提示词写入对应 Cron 的任务正文，在后台任务运行时使用。每次加载插件会同步这些任务正文，保留用户设置的执行时间、模型和暂停状态。

## 会话钩子

| 钩子 | 作用 |
| --- | --- |
| pre_llm_call | 在回复前记录最多8,000字符及原turn/session身份，并在本轮上下文中附上状态工具入口，供助手处理新目标、兴趣和反馈；同时提示核对本轮接受的请求或承诺是否已有活动目标，并列出本会话最近24小时创建的目标标题。 |
| post_llm_call | 关联插件 Cron 会话及原生执行 ID，供后续自查；用户消息达到 80 个字符时，回复后等待 5 分钟安静期，再安排记忆整理；新消息会取消本次计时，每天最多提前触发 3 次。 |
| transform_llm_output | 巡查／监控调用状态工具后，将最终输出固定为通过检查的通知正文；没有可发内容时静默。发往 Bot Chat 的提醒和周／月报告附带统一接收说明；普通聊天不受影响，直接投递到其他渠道的报告保留正文。 |
| post_tool_call | 根据真实原生结果记录最少的记忆／技能写入反馈引用，不复制记忆正文。 |
| on_skill_lifecycle | 把最近的 `skill_manage` 写入回执标记为已被 Hermes 技能生命周期观察到；没有对应工具回执就不做推断。记忆和技能写入按实际工具结果分类（已保存、待批、提交未确认、失败、无变化、未知），只保留最少的关联引用。 |
| on_session_end | 清除本轮的忙碌标记，避免后续提醒一直等待已经结束的对话。 |

私聊收到其他原生 Cron 的投递时，也会使用同一套接收规则；这些结果不作为用户表达记录。用户信号钩子处理私聊和本地对话，跳过群聊、Cron、子任务输入及其完成回调；旧版误记的后台消息保留为审计记录，不再作为用户表达使用。输出钩子绑定当前插件巡查、监控或周／月复盘的执行轮次，不改写普通回复。延迟整理复用现有的记忆维护 Cron；插件从运行进程卸载时会取消临时计时器。

## 六个固定 Cron

| 任务 | 默认频率 | 输出 |
| --- | --- | --- |
| muse-proactive-watch | 每 30 分钟 | 来源检查记录、有价值的提醒、证据和交付状态 |
| muse-memory-upkeep | 每小时 | 新事实、人物和群组资料 |
| muse-nightly-review | 每天 03:20 | 对齐记录、目标补录（补上尚无目标的已接受请求，每次最多5个）、目标淡出（休眠目标只问一次，宽限期后取消）、目标研究、Ideas、工作经验和技能复盘 |
| muse-feed-pulse | 每小时 | 本地 Feed 文章 |
| muse-weekly-governance-review | 周日 21:15 | 实际帮助、执行情况和调整建议的复盘 |
| muse-monthly-system-audit | 每月 1 日 10:40 | 用户需求、提示词和运行流程的简短审计 |

插件不会为固定任务指定模型或 provider。Hermes 每次执行时按这个顺序选择：**任务单独指定的模型 → 当前 profile 的 `cron.model` → profile 主模型**。不设 Cron 默认模型，每小时的后台工作也可能使用主对话的高成本模型，建议先配好再让调度器持续运行。

例如，将下面的配置合并到当前 profile 的 `config.yaml`。模型和 provider 要换成自己实际可用的名称；这里的 `llm` 是已配置的 provider 名称：

```yaml
cron:
  model: gpt-6-luna
  model_provider: llm
agent:
  reasoning_overrides:
    gpt-6-luna: high
```

推理级别依次取任务的 `reasoning_effort`、该模型的 override、全局 `agent.reasoning_effort`；已测试的 Hermes 版本不支持 `cron.reasoning_effort`。上例会将该 profile 中 Luna 的推理设为 `high`，也适用于使用 Luna 的普通会话。任务已有的单独模型和推理设置优先，插件重载时会保留。Cron 默认配置在下次运行时读取。Bot Chat 接收结果后使用自己的会话模型再处理一轮，仍会产生模型调用；`cron.model` 不控制这一轮的模型。并行自查的子代理也沿用 Hermes 的委派模型配置（如 `delegation.model`），不会被 `cron.model` 强制覆盖。插件安装不会替用户写入上述模型配置。

频率使用 Hermes 当前时区。有明确截止时间的监控会在到期后停止。用户明确要求的定时提醒和专项监控可以产生额外 Cron，均记录到安装清单；六个是固定任务数量。新建重复 watch（interval 或 cron）必须能证明间隔不短于 30 分钟，否则拒绝创建；一次性（once）不受限，已有任务不改动。巡查直接投递本轮筛选后的消息，不另建投递 Cron；暂缓的候选记录阻塞原因和最早可发时间，留到下一轮巡查重新核实；这个时间不是精确投递承诺。普通和时效性事项遵守免打扰时段，确有紧急风险时可凭新证据重新分级，用户延后和禁提设置仍然有效。投递结果按该次执行的持久回执核对，后续巡查不会覆盖此前的送达依据。

六个固定任务按时调用模型，各自检查需要的信息。主动巡查每轮发现当前可用的连接与设备，即使还没有目标或兴趣记录，也会检查邮件、日历、提醒事项等来源。同一来源复用固定记录和已验证的读取方法；确认是同一连接的工具别名归入原记录，旧重复记录保留备查，避免每轮重新摸索工具。优化以内容充分、判断到位、主动交互和及时跟进为前提，不为节省调用而省略相关信息或把研究做浅。调用失败时先诊断并尽可能在本轮恢复；日常巡查本身也是已承担的工作，重要漏查可以主动说明，不要求先有目标记录。先核实授权再查询受保护数据，不把来源不可用当成没有新信息。应查与已查资源分别记录；漏查日历、未读完分页或查询失败时，不当作完整检查，也不推进成功进度。每次检查都会消耗模型调用，工具和子代理可能带来额外用量。

巡查先核对原生会话中用户的请求、助手已经完成的结果和后续纠正，再决定是否提醒；发送前会话有新消息，只需补读新增部分并重新复核。同一问题的新版本可以替代旧的未发候选，已发历史保留。具名提醒保留原始标题、条目 ID 和到期时间，不擅自解释缩写。默认不再限制普通提醒每天只能一条，仍遵守免打扰、去重和用户明确设定的上限；升级只迁移完全未改动的旧默认偏好文件。

巡查先查清情况、准备有用的结果，再决定是否打扰。比如出行前天气突变（天气默认关闭；位置属于隐私数据，需把 `weather_context` 偏好设为 true，所配位置会发给第三方天气服务）、会议改期、感兴趣的主题出现重要进展，或近期计划正好遇到合适的活动、展览和预约窗口。位置、日期和相关性要有依据，普通天气更新和重复新闻不必推送。没有值得提醒的内容就静默；日常主动消息优先讲一件最有价值的事，不因用户沉默而反复追问。这些是模型执行规程，判断质量取决于当前模型和信息源。

主对话和后台任务共用目标、兴趣和反馈记录。已接受的工作可以跨天推进，凭核实后的结果收尾；完成标准要求用户确认时，仍需用户确认。完成、取消或改期的事项会重新核查，撤下过时的待发提醒。兴趣续期需要新的用户表达，助手自己的研究不会续期；延后也不会延长原有效期。

小时维护将真实用户决定处理一次，每轮兼顾旧待办和新纠正，完整阅读并保存后按ID留下处理标记；时间游标只越过真实连续前缀，旧未读授权不会被丢弃。夜间综合已确认记忆与变化，不再第二遍逐条处理原信号。操作复盘按有后果的事情、未解决失败/unknown与缺验证完成选候选，普通成功读取不再深审；opaque执行仅有明确盲点的有界抽查。调用/结果共享一次原用户上下文。旧全量日志保留为未审历史，不再自动追完；重要finding及未知owner仍可核查，不盲重派。本轮结果和全局待办分开，Feed保持静默与深度，周/月复用结果和变化提出建议。

Bot Chat 会先说清这是什么事、发生了什么，再说明发现、实际影响和已经查清的结果。不看后台投递或工具日志，也应能读懂这条回复。它会把后台工作和已经证实的错误当作自己的责任，不把另一位助手或后台报告当作消息发送者。它不会对后台任务回复“收到，我会处理”，也不会承诺再查一遍已经查过的内容。确实需要你决定时才提问，提到过去的交流必须有依据。统一接收说明随成果投递，也由会话钩子提供；这是对模型行为的引导，不是固定话术或措辞保证。

任务状态会区分信号处理、操作自查、成果文件和通知输出。`muse_manage context` 或 `status` 的 `task_runs` 给出当前、最近结束和上次完整完成记录的有界引用，打开引用再看具体阶段与缺口；两条信号游标还分别显示待处理数量与最早时间。原生 Cron 的“成功”只表示执行结束，不能替代这些结果。中断、达到调用上限或缺少必需成果时保留部分完成状态，下一轮从已保存的位置接续。

最近一次周／月报告摘要可通过 `muse_manage status` 查询，完整输出保留在原生 Cron 历史中，下一轮可据此检查之前的建议有没有带来改善。

## 可选的唤醒判断（默认关闭）

三个高频任务（proactive-watch、memory-upkeep、feed-pulse）默认每次到点都会唤醒模型。如果多数时候什么都没有，可以先让一个小型是/否模型判断一次。默认关闭，开启前行为完全不变。

```sh
hermes config set plugins.hermes-muse.wake_gate.mode typesafe   # off | typesafe
export TYPESAFE_API_KEY=...                                     # 放在 profile 的环境变量里，不要写进 config.yaml
```

开启后，任务的预检查脚本会把“本轮将要审阅的内容”整理成一份有界摘要，向 [TypeSafe](https://docs.typesafe.ai) System One（`jev-latest`）问一个问题：现在跑一轮，是否可能发现值得处理或告诉用户的事？概率低于 `min_probability` 时这次 Cron 静默结束，不启动 agent。

`plugins.hermes-muse.wake_gate` 下的设置：`mode`（默认 `off`）、`jobs`（默认三个高频任务，其他任务从不接入）、`min_probability`（默认 `0.15`，低于才跳过）、`timeout_seconds`（默认 `3`，超时即唤醒）、`max_skip_minutes`（默认 `120`，连续跳过的上限，`0` 为不限；邮件和日历只有 agent 能看到，所以需要定期醒一次）、`include_message_text`（默认 `true`，设为 `false` 只发送数量和时间）、`model` / `base_url` / `api_key_env`（默认 `jev-latest`、`https://api.typesafe.ai/v1`、`TYPESAFE_API_KEY`）。

- 失败即唤醒：缺 key、超时、HTTP/解析错误、Muse 状态读不到，都和没开一样唤醒 agent。
- 只读：不推进游标、不消费信号，被跳过的轮次里到达的消息，下一轮仍然是“新消息”。
- 有些状态本身就需要运行，不经过模型：自上次运行后才到期的目标检查/复盘（拖了好几天的旧目标不算）、待发（candidate/pending）通知、明确的 Feed 请求、积压达到一页的消息。
- 它看不到邮件和日历，只看 Muse 自己的状态，所以才有 `max_skip_minutes`。
- 每次判断在 profile 的 `logs/hermes-muse-wake-gate.jsonl` 追加一行（任务、概率、是否跳过、原因、耗时，不含消息正文），文件最多约 256 KB。可据此调整 `min_probability`。

**开启后数据会发往外部。** 发送的摘要包含任务名及一句说明、本地时间、距上次运行的分钟数、新用户消息数量及最新最多 8 条（每条截取 240 字符）；巡检还有活跃目标数量，以及未来两小时内要检查的目标（标题和分钟数，最多 8 个）；Feed 还有最多 8 个兴趣标题、最近一天的文章数量和最新文章的时间。设置 `include_message_text: false` 可去掉消息、目标和兴趣的文本。关闭时不发送任何内容。TypeSafe 对数据的处理受其条款约束；也可以把 `base_url` 指向自己的兼容端点。

## 文件

根目录是当前 profile 的 `HERMES_HOME`，通常为 `~/.hermes`。

```text
$HERMES_HOME/
├── plugins/hermes-muse/             # 插件代码与资源
│   ├── skills/companion/
│   │   ├── SKILL.md                # 共用操作规程
│   │   └── references/             # 目标、提醒、记忆、Feed、研究、周／月复盘
│   ├── prompts/                    # 上述 8 份提示词
│   └── templates/                  # 工作区初始文件
├── muse/
│   ├── install.json                # 任务 ID 和文件归属
│   ├── install.lock
│   ├── audit.lock                  # 自查批次分配锁
│   ├── state.db                    # 来源检查、处理进度、兴趣、提醒、Feed、研究、自查问题
│   ├── AGENTS.md
│   ├── TOOLS.md                    # 已确认的连接、设备和使用限制
│   ├── PROACTIVE_PREFERENCES.md
│   ├── memory/
│   │   ├── YYYY-MM-DD.md
│   │   ├── people/INDEX.md, <人物>.md
│   │   └── groups/INDEX.md, <群组>.md
│   ├── dreams/
│   │   ├── YYYY-MM-DD.md
│   │   └── alignment/derived/ALIGNMENT_SYNTHESIS.md
│   └── workspace/
│       ├── goals/<目标>/GOAL.md, files/, hidden_files/
│       └── your_files/feed/<id>.md
├── scripts/hermes-muse-*.py         # 原生 Cron 入口
├── memories/USER.md, MEMORY.md      # 现有原生记忆（如当前后端使用）
└── cron/                           # Hermes 自己管理的任务及执行记录
```

操作自查直接读取 Hermes 保存的工具调用、返回结果和插件 Cron 执行记录，也包括已保存的关联子任务。首次检查最近 24 小时，之后从已检查位置继续；读不到的记录会明确说明。并发审查使用独立快照，完成结果可以乱序返回，进度只越过连续审完的部分。中断后继续未完成的批次，接近宿主调用上限时先保存进度并正常结束；页内证据引用保留七天，便于中断后续接。成功的 Skill 和工具目录读取不进入操作审查，失败和实际修改仍保留。现有 `state.db` 保存检查进度、页内引用、会话／执行关联、任务完成状态和问题记录，不复制整份日志、不新建数据库或 Cron。发现问题后，按已有授权修正并验证；影响用户的错误需要说明实际影响和处理结果，不能把准备发送当作已经告知，也不会凭空自动恢复误删内容。

`memory`、`dreams` 和目标目录借鉴 Muse 的组织方式。`install.json`、`state.db`、Feed 落盘和原生记忆路径是 Hermes 适配。首次加载只补齐缺失的工作区模板；目标、笔记和文章在使用时生成。重载不会重复创建固定任务。

夜间任务还可在 `muse/AGENTS.md` 的 Working lessons 中积累少量已验证的工作方法，合并重复、更新过时条目；用户原有约定保留，个人事实和任务流水分别留在记忆与目标记录中。已有安装按需补充该小节，不覆盖整个文件。Muse 定时任务不设工作目录（Docker/远端终端后端同样可用），改由 `content_read` 读取该小节。

安装不改写 Hermes 核心、`SOUL.md`、原生 `USER.md`/`MEMORY.md`、用户项目 `AGENTS.md` 或配置中的 `agent.system_prompt`。运行期间，助手通过现有记忆工具保存新事实。

会话inbox保留每条首8,000字符及来源身份，已处理ID有一个revision绑定标记；不按行数/时间自动丢弃未处理授权。幂等回执/过大工具结果可能包含内容且无自动过期，原生历史、Feed/目标、备份有各自生命周期。详见留存边界与[SECURITY.md](SECURITY.md)。无遥测，不进入个人群聊流程。

## 使用

**推荐配合当前 profile 的 Bot Chat 使用。** 在这里聊目标、兴趣和反馈，后台有值得通知的结果时，Bot 会接着向你说明：

`Cron 准备结果 → 投递到当前 profile 的 Bot Chat → Bot 阅读结果 → 在聊天中回复用户`

Bot Chat 的接收会触发一轮助手处理，使用该 profile 的模型。没有 Bot Chat 会话时，Hermes 可在首次投递时自动创建；已有会话繁忙时由原生机制排队。Feed 和日常整理仍保持静默，筛选出的提醒和定期周／月报告进入这条流程。

官方安装命令会显示插件的安装后说明，目前不提供插件自定义的渠道／会话选择菜单。本版默认使用 Bot Chat，不要求先接 Telegram 等外部渠道。已用 `/sethome` 设置网关 home 频道时，新建的目标监控改为投递到 `all`；可选偏好 `deliver`（`all`、`bot-chat` 或单个平台）可覆盖之后新建任务的目的地，包括首次加载时创建的固定任务。

直接聊计划、兴趣和反馈即可，不需要先手工建好目标才能开始巡查。例如：

```text
建立购车目标，预算两万元，月底前决定。
帮我检查这封邮件的回复，到周五停止。
周末去杭州走走，出行前天气有明显变化再告诉我。
我关注航天，重要进展可以提醒，普通新闻不用每天发。
这个提醒已经处理了。
这个主题以后不用提醒。
列出当前目标、兴趣和待发送的提醒。
搜索 Feed 中关于城市园艺的文章。
```

目标和兴趣由模型识别后调用状态工具记录。正式目标以用户明确表达的计划或承诺为依据，不要求说出“创建目标”；临时兴趣默认 14 天到期，不自动创建长期监控。新的用户消息可以续期兴趣，助手自己的研究不能续期。投递或模型处理失败时保留状态供检查，不自动换渠道重发。想知道检查了哪些来源、哪里失败或有什么待办，可以直接问助手，状态可通过 `muse_manage status` 查询。

## 卸载

```sh
hermes plugins remove hermes-muse
```

然后让 Hermes 清理关联任务：

> 读取当前 profile 的 muse/install.json。暂停并删除其中登记的 Cron，检查仍在运行的任务，清理登记的 scripts/hermes-muse-* 入口。保留目标、文章、笔记和记忆，并报告未完成的清理项。

原生 remove 不保证清理插件创建的 Cron，因此需要这一步。插件被删除或停用后，遗留入口会静默；已经开始的任务仍需检查。重装保留数据，普通重载不会重新启用用户暂停的任务。

## 开发与验证

```sh
python -m unittest discover -s tests -v
node --test tests/page_markdown.test.mjs
# 在 Hermes 环境中：
python tests/native_smoke.py
hermes plugins validate . --json
```

测试使用临时 profile，不调用真实模型或消息渠道。[验收清单](docs/ACCEPTANCE.md)列出了需要在实际环境检查的项目。

[官方插件目录申请](https://github.com/NousResearch/hermes-agent/pull/134976)正在等待审核；当前可用上面的仓库地址安装。

MIT 开源协议。

## 未来 30 天日历与提前准备

可以直接说“查看未来 30 天日历”，窗口从现在连续延伸到本地时间 30 天后；“查看本月日历”使用自然月。两者采用当前 Hermes profile 的时区。IANA 时区处理夏令时；原生环境仅提供固定偏移时，保留实际偏移并明确提示无法推断夏令时。全天事件突出显示原始日历日期，时区换算跨到相邻日期时额外说明。保留来源、日历名称、重复实例、单次例外和取消、多日事件、忙碌日期、重叠与准备原因。

现有巡查同样覆盖未来 30 天，包括第 8–30 天需要订票、出行、材料准备或提前决策的事件。仍保持每 30 分钟一次的默认巡查、安静时间、相关性判断、去重和通知生命周期，不为事件新增 Cron 或 watch，不因扩大窗口而每天重复提醒。通过已经启用并授权的 Apple、Google 等连接枚举全部账户和日历并完成分页；缺页、失联或权限不足属于覆盖缺口，不能声称全局空闲。

现有 muse_manage 新增无持久化日历数据的 calendar_window / calendar_view 辅助操作，详见 [日历流程](skills/companion/references/calendar.md)。滚动窗口、自然月和原有七天检查使用独立的稳定资源标识。仅按来源、账户、日历和实例的明确身份保守合并；跨日历相同 UID 副本默认保留，不能仅凭标题和时间合并。没有新增日历数据库、同步服务、必选供应商或写权限；实际读取与准备价值判断仍由模型结合现有工具完成。

## 接受的工作与跟进

目标只有 active/done/cancelled。核对结果并在对话里告知用户后，调用一次
`goal_update(id=...,status="done",note="已核对并说明结果")`。可选owner句柄
关联原生委派、Cron、Paseo或外部工作；读取owner不会自动重派或关闭目标。
`context(section="followups")`只列真正active且到期的目标。目标写入无需
request_id、scope_token、回复hash或handoff；可选整数expected_revision冲突时返回最新目标。
子代理/Cron可查看并报告，只有私有父会话修改目标；普通task_id不表示子代理。

## 兼容、留存与证据边界

用户输入inbox保留首8,000字符、截断标记、turn/session与时间，route可含chat/user/thread/message等身份。signal与处理标记没有自动行数/时间清理；原生完整正文受宿主保留约束。旧nightly/全量操作cursor只作未审历史，不作为新处理完成证明。研究payload、回执、旧跟进、目标/Feed/记忆文件和备份各有生命周期，清一处不等于全局删除。

会话context为有限预览：每session最新24条、单条至多1,000字符、共享24,000文本字符；可按精确revision通过conversation_read每页12,000字符读全。旧承诺、长消息尾部取消都保留可达；任务通知审批只要求各session最新24,000字符已读，新消息只需补读新增部分再复核。已判定相关、因新消息或用户正在对话被拦满1小时的通知会放行到其余关卡（免打扰、额度、稍后提醒、主题退订、去重仍生效）。此预算不是总工作量或原操作审计上限。详见[规程](skills/companion/references/conversations.md)。

Cron新建watch遵循cron.allow_agent_scheduling；本地goal登记不创建Cron。watch的prompt/stop各显式校验4,000字符，不静默截断；省略schedule默认每30分钟；新建重复watch（interval或cron）必须能证明间隔不短于30分钟，否则拒绝创建，一次性（once）不受限；不改已有jobs。

Muse 工人一律通过公开 ctx.dispatch_tool 调用原生 delegate_task，尊重用户 delegation.* 的 provider/model/reasoning/concurrency；未覆盖时才由宿主采用父模型默认（想沿用父模型，就把 `delegation.provider` 和 `delegation.model` 留空）。研究最多 3 并尊重更低限额；审阅工作包交给原生 child，在当前 profile 通过 muse_manage 分页读取实际证据，返回结论后由父任务核验并提交；父直接审阅作为 fallback，不再额外要求 child grant，全部问题与审阅槽保留。新建重复 watch 至少 30 分钟，once 豁免；已有排程不追改。Cron 修改 feed_brief 不自唤醒。详见[兼容说明](docs/CATALOG-COMPATIBILITY.md)。


## 统一入口与页面

同一个发行包提供 Dashboard/Desktop 页面与纯读 API，保留完整 Feed、主题、点子和目标，浏览不跑模型、不记录阅读进度。Desktop 仅向选定真实原聊天填入草稿，由用户发送后按正常工具授权执行。页面跟随宿主主题，Feed 与目标正文按安全 Markdown 渲染（不解析原始 HTML），未完成目标在上、已完成/已取消折叠在下；点子为空时会说明原因（只有每晚复盘或对话新增后才会出现）。目标接口支持 `status=open|closed`。

所有 Muse 正文使用 content_read/content_write 与对象 ID，不要求模型知道私有路径，也不增加 Docker 的 Muse 挂载。目标使用可选整数 expected_revision；其他受管修改使用对象版本和每次运行唯一的 request_id（动作加本轮开始时间到分钟，不能只用日期；冲突时 suggested_request_id 给出可用的新 key）；冲突先回读，未知结果先核原执行。原生 memory、SOUL、skills 与 connector 不迁移。40 个动作及字段见 [INTERFACE](docs/INTERFACE.md)。

有计划停用时，启用状态下调用 installation_prepare 保存逐任务续点并暂停经安装身份核验的任务，然后通过宿主原生入口停用/移除。普通卸载回调不暗中暂停任务。确认记忆时区分已保存、待批、提交未确认、失败、重复和未知；批准完成事件、provider 持久化证明、后台通知合并与所有直接停用路径清扫仍按公开宿主契约降级。

独立验收修复：installation_prepare 只接受明确指向 Muse 且与 operation 匹配的当前整句命令，例如“请停用Muse插件”或“请卸载Muse插件”；引用、询问、否定、延后和无关删除不构成授权。旧 saved 回执只可在原回合告知，staged 跨轮仍是待确认。source_check 的 CAS 和实际更新共用 canonical 来源解析。

**平台限制：** 安全文件访问依赖 POSIX，0.1.x 的 Windows 支持尚未恢复。Windows 暂不支持（依赖 POSIX 文件锁），CI 不覆盖；需要安全的原生句柄适配，不能靠取消 symlink 检查降级。

## 统一 Dashboard

default 通过 Hermes 原生插件管理安装并启用完整 Muse，作为本机 Dashboard 页面/API 的入口。页面仅列当前宿主已注册 profiles，明确选择后才读取 Feed、点子和目标。所有 profile 都只读浏览：页面及其 API 不能在任何 profile（包括本机其他 profile）安装、启用、停用或移除 Muse。要在其他 profile 使用 Muse，请在该 profile 用 Hermes 插件管理安装启用（如 `hermes -p <profile> plugins install …`），页面随后即可读取。页面、API、muse_manage 和子代理都没有跨 profile 管理能力。

terminal.backend=docker 只影响 terminal；Muse 工具及 companion skill 仍由 Hermes 宿主调度，使用同一当前 profile，无须新增挂载、复制或 Docker 路径分支。整个 Hermes 在独立容器/远端 HOME 中运行则是独立安装，同名 profile 不共享数据；页面不管理远端实例。

## 分页与维护上下文

context/status改为有界目录，按section/cursor读集合；长信号使用冻结window/through和正文分段，完整读取并实质审阅后才推进review_complete。并发新信号留下一窗口，修改/回填使旧窗口明确失效，时间戳相同的未读信号不能跳过。长会话保留未改变前缀的覆盖，新消息/被改内容必须补读；通知审核、静默时段和送达回执不放宽。Followup支持包含孤立回执的完整分页。原有Feed正文更正入口与finding创建absent用法已明确；详见companion references/context.md。旧版扁平context消费方须同步迁移。

## 有界响应与恢复

每个动作先校验整个请求（512 KiB、嵌套层数和控制标记），再记录、绑定或派发；响应上限 256 KiB。已提交的大结果以 `result_is_reference/content_ref` 返回，用 `content_read` 分页读全后再解析；大的原生错误仍是 `ok:false`，带有限的原因和 `error_ref`。`kind=tool_result` 是原工具响应的只读副本，仅限发起它的私聊会话按 ID 读取，不授权任何副作用，也不证明原生任务已完成。

参数错误以“可照着改”为目标：每个 `ok:false` 都会写明 action 和字段路径（如 `data.query_window`）、期望的类型/格式/可选值、实际收到的内容（类型和截断后的值）以及具体改法（例如“把 data.account、data.connection 移到 data.source”或“这不是日历来源：删除 data.query_window，把时间范围写进 data.scope”）；常见错误还带 `field`、`expected`、`received`、`hint` 和最小 `example`。放错位置的字段会指出正确位置，其他不支持的字段会列出该 action 允许的字段。解释器异常（`KeyError`、`TypeError`、`AttributeError`）不会返回给调用方：统一入口把它们转成带 action、字段和 schema 提示的 `invalid_argument`。`retryable` 只在按提示修正后重发可能成功时为 true（revision_conflict、revision_required、cursor_expired、source_unavailable）；其余校验错误都会说明原样重发仍会失败。`source_check` 可省略 `started_at`（默认取 Cron 运行开始时间，否则取调用时刻，并在结果里说明）；放在 `action`/`data` 旁边的参数会得到改好的调用示例。错误码和成功结果不变。

`delegate`、`research_start`、`watch_stop`、`operations_*`、`review_complete`、`run_complete`、`notification_*` 等动作可带 `request_id`（1..128 字符）：参数相同的重复调用返回原回执，参数不同则拒绝；执行中断时返回 `outcome_unknown`，不会重复执行，须先核对原生证据，不能换新键重派。

## 定时来源覆盖

定时检查的来源按用户的明确要求和该 profile 声明的职责决定。`PROACTIVE_PREFERENCES.md` 中可选的自由文本 `Coverage scope` 用来细化来源和排除项，不增加字段或运行时分支。没有排除项的通用助理仍保持完整的 30 天日历、分页、提前准备、邮件、提醒事项和出行覆盖；专项角色不会去读无关的个人来源。范围内的来源不可用或失败时如实记录缺口并尝试恢复。重载会刷新随插件发布的 Cron 提示词，保留已有偏好文字、任务 ID、频率、暂停状态和投递目标。模型是否照做仍需上线后观察，静态或隔离测试不能证明。

## 0.3.0目标迁移

安装时把旧GOAL文件与followup记录归档到`muse/archive/followup-0.2.x/`，
按root状态completed→done、cancelled→cancelled、active→active迁移，保留progress和owner。
纯读页面不会初始化或迁移。离线副本可用`python -m hermes_muse.migration --home <home>`，
追加`--rollback`可在目标未被新活动改动时回滚；先备份并停止写入，不整库覆盖。
回滚完成后保留原归档；再次升级使用新归档，前一轮存为`followup-0.2.x-<id>`。
Windows仍不支持。
