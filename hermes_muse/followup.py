"""Read due goals and optional native owner status; never dispatch or change goals."""
import json
import re
import sqlite3
import time
from pathlib import Path

from .content_io import DomainError, Files
from .fade import Fade
from .views import Views


def owner(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {'kind', 'id'}:
        raise ValueError('owner uses kind and id')
    if value['kind'] not in {'native_delegation', 'cron', 'paseo', 'external'}:
        raise ValueError('Unknown owner kind')
    if not isinstance(value['id'], str) or not value['id'].strip() or len(value['id']) > 2000:
        raise ValueError('Owner handle must be nonempty text up to 2000 characters')
    return dict(value)


def owner_status(home, handle):
    """Best-effort native reads. A finished owner still needs the parent's judgment."""
    if not handle:
        return 'unassigned'
    if not isinstance(handle, dict):
        return 'unknown'
    kind, key = handle.get('kind'), handle.get('id')
    if kind not in {'native_delegation', 'cron'}:
        return 'unknown'  # Inspect optional external handles with their existing tools.
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', key):
        return 'unknown'
    path = Path(home) / ('state.db' if kind == 'native_delegation' else 'cron/executions.db')
    if path.is_symlink():
        return 'unknown'
    try:
        if path.is_file():
            with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
                db.execute('PRAGMA query_only=ON')
                if kind == 'cron':
                    row = db.execute('SELECT status FROM executions WHERE job_id=? ORDER BY claimed_at DESC LIMIT 1', (key,)).fetchone()
                    return row[0] if row else 'unknown'
                row = db.execute('SELECT state,result_json FROM async_delegations WHERE delegation_id=?', (key,)).fetchone()
                if row:
                    if row[0] != 'completed':
                        return row[0]
                    result = json.loads(row[1])
                    if not isinstance(result, dict):
                        return 'unknown'
                    children = result.get('results')
                    if not isinstance(children, list) or not children or any(not isinstance(r, dict) for r in children):
                        return 'unknown'
                    states = [r.get('status') for r in children]
                    return next((s for s in states if s != 'completed'), 'completed') or 'unknown'
        if kind == 'native_delegation':
            files = Files(Path(home))
            files.root = Path(home) / 'cache/delegation/live'
            raw = files.read(key + '/manifest.json')
            if len(raw) > 262144:
                return 'unknown'
            manifest = json.loads(raw)
            if not isinstance(manifest, dict):
                return 'unknown'
            tasks = manifest.get('tasks', [])
            if manifest.get('delegation_id') != key or not manifest.get('completed') or not tasks:
                return 'unknown'
            states = [t['status'] for t in tasks]
            return 'completed' if all(s == 'completed' for s in states) else next(s for s in states if s != 'completed')
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        pass
    return 'unknown'


def due_goals(home, now, session_id=None):
    view = Views(home)
    view.manifest()
    goals = [view.object('goal', key)[1] for key in view.keys('goal')]
    fade = Fade(view.home, goals, now)
    return view, [goal for goal in goals
                  if (goal['status'] == 'active' and (not session_id or goal.get('session_id') == session_id)
                      and goal.get('next_check_at', 0) <= now and not fade.dormant(goal))]


def snapshot(home, *, now=None, session_id=None, cursor='', limit=25):
    now = time.time() if now is None else now
    if not isinstance(cursor, str) or len(cursor) > 80 or type(limit) is not int or not 1 <= limit <= 50:
        raise DomainError('invalid_argument', 'Use a goal ID cursor and limit 1..50')
    view, due = due_goals(home, now, session_id)
    selected = [g for g in due if g['id'] > cursor][:limit]
    items = []
    from .responses import LIMIT
    for g in selected:
        item = {'id': g['id'], 'title': g['title'], 'status': g['status'], 'revision': g['revision'],
                'owner': g.get('owner'), 'owner_status': owner_status(view.home, g.get('owner')),
                'waiting_user': g.get('waiting_user', False), 'blocked': g.get('blocked'),
                'next_check_at': g['next_check_at']}
        if items and len(json.dumps(items + [item], ensure_ascii=False).encode()) > LIMIT - 2048:
            break
        items.append(item)
    return {'items': items, 'due_total': len(due),
            'next_cursor': items[-1]['id'] if items and any(g['id'] > items[-1]['id'] for g in due) else None,
            'pagination': 'Dormant goals (see context section=goal) are left out. Live goal IDs in order; restart a pass to include newly added earlier IDs.',
            'instruction': 'Inspect owner results, tell the user, then goal_update(status=done,note=...). Unknown writes are never automatically retried.'}


_shown = {}  # (home, session) -> fingerprint of the due set last injected in this process


def context(home, *, now=None, session_id=None):
    """Inject only when the due set changed; name the goals that have waited longest."""
    now = time.time() if now is None else now
    _, due = due_goals(home, now, session_id)
    key = (str(home), session_id)
    if not due:
        _shown.pop(key, None)
        return ''
    fingerprint = sorted((g['id'], g.get('next_check_at'), g.get('revision')) for g in due)
    if _shown.get(key) == fingerprint:
        return ''
    _shown[key] = fingerprint
    oldest = sorted(due, key=lambda g: (g.get('next_check_at', 0), g['id']))[:2]
    names = '; '.join(g['id'] + ' ' + g['title'][:60] for g in oldest)
    return (f"[Muse] {len(due)} active goal(s) need attention, past their next check; longest waiting: {names}.\n"
            "For each: read context(section=followups) and inspect its owner, then goal_update(id, progress=<what changed>) "
            "(moves its next check one day), goal_update(id, next_check_at=<when to look again>), or status=done/cancelled with a note. "
            "Respect changed user instructions and never resend an unknown write.")
