# Silent Feed

The daily Feed job researches each active interest about once a day and writes articles. An article
must add real understanding: what happened, why it matters to this user, examples, and cited original sources. When
a real search finds nothing worthwhile, write no article. Quotas, recycled news and filler do not belong. Raw
private conversation is never a public citation. Muse stores each article under
`workspace/your_files/feed/<key>.md`. Publishing is silent.

Interest news: when research turns up something the user would want to hear now, such as a release, a changed date,
or an answer to what they asked, the article may carry one notice. Muse dedupes it against what the patrol already
sent, applies quiet hours and budgets, and withdraws it when the interest ends. Most findings stay in the article.
An interest is temporary, not a subscription: it never creates a watch, a goal or a job.

In the main conversation, `read(kind=feed, query=…)` finds articles, and `read(kind=feed, id=…)` gives an article's
path. `feed_update` records feedback, pins or deletes an article; feedback is the user's actual reaction, not a
reading event. Deleting an article removes its file. When the user wants to act on an article, use the goals
procedure.
