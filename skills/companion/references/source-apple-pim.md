# Apple PIM (external MCP server)

Back to [sources](sources.md). macOS Calendar, Reminders, Mail and Contacts through EventKit, Contacts and Mail's local index. Upstream: <https://github.com/omarshahine/apple-pim> (MIT).

## Install and authorize

Prerequisites: macOS 13+, Swift 5.9+ (Xcode 15+), Node.js 18+.

1. Clone the upstream repository, build and install the Swift CLIs with `./setup.sh --install` (copies them to `~/.local/bin`), then check with `scripts/doctor.sh`, as its README describes.
2. Register its MCP server in the Hermes profile `config.yaml` (paths are examples; use the real ones):

   ```yaml
   mcp_servers:
     apple_pim:
       command: /path/to/node
       args: [/path/to/apple-pim/mcp-server/dist/server.js]
       env:
         APPLE_PIM_BIN_DIR: /Users/<you>/.local/bin
         # Optional: the bundled helper app holds macOS permissions independently of the launching process.
         APPLE_PIM_HELPER_APP: /Users/<you>/Applications/Apple PIM Helper.app
       timeout: 150
   ```

3. The user grants Calendar, Reminders and Contacts access when macOS asks, plus Automation for Mail.app; Full Disk Access is recommended for fast mail reads. Optional upstream access control (allowlist/blocklist per calendar or list) narrows what any agent can see.
4. Restart the gateway so the MCP server loads; the tools then appear with the server prefix (for example `mcp__apple_pim__calendar`).

## Self-check (read-only)

1. Shell: `hermes mcp test apple_pim` connects and lists the tools.
2. Tool `apple-pim` with `action:"status"`: each in-scope domain (calendars, reminders, contacts, mail) reports `authorized`.
3. Calendar: `calendar` `action:"list"` returns the calendars. If they include `sourceId`, the CLI emits the full EventKit fields described below.
4. Reminders/Mail/Contacts in scope: `reminder` `action:"lists"`, `mail` `action:"accounts"`, `contact` `action:"containers"` each return data.

Not connected when the server does not load, the tools are missing, or status is `notDetermined`, `denied`, `restricted` or `writeOnly`. Fix: install or register the server, then grant the named permission in System Settings > Privacy & Security. Never call `action:"authorize"` from a patrol: it opens permission prompts and is a user step.

## Patrol reading

- **Calendar:** for each in-scope calendar ID, `calendar` `action:"events"` with `calendar:<id>`, `from:<window.start>`, `to:<window.end>` and a `limit` above the expected count (for example 1000). **Do not pass `fields`**: it removes every key not listed, including `recurrence` and the full EventKit fields, which silently downgrades events to observation mode. Pass the raw calendar and event objects to `calendar_view` as described in [calendar §Apple PIM receipts](calendar.md#apple-pim-receipts), with `device_timezone` from the reading Mac. A result that reaches the limit has no continuation token: mark that page partial.
- **Reminders:** `reminder` `action:"lists"`, then per list `action:"items"` with `list:<id>`, `filter:"upcoming"`, `completed:false`; also `filter:"overdue"`. Verify a surprising empty `week` result with `upcoming` before treating it as empty.
- **Mail:** `mail` `action:"accounts"`, then `action:"search"` with a non-empty `query`, `since:<date of last success>` and a small `limit`, per account and mailbox; `action:"get"` for each candidate before judging it. `messages` has no page token and can be slow on a large inbox.
- **Contacts:** only to resolve a concrete person in an in-scope item; never dump the address book.

## Normalization

Calendar uses `receipt_format:"apple-pim"`; Muse decides per event between full and observed events (see the calendar procedure). Patrol `source_id`s: `calendar:eventkit`, `reminders:eventkit`, `mail:<account>` (the local EventKit store, not a provider account).

## Known limits

- Full calendar receipts need EventKit fields beyond upstream 3.19 (`occurrenceDate`, `isDetached`, `isRecurring`, `status`, event `timeZone`, `allDayStart`/`allDayEnd`, calendar `sourceId`). They are an optional enhancement: a patched build of the CLI supplies them; without them events stay in observation mode and the source stays partial. Upstream 3.19 already reports `availability`, which Muse uses for busy in either mode.
- Even with full fields: deleted recurring instances (EXDATE), the current user's attendee response and the iCalendar UID are not reported; these are known limitations, not partial coverage.
- macOS permissions belong to the process that launches the CLI unless the helper app is configured; a check from another terminal can differ from what the gateway sees.
- Reinstalling the CLIs from an unpatched checkout replaces a patched build; recheck with step 3 after any apple-pim update.
