# Muse 0.3.12 interface

Single tool: `muse_manage(action,data)`. Profile/home come from Hermes, never caller parameters.
Goals need no request ID or caller scope token. Optional goal expected_revision is one integer; conflicts return current goal.
Content page revisions and non-goal mutation journals retain their existing bounded/concurrent-read contracts.

`request_id` is unique to each run: action plus run start time to the minute plus object or source label, never the date alone. The same key with different arguments returns `idempotency_conflict` with `suggested_request_id`, an unused replacement.

| Action | Fields | Write/version contract |
|---|---|---|
| `context` | `section job window cursor limit id offset length revision part` | Action-specific fields or pure read |
| `status` | `section job window cursor limit id offset length revision part` | Action-specific fields or pure read |
| `conversation_read` | `session_id revision offset read_only since until descriptor` | Action-specific fields or pure read |
| `content_read` | `kind id resource offset length revision cursor limit topic query part` | Action-specific fields or pure read |
| `content_write` | `kind id resource body expected_revision request_id signal_id` | Body revision + request ID |
| `memory_feedback_review` | `receipt_id decision reason expected_revision request_id` | Receipt integer revision + request ID |
| `installation_prepare` | `operation signal_id expected_revision request_id` | Installation revision + request ID |
| `goal_create` | `signal_id title description completion_criteria id parent_id deadline external_ref owner` | Private parent only; starts at revision 1 |
| `goal_update` | `id progress note status owner waiting_user blocked next_check_at expected_revision research_review_at description completion_criteria` | Private parent only; optional integer revision |
| `interest_record` | `signal_id title id expires rationale expected_revision request_id` | Request ID + natural key |
| `idea_add` | `title rationale goal_id sources expires expected_revision request_id` | Request ID + natural key |
| `feedback` | `kind id action signal_id until expected_revision request_id` | Required revision + request ID |
| `preferences` | `start_hour end_hour ordinary_per_day time_sensitive_per_day blocked_topics feed_brief deliver weather_context signal_id expected_revision request_id` | Required revision + request ID |
| `notification_add` | `event_key message rationale sources verified_at expires goal_id interest_id priority priority_reason issue_key source_snapshot supersedes signal_id watch_id final_result expected_revision request_id` | Request ID + natural key |
| `notification_refresh` | `id verified_at sources message priority priority_reason source_snapshot expected_revision request_id` | Required revision + request ID |
| `notification_review` | `id verdict reason expected_revision request_id` (`conversation_revisions` optional, ignored) | Required revision + request ID |
| `notification_resolve` | `id verified_at sources reason expected_revision request_id` | Required revision + request ID |
| `notification_prepare` | `id request_id` | Optional replay request ID |
| `notification_queue` | `id request_id` | Optional replay request ID |
| `notification_handoff` | `id request_id` | Optional replay request ID |
| `watch_create` | `goal_id signal_id schedule prompt stop_condition expires expected_revision request_id` | Request ID + natural key |
| `watch_stop` | `job_id request_id` | Optional replay request ID |
| `feed_add` | `event_key title content sources why goal_id interest_id expected_revision request_id` | Request ID + natural key |
| `feed_list` | `query topic cursor limit` | Action-specific fields or pure read |
| `feed_update` | `id signal_id feedback position delete title content sources expected_revision request_id` | Required revision + request ID |
| `calendar_window` | `mode` | Action-specific fields or pure read |
| `calendar_view` | `window sources discovery_complete` | Action-specific fields or pure read |
| `source_check` | `source started_at? status scope summary expected_resources checked_resources cursor query_window observed_at source_id alias_reason expected_revision request_id` | Required revision + request ID |
| `operations_read` | `through request_id` | Optional replay request ID |
| `operations_batch` | `request_id` | Optional replay request ID |
| `operations_evidence` | `receipt ref offset length limit session_id job_id since until` | Action-specific fields or pure read |
| `operations_complete` | `token summary request_id` | Optional replay request ID |
| `operations_commit` | `batch reviews request_id` | Optional replay request ID |
| `finding_record` | `id evidence summary impact status verification disclosure no_notice_reason expected_revision request_id` | Required revision + request ID |
| `review_complete` | `job up_to through summary goal_id stop_condition source_check_ids window request_id` | Optional replay request ID |
| `run_complete` | `outcome summary gaps request_id` | Optional replay request ID |
| `delegate` | `goal_id goal tasks context output_schema images action subagent_id message request_id` | Optional replay request ID |
| `research_start` | `items goal_id context request_id` | Optional replay request ID |
| `research_status` | `id request_id` | Optional replay request ID |
| `forget` | `kind object_kind id resource signal_id expected_revision request_id` | Required revision + request ID |

Due followups: `context(section=followups,cursor?,limit?)` returns active due goals and read-only owner status. Completed owners need parent review, not automatic goal completion.
Ordinary parent task_id values are accepted. Native session kind and delegated context reject child/Cron goal mutations.
Request budget512 KiB, final response256 KiB. Follow real cursors; oversized committed non-goal results expose content_ref.
Notification review, quiet hours, actual transport receipts and unknown-write no-resend behavior remain unchanged.
Offline migration/rollback: `python -m hermes_muse.migration --home <profile-home> [--rollback]`; back up and stop writers.

Errors: `{ok:false, error, code, retryable, ...}`. `error` is self-contained: action, field path (`data.source.account`), expected type/format/values, what arrived and the repair. Optional extra fields: `field`, `fields`, `expected`, `received`, `hint`, `example` (a minimal correct call), plus `current_revision`/`read` on revision errors. Arguments beside `action`/`data` are answered with the corrected call (the tool schema does not reject extra top-level properties, so this message reaches the model). `source_check` may omit `started_at`: Muse uses the bound Cron run's start (the call time outside a run) and adds `started_at_defaulted` to the result; an explicit value wins and is validated as before. Misplaced fields (for example `account`/`connection` beside `source`, `task` on `delegate`, `revision` instead of `expected_revision`) are answered with their destination; other unsupported fields list the action's allowed fields. Python exceptions never surface: `KeyError`/`TypeError`/`AttributeError` become `invalid_argument` naming the action and field. `retryable:true` only for `revision_conflict`, `revision_required`, `cursor_expired`, `source_unavailable`; other non-retryable errors state that an unchanged resend fails again. Codes and successful results are unchanged.
