# Apple Reminders via remindctl (Hermes built-in Skill)

Back to [sources](sources.md). macOS only. Apple PIM also reads Reminders; record which route is primary.

## Install and authorize

Load the bundled Hermes Skill `apple-reminders` and follow its prerequisites: `brew install steipete/tap/remindctl`, then `remindctl authorize` once, interactively by the user. Patrols never authorize.

## Self-check (read-only)

`remindctl status` reports authorized access; then `remindctl list --json` returns the lists.

Not connected when the command is missing or status is not authorized (notDetermined, denied, restricted). Fix: install it, then the user runs `remindctl authorize` or grants Reminders access in System Settings > Privacy & Security for the process that runs Hermes.

## Patrol reading

`remindctl show upcoming --json` for the full upcoming set and `remindctl show overdue --json` for overdue items; `--list <name>` narrows to one list when scope requires it. Due dates are UTC; convert with the profile timezone before judging them.

## Normalization

source_check identity: connection `remindctl`, account `local-reminders`, resource `reminders:open`, `checked_resources` the list IDs read. A notice's source_snapshot uses the reminder ID, literal title and original due date.

## Known limits

No change cursor: every patrol rereads the open set; completed history needs `show completed`; permission is granted per host process, so a terminal grant does not cover a differently launched gateway.
