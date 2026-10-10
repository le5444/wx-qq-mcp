"""Resumable local media stages for single- or dual-platform snapshots.

SQLite commits each completed stage and reconstructs the JSONL projection after
interruptions. Failed stages are explicit and retryable; ASR is opt-in. No remote
media downloads or modifications to source snapshots are performed.
"""
from __future__ import annotations
import argparse
import asyncio
from collections import Counter
import copy
import json
import os
from pathlib import Path
import time

from unified_mcp.batch_support import atomic_json, canonical, digest, file_fingerprint, open_store, output_lock, project_jsonl, snapshot_sources
from unified_mcp.image_text import ImageTextReader, image_reference_rank, OCR_PREPROCESSING_VERSION
from unified_mcp.voice_transcription import VoiceService
from unified_mcp import voice_transcription as voice_module
from unified_mcp.wechat_media import enrich_wechat_media
from unified_mcp.video_media import enrich_wechat_video
from unified_mcp.qq_media import resolve_message_images

STAGES = ('media', 'voice', 'video', 'ocr')


class LocalMetadata:
    """Lazily ask the local reader for one exact message and its resources."""
    def __init__(self):
        self.gateway = None

    async def load(self, row, include_paths=False):
        original = row.get('original') or {}
        identity = original.get('id') if isinstance(original.get('id'), dict) else original
        talker = row.get('chat_id') or original.get('talker') or identity.get('talker')
        server = identity.get('server_id_str') or original.get('server_id_str')
        local_id = identity.get('local_id')
        timestamp = row.get('timestamp', original.get('create_time'))
        if not talker or timestamp is None or (not server and local_id is None):
            return [], []
        if self.gateway is None:
            from unified_mcp.server import Gateway
            self.gateway = Gateway()

        async def call(name, args):
            result = await self.gateway.wechat.call(name, args)
            if isinstance(result, (dict, list)):
                value = result
            else:
                if result.isError:
                    raise RuntimeError('Local media metadata query failed')
                blocks = [block.text for block in result.content if block.type == 'text']
                if len(blocks) != 1:
                    raise RuntimeError('Unexpected local media metadata response')
                value = json.loads(blocks[0])
            if isinstance(value, dict):
                if value.get('error') or value.get('errors'):
                    raise RuntimeError('Local media metadata query failed')
                value = value.get('messages', value.get('resources', []))
            if not isinstance(value, list):
                raise RuntimeError('Local media metadata did not return records')
            return value

        bound = {'talker': talker, 'after': str(int(timestamp)), 'before': str(int(timestamp) + 1)}
        resource_args = {**bound, 'limit': 100, 'include_local_paths': include_paths, 'include_debug': True}
        if server:
            resource_args['server_id_str'] = str(server)
        if local_id is not None:
            resource_args['local_id'] = int(local_id)
        resources = await call('media_resources', resource_args)
        rows = await call('messages', {**bound, 'type': row.get('kind'), 'fields': 'full',
                                      'limit': 1000, 'include_media_paths': include_paths})
        exact = []
        for candidate in rows:
            native = candidate.get('id') if isinstance(candidate.get('id'), dict) else candidate
            if local_id is not None and str(native.get('local_id')) != str(local_id):
                continue
            if server and str(native.get('server_id_str') or candidate.get('server_id_str')) != str(server):
                continue
            if candidate.get('create_time') is not None and candidate['create_time'] != timestamp:
                continue
            exact.append(candidate)
        # Multiple exact matches may be shard collisions; never guess an audio.
        if len(exact) > 1:
            raise ValueError('Ambiguous local message identity; media was not assigned')
        return exact, resources

    async def close(self):
        if self.gateway is not None:
            await self.gateway.close()


