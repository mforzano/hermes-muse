# Windows implementation: release validation pending

This branch is not a Windows support release. An explicitly authorized local
installation has now been tested against the actual Windows Hermes host (see
below); its six autonomous jobs remain paused. Do not treat this as a generally
released platform-support claim.

## Implemented

- Conditional POSIX imports: importing locks, service, host, runtime, migration
  and views no longer requires `fcntl` on Windows.
- Native `LockFileEx`/`UnlockFileEx` sidecar locks, preserving thread-local
  reentrancy, the process/thread lock boundary, exception cleanup, and the
  mutations-before-specific lock order. Case-normalized lock keys preserve
  ordinary case-alias reentrancy.
- Native directory traversal and file opens with `NtCreateFile.RootDirectory`.
  Each opened object is inspected; reparse directories and reparse/hardlinked
  leaves are rejected. Handles stay alive for the operation. Content operations
  do not reopen checked absolute child paths; SQLite's separate lifetime contract
  is explained below.
- Bounded binary file reads and native handle-based, sorted directory listing,
  including listings larger than one native result buffer. Missing reads/listings
  do not initialize a profile.
- Windows resource validation rejects DOS devices, alternate streams, unsafe
  namespaces/components and trailing-dot/space aliases without rewriting IDs.
- Native atomic replacement/unlink with exclusive temporary files, file flush,
  revision/cancellation checks, retained proposals and no copy/delete fallback.
- Protected DACLs at creation for new directories/files (current user, SYSTEM,
  Administrators); existing unrelated permissions are not rewritten.
- Store initialization and standard protected SQLite opening (`mode=ro` plus
  `query_only` for reads), with normal writers and handles retained until close.
  Views preserves its transaction and unexpected-WAL refusal. No immutable mode,
  database copies, replacement database or custom VFS.
- Migration/rollback/interruption recovery/re-migration with handle-relative
  archive rename; owned executable launcher publication with Unicode/space paths;
  private bounded wake-gate logging and deterministic read-only connection close.
- Case and existing 8.3 lock aliases share a reentrant lock key. Normalizing a key
  never replaces secure traversal. POSIX filesystem branches remain in place.

The manifest explicitly declares Windows-only `pywin32>=311,<312` and
`tzdata>=2025.3,<2027`; Python 3.11–3.14 and local fixed NTFS are the targets.
The pinned host only declares these dependencies transitively on Python 3.14,
so Python 3.11 must not assume they are present. No local package was installed.
Intended initial deployment scope remains Windows 11
x64, a user-private profile outside synchronized directories. UNC, network,
OneDrive-backed profiles and ARM64 have not been validated. The host's dependency
compatibility across all minimum-supported releases has not yet been established;
this requirement is explicit rather than assuming a transitive dependency.

## Real local verification

Run from the repository root:

```sh
python -W error -m unittest discover -s tests -p test_windows_compatibility.py -v
python -m compileall -q hermes_muse tests
```

Fixtures are explicitly created beneath the checkout's scratch parent, not the
OS temporary directory. Tests require neither Administrator nor Developer Mode.
The suite uses actual plugin functions and native Windows APIs, not standalone
simulations. Junction tests use ordinary `mklink /J` or
`FSCTL_SET_REPARSE_POINT`; they are not hidden behind symlink-privilege skips.

Verified locally on Windows build 26200, x64 Python 3.11.15 and 3.14.7,
pywin32 311, SQLite 3.53.1, tzdata 2025.3, unelevated: **26 focused native tests
passed with `-W error` on both Python versions**, no skips.
Coverage includes interprocess contention and cross-profile independence,
reentrancy/exception release, Unicode/space profile paths, bounded/empty reads,
large directory listing, no-creation reads, external hardlinks, pre-existing
ancestor junctions and ancestor replacement. Attributes-only in-place junction
changes are actually performed after the directory has been opened: read/lock
operations must not access or create files in the outside directory. This is why
handle-relative operations matter: denying write/delete sharing alone does not
prevent attributes-only reparse mutation.

