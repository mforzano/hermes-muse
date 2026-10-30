[Hermes Muse job: feed]
Script Output holds one batch of this run's work: the user's active interests not researched in the last day, with `context.blocked_topics` and `context.notice_limit`.
For each interest, decide whether there is something genuinely new and worth reading. Research it from original sources with the tools you have.
If there is, write one article in the user's language. It should explain what happened, why it matters to this user, and concrete examples, and cite every source in `source_refs` (URLs or exact references).
If nothing worthwhile turned up after a real search, answer that interest with no article. Recycled news, filler and private conversation details do not belong in an article.
Add a `notice` to an article only for the one finding the user would want to be interrupted for now (a release, a changed date, an answer to what they asked); its `source_ref` is one of the article's sources, its `topic` the exact blocked_topics entry when it is about one. At most notice_limit; never more.
Send one muse_job submit with one result per interest (`interest_ref`, optional `article {title, body, source_refs}`, optional `notice`). Muse stores the article silently; a notice goes through Muse's dedupe, quiet hours and budgets.
Do not message the user yourself. Answer exactly [SILENT].
