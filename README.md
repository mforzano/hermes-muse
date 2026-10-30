# Hermes Muse 0.4.0

[简体中文](README.zh-CN.md) · [Design](docs/DESIGN.md) · [Interface](skills/companion/references/interface.md) · [Acceptance](docs/ACCEPTANCE.md) · [Changelog](CHANGELOG.md)

An assistant that only answers when you speak leaves a lot undone: the trip you mentioned on Monday, the reply you were waiting for, the decision you said you would make by month-end. Hermes Muse is a plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent) that lets Hermes follow up on those things itself. It was inspired by the proactive interactions in [Meta's Muse](https://about.fb.com/news/2026/09/introducing-muse-personal-ai-agent/).

In practice it looks like this:

- You say you want to pick a bicycle by month-end. Muse keeps it as a goal, checks on it, and closes it only when you say you have decided.
- A reply you were waiting for arrives, or a meeting moves. The next patrol reads the original mail or event with your tools and tells you what happened and why it matters, once.
- "Done", "later" and "stop bringing this up" each do something different. When there is nothing worth saying, it stays quiet, keeps your quiet hours and never repeats the same notice.
- A goal you stopped talking about gets one question before it is closed, instead of being chased forever.

It is built on what Hermes already has: sessions, cron, delivery, delegation and native memory, with your existing model and memory provider. This is an independent project, unaffiliated with Meta/Muse; its code and prompts are independently written.

**Version 0.4.0 is Muse v2**: a deterministic kernel keeps the books (watermarks, dedupe, state machines, delivery), the model only judges and writes, and an independent Health check makes failures visible. Upgrading from 0.3.x is automatic; read [the upgrade notes](CHANGELOG.md) first.

## Plugin pages

Muse's Dashboard and Desktop page lets you choose a profile and browse its Feed, Ideas and Goals inside Hermes, with the last Health check on top. It is read-only and follows the host theme.

![Muse overview with Feed article list](docs/images/muse-dashboard-overview.png)
*Choose a profile, browse or search its Feed articles, and switch to Ideas or Goals. All screenshots use demo data.*

![Muse Feed article body](docs/images/muse-feed-article.png)
*Open a Feed article to read its Markdown-rendered text, with its sources listed below.*

![Muse Ideas](docs/images/muse-ideas.png)
*Ideas groups suggestions by the goal they relate to.*

![Muse Goals](docs/images/muse-goals.png)
*Goals lists open goals first, with status, next check and latest progress; done and cancelled goals are folded below.*

## Install

```sh
hermes plugins install xiaohei-info/hermes-muse --enable
```

Installation uses the selected profile (`hermes -p <profile> plugins install …`); each enabled profile has its own state, jobs and delivery. If you maintain explicit `platform_toolsets` lists, include `muse` where you use the plugin.

Requires Hermes 0.21.5 or later with the plugin APIs for tools, Skills, prompt sections and hooks; tested on Hermes 0.22.5 ([baseline](docs/ACCEPTANCE.md)). First load registers one Skill, two tools (`muse_manage`, `muse_job`), three hooks and one system prompt section, creates five recurring jobs and the `muse-health` check, and adds missing content files from `templates/` without overwriting anything. Start a new conversation to load the prompt section; background work needs a running scheduler and an available model, at their normal cost.

**Upgrading from 0.3.x** (0.3.1 from the catalog or any later 0.3 release): the first load migrates `muse/state.db` into `muse/state-v2.db` and refreshes the jobs in place, keeping their ids, schedules, routes and pauses. Rewrite personal cron prompts or scripts that call removed actions first ([caller migration](docs/CALLER-MIGRATION.md)), and follow [cutover and rollback](docs/CUTOVER.md) to go step by step or back to 0.3.x.

## How it works

| Job (cron key) | Default schedule | What the model does | Delivery |
|---|---|---|---|
| intake (`memory-upkeep`) | hourly | decides what each new private message means and which goal it touches | local |
| patrol (`proactive-watch`) | every 30 min | reads your sources with your tools, judges what is worth a notice and writes it | your route |
| nightly (`nightly-review`) | 03:20 | assesses due goals, judges review candidates, writes dream and alignment | local |
| feed (`feed-pulse`) | 08:00 | writes up to 3 Feed articles for active interests | silent |
| weekly (`weekly-governance-review`) | Sunday 21:15 | one digest from the Health summary and 7-day counts | your route |

Each job's launcher first runs the kernel and builds a bounded packet; intake, nightly and feed leave the model asleep when their packet is empty, while patrol wakes every period because only the model can read your sources. Code captures messages, derives every id and key, dedupes notices against 7 days of history, applies quiet hours and budgets, delivers, reads delivery receipts, moves next checks, and asks about and closes dormant goals. The model returns only references, enum values and text. Behavior lives in `prompts/jobs/<job>.md`; fields in the generated [interface](skills/companion/references/interface.md). See the [design](docs/DESIGN.md) and [feature coverage](docs/FEATURE-COVERAGE.md).

Only private conversations on user surfaces (CLI, TUI, desktop, phone and messaging DMs listed in `USER_SOURCES`) are read and can change Muse state. Cron runs, delegated children, group chats and unclassified sources cannot.

## Data sources

Muse has no connectors. The patrol reads mail, calendars, reminders and the web only through tools your Hermes already has: Hermes built-ins first (`web_search`/`web_extract`, `google-workspace`, `himalaya`, `apple-reminders`, `imessage`, `apple-notes`), then [Apple PIM](skills/companion/references/source-apple-pim.md), then your own sources recorded in the profile's `muse/TOOLS.md` ([sources](skills/companion/references/sources.md)). Each check is recorded per source with its minimum interval; a source that is not connected or keeps failing shows up in Health. After installing, verify each source with the [source verification](skills/companion/references/source-verification.md) procedure.

## Preferences

Ask in chat, or edit `muse/PROACTIVE_PREFERENCES.md`. Fields: `start_hour`/`end_hour` (quiet hours outside them, default 9–22), `ordinary_per_day` (default unlimited), `time_sensitive_per_day` (default 3), `urgent_per_day` (urgent notices that may skip quiet hours, default 2; more are gated as time-sensitive), `blocked_topics`, `deliver` (route, default this profile's Bot Chat), `goal_dormant_days` and `goal_fade_days` (default 7 each; `null` turns them off), `fade_question` and `weather_context` (off by default; it uses a location you gave). Invalid preferences stop delivery with an error instead of guessing.

## Health

`muse-health` runs every 30 minutes without a model and writes `muse/health.json`: host probes plus invariants over signals, packets, notices, deliveries, runs, goals and sources, each with a level and a fix. A high item appears in your next private conversation and on the Muse page; the cron prints when the alert set changes; the plugin setting `health_deliver` (under `plugins.entries.hermes-muse.settings`) also sends it to a second route. Health never goes through the notice pipeline and never repairs anything. Ask "what is Muse's health?" or `muse_manage read(kind=status)`.

## Use

Talk normally in a private conversation, for example:

```text
Create a bicycle-purchase goal: budget $1,000, decide by month-end.
Check for a reply to this message until Friday.
I follow space exploration; important developments are welcome, no daily digest.
This reminder is done. / Stop reminders about this topic.
Show my calendar for the next 30 days.
List my goals, interests and pending notices.
Forget what I told you about the move.
```

Goals need a clear plan or commitment from you; passing interests expire after 14 days unless you bring them up again. Ask what was checked, what failed or what is still open; the assistant reads Muse state instead of guessing.

## Files

Under `<profile home>/muse/`: `state-v2.db` (Store v2, written only by the kernel), `health.json`, `workspace/goals/<slug>/GOAL.md` (goal source of truth), `memory/`, `dreams/`, `memory/people|groups/`, `workspace/your_files/feed/*.md`, `AGENTS.md`, `TOOLS.md`, `PROACTIVE_PREFERENCES.md`, and after an upgrade the read-only `state.db` and `backup/cron-prompts-0.3.json`. Launchers are `<profile home>/scripts/hermes-muse-*.py`. Muse never edits SOUL, `memories/`, `cron/jobs.json` or Hermes's databases. Retention: [compatibility and retention](docs/CATALOG-COMPATIBILITY.md#retention), [SECURITY.md](SECURITY.md).

## Remove

Ask in a private conversation to remove Muse. The assistant runs `installation_prepare`, confirms with you and pauses every Muse job (`preferences pause_jobs=true`); then run:

```sh
hermes plugins remove hermes-muse
```

Launchers of a disabled or removed plugin stay silent. Data stays in `<profile home>/muse/`; reinstalling picks it up again.

## Development and verification

```sh
export TMPDIR=$(mktemp -d) HERMES_HOME=$(mktemp -d)
python scripts/check_boundaries.py
python scripts/check_callers.py prompts templates skills after-install.md
PYTHONPATH=<hermes-agent> <hermes python> tests/kernel_acceptance.py   # and the other tests/*_acceptance.py
```

The full list, with what runs without Hermes, is in [ACCEPTANCE.md](docs/ACCEPTANCE.md); contributor rules are in [AGENTS.md](AGENTS.md). Tests use temporary profiles and synthetic data, with no model calls or real deliveries.

Muse is listed in the official Hermes plugin catalog: `hermes plugins install hermes-muse` installs the reviewed version pinned there, which can lag behind this repository.

MIT licensed.
