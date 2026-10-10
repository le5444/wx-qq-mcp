"""One local job: snapshot export -> retryable media stages -> offline reader.

Every stage owns a durable checkpoint. --resume verifies the original inputs and
continues interrupted stages. A changed media result creates a new reader
revision, preserving earlier deliverables.
"""
import argparse
import asyncio
import json
from pathlib import Path

from unified_mcp.batch_support import atomic_json, digest, file_fingerprint, output_lock, project_jsonl
from unified_mcp import export_snapshot, process_media, reader_export


def _scope(args):
    return {name: getattr(args, name, None) for name in ('wechat', 'qq', 'qq_chat_type', 'after', 'before', 'page_size')}


async def run(args):
    output = Path(args.output).resolve()
    with output_lock(output):
        job_path = output / 'job.json'
        resume = getattr(args, 'resume', False)
        scope = _scope(args)
        if job_path.exists():
            if not resume:
                raise FileExistsError('Job already exists; use --resume or choose a new output directory')
            job = json.loads(job_path.read_text(encoding='utf-8'))
            if job.get('scope') != scope:
                raise ValueError('Job chat/range/type/page-size inputs changed; existing results preserved')
        else:
            if resume:
                raise ValueError('No job manifest exists to resume')
            if any(path.name != '.workflow.lock' for path in output.iterdir()):
                raise FileExistsError('Choose an empty job output directory')
            job = {'schema': 1, 'scope': scope, 'input_fingerprint': digest(scope), 'steps': {}, 'readers': []}
        job.update(state='running', error=None)
        atomic_json(job_path, job)
        try:
            snapshot = output / 'snapshot'
            export_args = argparse.Namespace(**scope, output=str(snapshot),
                                             resume=(snapshot / '.export-checkpoint.sqlite3').is_file())
            job['active_stage'] = 'export'
            atomic_json(job_path, job)
            snapshot_report = await export_snapshot.export(export_args)
            job['steps']['export'] = {'state': 'complete', 'records': snapshot_report['total_records'], 'coverage': 'snapshot/coverage.json'}
            job['active_stage'] = 'media'
            atomic_json(job_path, job)
            media_output = output / 'media'
            media_report = await process_media.run(snapshot, media_output,
                resume=(media_output / '.media-checkpoint.sqlite3').is_file(),
                transcribe=getattr(args, 'transcribe', False), retry_failed=getattr(args, 'retry_failed', False),
                refresh=getattr(args, 'refresh', False), stages=getattr(args, 'stages', None),
                qq_data_root=getattr(args, 'qq_data_root', None))
            job['steps']['media'] = {'state': media_report['state'], 'processed': media_report['processed'],
                                     'summary': 'media/summary.json', 'stage_counts': media_report['stage_counts']}
            job['active_stage'] = 'reader'
            atomic_json(job_path, job)
            merged, updates = snapshot / 'merged.jsonl', media_output / 'media-messages.jsonl'
            reader_options = {'portable': getattr(args, 'portable', False), 'chunk_size': getattr(args, 'chunk_size', 2000),
                              'title': getattr(args, 'title', '微信与 QQ · 本地聊天阅读')}
            reader_input = digest({'merged': file_fingerprint(merged), 'updates': file_fingerprint(updates), **reader_options})
            latest = job['readers'][-1] if job['readers'] else None
            reader_valid = bool(latest and latest.get('input_fingerprint') == reader_input)
            if reader_valid:
                manifest = output / latest['artifact_manifest']
                reader_valid = manifest.is_file() and file_fingerprint(manifest) == latest['artifact_manifest_fingerprint']
                if reader_valid:
                    for artifact in export_snapshot.iter_jsonl(manifest):
                        path = output / artifact['path']
                        if not path.is_file() or file_fingerprint(path) != artifact['fingerprint']:
                            reader_valid = False
                            break
            if not reader_valid:
                revision = len(job['readers']) + 1
                # A killed reader build may have published some files just before
                # the job commit. Pick a fresh revision and preserve that folder.
                while (output / f'reader-{revision:04d}').exists():
                    revision += 1
                reader_path = output / f'reader-{revision:04d}' / 'reader.html'
                reader_report = await asyncio.to_thread(reader_export.build, merged, reader_path,
                    updates=updates, **reader_options)
                artifacts_path = reader_path.parent / 'artifacts.jsonl'
                def artifacts():
                    for path in reader_path.parent.rglob('*'):
                        if path.is_file() and path.name not in ('.workflow.lock', 'artifacts.jsonl') and path.suffix != '.tmp':
                            yield {'path': path.relative_to(output).as_posix(), 'fingerprint': file_fingerprint(path)}
                artifact_count = project_jsonl(artifacts_path, artifacts())
                latest = {'path': reader_path.relative_to(output).as_posix(), 'input_fingerprint': reader_input,
                          'displayed': reader_report['displayed'], 'pages': reader_report['pages'],
                          'artifact_manifest': artifacts_path.relative_to(output).as_posix(),
                          'artifact_manifest_fingerprint': file_fingerprint(artifacts_path), 'artifact_count': artifact_count}
                job['readers'].append(latest)
            job['steps']['reader'] = {'state': 'complete', 'path': latest['path']}
            job.update(state='complete_with_errors' if media_report['state'] != 'complete' else 'complete', active_stage=None)
            atomic_json(job_path, job)
            return job
        except BaseException as exc:
            job.update(state='interrupted' if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)) else 'failed',
                       error={'type': type(exc).__name__, 'message': str(exc)})
            atomic_json(job_path, job)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wechat')
    parser.add_argument('--qq')
    parser.add_argument('--qq-chat-type', choices=('private', 'group', 'discuss'), default='private')
    parser.add_argument('--after')
    parser.add_argument('--before')
    parser.add_argument('--page-size', type=int, default=5000)
    parser.add_argument('--output', required=True)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--transcribe', action='store_true')
    parser.add_argument('--retry-failed', action='store_true')
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--stages', nargs='+', choices=process_media.STAGES)
    parser.add_argument('--qq-data-root', type=Path)
    parser.add_argument('--portable', action='store_true')
    parser.add_argument('--chunk-size', type=int, default=2000)
    parser.add_argument('--title', default='微信与 QQ · 本地聊天阅读')
    args = parser.parse_args()
    result = asyncio.run(run(args))
    print(json.dumps({'state': result['state'], 'steps': result['steps']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
