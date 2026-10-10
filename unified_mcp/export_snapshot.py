"""Stream locally synchronized history with transactional page checkpoints.

Resume rechecks all committed backend pages before advancing. SQLite is the
record journal; JSONL files are recoverable projections. The external database
is not frozen, so this detects changes across runs without claiming a DB snapshot.
"""
import argparse
import asyncio
from collections import Counter
import datetime as dt
import heapq
import json
from pathlib import Path

from unified_mcp.batch_support import atomic_json as write_json, canonical, digest, file_fingerprint, open_store, output_lock, project_jsonl
from unified_mcp.server import Gateway
from unified_mcp.timeline import decoded_result, normal_message, start_bound, TZ


def _stable_row(row):
    # Paths, decoder verdicts and OCR are rebuildable convenience fields. A
    # downloaded image must not look like an edit to the source message.
    derived = {'images', 'cached_images', 'videos', 'image_text', 'voice_transcript',
               'qq_media_resolution', 'wechat_media_resolution', 'wechat_video_resolution'}
    return {key: value for key, value in row.items() if key not in derived}


def _page(page):
    if page.get('errors') or page.get('error') or page.get('status') == 'partial':
        raise RuntimeError(f"Source returned errors: {page.get('errors') or page.get('error')}")
    rows, query = page.get('messages'), page.get('query', {})
    if not isinstance(rows, list) or type(query.get('has_more')) is not bool:
        raise RuntimeError('Source omitted messages or terminal pagination metadata')
    if any(not isinstance(row, dict) or row.get('error') for row in rows):
        raise RuntimeError('Source returned an unreadable row')
    return rows, query, digest({'rows': [_stable_row(row) for row in rows], 'has_more': query['has_more']})


def iter_jsonl(path):
    with Path(path).open(encoding='utf-8-sig') as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def merge_jsonl(paths, output):
    """Memory is O(number of sources); each input is already ordered."""
    streams = [iter_jsonl(path) for path in paths]
    try:
        return project_jsonl(output, heapq.merge(*streams, key=lambda row: (row['timestamp'], row['source'])))
    finally:
        for stream in streams:
            stream.close()


def _args(source, chat, scope, state):
    args = {'limit': scope['page_size'], 'offset': state.get('offset', 0), 'order': 'asc',
            'display_order': 'asc', 'before': start_bound(str(scope['before'])), 'include_image_text': False}
    if scope['after'] is not None:
        args['after'] = start_bound(str(scope['after']))
    if source == 'wechat':
        args.update(talker=chat, include_media_paths=False, include_images=False)
    else:
        args.update(contact=chat, chat_type=scope['qq_chat_type'], include_media=True)
        if state.get('cursor'):
            args['cursor'] = state['cursor']
            args.pop('offset', None)
    return args


def _initial_state(chat):
    return {'chat_id': chat, 'complete': False, 'pages': 0, 'records': 0,
            'duplicate_records': 0, 'message_id_collisions': 0, 'offset': 0,
            'warnings': [], 'kinds': {}, 'directions': {}}


def _states(db):
    return {source: json.loads(value) for source, value in db.execute('SELECT source,value FROM source_state ORDER BY source')}


def _project_sources(db, output):
    for source, in db.execute('SELECT source FROM source_state ORDER BY source'):
        project_jsonl(output / (source + '.jsonl'),
                      (row[0] for row in db.execute('SELECT payload FROM records WHERE source=? ORDER BY seq', (source,))))


async def export(options):
    output = Path(options.output).resolve()
    with output_lock(output):
        return await _export_locked(options, output)


