"""One-time 0.2.x goal migration, with a file archive and guarded offline rollback."""
import json
import os
import time
import uuid
from pathlib import Path

from .content_io import Files, revision
from .locks import file_lock
from .store import Store

ARCHIVE = 'archive/followup-0.2.x'
REMOVED = ('followup_',)


def migrate(store):
    with file_lock(store.root / 'mutations.lock'):
        if store.read('meta', 'goal_schema') == 3:
            return {'migrated': False}
        files = Files(store.home)
        manifest_path = ARCHIVE + '/manifest.json'
        try:
            previous = json.loads(files.read(manifest_path))
        except FileNotFoundError:
            previous = {}
        if previous.get('rolled_back_at'):
            # Keep the completed cycle intact; a downgraded install may have new work.
            with files.directory('archive') as parent:
                target = 'followup-0.2.x-' + uuid.uuid4().hex
                if os.name == 'nt':
                    from .content_io import _windows_child, _windows_rename
                    source = _windows_child(parent, 'followup-0.2.x', directory=True, delete=True)
                    try:
                        _windows_rename(source, parent, target)
                    finally:
                        source.Close()
                else:
                    os.rename('followup-0.2.x', target, src_dir_fd=parent, dst_dir_fd=parent)
                    os.fsync(parent)
        records_path = ARCHIVE + '/records.json'
        try:
            records = json.loads(files.read(records_path))
        except FileNotFoundError:
            with store.transaction() as db:
                records = [{'kind': kind, 'id': key, 'data': json.loads(raw)}
                           for kind, key, raw in db.execute('SELECT kind,id,data FROM records')
                           if kind.startswith(REMOVED) or kind in {'mutation_request', 'tool_result'} and json.loads(raw).get('action') in {'followup_update', 'goal_create', 'goal_update'}]
            store.write_text(records_path, json.dumps(records, ensure_ascii=False), create_only=True)
        try:
            manifest = json.loads(files.read(manifest_path))
        except FileNotFoundError:
            manifest = {'at': time.time(), 'goals': {}, 'records_hash': revision(files.read(records_path))}
            store.write_text(manifest_path, json.dumps(manifest, ensure_ascii=False))
        for goal in store.goals():
            key = goal['id']
            relative = f'workspace/goals/{key}/GOAL.md'
            archived = f'{ARCHIVE}/goals/{key}.md'
            if key not in manifest['goals']:
                raw = files.read(relative)
                store.write_text(archived, raw.decode(), create_only=True)
                manifest['goals'][key] = {'before': revision(files.read(archived))}
                store.write_text(manifest_path, json.dumps(manifest, ensure_ascii=False))
            raw = files.read(archived)
            text = raw.decode().replace('\r\n', '\n').replace('\r', '\n')
            first, _, body = text.partition('\n')
            original = json.loads(first[17:-4])
            current = files.read(relative)
            if revision(current) == manifest['goals'][key].get('after'):
                continue
            goal = dict(original)
            old = goal.pop('followup', {})
            goal['status'] = {'completed': 'done'}.get(goal['status'], goal['status'])
            if goal['status'] not in {'active', 'done', 'cancelled'}:
                raise ValueError('Unknown goal status; preserve the migration archive')
            goal.update(revision=1, owner=goal.get('owner') or old.get('owner'),
                        next_check_at=old.get('next_check_at', goal.get('deadline') or manifest['at']),
                        waiting_user=old.get('execution_state') == 'needs_user',
                        blocked=old.get('blocker_reason'))
            for field in ('outcome_refs', 'outcome_evidence', '_request_id'):
                goal.pop(field, None)
            transformed = ('<!-- hermes-muse ' + json.dumps(goal, ensure_ascii=False, allow_nan=False) + ' -->\n' + body).encode()
            if revision(current) not in {manifest['goals'][key]['before'], revision(transformed)}:
                raise ValueError('Goal changed during migration; preserve archive and merge explicitly')
            files.replace(relative, transformed, revision(current))
            manifest['goals'][key]['after'] = revision(transformed)
            store.write_text(manifest_path, json.dumps(manifest, ensure_ascii=False))
        with store.transaction() as db:
            for row in records:
                store.delete(db, row['kind'], row['id'])
            store.put(db, 'meta', 'goal_schema', 3)
        return {'migrated': True, 'goals': len(manifest['goals']), 'archived_records': len(records)}


def rollback(store):
    """Offline only: refuse newer goal edits; never restore a whole state.db."""
    with file_lock(store.root / 'mutations.lock'):
        files = Files(store.home)
        manifest = json.loads(files.read(ARCHIVE + '/manifest.json'))
        raw_records = files.read(ARCHIVE + '/records.json')
        if revision(raw_records) != manifest['records_hash']:
            raise ValueError('Migration archive changed; rollback refused')
        records = json.loads(raw_records)
        current_keys = {g['id'] for g in store.goals()}
        if current_keys != set(manifest['goals']):
            raise ValueError('Goals changed after migration; do not overwrite newer work')
        for key, entry in manifest['goals'].items():
            if revision(files.read(f'{ARCHIVE}/goals/{key}.md')) != entry['before']:
                raise ValueError('Original goal archive changed; rollback refused')
            if revision(files.read(f'workspace/goals/{key}/GOAL.md')) != entry['after']:
                raise ValueError('Goal changed after migration; rollback refused')
        with store.transaction() as db:
            for row in records:
                if store.get(db, row['kind'], row['id']) is not None:
                    raise ValueError('Archived record ID has newer data; rollback refused')
            for key in manifest['goals']:
                relative = f'workspace/goals/{key}/GOAL.md'
                files.replace(relative, files.read(f'{ARCHIVE}/goals/{key}.md'), manifest['goals'][key]['after'])
            for row in records:
                store.put(db, row['kind'], row['id'], row['data'])
            store.delete(db, 'meta', 'goal_schema')
        manifest['rolled_back_at'] = time.time()
        store.write_text(ARCHIVE + '/manifest.json', json.dumps(manifest, ensure_ascii=False))
        return {'restored_goals': len(manifest['goals']), 'restored_records': len(records)}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Offline Muse 0.3 goal migration/rollback; back up first, stop writers.')
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--rollback', action='store_true')
    args = parser.parse_args()
    print(json.dumps((rollback if args.rollback else migrate)(Store(args.home))))