def image_references(value, best_only=False):
    """Keep per-message occurrence identity; select variants only within an ID."""
    result = []
    def visit(node, prefix):
        if isinstance(node, dict):
            for key, child in node.items():
                if key in ('images', 'cached_images') and isinstance(child, list):
                    groups = {}
                    for index, item in enumerate(child):
                        if not isinstance(item, dict):
                            continue
                        path = item.get('path') or item.get('cache_path')
                        if not path:
                            continue
                        identity = str(item.get('media_id') or item.get('md5') or item.get('hint') or index)
                        ref = {'path': path, 'media_ref': f'{prefix}/{key}/{index}', 'identity': identity, 'reference': item}
                        # Only explicit variant sets describe alternate files for
                        # one occurrence. Repeated images remain repeated refs.
                        group = item.get('variant_group')
                        if best_only and group:
                            old = groups.get(str(group))
                            if old is None or image_reference_rank(item) > image_reference_rank(old['reference']):
                                groups[str(group)] = ref
                        else:
                            result.append(ref)
                    result.extend(groups.values())
                elif isinstance(child, (dict, list)):
                    visit(child, prefix + '/' + key)
        elif isinstance(node, list):
            for index, child in enumerate(node):
                visit(child, prefix + '/' + str(index))
    visit(value, '')
    return result


def image_paths(value, best_only=False):
    return [ref['path'] for ref in image_references(value, best_only)]


def _model_revision():
    result = {'engine': os.environ.get('WX_UNIFIED_ASR_ENGINE', 'auto')}
    for name, folder, files in [('whisper', voice_module.MODEL, ('model.bin',)),
                                ('sensevoice', voice_module.SENSE_MODEL, ('model.int8.onnx', 'tokens.txt'))]:
        result[name] = {'path': str(folder), 'files': []}
        for file in files:
            try:
                stat = (folder / file).stat()
                result[name]['files'].append((file, stat.st_size, stat.st_mtime_ns))
            except OSError:
                result[name]['files'].append((file, 'missing'))
    return result


def _stage_versions(transcribe, qq_data_root):
    return {'media': digest({'v': 2, 'qq_data_root': str(qq_data_root or '')}),
            'voice': digest({'v': 2, 'transcribe': transcribe, 'models': _model_revision()}),
            'video': '2', 'ocr': str(OCR_PREPROCESSING_VERSION)}


def _stage_version(stage, version, row):
    if stage != 'ocr':
        return version
    references = []
    for ref in image_references(row.get('original', {})):
        try:
            stat = Path(ref['path']).stat()
            revision = [stat.st_size, stat.st_mtime_ns]
        except OSError:
            revision = ['missing']
        references.append([ref['media_ref'], ref['path'], revision])
    return digest({'version': version, 'references': references})


def _iter_source(snapshot, source):
    with (snapshot / (source + '.jsonl')).open(encoding='utf-8-sig') as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or row.get('source') != source or not row.get('record_id'):
                raise ValueError(f'Invalid normalized record in {source} line {number}')
            yield row


def _media_row(row):
    original = row.get('original') or {}
    return row.get('kind') in ('image', 'sticker', 'emoji', 'voice', 'video', 'file', 'mixed') or bool(
        original.get('images') or original.get('videos') or original.get('media') or original.get('qq_media_resolution'))


def _status(stage, row):
    original = row['original']
    if stage == 'voice':
        status = original.get('voice_transcript', {}).get('status', 'not_transcribed')
        return 'ok' if status in ('ok', 'no_speech') else 'failed'
    if stage == 'ocr':
        results = row.get('image_text', [])
        return 'ok' if all(item.get('status') in ('ok', 'no_text') for item in results) else 'failed'
    resolution = original.get('wechat_video_resolution' if stage == 'video' else
                              'wechat_media_resolution' if row['source'] == 'wechat' else 'qq_media_resolution', {})
    status = resolution.get('status', '')
    return 'ok' if status in ('ok', 'available', 'readable_local', 'playable_local', 'recovered', 'local_available') or (
        stage == 'media' and bool(original.get('images')) and status not in ('partial', 'unavailable')) else 'failed'


