[Hermes Muse job: feed]
Script Output holds this run's packet: the user's active interests that have had no article in three days.
For each interest, decide whether there is something genuinely new and worth reading. Research it from original sources with the tools you have.
If there is, write one article in the user's language. It should explain what happened, why it matters to this user, and concrete examples, and cite every source in `source_refs` (URLs or exact references).
If nothing worthwhile turned up after a real search, answer that interest with no article. Recycled news, filler and private conversation details do not belong in an article.
Send one muse_job submit with one result per interest (`interest_ref`, optional `article {title, body, source_refs}`). Muse stores the article; publishing is silent.
Do not message the user. Answer exactly [SILENT].
