# Follow through on accepted work

Create a goal when you accept a concrete user request or promise. Check for an
existing goal first; ordinary questions and casual mentions do not become goals.

A goal has one status: active, done or cancelled. Use goal_create with title,
description and completion_criteria; signal_id is optional and does not expire.
Record an optional owner {kind,id}: native_delegation, cron, paseo or external.
Owner handles are references, not permission to start or repeat work.

Use context(section=followups) for due active goals and available native owner
status. Follow next_cursor for the rest. Read completed owner results, judge them
against the accepted request, and tell the user. Then call
`goal_update(id=...,status=done,note="Result checked and explained")` once.
No reply receipt, handoff, source hash or delivery confirmation is required.

For unfinished work, update note/progress and next_check_at. Use waiting_user=true
or blocked="reason" when appropriate. Respect the user's current changes; cancel
tracking with status=cancelled when asked. Cancellation does not prove the external
owner stopped. Inspect unknown writes through their owner; never automatically resend.

Only private parent conversations change goals. Cron and children may inspect and
report (the nightly review may also goal_create with a signal_id, see goals.md); a normal parent task_id is not a child marker. Optional expected_revision
is the goal's integer revision; a conflict returns the latest goal for merging.

Use existing notification review, quiet hours and deduplication for reminders.
A notification candidate/queue is not delivery. Done goals never return to due.
