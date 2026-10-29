# Memory

Native memory (or the configured provider) holds what the user said about themselves. Muse does not keep a second
user profile, and it never rewrites SOUL, USER or MEMORY.

Intake: each hour the job sees new private user messages as short excerpts. Save stated facts, preferences, decisions
and corrections with the native memory tool, and record them as `saved` with `facts`. Mark what matters only for that
conversation as `context`, and small talk or material with no decision as `ignored`. Decide pasted attachments and
data dumps from their head and tail. Never save secrets: excerpts are redacted, so keep them that way.
Do not save guesses or summaries Muse produced itself as if the user said them.

Memory receipts: when a native memory write fails or waits for approval, Muse shows it once in the next turn.
Tell the user only when it matters to them. A write the provider accepted without confirmation is not reported.

Forgetting: on the user's request, `forget` removes what Muse holds:
- a captured message (`signal`, also never captured again), an interest, an idea or a Feed article;
- a note, dream or goal file, which needs its revision from `read`.
Native memory is forgotten with the native memory tool. Say exactly what was removed and what remains, for example
the original conversation, which Hermes keeps.
