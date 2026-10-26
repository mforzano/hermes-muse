---
name: companion
description: Goals, proactive reminders, calendar views, memory upkeep, Feed and research in the current Hermes profile.
version: 0.3.14
---
# Companion

Use the single muse_manage tool for Muse state. Skills explain procedures; they
do not perform IO. Hermes owns native memory, skills, providers and permissions.

Choose the relevant procedure:
- Accepted work and completion: [followup](references/followup.md), [goals](references/goals.md).
- Calendar windows and source coverage: [calendar](references/calendar.md), [proactivity](references/proactivity.md), [weather](references/weather.md) (only when `weather_context` is on).
- Content/pages and bounded context: [content](references/content.md), [context](references/context.md), [conversations](references/conversations.md).
- Memory feedback/forgetting: [memory](references/memory.md).
- Feed, research and reports: [Feed](references/feed.md), [research](references/research.md), [briefings](references/briefings.md).
- Result writing, self-review and governance: [communication](references/communication.md), [self-review](references/self-review.md), [governance](references/governance.md).

Own accepted outcomes: persist a goal, inspect results, explain them and mark done.
Ordinary questions do not need goals. Respect changed user intent; never resend an
external write with unknown outcome. Children/Cron inspect parent work but cannot
change parent goals, except the nightly review's capped goal_create and its cancel of a faded goal (see goals.md). A parent task_id does not make it a child.

Read context/status sections as needed and follow real pagination. Correct bad
arguments and recover transient reads; report genuine gaps. Do not broaden source
coverage beyond declared profile duties, or reduce in-scope coverage to save calls.
Check original intent against consequential writes. Native SOUL/memory ownership,
quiet hours, notification review/dedup and actual delivery receipts remain authoritative.

Use review_complete for each job's processed prefix. In Cron, run_complete records
complete/partial and remaining gaps; index reads are not substantive review.
At the start of each Muse Cron run, call muse_manage with
`{"action":"content_read","data":{"kind":"working_lessons","id":"current"}}`
and follow its pagination before acting. Apply the relevant verified conventions.
Hourly upkeep owns operation audit; other jobs check their own work. Keep Feed
silent and return patrol final_response exactly; use [SILENT] after useful checks.

Muse tools execute on the Hermes host/profile even when terminal.backend=docker.
A separate Hermes container is an independent installation. The authenticated
Dashboard manages only this host's registered profiles after explicit selection;
default installation management remains with Hermes. Never hand-edit state.db,
install.json or native Cron stores, and never invent a signal or delivery receipt.
