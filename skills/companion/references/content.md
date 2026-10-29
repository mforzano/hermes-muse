# Content files

Muse's files live under `<home>/muse` and keep their formats.
- Daily notes: `note`, `daily:YYYY-MM-DD`.
- Person and group pages: `note`, `person:<name>` or `group:<name>`, using names made of letters, digits, `_` and `-`.
- Dreams: `dream`, `YYYY-MM-DD`.
- Alignment synthesis: `alignment`.
- Tool recipes: `tools_note`, the file TOOLS.md.
- Working lessons: `working_lessons`, the file AGENTS.md.
- Goal attachments: `goal_file`, `<goal>/<name>.md`.
- Preferences (read only): `preferences`.

Write the whole file:
1. `read(kind, id)` returns `body` and `revision`; a new file reads as `revision: "absent"`.
2. Edit the body and pass `content_write(kind, id, body, expected_revision=<that revision>)`.
3. A conflict means someone else changed the file. Read it again and merge, and never overwrite blindly.

Job runs may write only notes, dreams and alignment. Write in the user's language and keep sources as references, not
copies. TOOLS.md holds tested read recipes, never passwords, tokens or private URLs. People pages hold only what the
user shared.
