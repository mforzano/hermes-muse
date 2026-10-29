# Migrating external callers to v2

Personal cron prompts and scripts that call Muse must be rewritten before the cutover; v2 has no
deprecation period for removed actions. `python scripts/check_callers.py <prompt files and scripts>`
lists every dependency below and must pass on the rewritten versions (acceptance A11).

| 0.3.x dependency | v2 replacement |
|---|---|
| `muse_manage` `context` / `status` / `content_read` / `feed_list` / `conversation_read` | `muse_manage` `read` (kind, id, query) |
| `source_check`, `review_complete`, `run_complete` (bookkeeping from a personal job) | none: source watermarks belong to Muse's patrol packets, run outcomes are computed by code; drop the calls |
| `notification_*`, `feed_add`, `calendar_window` | none for external callers; notices come from the patrol packet, Feed articles from the feed packet; `calendar_view` stays |
| writing notes, dreams or alignment, or reading Muse state from a personal job | allowed as before: any cron run may call `muse_manage` `read`, `calendar_view` and `content_write` (kinds `note`, `dream`, `alignment`); every other write belongs to the private main session, and `muse_job` only to Muse's own runs |
| cron sessions found by id prefix `cron_<job id>_` | `sessions.source = 'cron'` and `title = '<job name>'` or `title LIKE '<job name> · %'`; keep the prefix for old rows |
| reading `muse/state.db` (`records` table) | `muse/state-v2.db` read-only (`entity(kind, natural_key, state, state_since, deadline, payload_json)`, `event`), or better `muse/health.json` |
| importing `hermes_muse.operations` / `context_pages` / `store` | `muse/health.json` (Health invariants, levels, evidence, fixes) |

Job ids do not change: the install refreshes the Muse jobs in place under their 0.3.x names
(`muse-memory-upkeep` → intake, `muse-proactive-watch` → patrol, `muse-nightly-review`,
`muse-feed-pulse`, `muse-weekly-governance-review` → weekly digest); their prompts become
`prompts/jobs/<job>.md`. `muse-monthly-system-audit` is paused, and `muse-health` is new.

Record kinds map to entity kinds as `notification` → `notice`, `interest` → `interest` and `idea`,
`source_check` → `source`, `memory_feedback` → `memory_receipt`, `operation_finding` → `finding`. `run`,
`signal` and `feed` keep their names; migrated rows keep their 0.3.x id in `payload.v1_id`. Status names
changed, for example a run is `ok/degraded/failed/abandoned` and a notice `drafted/scheduled/sending/sent/…`.