def _applicable(stage, row):
    kind = row.get('kind')
    if stage == 'voice':
        return kind == 'voice'
    if stage == 'video':
        return kind == 'video'
    if stage == 'ocr':
        return bool(image_references(row.get('original', {})))
    return kind not in ('voice', 'video')


def _project(db, result_file):
    return project_jsonl(result_file, (row[0] for row in db.execute('SELECT payload FROM work ORDER BY seq')))


async def run(snapshot, output, qq_enriched=None, limit=None, resume=False, *,
              transcribe=False, retry_failed=False, refresh=False, stages=None, qq_data_root=None):
    snapshot, output = Path(snapshot).resolve(), Path(output).resolve()
    qq_data_root = qq_data_root or os.environ.get('QQ_MCP_DATA_ROOT')
    if not qq_data_root and os.environ.get('QQ_MCP_DB_ROOT', '').strip():
        qq_data_root = Path(os.environ['QQ_MCP_DB_ROOT']).expanduser().parent / 'nt_data'
    stages = tuple(stages or STAGES)
    if not stages or any(stage not in STAGES for stage in stages):
        raise ValueError('Stages must be selected from media, voice, video, ocr')
    # Dependencies retain the public order regardless of CLI ordering.
    stages = tuple(stage for stage in STAGES if stage in stages)
    if limit is not None and limit < 1:
        raise ValueError('Limit must be positive')
    with output_lock(output):
        return await _run_locked(snapshot, output, qq_enriched, limit, resume,
                                 transcribe, retry_failed, refresh, stages, qq_data_root)


