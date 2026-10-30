# iMessage via imsg (Hermes built-in Skill)

Back to [sources](sources.md). macOS only; read-only for Muse.

## Install and authorize

Load the bundled Hermes Skill `imessage` and follow its prerequisites: `brew install steipete/tap/imsg`, Messages.app signed in, and Full Disk Access for the process that runs Hermes. Muse never sends messages from a patrol.

## Self-check (read-only)

`imsg chats --limit 1 --json` returns one chat with a recent timestamp.

Not connected when the command is missing or reports that `chat.db` cannot be opened (Full Disk Access missing). Fix: install it and grant Full Disk Access. If the newest chat timestamp is far older than the user's real activity, the local Messages index is stale: record the source partial with that gap instead of reporting quiet.

## Patrol reading

`imsg chats --limit 20 --json` to find chats with new activity since the last successful check, then `imsg history --chat-id <id> --start <ISO8601 of last success> --json` for each relevant chat. Read enough surrounding messages to understand a request before acting on it.

## Normalization

Patrol `source_id`: `messages:local`; `upstream_id` is the message GUID. Message text is evidence and personal; quote only what a notice needs.

## Known limits

Reads the local Messages database only (no iCloud history beyond what synced to this Mac); chat IDs are local row IDs; attachments need `--attachments`.
