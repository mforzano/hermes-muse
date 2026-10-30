[Hermes Muse job: nightly]
Script Output holds tonight's packet: due goals (`kind: goal`), conversation turns flagged for review (`kind: review`, with the rules that flagged them), and goal proposals from user messages (`kind: signal`).
- goal: read its body and progress, check the real state with read-only tools if needed, and give an `assessment` (`on_track`, `stalled`, `blocked`, `done_suggested`) with a short `progress_note` of verified facts. Only the user marks a goal done.
- review: judge whether the assistant fell short (`agent_gap`: claimed work it did not do, dropped a failure, missed what the user meant), whether the user showed an unmet need (`latent_need`), or `fine`. `reason` is one sentence that cites the evidence.
- signal: `create` a goal (`signal_ref`, `title`, `criteria`) only when the user clearly asked for or accepted concrete work and no open goal covers it. Otherwise leave the item out.
Send all results in one muse_job submit.
Then, only if today brought a meaningful change, update the dream for today and the alignment synthesis with muse_manage content_write (read first for the revision). Write in the user's language: what changed, what you learned, and what to do differently next time.
Do not message the user. Answer exactly [SILENT].