async def _run_locked(snapshot, output, qq_enriched, limit, resume, transcribe, retry_failed, refresh, stages, qq_data_root):
    result_file, summary_file, checkpoint = output / 'media-messages.jsonl', output / 'summary.json', output / '.media-checkpoint.sqlite3'
    if (result_file.exists() or checkpoint.exists() or summary_file.exists()) and not resume:
        raise FileExistsError('Use a new media output directory; previous records are preserved')
    if resume and not checkpoint.is_file():
        raise ValueError('No transactional media checkpoint; choose a new directory for legacy results')
    sources = snapshot_sources(snapshot)
    files = {source: file_fingerprint(snapshot / (source + '.jsonl')) for source in sources}
    if (snapshot / 'coverage.json').exists():
        coverage = json.loads((snapshot / 'coverage.json').read_text(encoding='utf-8'))
        # Revalidating an export updates finish times but not its content/range.
        # Bind to semantic scope, rather than volatile diagnostics/timestamps.
        files['coverage'] = {'sha256': digest({'complete': coverage.get('complete'), 'scope': coverage.get('scope'),
            'sources': {name: {key: state.get(key) for key in ('chat_id', 'records', 'complete')}
                        for name, state in coverage.get('sources', {}).items()}})}
    if qq_enriched:
        files['qq_enriched'] = file_fingerprint(qq_enriched)
    scope = {'snapshot': str(snapshot), 'files': files, 'sources': sources}
    versions = _stage_versions(transcribe, qq_data_root)
    db = open_store(checkpoint)
    voices, reader, metadata = None, None, LocalMetadata()
    report = {'state': 'running', 'snapshot': str(snapshot), 'output': str(result_file), 'sources': sources,
              'input_fingerprint': digest(scope), 'processed': 0, 'stages': list(stages),
              'audio_transcription_mode': 'local_asr' if transcribe else 'success_cache_only',
              'semantic_scope': 'Local availability and OCR/ASR; no claim to understand all images or whole videos.'}
    verified = False
    started = time.monotonic()
    try:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS work(seq INTEGER PRIMARY KEY,source TEXT,record_id TEXT,payload TEXT,
                UNIQUE(source,record_id));
            CREATE TABLE IF NOT EXISTS stages(source TEXT,record_id TEXT,name TEXT,version TEXT,status TEXT,
                PRIMARY KEY(source,record_id,name));
            CREATE TABLE IF NOT EXISTS qq_updates(record_id TEXT PRIMARY KEY,msg_id TEXT,payload TEXT);
            CREATE INDEX IF NOT EXISTS qq_native_id ON qq_updates(msg_id);
        ''')
        saved = db.execute("SELECT value FROM meta WHERE key='scope'").fetchone()
        if saved and json.loads(saved[0]) != scope:
            raise ValueError('Resume input snapshot changed; existing results are preserved')
        if not saved:
            with db:
                db.execute("INSERT INTO meta VALUES('scope',?)", (canonical(scope),))
                if qq_enriched:
                    with Path(qq_enriched).open(encoding='utf-8-sig') as stream:
                        for number, line in enumerate(stream):
                            if not line.strip():
                                continue
                            item = json.loads(line)
                            raw = item.get('original', item)
                            db.execute('INSERT INTO qq_updates VALUES(?,?,?)',
                                       (item.get('record_id', f'legacy:{number}'), str(raw.get('msg_id', item.get('message_id', ''))), canonical(raw)))
        verified = True
        voices, reader = VoiceService(), ImageTextReader()
        handled = 0
        for source in sources:
            for incoming in _iter_source(snapshot, source):
                if not _media_row(incoming):
                    continue
                key = (source, incoming['record_id'])
                existing = db.execute('SELECT payload FROM work WHERE source=? AND record_id=?', key).fetchone()
                row = json.loads(existing[0]) if existing else copy.deepcopy(incoming)
                did_work = not existing
                row.setdefault('original', {})
                if source == 'qq' and not existing:
                    update = db.execute('SELECT payload FROM qq_updates WHERE record_id=?', (row['record_id'],)).fetchone()
                    if update is None:
                        matches = db.execute('SELECT payload FROM qq_updates WHERE msg_id=? LIMIT 2', (str(row.get('message_id', '')),)).fetchall()
                        # Legacy native IDs may collide; do not guess which row.
                        update = matches[0] if len(matches) == 1 else None
                    if update:
                        row['original'] = json.loads(update[0])
                for stage in stages:
                    if not _applicable(stage, row):
                        continue
                    current_version = _stage_version(stage, versions[stage], row)
                    saved_stage = db.execute('SELECT version,status FROM stages WHERE source=? AND record_id=? AND name=?', (*key, stage)).fetchone()
                    if saved_stage and saved_stage[0] == current_version and not refresh and not (retry_failed and saved_stage[1] != 'ok'):
                        continue
                    did_work = True
                    status = 'failed'
                    try:
                        if stage == 'media':
                            if source == 'wechat':
                                row['original'] = await asyncio.to_thread(enrich_wechat_media, row['original'])
                                resolution = row['original'].get('wechat_media_resolution', {})
                                if resolution.get('status') in ('metadata_missing', 'unavailable') and not row['original'].get('images'):
                                    details, resources = await metadata.load(row)
                                    if details or resources:
                                        row['original'] = await asyncio.to_thread(enrich_wechat_media, row['original'], details, resources)
                            elif qq_data_root:
                                row['original'] = await asyncio.to_thread(resolve_message_images, row['original'], qq_data_root)
                            status = _status(stage, row)
                        elif stage == 'voice':
                            if source == 'wechat':
                                row['original'] = await voices.enrich(row['original'], transcribe=transcribe)
                                if transcribe and row['original'].get('voice_transcript', {}).get('status') == 'audio_missing':
                                    details, resources = await metadata.load(row, include_paths=True)
                                    # Exact message payloads can carry decoded or
                                    # SILK paths even when the snapshot omitted them.
                                    refreshed = copy.deepcopy(row['original'])
                                    if details:
                                        refreshed['local_media_metadata'] = details[0]
                                    if resources:
                                        refreshed['local_media_resources'] = resources
                                    row['original'] = await voices.enrich(refreshed, transcribe=True)
                                status = _status(stage, row)
                            else:
                                status = 'unsupported'
                        elif stage == 'video':
                            if source == 'wechat':
                                row['original'] = await asyncio.to_thread(enrich_wechat_video, row['original'])
                                if row['original'].get('wechat_video_resolution', {}).get('status') == 'metadata_missing':
                                    _, resources = await metadata.load(row)
                                    if resources:
                                        row['original'] = await asyncio.to_thread(enrich_wechat_video, row['original'], resources)
                                status = _status(stage, row)
                            else:
                                status = 'unsupported'
                        elif stage == 'ocr':
                            results = []
                            for ref in image_references(row['original'], best_only=source == 'wechat'):
                                value = await asyncio.to_thread(reader.read, ref['path'])
                                results.append({'path': ref['path'], 'media_ref': ref['media_ref'], 'media_identity': ref['identity'], **value})
                            row['image_text'] = results
                            status = _status(stage, row)
                        detail = {'status': status, 'version': current_version, 'retryable': status != 'ok'}
                    except Exception as exc:
                        detail = {'status': 'failed', 'version': current_version, 'retryable': True,
                                  'error_type': type(exc).__name__, 'error': str(exc)}
                    row.setdefault('processing_stages', {})[stage] = detail
                    with db:
                        db.execute('INSERT INTO work(source,record_id,payload) VALUES(?,?,?) ON CONFLICT(source,record_id) DO UPDATE SET payload=excluded.payload', (*key, canonical(row)))
                        db.execute('INSERT INTO stages VALUES(?,?,?,?,?) ON CONFLICT(source,record_id,name) DO UPDATE SET version=excluded.version,status=excluded.status',
                                   (*key, stage, current_version, detail['status']))
                # Keep unsupported/file rows visible, even when no stage applies.
                with db:
                    db.execute('INSERT OR IGNORE INTO work(source,record_id,payload) VALUES(?,?,?)', (*key, canonical(row)))
                handled += bool(did_work)
                if did_work and handled % 100 == 0:
                    report.update(processed=db.execute('SELECT COUNT(*) FROM work').fetchone()[0], elapsed_seconds=round(time.monotonic() - started, 2))
                    atomic_json(summary_file, report)
                if limit and handled >= limit:
                    report['state'] = 'paused_limit'
                    break
            if report['state'] == 'paused_limit':
                break
        if report['state'] == 'running':
            report['state'] = 'complete_with_errors' if db.execute("SELECT 1 FROM stages WHERE status!='ok' LIMIT 1").fetchone() else 'complete'
    except BaseException as exc:
        if verified:
            report.update(state='interrupted' if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)) else 'failed',
                          error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        try:
            if verified:
                report['processed'] = _project(db, result_file)
                report['stage_counts'] = {f'{name}:{status}': count for name, status, count in db.execute('SELECT name,status,COUNT(*) FROM stages GROUP BY name,status')}
                report['elapsed_seconds'] = round(time.monotonic() - started, 2)
                report['files'] = {result_file.name: file_fingerprint(result_file)}
                atomic_json(summary_file, report)
        finally:
            db.close()
            try:
                if voices is not None:
                    await voices.close()
            finally:
                try:
                    if reader is not None:
                        await asyncio.to_thread(reader.close)
                finally:
                    await metadata.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--qq-enriched', type=Path)
    parser.add_argument('--qq-data-root', type=Path)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--transcribe', action='store_true', help='Run local ASR for uncached WeChat voice messages')
    parser.add_argument('--retry-failed', action='store_true')
    parser.add_argument('--refresh', action='store_true', help='Revisit selected stages, using content-addressed engine caches')
    parser.add_argument('--stages', nargs='+', choices=STAGES)
    parser.add_argument('--limit', type=int)
    args = parser.parse_args()
    report = asyncio.run(run(args.snapshot, args.output, args.qq_enriched, args.limit, args.resume,
                            transcribe=args.transcribe, retry_failed=args.retry_failed, refresh=args.refresh,
                            stages=args.stages, qq_data_root=args.qq_data_root))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
