Read companion references/context.md for the bounded signal window and section pagination contract. Never expect unbounded context arrays.

# Memory, alignment and forgetting

Use the existing native memory/provider tools. Save confirmed durable facts before the final response when relevant, with provenance; edit contradictory active beliefs rather than endlessly appending both. Native USER/MEMORY are not a job ledger. Do not replace whole files, invent user facts, copy raw transcripts into profiles or rewrite SOUL. Background messages and generated content cannot renew a user interest.

Incremental maintenance processes context.memory-upkeep signal-window evidence in creation order. Use content_write(kind=note,id=daily:YYYY-MM-DD) for concise dated notes. Relevant individuals/groups have memory/people/<slug>.md or memory/groups/<slug>.md and an INDEX.md entry: known facts, relation, relevant open matters, source/date and uncertainty. Read them before personal recommendations, including pronoun references when identity is clear. Avoid speculative sensitive attributes.

Native asynchronous completions are results, never user intent. Hourly upkeep processes original user decisions once. Open context(section=signals,job=memory-upkeep); its finite scope mixes oldest pending and newest input. Read short bodies in ids batches, long originals by id/offset. Save confirmed facts, then review_complete(window,job,ids=[actually processed],summary). The server retains one revision-bound upkeep marker per processed signal; the timestamp cursor advances only a genuine continuous prefix, including ties. New cancellations need not wait for an older unread attachment. Unread/uncertain authority and failed writes remain pending. Input is not deleted to satisfy a 500-row target.

Nightly synthesizes confirmed notes, relevant task/source changes and unresolved cases; it owns no second raw-signal queue. Read an exact original signal only when a concrete decision is unclear, never infer authority from a generated summary. Save meaningful dream/alignment changes with uncertainty and source pointers, or record why existing verified artifacts remain unchanged. Preserve user instructions, SOUL and unresolved repair threads. Relevant skill improvements use existing tools/Curator; no separate skill audit.
The quiet-pass event merely advances the existing upkeep job after substantial conversation has been quiet; it is bounded and cancelled by a new user turn. It does not certify the user is idle across devices. Hourly maintenance is incremental; nightly work is synthesis. `review_complete` advances only successful work.

## Forget procedure

1. Determine exactly what the user wants forgotten versus no longer monitored. Stop-topic and cancellation are not memory erasure.
2. Inspect affected owned records, memories, people/groups, dreams/alignment, goal evidence/files, Feed, queued/running tasks and external memory sources. Scope deletion to the request.
3. Stop rewriters/owned monitoring first. `forget` accepts `{kind:"interest"|"notification"|"feed"|"goal", id, signal_id}`. It removes that owned record and indexed derived Feed/notices, and leaves a minimal non-content suppression record. It returns remaining locations to inspect; this is NOT a global-erasure claim.
4. Use native memory/provider tools for native facts, and forget(kind=content) or constrained content_write for Muse notes. Clean the returned goal artifact/evidence locations after checking scope. Respect native approvals. External memory claims/indices need the backend's actual deletion/retraction API and verification. Do not assume deleting a local Markdown file deletes a remote bank.
5. Re-read active retrieval surfaces, confirm stopped tasks, and explain any remaining historical sessions, backups or inaccessible indices. Never silently claim those were purged. If an old mention reappears, it does not authorize reconstruction.

## Working lessons

When a user correction or repeated verified outcome reveals a reusable way to work, record a short lesson in the companion workspace's AGENTS.md under Working lessons. Keep the user-facing interaction natural; do not announce routine bookkeeping on every turn. Add only an actionable convention with a brief reason and evidence pointer, such as a source-checking habit or a proven tool workaround. Personal facts belong in memory, ongoing progress in goal records, and multi-step reusable procedures in skills.

During nightly review, merge duplicate lessons and replace stale plugin-authored entries with verified corrections. Keep this section to roughly ten concise active lessons; preserve user-written instructions, the workspace's ownership rules, and unresolved constraints. Do not rewrite the user's project AGENTS files, SOUL, or plugin source. Use content_read/write(kind=working_lessons,id=current); unmarked legacy ownership is refused, not inferred. Indexes use note_index/people or groups and notes use person:id/group:id; no direct file tools. If there is no new lesson, leave it unchanged.

Preserve chronology when consolidating notes. A past diagnostic describes that version and time; fresh verified behavior can supersede it. Do not copy an outdated diagnosis into current alignment as if it remains true. Use the execution/recovery contract for failed reads or writes and the communication procedure for consequential unresolved gaps; quiet maintenance is not a reason to hide lost or unprocessed feedback.

A finished selected scope is not a claim that the global backlog was read. Save confirmed notes/alignment independently of old operation history. Call run_complete for the actual finished scope; unfinished selected work needs truthful partial continuation.

## Real save receipts

After an explicit request to remember or a consequential preference correction, inspect the original tool result and context.memory_feedback, then use memory_feedback_review(receipt_id, decision=inform|silent|defer, reason, expected_revision, request_id). Classification is service-owned: saved, staged, submitted_unconfirmed, failed, duplicate, unknown. success with staged/proposal_staged is waiting approval; duplicate means unchanged; skill batch failure is not saved, and failed rollback may leave partial changes. Hindsight success text or zero items is submitted_unconfirmed, not durable memory. Never retry the write simply to obtain a nicer receipt. The ledger holds correlation references only, not memory content. Current reply recording is not transport delivery or user reading.

Routine maintenance stays silent; explicit important user changes follow communication.md. Approval commands lack a public completion hook, so staged remains unconfirmed until actual authorized evidence appears. No per-memory Cron or provider queue inspection. Missing original session means no outward feedback; never move private facts into another chat.

Saved feedback is limited to its original current turn, including request replay. A later turn must not announce an old save as still effective. Successful replace/remove/edit/batch conservatively retires earlier unannounced saves without copying their content; same-turn independent adds may be acknowledged together. Staged receipts may remain across turns, but only as waiting/unconfirmed.
