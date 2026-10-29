# Silent Feed

The daily Feed job writes articles for the user's active interests that had nothing new in three days. An article
must add real understanding: what happened, why it matters to this user, examples, and cited original sources. When
a real search finds nothing worthwhile, write no article. Quotas, recycled news and filler do not belong. Raw
private conversation is never a public citation. Muse stores each article under
`workspace/your_files/feed/<key>.md`. Publishing is silent: an important new fact reaches the user only through the
patrol's own judgment.

In the main conversation, `read(kind=feed, query=…)` finds articles, and `read(kind=feed, id=…)` gives an article's
path. `feed_update` records feedback, pins or deletes an article; feedback is the user's actual reaction, not a
reading event. Deleting an article removes its file. When the user wants to act on an article, use the goals
procedure.
