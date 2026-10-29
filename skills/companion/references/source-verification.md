# Post-deployment data source verification

Run this after installing or upgrading Muse, after changing the sources in TOOLS.md, and before enabling or changing Muse Cron jobs. It is read-only: it never authorizes a connector, writes to a source or edits Cron except the explicit pause/resume below. Per-source commands live in each procedure linked from [sources](sources.md).

## 1. Hold the source patrols

Muse creates its six jobs on first load, enabled. Until verification passes, pause the two that read external sources (the user runs these, or confirms before the assistant does):

```bash
hermes cron list                      # note the IDs of muse-patrol and muse-feed
hermes cron pause <patrol-id>
hermes cron pause <feed-id>
```

Upgrades keep an existing pause, schedule and model. Intake, nightly and weekly read only Muse and native state and may keep running.

## 2. List the declared sources

Read TOOLS.md with `muse_manage read {kind:"tools_note"}`. Every source recorded there is checked. Add any layer 1 or 2 source that the profile's Coverage scope includes and that is installed but not yet recorded. Outside-scope sources are not checked.

## 3. Run each self-check where the Cron jobs run

Run the self-check from each source's procedure in the same execution context as the scheduler: ask this profile's assistant in its Bot Chat (or another gateway conversation of the profile), so its terminal and MCP tools run inside the gateway. macOS privacy grants belong to the launching app, so a check from an unrelated terminal can disagree with what the Cron jobs see. Shell checks the user can run directly:

```bash
hermes tools list                                    # web toolset enabled
hermes mcp test apple_pim                            # Apple PIM server loads (if registered)
python "${HERMES_HOME:-$HOME/.hermes}/skills/productivity/google-workspace/scripts/setup.py" --check
himalaya account list
remindctl status
imsg chats --limit 1 --json
memo notes --flist
```

MCP permission checks (Apple PIM `apple-pim` `action:"status"`) and personal sources' own self-checks run as tool calls in that conversation.

## 4. Report every source

One row per declared source, nothing omitted:

| Source | Route | State | Cause | Fix |
| --- | --- | --- | --- | --- |
| Google Calendar | google-workspace | connected | 4 calendars listed | — |
| Apple Reminders | remindctl | not connected | status: denied | Grant Reminders access to the gateway's app in System Settings > Privacy & Security |
| Work tracker | worktracker CLI | failing | whoami timed out | Check VPN, rerun |

States follow [sources §Connection states](sources.md#connection-states): connected, not connected (missing install, sign-in or permission) or failing (installed and authorized, but the read failed). Report counts only; do not paste private items into the report.

## 5. Resume only when every row is resolved

Fix not-connected and failing sources with their procedures and rerun their checks. A source the user decides to live without is either removed from TOOLS.md or recorded as an accepted gap in the Coverage scope prose of PROACTIVE_PREFERENCES.md (`Accepted gap: Apple Reminders not connected, accepted <date>`), set with `preferences.notes`. Only when every row is connected or explicitly accepted:

```bash
hermes cron resume <patrol-id>
hermes cron resume <feed-id>
```

Update each verified TOOLS.md section's verification date and any corrected command through `content_write`.

## 6. Recheck after the first patrol

When the first resumed patrol has finished (`hermes cron runs <patrol job id>` shows it completed), compare each source's state with step 4: in a conversation, `muse_manage read {kind:"source"}` lists every observed source with `ok`, `limited` or `failing`, and `read {kind:"source", id}` shows its last limitation or error.
