# Apple Notes via memo (Hermes built-in Skill)

Back to [sources](sources.md). macOS only. Notes are reference material, not a patrol feed: read them for a concrete in-scope question, not on every tick.

## Install and authorize

Load the bundled Hermes Skill `apple-notes` and follow its prerequisites: `brew tap antoniorodr/memo && brew install antoniorodr/memo/memo`, then allow Automation access to Notes.app when macOS prompts the user.

## Self-check (read-only)

`memo notes --flist` lists the folders.

Not connected when the command is missing or Notes Automation is denied. Fix: install it and allow the host process to control Notes.app in System Settings > Privacy & Security > Automation.

## Reading

`memo notes -f "<folder>"` lists a folder's notes; `memo notes -v <N>` shows note N from that list. Never use the add, edit, delete, move or remove flags from Muse work. Avoid `-s` (interactive fuzzy search) in background jobs.

## Normalization

When notes are a declared source: connection `memo`, account `local-notes`, resource `notes:<folder>`.

## Known limits

No modification cursor; list numbering changes when notes change, so re-list before viewing; Automation access can prompt the first time and must be granted by the user.
