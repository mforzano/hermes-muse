# Google Workspace (Hermes built-in Skill)

Back to [sources](sources.md). Gmail, Google Calendar, Contacts, Drive, Docs and Sheets.

## Install and authorize

Load the bundled Hermes Skill `google-workspace` (`skill_view google-workspace`) and follow its First-Time Setup: OAuth client, `setup.py --services ...` with only the services in scope, and the `gws` CLI backend when installed. Do not duplicate or bypass that flow. If the user's host instead uses another Google CLI they already authorized (for example `gog`), record that route and its tested commands in TOOLS.md; the reading and normalization rules below stay the same.

```bash
GSETUP="python ${HERMES_HOME:-$HOME/.hermes}/skills/productivity/google-workspace/scripts/setup.py"
GAPI="python ${HERMES_HOME:-$HOME/.hermes}/skills/productivity/google-workspace/scripts/google_api.py"
GWS="python ${HERMES_HOME:-$HOME/.hermes}/skills/productivity/google-workspace/scripts/gws_bridge.py"   # needs gws on PATH
```

## Self-check (read-only)

1. `$GSETUP --check` prints `AUTHENTICATED`.
2. Gmail in scope: `$GAPI gmail search "newer_than:1d" --max 1` returns a JSON array (empty is fine).
3. Calendar in scope: `$GWS calendar calendarList list` returns the calendar inventory (with gws), or `$GAPI calendar list --max 1` returns an array.

Not connected when the check prints anything other than `AUTHENTICATED`, the token file is missing, or the needed service scope was not granted. Fix: rerun the Skill's setup for the missing service.

## Patrol reading

- **Mail:** `$GAPI gmail search "newer_than:2d in:inbox" --max 50` (widen to cover the time since the last successful check); read candidate threads with `$GAPI gmail get MESSAGE_ID` before judging them. The wrapper returns no page token: when a search returns exactly `--max` items, raise `--max` or narrow the window and mark the check partial until the window is fully read. Resource `mail:inbox`.
- **Calendar (full coverage):** with gws, list every calendar with `$GWS calendar calendarList list`, then per calendar `$GWS calendar events list --params '{"calendarId":"<id>","timeMin":"<window.start>","timeMax":"<window.end>","singleEvents":true,"maxResults":250}'`, repeating with `"pageToken"` until `nextPageToken` is absent. Pass the calendar ID, not its display name.
- **Calendar (wrapper only):** `$GAPI calendar list --calendar <id> --start <window.start> --end <window.end> --max 250` reads one calendar, has no page token or calendar inventory, and drops recurrence identity, timezone and transparency. Treat such reads as partial with that limitation.
- **Contacts/Drive:** only for a concrete in-scope item; never dump them during routine patrols.

## Normalization

Calendar events follow the strict [calendar](calendar.md) contract: `start.dateTime` (RFC3339) or `start.date` (all-day, end date already exclusive) with the event or calendar `timeZone`; `recurringEventId` plus `originalStartTime` as `occurrence_id`; `status:"cancelled"` instances stay as evidence; `transparency:"transparent"` or the user's declined response means `busy:false`; `iCalUID` as `uid`; `htmlLink` as locator. Patrol `source_id`s: `calendar:<address label>` and `mail:<address label>`; a Gmail message's `upstream_id` is its message id, and an event's is its id plus `updated`.

## Known limits

The compatibility wrapper truncates at `--max` without a continuation token; Google Tasks needs gws or another recorded route; OAuth tokens can expire or be revoked, which is a not-connected state, not a read failure.