The full isolated suite passed on Python 3.11.15 and 3.14.7: **320 tests, OK
(one existing symlink-privilege skip)** on each interpreter. Hardlink and junction
tests run without symlink privileges. The final native Windows suite and later
hardening regressions also pass on both versions. Official pinned-host validation
returned **ok=true** (including isolated registration and dependencies), with
existing prose security-scan cautions. The complete pinned-host native smoke
returned **exit 0**, including discovery, registered tools/Skill/prompt, six actual
Cron gates, A→B→A isolation, idempotent reload, preserved files, disable/remove,
compatibility and goal lifecycle checks. Native host logging is drained/closed
before fixture cleanup so Windows log locks do not leak.
No Windows support release is declared. POSIX and remote CI remain unverified;
WSL is not installed on the test machine, and no installation was attempted.

## Handle lifetime and release gates

Directory handles retain deny-DELETE sharing. They now permit READ|WRITE sharing:
NT rename internally opens the destination parent for write, so refusing WRITE
sharing blocks Muse's own atomic publication with WinError 32. Reparse/identity
checks and ancestor retention were not removed; content mutations still use
native handle-relative operations, never absolute-path replacement or copy/delete.

For SQLite, the verified DB leaf is retained without DELETE sharing before the
standard filename open. Each retained child keeps its parent nonempty, and NTFS
refuses in-place junction conversion of nonempty directories. The final parent
is inspected again after leaf acquisition, closing the empty-parent race. WRITE
sharing permits normal SQLite writers while replacement remains blocked. Actual
attributes-only attack tests verify refusal and unchanged outside sentinels.
File flushing/atomic visibility are not claims of POSIX directory-fsync-equivalent
power-loss durability.

The supported-host reference is the existing CI pin
`f594424e58f18ebc2607decaa72f300e0a26a24b`, extracted from existing local Git objects
into scratch without installation or core changes. Newer local source removed
`cron.bot_chat_delivery` and failed the old smoke API; it is not the CI reference.

Remaining release gates: POSIX regression execution and review of the Windows
assumptions/CI results. Tests do not certify every
separate-process reparse schedule, editor/antivirus configuration or power-loss
scenario. New Windows CI includes complete units (3.11/3.14) and a separate
pinned-host native/official-validation job; no remote run is claimed. Existing
Linux/macOS jobs remain. No live profiles, credentials, configuration, installed
plugin or core code were changed during that isolated validation phase.

## Authorized actual-host acceptance (local, not published)

Installed through official plugin management from `mforzano/hermes-muse`, immutable
pin `a13eef907c661fed6b25be64db27ea9a5c7f0c9f`, into the active default profile.
Actual host revision: `046094df28b712f62a5e3c728a9e59da05dcbf1e`, Python 3.11.15.
The installed checkout and development checkout both have two additional local,
unpublished fixes: explicitly bootstrap pywintypes before a cold win32api read;
and preserve unknown delivery status when the newer host has retired
`cron.bot_chat_delivery` while retaining canonical live-owner receipts.

The actual gateway-backed CLI made six successful `muse_manage` calls to create,
read, update, read back, cancel and verify a clearly labelled local acceptance
goal. Native host script launching executed all six installed Cron gates with
success. Official actual-host validation with `--install-deps --json` returned
`ok=true`; the dependency-less probe alone could not import pywintypes in its
separate environment. New regressions were observed failing before each fix.
The full isolated suite now passes 322 tests (one existing skip) on Python 3.11;
the 27 focused Windows tests pass with `-W error` on Python 3.14.

Activation used supported live plugin reload, not a gateway restart. Existing
session prompt/tool snapshots are deferred to a new conversation, which the
successful gateway CLI test created. Six original Cron job definitions were
preserved; normal scheduling independently advanced bookkeeping for one job.
SOUL and native memory hashes remain unchanged. All four labelled acceptance
goals are cancelled. All six Muse jobs remain paused pending an explicit decision
to permit autonomous monitoring. No external messages were sent; missing-receipt
lookup returns unknown, but end-to-end notification delivery has not been tested.
No live dashboard server was found; visual/UI/accessibility acceptance is not
claimed. Backups, scripts, CLI events and final readback evidence are retained in
`data/muse-windows-install-test/` beneath the authorized active Hermes home.
No Hermes core modifications or new publication were made.

