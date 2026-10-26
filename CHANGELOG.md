# 0.3.14 — goal fade: dormant after 7 idle days, cancelled 7 days later after a delivered question (2026-10-26)

Goals never left `active` by themselves, so abandoned ones kept attracting follow-ups and patrol attention. Released together with 0.3.13 (its entry below).

- Dormancy is derived, not stored: status stays active/done/cancelled. An active goal with no activity for `goal_dormant_days` (default 7) is dormant. Activity is the latest of creation, any `goal_update` (progress, owner, deadline, next check), an active subgoal's activity, the moment a deadline or watch that exempted the goal ends, and a user message containing the goal's ID or title (3+ characters). `research_review_at` is unchanged.
- Dormant goals leave `context(section=followups)`, the "need attention" hint and the wake gate's due-goal check and active-goal count; the patrol prompt says to leave them alone. They stay in `context(section=goal)` with `dormant_since`, `fade_at`, `fade_due`, `ask_key`, `fade_wait`; the new `status` argument of `context` (`open|closed|dormant`) filters goals.
- Exempt: `no_fade: true`, a future deadline, an unexpired watch (checked through `Host.is_active_watch`), a running owner, or an active subgoal that is exempt (a parent never fades into cancelling a protected subgoal). `waiting_user`/`blocked` goals are not exempt. Anything that cannot be read (damaged preferences, signal store, watch state) means the goal does not fade.
- `no_fade` (new optional boolean on `goal_create`/`goal_update`, parent conversation only, never Cron): keeps a standing goal, such as a long-term one reported by a scheduled job without a deadline, from ever going dormant or being cancelled.
- Nightly review: new "Goal fade" step asks once per dormancy episode (`notification_add`, `event_key` = the row's `ask_key`, which contains the dormancy start date, so a revived goal that goes dormant again can be asked again) and cancels with the note "no user activity for N days; auto-faded" through the normal cancel path (subgoals, owned watches, pending notices; external agents untouched). `fade_due` needs `goal_fade_days` (default 7) after dormancy **and** the question to exist, to have left the candidate/pending/queued states and to be at least a day old, so the first run after an upgrade only asks and never asks and cancels in one night. A question that is never delivered leaves the goal dormant, not cancelled. Dormant goals are not researched; the run summary gains a Goal fade sentence.
- Guard: the nightly-review Cron (Cron session plus host-bound running nightly execution, never `task_id` alone) may also send `goal_update` with only `id` and `status="cancelled"`; the service recomputes eligibility and refuses anything else with an error naming the field, expectation and reason. Everything else stays parent-only. `goal_dormant_days` and `goal_fade_days` cannot be written from Cron (eligibility is computed from them); the error says to describe the proposal in the run summary. The parent revives a faded goal with `goal_update(status="active")`.
- Preferences `goal_dormant_days`, `goal_fade_days`: whole days 1-365, `null` turns dormancy (respectively automatic cancel) off; invalid values get a repairable `data.<field>` error.
- Stored Cron prompts are rewritten when the plugin loads, not when Cron runs: the new nightly wording takes effect only after a Gateway restart.
- Tests: `tests/test_goal_fade.py` and `tests/native_goal_fade.py` (timeline day 6/7/13/14, activity sources, lapsing exemptions, exempt subgoals, ask-before-cancel with a five-goal backlog, fail-safe reads, episode keys, `no_fade`, Cron refused for fade preferences through real dispatch, the guard and revival).

# 0.3.13 — goal intake safety nets (2026-10-26)

Goals were created only when the assistant remembered to call `goal_create` during a chat turn, so accepted requests and promises could go unrecorded and the patrol never saw them.

- Per-turn check: the `[Muse] User signal` hint now also says: if you accepted a concrete request or promise in this turn, make sure an active goal covers it (`context section=goal`) and create one if not; casual questions and passing mentions do not become goals. It lists the titles (up to 5) of goals created in the last 24 hours in this conversation (newest first) so duplicates are visible. No new storage.
- Nightly intake step in the existing nightly-review job (no new Cron job): from the window's signals and conversation text it finds accepted requests or promises without a matching goal, is told to dedupe against active, done and cancelled goals itself (prompt-level, not enforced in code), and calls `goal_create` with `signal_id`, a title, a description citing the basis in a short paraphrase and `completion_criteria`. At most 5 per run; borderline items are skipped; it never updates, completes, cancels or re-owns a goal. The run summary states how many were created or why none.
- Guard exception: `goal_create` is allowed from the nightly-review Cron and nothing else. Identity is the Cron session plus the host-bound running nightly-review execution (`task_id` is only used to name the job, since the main session also carries one). `signal_id` is required and the cap is 5 per execution, counted in the run record and released if the create fails; the goal belongs to the signal's conversation. `goal_update`, `forget` of a goal, other Cron jobs and children remain rejected. New errors name the field, expectation and fix (`data.signal_id`; `size_limit` on `data` for the cap).
- Stored Cron prompts are rewritten when the plugin loads, not when Cron runs: the new nightly wording takes effect only after a Gateway restart. The guard and hint change apply once the plugin is reloaded.
- Tests: `tests/native_goal_intake.py` (real dispatch: main session with a Cron-looking `task_id` still allowed, nightly create/cap/signal, nightly `goal_update`/`forget` rejected, other Cron jobs, task-less Cron, ended execution and child rejected) and unit tests for the hint and the prompt.

# 0.3.12 — contextual weather recipe (PR #2), behind an off-by-default privacy switch (2026-10-25)

Merges community PR #2 (feat(proactivity): add contextual weather correlation recipe and guidance). Thank you, BrunoBza — the original commit and authorship are kept in the merge history.

- New optional preference `weather_context` (boolean, default `false`) in `PROACTIVE_PREFERENCES.md`, settable through `muse_manage preferences`. A location is private data, so weather is opt-in: when it is off or absent (every existing install after upgrade) the patrol skips weather completely — no location lookup, no request to any weather service.
- When `true`, the patrol reads the new `references/weather.md`: the Open-Meteo recipe and the PR's thresholds (precipitation probability >= 70% or severe weather, or a >= 5°C shift, overlapping a real event, trip or routine, with concrete advice). The recipe moved out of the `TOOLS.md` template and the thresholds out of the default proactivity text so nothing weather-related runs by default.
- The location must come from a place the user explicitly gave or an authorized device-location tool; the plugin never obtains, stores or forwards it. Turning the preference on sends that place to a third-party weather service.
- Invalid values are rejected with a repairable `data.weather_context` error (`invalid_argument`, expected, received, fix). The `PROACTIVE_PREFERENCES.md` template is unchanged so the byte-compared default migration keeps working.
- Tests: the PR's three tests now follow the switch; added default-off, on, invalid-value and template-migration tests.

# 0.3.11 — delivery follows /sethome; configurable destination (2026-10-24)

Merges community PR #1 (feat(delivery): support dynamic /sethome delivery and configurable destination). Co-authored with BrunoBza — thank you for the contribution; the original commit and authorship are kept in the merge history.

- New goal watches pick their destination through `default_deliver()`: the `deliver` preference if set, else `all` when a gateway home channel exists (`/sethome`), else `bot-chat`. With no home channel (desktop-only profiles) behaviour is unchanged.
- Fixed jobs (governance reports, proactive-watch) use the `deliver` preference or `bot-chat` when first created. The PR as submitted also looked up home channels while the plugin loads; on a gateway with platform plugins that lookup waits on the load already in progress, so Hermes abandoned the plugin after 10 s (`load timed out`; `native_smoke.py` reproduced it, 0.3.10 passes). Plugin load no longer queries home channels.
- New optional `deliver` preference in `PROACTIVE_PREFERENCES.md`: `all`, `bot-chat` or one platform such as `telegram`; `null` restores automatic selection. It applies to jobs created afterwards; existing jobs keep the destination they have. It is accepted by `muse_manage preferences` (added to the action contract).
- Repairable error for `data.deliver` in the same style as the other preference errors (`invalid_argument`, field, expected, received, fix). Comma lists are rejected: a list containing `bot-chat` would skip the Bot Chat envelope and receipt tracking.
- Notification flow checked for `all`/platform destinations: the notice is bound, rendered as plain text without the Bot Chat envelope, and reconciled through the native delivery queue (`delivered`→sent, `queued`→transport_queued/sent). An unresolved `all` target (home channel removed) ends as unknown and is never replayed.
- Tests: the PR's four tests no longer need a Hermes install or a fixed `/tmp` path; added flow, null-reset, load-time and contract tests.

# 0.3.10 — source_check fills started_at; stray top-level arguments get a usable error (2026-10-24)

Cause: from 2026-10-23 the half-hourly patrol failed whole rounds on weaker models. The commonest error was `source_check` without `data.started_at` (1-12 times per round), repeated verbatim until the tool-loop guard stopped the run; the other was arguments such as `section` or `body` placed beside `action`, which the host rejected with a bare `additionalProperties` message before Muse ran.

- `source_check`: a missing `data.started_at` is filled by Muse. Inside an owned Cron run it is that run's start time; otherwise the time of the call. A value you send is used as given and keeps its validation (not in the future; an older one than the stored check of the same source leaves that check unchanged; an equal one is accepted). Calendar sources with `query_window` are unchanged. The result carries `started_at_defaulted` saying which default was used; the stored record and every existing field and error code are unchanged. The default is applied after the request journal signature, so replaying the identical call later is still a replay.
- `muse_manage` tool schema no longer sets `additionalProperties: false`. The host validated that before the handler ran, so the model only saw a bare "Additional properties are not allowed". The handler now answers stray top-level arguments with the corrected call (`move limit, section inside data, e.g. {...}`), names the valid actions when `action` is wrong too, and says an unchanged resend fails again. Error codes are unchanged. A wrong `action` value is still rejected by the host's enum check.
- `prompts/proactive-watch.md`: the sentence telling the model not to rediscover tool schemas is replaced by "read the Muse field lists in the companion references before writing records". New paragraph: build `source_check` and other records from `references/proactivity.md`, keep every argument inside `data` (`section` for context/status, `kind`/`id` for content_read/content_write), omit `started_at` unless overriding, read the `Fix:` text and `example` of an error and never resend identical arguments. The first line and the other prompts are unchanged. A changed Cron prompt reaches stored jobs only after the Gateway restarts.
- Docs: `references/proactivity.md`, `docs/INTERFACE.md`, README.
- Not fixable here: `tool_call calls[0] requires a 'name'` is raised by the host's `tool_call` wrapper before any tool runs.

# 0.3.9 — nightly review has a real Ideas step (2026-10-23)

Cause: Ideas were one clause at the end of the long goal-research paragraph of the nightly prompt, after many required steps. Four consecutive nights ended without any Ideas judgment or trace, so it was not "considered and declined" but never reached.

- `prompts/nightly-review.md`: Ideas are now their own short step after goal research, closure checks and the reflection/alignment rule, before the closing paragraph. Scope is limited to active goals and interests, recent Feed and recently verified user signals; 0-3 Ideas, no quota, duplicates skipped via `context(section=idea)`, fields `title`, `rationale`, `sources?`, `goal_id?`, `expires?` (plus `request_id` and `expected_revision`), and an Idea is never execution authority. The step is explicitly after reflection and alignment, so those still come first.
- The nightly `run_complete` summary must contain one sentence on Ideas: how many were proposed, or why none. This does not force a number; it makes a night without Ideas traceable.
- `idea_add` errors: `text`/`content`/`description` point to `title` + `rationale`, `reason`/`why` to `rationale`, `source` to `sources`, `kind`/`type` say the action takes no kind (and that reading uses `kind="idea"`, singular). The error carries a full `idea_add` example.
- Docs: companion `goals.md`, DESIGN. Other steps and their order are unchanged.

# 0.3.8 — a proactive-watch round's source_checks write right the first time (2026-10-23)

Cause: the 13:00 patrol sent six source_checks in one parallel batch; each tripped on a different avoidable mistake, and the tool-loop guard stopped the run after eight failures.

- `source_check`: `account`/`connection`/`resource` repeated beside an identical `data.source` are ignored instead of rejected (a differing value is still an error and says to delete the top-level copy because `data.source` already holds it). For a calendar source `started_at` and `observed_at` may be omitted; both are filled from `query_window.observed_at`. A value you send that disagrees is rejected with "delete data.started_at and data.observed_at".
- `query_window` is compared on its machine fields only; the descriptive `timezone_note` (models retype it) is ignored and the stored window is the canonical one. Any other change is still rejected and named.
- A `source_check` rejected for invalid arguments no longer uses up its `request_id`: correcting the arguments and resending under the same key works. Replays and different arguments after a successful write still return the same result / `idempotency_conflict`; other actions keep journaling rejections.
- A stale `expected_revision` that is shorter or longer than a real revision says it was truncated or retyped, next to the existing `current_revision`.
- `content_read`: `data.kind` typos get a suggestion (`ideas` -> `idea`); an unknown object id says which kind and how to list valid ids instead of "Object unavailable".
- Considered and not done: a stored coverage handle for `source_check`. The window is derivable from `calendar_window`, so the remaining hand-typed values were prose and timestamps, now optional or ignored; a handle would add a persisted object with its own expiry for no further one-shot gain.

# 0.3.7 — request_id is unique per run (2026-10-23)

- Cause: the `request_id required` error and the docs showed a date-only key (`source_check-20261017-mail-icloud`). A job that runs many times a day copied the pattern, so its second run reused a key with different arguments and failed with `idempotency_conflict` until the tool-loop guard stopped it. Idempotency itself is unchanged.
- The missing-`request_id` error, the `source_check`/`feedback`/`feed_add`/`notification_add`/`watch_create` examples, the companion references, the README/INTERFACE and every background prompt now say a key must be unique to each run (action plus run start time to the minute plus a label) and must not be reused from an earlier run or the same day.
- `idempotency_conflict` (CAS actions and replayable actions) returns `suggested_request_id`: the old key plus the current time and a random tail, unused and within 128 characters, and the message tells the model to resend the same arguments under it. `revision_conflict` asks for a run-unique new key.
- `calendar_view`: a text entry in `data.sources` now also says it usually means stray text or an unbalanced brace in the array.
- Considered and not done: generating a `request_id` when it is missing. `source_check` replay and interrupted-call recovery depend on the caller-held key, so an invented key would silently drop that guarantee and change a documented contract; `expected_revision` is untouched.

# 0.3.6 — actionable argument errors (2026-10-23)

- Every `muse_manage` argument failure now names the action and field path (`data.query_window`), the expected type/format/values, what was received (type and clipped value) and the exact repair; common ones add `field`, `expected`, `received`, `hint` and a minimal `example`. All missing required fields and all unsupported fields are reported at once. Misplaced fields get their destination (`account`/`connection`/`resource` into `data.source`, `task` on `delegate` into `goal`/`tasks`, `revision` into `expected_revision`); other unsupported fields list the allowed ones.
- `source_check`: a text `query_window` (or any `query_window` on a non-calendar source) says to delete it and put the time range in `scope`; calendar sources are told to pass the `calendar_window` object unchanged. Calendar `window`/`sources` are checked with indexed paths (`data.sources[0].inventory`) and name the keys that differ from the `calendar_window` result.
- The tool entry point converts `KeyError`/`TypeError`/`AttributeError` into `invalid_argument` with the action, field and schema hint; interpreter text such as `'str' object has no attribute 'get'` or a bare `'window'` is no longer returned. Revision, request_id and cursor errors say how to recover (new `request_id` plus the current `expected_revision`, or drop the cursor).
- `retryable` is accurate: true for `revision_conflict`, `revision_required`, `cursor_expired`, `source_unavailable`; other validation errors say an unchanged resend fails again. Error codes and successful results are unchanged.

# 0.3.5 — wake gate: judge change, not standing conditions (2026-10-21)

- Wake gate (introduced in 0.3.4, still off by default): a follow-up or review forces a run only when it became due since the job last woke; a goal that has been overdue for days no longer wakes every tick (Cron cannot clear it, which would have disabled the gate on real profiles).
- Each job asks its own question and receives only the data it needs: the patrol gets new messages and follow-ups or deadlines due within two hours; memory-upkeep gets new messages; feed-pulse gets new messages, interests and article ages. Stale source checks are no longer part of the summary. In a calibration on synthetic summaries, empty and acknowledgement-only cases scored 0.03-0.11 and substantive ones 0.46-0.97.
- `min_probability` stays 0.15.

# 0.3.4 — optional wake gate for the high-frequency jobs (2026-10-21)

- New, off by default: `plugins.hermes-muse.wake_gate` in the profile `config.yaml`. With `mode: typesafe`, the pre-run script of proactive-watch, memory-upkeep and feed-pulse sends a bounded summary of what the run would review (new user messages, goals, interests, source-check ages) to a TypeSafe System One model and skips the run when the probability that it finds something worthwhile is below `min_probability` (default 0.15). With `mode: off` nothing changes.
- Safe by construction (0.3.5 corrects the due-goal rule below): the gate only reads (it never advances a cursor or consumes a signal, so a skipped run's signals are seen next time); a missing key, timeout, HTTP/parse error or unreadable state wakes the agent; due goals, live notices, an explicit Feed request and a full message backlog wake it without asking the model; a job is never skipped for longer than `max_skip_minutes` (default 120) because mail and calendars are only visible to the agent.
- One metadata-only line per decision (job, probability, skipped, reason, milliseconds, never message text) goes to `logs/hermes-muse-wake-gate.jsonl` in the profile.
- Enabling it sends the summary, including clipped message text unless `include_message_text: false`, to the configured endpoint. See the README.
- No change to schedules, tools, state schema or the other jobs.

# 0.3.3 — The main conversation delegates multi-step work and stays responsive (2026-10-21)

- Delegation guidance: the main conversation replies first and plans, dispatches, follows up, reviews and reports; it does directly only what one or two steps settle and delegates multi-step, waiting or bulky-output work asynchronously with context and completion criteria. The host chooses the executor. Delegation is not narrated (executor, method, IDs, waiting) unless it matters to the user: a non-default model/effort, a needed action or decision, or a blocker/tradeoff. Prompt text and the companion communication/research references are aligned; no tool, schema or state change.

# 0.3.2 — Cron no longer depends on a workdir (2026-10-20)

- Muse Cron jobs no longer set `workdir`. A host path there made every terminal command fail (`cd: No such file or directory`, exit 126) when the profile's terminal backend is Docker or another remote/container backend.
- Job ownership is the installation-scoped prompt marker recorded in `muse/install.json`; the workdir comparison is gone (host, installation prepare/disable/remove).
- Upgrade: on load, owned 0.3.1 jobs (fixed jobs and watches) have their workdir cleared in place. No new jobs, and schedules, delivery routes, pauses, models and one-shot watches are preserved; repeated loads are idempotent.
- Closing a goal no longer fails when one of its watches is a finished one-shot (`pause_owner` skips completed/error jobs).
- Working lessons: without a workdir Hermes no longer injects `muse/AGENTS.md` into Cron prompts, so the Skill tells each Muse Cron run to read `content_read(kind=working_lessons)` first.

# 0.3.1 — delivery gate, watch floor, read-only Dashboard, page redesign (2026-10-17)

- Dashboard/Desktop page: theme-variable styling, shared components, tabs with counts, clickable Feed rows, safe Markdown detail pages with a separate source list, grouped Ideas with an explanatory empty state, and open-first Goals with a folded closed section.
- Delivery gate: notice review needs only each session's newest 24,000 characters served; a review records each session's newest message and later preparation holds only on newer messages, returning the exact conversation_read calls for that new text. Unavailable unlinked sessions no longer block. A reviewed-relevant notice held for an hour by new messages or an active conversation is released to the remaining gates (quiet hours, allowance, snooze, topic opt-out, dedup).
- New repeating watches: `N/step` cron terms expand like croniter (`0/5 * * * *` is refused; `0 0/1 * * *` is hourly and allowed).
- Dashboard: the cross-profile install/enable/disable route and UI are removed; profile selection is read-only for every profile. Install Muse in another profile with Hermes plugin management.
- Error guidance: missing expected_revision returns revision_required with current_revision (and source_id); request_id errors explain both fields; source_check list rows include source; unknown context sections list valid ones; followups cap limit at 50; duplicate calendar sources explain the merge; calendar_view proposals carry expected_revision; notification_add accepts topic; the tool description puts every argument inside data.
- CAS errors also return the exact content_read call (notifications are kind=notice) and reject a non-string expected_revision with the current revision string; notification_resolve accepts the same 60-second verified_at clock skew as add/refresh; unknown content kinds list valid kinds.
- Reconcile no longer rewrites unchanged notices (which expired notice list cursors); tools notes accept recipe prose such as "authorization: `tool(...)`"; briefings write the report before optional bookkeeping.
- Read-only list projections: goals `status=open|closed` filter, state/time/latest-progress fields and open-first ordering; ideas `last_review_at`; feed/idea `source_count`.

# 0.3.0 — simple goals (2026-10-16)

- Replace execution/result/delivery followup state machines with active/done/cancelled goals, optional owner handles, short due summaries and one optional integer write revision.
- Breaking: remove followup_update (including rebind/reconcile/repair), orphan receipts, reply-hash handoff checks, newer-input freezes, signal age gates and caller scope_token fields. Use context(section=followups) and parent goal_update instead.
- Archive original 0.2.x goals/records on first load. Back up and stop writers before offline rollback: run python -m hermes_muse.migration --home <profile-home> --rollback using 0.3.0, then restore the prior plugin code. New goal edits refuse rollback; completed cycles remain archived on a later upgrade. Preserve profile/child/Cron boundaries, notification delivery and unknown-write safety.

# 0.2.4 — historical followup rebinding (2026-10-15)

- Enforce exclusive successor ownership from existing goals, retain delivery evidence by rejecting conflicting rebinds, and require strictly later receipt/result timestamps.

- Add parent-only historical reply/successor binding through `followup_update(kind=rebind)` with exact native evidence, CAS, idempotency and repair preimages/conditional rollback.
- Keep original execution receipts and unknown delivery semantics; no dispatch, automatic delivery inference or automatic production migration.

# 0.2.3 — followup evidence convergence (2026-10-15)

- Converge native delegation terminal evidence through existing hooks; retain interrupted/failed distinctions and unknown safety.
- Replay explicitly reviewed original-session handoffs without claiming transport delivery; unreviewed summary/path mentions never prove delivery. Settle only complete literal inquiries from a closed set without goals; arbitrary prefixes/topics and mixed instructions remain pending for explicit review.
- Fail closed on corrupt/conflicting durable evidence. Expose newer-input blockers and support goal-scoped semantic unrelated reviews without hiding older cancellations.
- Add bounded followup reconciliation dry-run, per-object CAS apply/preimage backup and conditional rollback in the existing tool/store.
- Retain current-profile/child protections, cancellation, budgets and no-redispatch behavior. No production migration or deployment implied.
- v0.2.3 is the first public release since 0.1.27; 0.2.0–0.2.2 were developed on the same branch and ship with it.
- State that Windows is not supported for now and drop it from the CI matrix.
- Document the 30-minute floor for new repeating watches and the parent-model default for workers consistently, and add regression tests for the floor, Cron `feed_brief` edits not waking `feed-pulse`, and the unit suite on hosts without Hermes core or with symlinked temp directories.

# 0.2.2 — bounded context and maintenance evidence (2026-10-15)

- Discoverable context/status section pages and frozen, reference-only signal review windows; reject unread checkpoint advances.
- Current conversation descriptors and unchanged-prefix coverage let long evidence reads survive appends without skipping corrections.
- Page oversized object metadata, return reference receipts for oversized committed mutations, and preserve CAS/idempotent replay.
- Fix orphan-only followup continuation, bind pages to selection revision; document existing Feed correction and finding absent creation.
- Align scheduled source coverage with explicit profile duties and optional preference prose; keep the full general-assistant default and all existing execution/delivery safeguards.
- Keep default Dashboard/profile management, child read boundaries, notification budgets and transport semantics. No deployment implied.

# 0.2.1 — unified local profile Dashboard (2026-10-15)

- Full default installation hosts the page; explicit registered-profile selection before content reads.
- Backend-enforced default read-only administration; native install/enable and data-preserving disable for other profiles.
- Reuse owned-task preparation, retain user pauses/cadences, isolate lifecycle processes and credentials, and keep GETs free of initialization.
- Clarify host-dispatched Muse tools with Docker terminal and independent remote Hermes instances. Windows compatibility remains unresolved.

# 0.2.0 — approved implementation (2026-10-15)

- Scoped content tools, explicit CAS/request keys, pure read projections and plugin-owned UI.
- Correlated memory feedback, current audit identities and replayable delivery evidence.
- Explicit installation preparation, public native delegation, conservative watch minimum and Cron wake protection.
- Breaking tool response migration: feed_list returns a paginated object; mutable actions require revision/request keys. See docs/INTERFACE.md.

# Changelog

## 0.1.27 catalog compatibility

Precise soft-inbox retention and instructed-read-only disclosures; Cron watch scheduling policy and explicit 4,000-character fields; native 30-minute default without changing explicit cadences. Conversation previews gain complete revision-checked chunk retrieval and range coverage that gates notices without advancing processing watermarks. Research/audit respect lower user concurrency while retaining every question/page. Public Cron delegation is used for compatible unpinned routes; pinned/unknown parent-routing exceptions remain explicitly unresolved. Includes the previously local 0.1.26 follow-through and calendar/Apple work, without deploying existing installations.


## 0.1.26

- Automatically retain real-turn review candidates and inject bounded due references through the native user-message hook; candidates never grant execution authority.
- Persist accepted-goal native dispatch intent before launch, observe exact receipts, recover a missing handle from a bounded native journal read, and reject unknown relaunches.
- Reuse the six Cron pre-run outputs for recovery/verification/handoff without changing cadence, source sweeps or calendar coverage.
- Separate owner execution, business verification and native notice delivery; add current, concrete user-handoff guidance in communication.
- Preserve legacy goals, user overlays, notification rules, profiles, native identity/memory and prompt-cache history.

## 0.1.25

- Have receiving conversations retrieve original source details when an incomplete handoff prevents a useful judgment, with truthful access failures and unchanged external-write authority.
- Preserve actionable source identities, timestamps and locators in prepared findings.
- Add a provider-neutral briefing procedure for user-subscribed native reports, sharing Muse source and communication rules without adding a fixed Cron or silently adopting existing jobs.

## 0.1.24

- Route Muse Cron worker creation through a plugin entry that inherits the actual parent model and connection; keep ordinary conversation delegation settings unchanged.
- Use the same entry for parallel self-review and direct research workers, joining results within the Cron run. Preserve native reasoning, permission and execution limits.
- Update the existing Skill and prompts; no new Cron jobs, profile configuration changes or state migration.

## 0.1.23

- Batch self-review into disjoint assignments for native parallel workers, keeping raw evidence out of the parent conversation.
- Add scoped evidence indexes and full chunked expansion; retain original intent and linked call results.
- Commit numeric batch/slot results using server-held receipts, advance only contiguous reviewed work, and resume unfinished assignments after interruption.
- Save partial progress before the host iteration budget is exhausted; keep model choice, source coverage and six fixed schedules unchanged.

## 0.1.22

- Mark off-schedule wakes as native manual runs so cron-expression repair does not silently discard quiet-time memory upkeep or Feed preference refreshes. Preserve recurring schedules and user-paused state.

## 0.1.21

- Give operation-review pages independent receipts; preserve evidence during concurrent reads and merge repeated/overlapping completions without losing progress.
- Keep failed tool reads visible while excluding successful Skill/tool-discovery boilerplate from historical audits.
- Assign the shared operation audit to hourly upkeep. Patrol and Feed verify their own work; nightly review writes confirmed alignment without waiting for the backlog.
- Track actual task stages and partial completion separately from native Cron exit status. Require hourly audit completion and fresh nightly artifacts before recording full completion; preserve recovery checkpoints.
- Keep the same six fixed jobs, schedule, source coverage, models and delivery controls.

## 0.1.20

- Teach the existing communication/goals procedures to consider a request’s practical consequences, carry notification/failure/exit rules into actual work, and verify changes before confirming.
- Encourage a natural, warm, candid voice that explains useful judgment without catchphrases, forced humor or a fixed reply template.
- Clarify that frequency/scope changes update the existing native Cron job; watch_create is not an update API. Preserve explicit report subscriptions and user constraints.
- Give existing conversations the procedure pointer and keep the system summary within the host’s prompt budget. No new Skill, prompt file, Cron or state storage.

## 0.1.19

- Make receiving replies understandable without reading internal deliveries: identify the subject and change, then integrate verified findings and practical impact.
- Apply shared receiving guidance to native Cron results in private conversations, including existing non-Muse jobs. Preserve their task definitions and keep generated results out of user signals.

## 0.1.18

- Preserve the review snapshot boundary across pages and return an explicit continuation instruction. Reject substituting the page checkpoint for the snapshot boundary, caught in a real maintenance run.

- Exclude self-review calls routed through Hermes’s native tool_call dispatcher, as well as direct calls. Mixed batches containing actual mutations remain visible. This closes a gap found during Macmini live acceptance of 0.1.17.

## 0.1.17

- Review native tool calls/results and owned Cron outcomes through the existing state tool, including persisted child sessions and late tool results.
- Keep only operation checkpoints, execution/session references and evidence-backed findings in the existing state database; failed reads do not advance the review.
- Add a self-review procedure to the existing Skill and hourly/nightly maintenance: verify actions and repairs, admit consequential mistakes and track actual disclosure.
- Make Bot Chat handoffs explicitly continue the same assistant's work and responsibility, without inventing completed actions or blaming a separate background report.
- Preserve six Cron jobs, existing settings, identity files and native execution history; add no model/provider dependency or automatic rollback.

## 0.1.16

- Include native assistant outcomes and user corrections in patrol context; require a current relevance review before sending and recheck at finalization.
- Group notice revisions by issue and retire superseded unsent candidates while retaining actual delivery history.
- Preserve original-item titles, IDs and due/timezone in notice evidence; do not expand ambiguous names.
- Remove the installation-default one-per-day ordinary limit; retain explicit limits and migrate only untouched defaults.
- Reuse verified tool-call conventions through the existing Skill and TOOLS.md.

## 0.1.15

- Align all job, Skill, system and research instructions around useful depth, initiative, completion and recovery; remove automatic next-pass deferral and rigid review formats.
- Add one shared Bot Chat receiving prompt and a communication procedure. Notices and review reports arrive as internal findings to explain directly to the user, not messages to acknowledge.
- Preserve direct-channel text, delivery checks, user intent boundaries, existing settings and the six recurring jobs; receiving envelopes never become user signals or new reminder loops.

## 0.1.14

- Resolve previously merged source IDs still held by in-flight patrols, so stale context cannot recreate duplicates.

## 0.1.13

- Return remaining review batches directly so hourly/nightly work can finish its snapshot in one run; preserve timestamp ties and truthful partial progress.
- Explain pending notification blockers and earliest eligibility; allow evidence-backed urgency reassessment without bypassing user snoozes or inventing promises.
- Bind verified tool aliases to existing source IDs and archive duplicate observations without transferring success checkpoints.


## 0.1.12

- Preserve all recorded real-user context from the last day for patrols, removing the 0.1.11 eight-message/1,000-character excerpt cap. Earlier context remains available through native memory/session retrieval.
- Make coverage, timely follow-through and user experience the priority; reuse tools and source records only without reducing useful checks or context. The 0.1.11 source, signal and receipt fixes remain.

## 0.1.11

- Exclude native async-completion envelopes from user signals and signal-backed actions. Preserve historical misclassified messages as background audit evidence.
- Derive source IDs from connection/account/resource identity; track expected and checked resources separately. Incomplete coverage cannot advance the successful checkpoint. Archive old free-form source records without guessing their identities.
- Read the native same-profile Bot Chat receipt by its execution-specific key. Repair finalized unknown outcomes when that exact receipt settles, even after later patrols overwrite the latest-job pointer; never resend.
- Give each Cron only its relevant signal context. Patrols reuse tested read recipes, batch independent reads and use source checkpoints while continuing live source inspection on every tick.

## 0.1.10

- Run all six fixed jobs on schedule instead of treating empty local goals/interests as proof there is no work. Patrols discover connected services/devices, inspect changes and upcoming deadlines, then stay silent when nothing merits a notice. This increases scheduled model usage.
- Record per-source coverage and successful watermarks; partial or failed reads preserve the previous successful position. Retire stale unsent notices from verified source evidence.
- Add weather/travel changes, interest news and relevant local opportunities to the existing patrol/Feed procedures, with current context, original sources, expiry and the same attention budget. No extra Cron or service dependency.
- Give nightly review its own signal cursor; keep pending notices visible, fix interest snooze/resumption and a nested SQLite transaction during interest-notice finalization.
- Allow evidence-backed completion of accepted goals across days, verified external cancellation and expiring Ideas without a fabricated goal. New commitments and scope changes still require user evidence.
- Use one native asynchronous coordinator for new research batches, with host result callbacks and durable status. No user-driven wave advancement for new work; legacy batches remain inspectable.
- Keep one Skill, seven prompts, four hooks and six fixed jobs. Preserve user templates, native identity/memory and job schedule/model/pause choices.

## 0.1.9

- Expand proactive behavior instructions: notice concrete conversational plans, prepare useful work before interrupting, select one strongest ordinary item, and avoid status chasing or repeated offers.
- Connect memory, goal research, patrol selection and feedback/expiry rules without new Skills, hooks or Cron jobs. Existing execution and permission checks are unchanged.
- Add natural conversation and evidence-first follow-through guidance, plus bounded working lessons maintained in the companion workspace AGENTS.md during nightly review. Existing user instructions and SOUL are preserved.

## 0.1.8

- Fix the live-acceptance failure in 0.1.7 where the model returned a dispatch-status summary instead of the notice body. A narrowly scoped native transform_llm_output hook now finalizes patrol/watch output from approved records.
- Recheck lifecycle and attention conditions at final output; require that finalization before recognizing a direct notice as sent. Other conversations and review reports are unchanged.
- Registers four hooks; still one Skill, seven prompt files and six fixed Cron jobs. No delivery-only Cron is created.

## 0.1.7

- Deliver selected reminders in the patrol/watch Cron's own final response; no additional delivery-only Cron is created. Deferred candidates remain for the next patrol.
- Keep lifecycle, freshness, deduplication and attention checks; bind native receipts to the exact preparing execution instead of the recurring job's latest run.
- Migrate the old silent patrol and unchanged generated monitoring instructions once; preserve schedules, pauses and user-selected destinations. Existing delivery-only jobs can finish through their original path.

## 0.1.6

- Exclude all native Cron deliveries from user-signal collection, including existing jobs outside Muse. Keeps background results from creating or renewing interests as if the user had spoken. No change to allowed background state operations.
- Resolve macOS temporary-directory aliases in the native integration test.
- Document explicit profile selection, Cron model defaults and the `muse` toolset requirement when platform tool lists are configured.

## 0.1.5

- Add weekly outcome/cost review and monthly instruction/workflow audit, using the shared Skill and current-profile Bot Chat. Reports propose changes, save summaries for follow-up, and exclude a separate Skill audit.

- Rebuild runtime behavior, background prompts and Skill procedures from v0.1.2, then reapply the current-profile Bot Chat delivery changes.
- Remove the 0.1.3 blanket Cron state-write restrictions, the plugin's 30-minute/4000-character watch limits and its extra schedule validator.
- Remove the 0.1.4 read-only restriction on Bot Chat receiving turns. Keep the original user-evidence checks and exclude delivered results from new user signals.
- Retain Bot Chat receipts, exact-notice deduplication and the README's Bot Chat workflow.


## 0.1.4

- Send selected Cron results to the current profile's Bot Chat by default. The bot reads the result and replies in that chat.
- Remove the separate source-session injection step; notification_handoff uses the same delivery queue.
- Track live-owner and deferred Bot Chat receipts, without treating admission as a completed reply.
- Exclude incoming delivery turns from user-signal collection and prevent recursive notification creation.
- Document Bot Chat as the recommended workflow, native missing-chat creation and the installer's static after-install notes.


## 0.1.3

- Refuse user-decision state operations from Cron, including use of saved user signals; keep research progress, reminder preparation and watch stopping available.
- Limit new watch prompts to 4000 characters and recurring clock slots to at least 30 minutes, including midnight. One-off reminders can be sooner.
- Exclude Hermes bot-chat targets from the single-home notification fallback.
- Use Hermes's read-only configuration loader.
- Add real-Hermes regressions for these boundaries and align the Skill/background prompts with them.
- Include the revised bilingual feature comparisons and bundled Skill, prompt and hook documentation.

## 0.1.2

Reorganized both READMEs as a project reference and added feature coverage tables. The tables distinguish implemented code, configured model procedures, partial support and missing features. Runtime behavior is unchanged.

## 0.1.1

Rewrote the English and Chinese READMEs around the project's inspiration from Meta Muse and the everyday follow-up behavior it aims to bring to Hermes. Installation and runtime behavior are unchanged.

## 0.1.0

Initial independent companion assembly: one Skill, four script-gated recurring jobs, five prompts, transactional operational state, goals/subgoals and expiring interests, reminder preparation and native delivery tracking, quiet upkeep, reflection/relationships, silent Feed operations, bounded research, profile-scoped initialization and documented native-remove cleanup. No mandatory external provider or core patch.

### Independent acceptance followup (2026-10-15)

- Restrict installation preparation to a whole, operation-matching Muse command.
- Confine saved feedback to its original turn and invalidate superseded unannounced saves before replay.
- Make audit packages parent-only while trusted child grants are unavailable; reject empty-evidence dispatch.
- Resolve source aliases once through the shared service resolver so CAS protects the actual canonical row.
- Document the unresolved Windows regression; no unsafe fallback or Windows acceptance claim.

### Child evidence and parallel review correction (2026-10-15)

- Allow native children to read current-profile evidence/content without an extra grant service; keep global writes/checkpoints parent-owned.
- Route child context/status and history to pure projections; add paged evidence indexes and time/session/job narrowing.
- Restore executable parallel audit packages and native dispatch, preserving original-owner recovery and contiguous parent commits.
