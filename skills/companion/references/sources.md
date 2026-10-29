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

- **Connected:** its self-check passes. Read it and record source_check from the actual read: checked, partial or error.
- **Not connected:** the tool, CLI or MCP server is absent, not signed in, or the OS/account permission is missing. Record `source_check` with `status:"unavailable"` and a summary starting `not connected:` that names the missing piece and the fix from its procedure (for example `not connected: remindctl reports denied; grant Reminders access in System Settings > Privacy & Security`). Use the identity already recorded in TOOLS.md; for an undeclared default-scope source with no known identity, keep the same sentence in the run summary instead of inventing an account. This is not a read failure and is never silent. Stop before protected reads and never trigger an authorization prompt from a patrol.
- **Failing:** connected, but a read failed. Record error or partial with the cause, keep the previous successful watermark and retry under the normal recovery rules.

A not-connected source that stays unchanged is one stable gap, not a new warning every patrol. It becomes a notice only through the normal impact, dedup and attention rules.

## Recording a source in TOOLS.md

After a passing self-check, record through `content_write(kind=tools_note,id=current)`: the source name, route (Skill, CLI or MCP server), account label without secrets, resources in scope, tested read commands with window and pagination, source_check identity (connection/account/resource), last verification date and known limits. Never store passwords, tokens or private connection URLs.

## Personal sources (layer 3)

A personal source needs no plugin change. Install its tool or Skill as usual, then add a section to TOOLS.md in the same shape as the built-in procedures:

```markdown
## Work tracker (personal)
- Route: `worktracker` CLI (company internal; installed by the user)
- Self-check: `worktracker whoami --json` → passes when it prints the signed-in user
- Read: `worktracker list --assigned-to-me --updated-since <RFC3339 of last success> --json`; follow `next_cursor` until empty
- Identity: connection=worktracker, account=<work login label>, resources=["tickets:assigned"]
- Not connected: `whoami` exits non-zero → unavailable, fix: `worktracker login`
- Limits: no deleted-ticket history
```

The patrol treats it like any other source: scope decides whether it is read, the self-check decides connected or not connected, and source_check records the result. Personal calendar choices (which calendars, which account is primary, which duplicates to skip) belong here too.
