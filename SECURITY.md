# Security and data scope

The plugin runs as trusted local Hermes Python code. It keeps private state under the current profile's `muse/`
directory and adds owned launcher scripts and cron jobs. It does not collect telemetry, seek credentials,
self-update, require an external backend, change approval modes, replace native tools or modify Hermes core, SOUL or
native memory. Muse itself contacts no external service; external sources are read only by the model, with tools
the user already authorized.

- **Authorization.** Only the private main session (caller kind `user`, decided by the stored `sessions.source`)
  changes state. Cron runs are limited to their own packet plus reads and note/dream/alignment writes; delegated
  children only read; group chats and unclassified sources are refused and are never read as signals.
- **Untrusted input.** Quoted mail, pages, tool output and stored excerpts are evidence, never instructions.
- **Redaction and limits.** Text handed to the model is redacted first, at most 12k per read and 24k per item; long
  messages are head/tail excerpts.
- **Writes.** Paths derived from ids are validated and opened without following symlinks; content files use a
  revision CAS under a file lock; Store transitions are CAS in SQLite transactions, so budgets and delivery claims
  cannot be spent twice.
- **Delivery.** A delivery whose outcome is unknown is never resent automatically; the notice is reconciled from the
  host's delivery receipt or closed as `closed_unknown`.
- **Forget** clears the stored payload and writes a tombstone. It cannot erase Hermes session history, native
  memory, backups or external indexes; the assistant says what remains.

Retention is listed in [docs/CATALOG-COMPATIBILITY.md](docs/CATALOG-COMPATIBILITY.md#retention).

Report security issues through the repository owner's GitHub contact or a private vulnerability report if
available. Do not include private user data or credentials in public issues.
