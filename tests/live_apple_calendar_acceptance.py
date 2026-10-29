"""Explicit opt-in live read acceptance. No private receipts are written or printed.

Run under the actual Hermes bootstrap; APPLE_PIM_REPO, APPLE_PIM_NODE and
APPLE_PIM_BIN_DIR must point to the existing authorized installation (the same
values as the apple-pim MCP server configuration). Never run in unit discovery.
"""
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


def main():
    os.environ['APPLE_PIM_BIN_DIR']  # Fail before any work when the runner location is not given.
    with tempfile.TemporaryDirectory(prefix='muse-apple-acceptance-') as temporary:
        home = Path(temporary)
        os.environ['HERMES_HOME'] = str(home)
        (home / 'config.yaml').write_text("plugins:\n  enabled: [hermes-muse]\ntimezone: ''\n")
        shutil.copytree(PROJECT, home / 'plugins/hermes-muse', ignore=shutil.ignore_patterns('.git', '__pycache__'))
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        token = set_hermes_home_override(home)
        try:
            from hermes_cli.plugins import discover_plugins
            from tools.registry import registry
            from cron.jobs import list_jobs
            discover_plugins(force=True)
            entry = registry.get_entry('muse_manage')
            def action(name, data=None):
                response = json.loads(entry.handler({'action': name, 'data': data or {}}))
                if not response['ok']:
                    raise RuntimeError('Native action failed: ' + name)
                return response['result']
            query = action('calendar_window')
            jobs = list_jobs(include_disabled=True)
            # Official runner, serial reads, one exact unchanged interval, no recurrence expansion of our own.
            script = r'''
import { createCLIRunner } from './lib/cli-runner.js';
const q = JSON.parse(process.env.MUSE_ACCEPTANCE_WINDOW);
const { runCLI } = createCLIRunner(process.env.APPLE_PIM_BIN_DIR);
const auth = await runCLI('calendar-cli', ['auth-status']);
// Read only after the effective runner route proves authorized.
if (auth.authorization !== 'authorized') throw new Error('Calendar not authorized');
const inv = await runCLI('calendar-cli', ['list']);
if (!inv.success) throw new Error('Inventory failed');
const at = () => new Date().toISOString();
const inventory = {pages:[{status:'checked',observed_at:at(),items:inv.calendars}]};
const calendars = [];
const counts = [];
for (const cal of inv.calendars) {
  const result = await runCLI('calendar-cli', ['events','--calendar',cal.id,'--from',q.start,'--to',q.end,'--limit','1000']);
  if (!result.success) throw new Error('Event read failed');
  // Swift formatDate uses ISO8601DateFormatter without fractional seconds.
  // Verify the echo at its actual precision; the query arguments remain unchanged.
  if (Date.parse(result.dateRange.from) !== Math.floor(Date.parse(q.start)/1000)*1000 || Date.parse(result.dateRange.to) !== Math.floor(Date.parse(q.end)/1000)*1000) throw new Error('Query interval changed');
  counts.push(result.events.length);
  calendars.push({resource_id:cal.id,window:q,pages:[{status:result.events.length>=1000?'partial':'checked',
    error:result.events.length>=1000?'limit reached; no continuation token':undefined,observed_at:at(),items:result.events}]});
}
const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
console.log(JSON.stringify({inventory,calendars,counts,device_timezone:{timezone:zone,timezone_kind:'iana'}}));
'''
            env = dict(os.environ, MUSE_ACCEPTANCE_WINDOW=json.dumps(query))
            result = subprocess.run([os.environ['APPLE_PIM_NODE'], '--input-type=module', '-e', script],
                                    cwd=os.environ['APPLE_PIM_REPO'], env=env, capture_output=True, text=True, timeout=180)
            if result.returncode:
                # Avoid dumping event payloads or identifiers from connector diagnostics.
                known = next((reason for reason in ('Calendar not authorized', 'Inventory failed', 'Event read failed', 'Query interval changed') if reason in result.stderr), 'private diagnostics suppressed')
                raise RuntimeError('Official Apple runner failed: ' + known)
            receipts = json.loads(result.stdout)
            source = {'source': {'connection': 'apple-pim', 'account': 'eventkit-local-store', 'resource': query['source_resource']},
                      'title': 'Apple Calendar', 'receipt_format': 'apple-pim',
                      'device_timezone': receipts['device_timezone'], 'inventory': receipts['inventory'], 'calendars': receipts['calendars']}
            view = action('calendar_view', {'window': query, 'sources': [source], 'discovery_complete': False})
            raw_count = sum(receipts['counts'])
            assert len(view['events']) == raw_count, 'Not every current Apple observation is displayed'
            assert all(row['event']['original']['startDate'] == row['event']['start'] for row in view['events'])
            assert not view['complete'] and view['coverage'][0]['source_check']['status'] == 'partial'
            assert view['calendar_writes'] == 0 and list_jobs(include_disabled=True) == jobs
            runtime = entry.handler.__self__
            assert not runtime.store.all('source_check') and not runtime.store.all('notification')
            summary = {'ok': True, 'window': query, 'device_timezone': receipts['device_timezone'],
                       'calendar_count': len(receipts['counts']), 'event_counts': receipts['counts'],
                       'raw_event_count': raw_count, 'displayed_events': len(view['events']),
                       'all_day_events': sum(r['event']['all_day'] for r in view['events']),
                       'rule_present_events': sum(r['event']['recurring'] for r in view['events']),
                       'source_status': view['coverage'][0]['source_check']['status'],
                       'read_resources': len(view['coverage'][0]['source_check']['checked_resources']),
                       'complete': view['complete'], 'gaps': view['gaps'], 'availability': view['availability'],
                       'calendar_writes': 0, 'checkpoints': 0, 'notifications': 0, 'jobs_unchanged': True}
            print(json.dumps(summary, ensure_ascii=False))
        finally:
            reset_hermes_home_override(token)


if __name__ == '__main__':
    main()
