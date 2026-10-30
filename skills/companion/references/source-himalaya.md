# Himalaya IMAP mail (Hermes built-in Skill)

Back to [sources](sources.md). Any IMAP account (Gmail with an app password, iCloud, Outlook, company mail).

## Install and authorize

Load the bundled Hermes Skill `himalaya` and follow its installation (`brew install himalaya` or the release binary) and configuration (`himalaya account configure`, IMAP/SMTP credentials kept in its config or keyring). Muse never stores mail credentials.

## Self-check (read-only)

1. `himalaya account list` lists the configured accounts.
2. Per in-scope account: `himalaya envelope list --account <name> --folder INBOX --page-size 1 --output json` returns a JSON array.

Not connected when the binary is missing, the account is absent, or the server rejects authentication. Fix: follow the Skill's configuration for that account.

## Patrol reading

`himalaya envelope list --account <name> --folder INBOX --page-size 50 --page <n> --output json "after <yyyy-mm-dd>"`, increasing `--page` until a page returns fewer than 50 envelopes; start one day before the last successful check because `after` is date-granular. Read candidates with `himalaya message read --preview --account <name> <id>`; `--preview` is required so patrol reads do not mark mail as seen. IDs are relative to the folder; re-list after a folder change.

## Normalization

Patrol `source_id`: `mail:<configured account name>`. Observe each relevant message with its Message-ID as `upstream_id`, and the subject and sender in `title`.

## Known limits

Date filters are whole days; without `--preview` reading changes the seen flag; very large folders are slow over IMAP.
