# Goals, interests and watches

A clear accepted request or promise becomes a goal. A passing interest stays an
interest; an Idea stays a suggestion until accepted. Preserve the user's criteria.

- goal_create: id?, title, description, completion_criteria, signal_id?, parent_id?, deadline?, external_ref?, owner?. One subgoal level; timezone-aware deadlines.
- Nightly intake: the nightly review is the only Cron that may goal_create. It needs signal_id, creates at most 5 per run, must itself dedupe against active, done and cancelled goals first (prompt-level; the code only rejects a reused id), and never calls goal_update. Ordinary conversations remain the first line: every user turn carries a one-line check to cover accepted requests.
- goal_update: id, status?, note?, progress?, owner?, next_check_at?, waiting_user?, blocked?, description?, completion_criteria?, research_review_at?, expected_revision?. Status is active/done/cancelled. Explain a checked result, then record done with a note. Cancellation stops owned watches; it does not stop an external agent by implication.
- A watch-cleanup warning means the goal was committed. Inspect its owned watches and finish cleanup; do not repeat completion as though the goal write failed.
- interest_record: id?, title, signal_id, expires?, rationale?. Default14 days from the actual mention. Only a newer user mention renews it.
- idea_add: goal_id?, title, rationale, sources?, expires?, plus a run-unique request_id and expected_revision="absent". Ideas are not execution authority. The nightly review has its own short Ideas step: at most 0-3 from active goals/interests, recent Feed and recently verified user signals, no quota, and run_complete's summary says how many were proposed or why none.
- feedback: kind, id, action, signal_id, until?. Distinguish accept/done/dismiss/stop/snooze.
- watch_create: goal_id, signal_id, schedule?, prompt, stop_condition, expires?. Only explicitly requested monitoring. New repeating watches must be provably at least30 minutes apart; once is exempt. Respect cron.allow_agent_scheduling.
- watch_stop: job_id. Pauses only Muse-owned watches; never change user pauses or unrelated jobs.

Review stale research after30 days without closing a goal merely because it went
quiet. Never add a watch for every goal or interest. Follow [followup](followup.md)
for simple completion and owner checks, and proactivity for notification rules.
