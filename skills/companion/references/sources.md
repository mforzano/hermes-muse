# Data sources

Muse reads the user's information only through tools the host already has. Apply [proactivity Coverage scope](proactivity.md#coverage-scope) first: a source listed here is a way to read, never permission to read. Three layers, in order of preference:

1. **Hermes built-in** (default). Tools `web_search`/`web_extract` and the bundled Skills `google-workspace`, `himalaya`, `apple-reminders`, `imessage`, `apple-notes`. Their Skills own installation and authorization; Muse adds only the patrol reading, normalization and limits below.
2. **Convenient external dependency.** Apple PIM (MCP server for macOS Calendar, Reminders, Mail and Contacts).
3. **Personal customization.** Company systems, internal chat, work tools, specific accounts, calendar IDs and choices such as "read the family calendar from Google, not its Apple copy". These live only in this profile's TOOLS.md (and the user's own Skills or native Cron jobs), never in the plugin.

| Source | Layer | Platform | Covers | Procedure |
| --- | --- | --- | --- | --- |
| Web search/extract | 1 | any | Feed, research, public facts | [source-web](source-web.md) |
| Google Workspace | 1 | any | Gmail, Google Calendar, Contacts, Drive | [source-google-workspace](source-google-workspace.md) |
| Himalaya | 1 | any | IMAP mail | [source-himalaya](source-himalaya.md) |
| Apple Reminders | 1 | macOS | Reminders | [source-apple-reminders](source-apple-reminders.md) |
| iMessage | 1 | macOS | Messages | [source-imessage](source-imessage.md) |
| Apple Notes | 1 | macOS | Notes | [source-apple-notes](source-apple-notes.md) |
| Apple PIM | 2 | macOS | Calendar, Reminders, Mail, Contacts | [source-apple-pim](source-apple-pim.md) |

Read one account through one route. When two routes reach the same data (Gmail via Google Workspace and via Apple Mail, Reminders via Apple PIM and remindctl), record the primary route in TOOLS.md and use the other only as a stated fallback. Verify every declared source after installation or upgrade with [source-verification](source-verification.md).

## Connection states

Every patrol classifies each in-scope source before reading it:

- **Connected:** its self-check passes. Read it and `observe` what you saw; add `limitation_code` when only part could be read.
- **Not connected:** the tool, CLI or MCP server is missing, not signed in, or lacks an OS or account permission. `observe` it with no items and `error` starting `not connected:`, naming the missing piece and the fix from its procedure (for example `not connected: remindctl reports denied; grant Reminders access in System Settings > Privacy & Security`). This is never silent. Stop before protected reads and never trigger an authorization prompt from a patrol.
- **Failing:** connected, but a read failed. `observe` with `error` set to the cause, and retry once if the cause is recoverable.

Muse turns 5 identical limitation codes in a row into a known limitation, and 3 errors in a row into a Health alert, so an unchanged gap is never a new warning every patrol.

## Recording a source in TOOLS.md

After a passing self-check, record through `content_write(kind=tools_note,id=current)`: the source name, route (Skill, CLI or MCP server), account label without secrets, resources in scope, tested read commands with window and pagination, patrol `source_id` (`<kind>:<account>`), last verification date and known limits. Never store passwords, tokens or private connection URLs.

## Personal sources (layer 3)

A personal source needs no plugin change. Install its tool or Skill as usual, then add a section to TOOLS.md in the same shape as the built-in procedures:

```markdown
## Work tracker (personal)
- Route: `worktracker` CLI (company internal; installed by the user)
- Self-check: `worktracker whoami --json` → passes when it prints the signed-in user
- Read: `worktracker list --assigned-to-me --updated-since <RFC3339 of last success> --json`; follow `next_cursor` until empty
- source_id: `tickets:<work login label>`; upstream_id = ticket key
- Not connected: `whoami` exits non-zero → error `not connected: …`, fix: `worktracker login`
- Limits: no deleted-ticket history
```

The patrol treats it like any other source: scope decides whether it is read, the self-check decides connected or not connected, and `observe` records the result. Personal calendar choices (which calendars, which account is primary, which duplicates to skip) belong here too.