async def _export_locked(options, output):
    manifest_path, store_path = output / 'coverage.json', output / '.export-checkpoint.sqlite3'
    resume = getattr(options, 'resume', False)
    existing = manifest_path.exists() or store_path.exists() or any(output.glob('*.jsonl'))
    if existing and not resume:
        raise FileExistsError('Choose a new output directory; existing exports are preserved. Use --resume for a checkpoint.')
    if resume and not store_path.is_file():
        raise ValueError('No transactional export checkpoint exists; choose a new output directory')
    chats = {key: getattr(options, key, None) for key in ('wechat', 'qq') if getattr(options, key, None)}
    if not chats:
        raise ValueError('Specify at least one resolved stable chat ID')
    if not 1 <= options.page_size <= 5000:
        raise ValueError('page-size must be between 1 and 5000')
    qq_type = getattr(options, 'qq_chat_type', 'private')
    if qq_type not in ('private', 'group', 'discuss'):
        raise ValueError('Invalid QQ chat type')
    db = open_store(store_path)
    gateway = None
    report = None
    verified = False
    try:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS source_state(source TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pages(source TEXT,number INTEGER,args TEXT,digest TEXT, PRIMARY KEY(source,number));
            CREATE TABLE IF NOT EXISTS records(seq INTEGER PRIMARY KEY,source TEXT,identity TEXT,
                message_id TEXT,record_id TEXT,payload TEXT,UNIQUE(source,identity));
            CREATE INDEX IF NOT EXISTS source_seq ON records(source,seq);
            CREATE INDEX IF NOT EXISTS native_ids ON records(source,message_id);
            CREATE INDEX IF NOT EXISTS local_ids ON records(source,record_id);
        ''')
        stored = db.execute("SELECT value FROM meta WHERE key='scope'").fetchone()
        previous_scope = json.loads(stored[0]) if stored else None
        if resume and previous_scope is None:
            raise ValueError('The checkpoint has no committed scope; choose a new output directory')
        before = getattr(options, 'before', None)
        if before is None:
            before = previous_scope['before'] if previous_scope else str(int(dt.datetime.now(TZ).timestamp()))
        scope = {'chats': chats, 'qq_chat_type': qq_type, 'after': getattr(options, 'after', None),
                 'before': before, 'page_size': options.page_size, 'schema': 2}
        if previous_scope and scope != previous_scope:
            raise ValueError('Resume inputs differ from the saved chat/range/type/page-size fingerprint')
        if not previous_scope:
            with db:
                db.execute("INSERT INTO meta VALUES('scope',?)", (canonical(scope),))
                db.execute("INSERT INTO meta VALUES('started_at',?)", (dt.datetime.now(TZ).isoformat(),))
                for source, chat in chats.items():
                    db.execute('INSERT INTO source_state VALUES(?,?)', (source, canonical(_initial_state(chat))))
        report = {'schema': 2, 'started_at': db.execute("SELECT value FROM meta WHERE key='started_at'").fetchone()[0],
                  'complete': False, 'scope': scope, 'input_fingerprint': digest(scope),
                  'before_epoch_exclusive': int(start_bound(str(scope['before']))), 'sources': _states(db),
                  'coverage': 'Locally synchronized records only; not an immutable database snapshot.',
                  'checkpoint': 'SQLite page commits; JSONL files are derived and rebuilt on resume.',
                  'media_content_read': False,
                  'media_note': 'No semantic media reading or OCR. WeChat media decoding is disabled; QQ may validate local images while preserving media hints.'}
        gateway = Gateway()
        if resume:
            for source, number, args_json, expected in db.execute('SELECT source,number,args,digest FROM pages ORDER BY source,number'):
                _, _, actual = _page(decoded_result(await gateway.fetch(source, 'chat_timeline', json.loads(args_json))))
                if actual != expected:
                    raise ValueError(f'Source history changed at {source} page {number}; saved export preserved, start a new snapshot')
            report['resume_revalidated_pages'] = db.execute('SELECT COUNT(*) FROM pages').fetchone()[0]
        verified = True
        write_json(manifest_path, report)
        for source, chat in chats.items():
            state = _states(db)[source]
            while not state['complete']:
                args = _args(source, chat, scope, state)
                payload = decoded_result(await gateway.fetch(source, 'chat_timeline', args))
                rows, query, page_hash = _page(payload)
                next_offset, next_cursor = query.get('next_offset'), query.get('next_cursor')
                if query['has_more']:
                    cursor_advances = isinstance(next_cursor, str) and bool(next_cursor) and next_cursor != state.get('cursor')
                    offset_advances = type(next_offset) is int and next_offset > state['offset']
                    if not rows or not (cursor_advances if next_cursor else offset_advances):
                        raise RuntimeError(f'{source} pagination stopped advancing')
                if query['has_more'] and next_cursor and db.execute(
                    "SELECT 1 FROM pages WHERE source=? AND json_extract(args,'$.cursor')=? LIMIT 1",
                    (source, next_cursor)).fetchone():
                    raise RuntimeError(f'{source} pagination cursor repeated an earlier page')
                kinds, directions = Counter(state['kinds']), Counter(state['directions'])
                previous_count = state['records']
                with db:
                    for row in rows:
                        native_identity = row.get('id') if isinstance(row.get('id'), dict) else {}
                        native_chat = (row.get('talker') or native_identity.get('talker')) if source == 'wechat' else row.get('chat_id')
                        if native_chat and str(native_chat) != str(chat):
                            raise RuntimeError(f'{source} returned a record from a different chat')
                        msg, identity = normal_message(source, row, chat), digest(_stable_row(row))
                        if not msg['message_id'] or not isinstance(msg['timestamp'], (int, float)):
                            raise RuntimeError(f'{source} returned a row without identity/time')
                        if msg['timestamp'] >= int(args['before']) or ('after' in args and msg['timestamp'] < int(args['after'])):
                            raise RuntimeError(f'{source} returned a record outside the requested time range')
                        if db.execute('SELECT 1 FROM records WHERE source=? AND identity=?', (source, identity)).fetchone():
                            state['duplicate_records'] += 1
                            continue
                        if state.get('last_timestamp') is not None and msg['timestamp'] < state['last_timestamp']:
                            raise RuntimeError(f'{source} page order moved backwards')
                        if db.execute('SELECT 1 FROM records WHERE source=? AND message_id=?', (source, msg['message_id'])).fetchone():
                            state['message_id_collisions'] += 1
                        if db.execute('SELECT 1 FROM records WHERE source=? AND record_id=?', (source, msg['record_id'])).fetchone():
                            msg['record_id'] += ':' + identity
                        db.execute('INSERT INTO records(source,identity,message_id,record_id,payload) VALUES(?,?,?,?,?)',
                                   (source, identity, msg['message_id'], msg['record_id'], canonical(msg)))
                        state['records'] += 1
                        state['last_timestamp'] = msg['timestamp']
                        state.setdefault('first_time', msg['time'])
                        state['last_time'] = msg['time']
                        kinds[msg.get('kind') or 'unknown'] += 1
                        directions[msg['direction']] += 1
                    if query['has_more'] and state['records'] == previous_count:
                        raise RuntimeError(f'{source} pagination returned only previously committed records')
                    state.update(pages=state['pages'] + 1, last_query=query, kinds=dict(kinds), directions=dict(directions),
                                 complete=not query['has_more'])
                    for warning in payload.get('warnings', []):
                        if warning not in state['warnings']:
                            state['warnings'].append(warning)
                    if query['has_more']:
                        if isinstance(next_cursor, str) and next_cursor:
                            state['cursor'] = next_cursor
                        if type(next_offset) is int:
                            state['offset'] = next_offset
                    db.execute('INSERT INTO pages VALUES(?,?,?,?)', (source, state['pages'], canonical(args), page_hash))
                    db.execute('UPDATE source_state SET value=? WHERE source=?', (canonical(state), source))
                report['sources'] = _states(db)
                write_json(manifest_path, report)
                if state['pages'] == 1 or state['pages'] % 10 == 0 or state['complete']:
                    print(f"{source}: {state['records']} records / {state['pages']} pages; complete={state['complete']}", flush=True)
        _project_sources(db, output)
        report['total_records'] = merge_jsonl([output / (source + '.jsonl') for source in chats], output / 'merged.jsonl')
        files = [*(output / (source + '.jsonl') for source in chats), output / 'merged.jsonl']
        report['files'] = {path.name: file_fingerprint(path) for path in files}
        report.update(complete=True, finished_at=dt.datetime.now(TZ).isoformat())
        write_json(manifest_path, report)
        return report
    except BaseException as exc:
        if report is not None and verified:
            _project_sources(db, output)
            report.update(sources=_states(db), complete=False, error=f'{type(exc).__name__}: {exc}')
            write_json(manifest_path, report)
        raise
    finally:
        db.close()
        if gateway is not None:
            await gateway.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wechat')
    parser.add_argument('--qq')
    parser.add_argument('--qq-chat-type', choices=('private', 'group', 'discuss'), default='private')
    parser.add_argument('--after')
    parser.add_argument('--before')
    parser.add_argument('--output', required=True)
    parser.add_argument('--page-size', type=int, default=5000)
    parser.add_argument('--resume', action='store_true')
    options = parser.parse_args()
    print(json.dumps(asyncio.run(export(options)), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
