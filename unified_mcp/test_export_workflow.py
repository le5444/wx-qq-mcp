"""Synthetic crash/retry/portable/large-stream acceptance tests; no personal data."""
import argparse
import asyncio
import contextlib
import copy
import io
import json
from pathlib import Path
import re
import shutil
import tempfile
import tracemalloc
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from unified_mcp import export_snapshot as exporter
from unified_mcp import process_media as media
from unified_mcp import workflow
from unified_mcp.reader_export import build
from unified_mcp.batch_support import canonical, file_fingerprint


def native(source, number):
    if source == 'wechat':
        return {'id': {'server_id_str': str(number + 1), 'local_id': number + 1}, 'create_time': number,
                'kind': 'text', 'text': f'synthetic-{number}', 'time': str(number), 'sender': 'Synthetic', 'is_from_me': False}
    return {'msg_id': str(number + 1), 'timestamp': number, 'kind': 'text', 'text': f'synthetic-{number}',
            'time': str(number), 'sender': 'Synthetic', 'direction': 'from_member'}


class FakeGateway:
    def __init__(self, rows, fail_call=None):
        self.rows, self.fail_call, self.calls, self.closed = rows, fail_call, [], False

    async def fetch(self, source, name, args):
        self.calls.append((source, name, copy.deepcopy(args)))
        if self.fail_call and len(self.calls) == self.fail_call:
            raise RuntimeError('synthetic interruption')
        offset = int(args.get('cursor', args.get('offset', 0)))
        selected = self.rows[source][offset:offset + args['limit']]
        end = offset + len(selected)
        more = end < len(self.rows[source])
        query = {'has_more': more, 'next_offset': end if more else None}
        if source == 'qq' and more:
            query['next_cursor'] = str(end)
        return {'messages': copy.deepcopy(selected), 'query': query}

    async def close(self):
        self.closed = True


def options(folder, **kwargs):
    return argparse.Namespace(output=str(folder), wechat='synthetic-chat', qq=None, qq_chat_type='private',
                              page_size=2, after=None, before='10000', resume=False, **kwargs)


def normalized(number, source='wechat', kind='image', **original):
    return {'source': source, 'record_id': f'record-{number}', 'message_id': str(number), 'kind': kind,
            'timestamp': number, 'time': f'2026-01-{number % 28 + 1:02d}', 'text': '',
            'original': {'kind': kind, 'id': {'local_id': number}, **original}}


def write_snapshot(folder, rows, manifest=True):
    folder.mkdir()
    sources = sorted(set(row['source'] for row in rows))
    for source in sources:
        (folder / (source + '.jsonl')).write_text(''.join(canonical(row) + '\n' for row in rows if row['source'] == source), encoding='utf-8')
    if manifest:
        (folder / 'coverage.json').write_text(canonical({'complete': True, 'sources': {s: {'complete': True} for s in sources}}), encoding='utf-8')


class ExportCheckpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_interruption_resume_and_truncated_projection_recover_without_loss(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); args = options(root / 'export')
            rows = {'wechat': [native('wechat', n) for n in range(7)]}
            interrupted = FakeGateway(rows, fail_call=3)
            with patch.object(exporter, 'Gateway', return_value=interrupted), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, 'interruption'):
                    await exporter.export(args)
            self.assertTrue(interrupted.closed)
            self.assertEqual(len(list(exporter.iter_jsonl(root / 'export/wechat.jsonl'))), 4)
            (root / 'export/wechat.jsonl').write_text('{truncated', encoding='utf-8')
            args.resume = True
            resumed = FakeGateway(rows)
            with patch.object(exporter, 'Gateway', return_value=resumed), contextlib.redirect_stdout(io.StringIO()):
                report = await exporter.export(args)
            output = list(exporter.iter_jsonl(root / 'export/merged.jsonl'))
            self.assertEqual([row['text'] for row in output], [f'synthetic-{n}' for n in range(7)])
            self.assertEqual(report['resume_revalidated_pages'], 2)
            self.assertEqual(report['total_records'], 7)
            self.assertTrue(report['complete'])
            with patch.object(exporter, 'Gateway', return_value=FakeGateway(rows)), contextlib.redirect_stdout(io.StringIO()):
                again = await exporter.export(args)
            self.assertEqual(again['total_records'], 7)

    async def test_changed_earlier_page_rejected_preserving_existing_artifacts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); args = options(root / 'export')
            rows = {'wechat': [native('wechat', n) for n in range(5)]}
            with patch.object(exporter, 'Gateway', return_value=FakeGateway(rows)), contextlib.redirect_stdout(io.StringIO()):
                await exporter.export(args)
            before = file_fingerprint(root / 'export/coverage.json')
            rows['wechat'][0]['text'] = 'changed upstream content'
            args.resume = True
            with patch.object(exporter, 'Gateway', return_value=FakeGateway(rows)):
                with self.assertRaisesRegex(ValueError, 'history changed'):
                    await exporter.export(args)
            self.assertEqual(file_fingerprint(root / 'export/coverage.json'), before)
            self.assertEqual(list(exporter.iter_jsonl(root / 'export/wechat.jsonl'))[0]['text'], 'synthetic-0')

    async def test_scope_change_and_overwrite_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            args = options(Path(folder) / 'export')
            with patch.object(exporter, 'Gateway', return_value=FakeGateway({'wechat': []})), contextlib.redirect_stdout(io.StringIO()):
                await exporter.export(args)
            with self.assertRaises(FileExistsError):
                await exporter.export(args)
            args.resume, args.after = True, '10'
            with self.assertRaisesRegex(ValueError, 'fingerprint'):
                await exporter.export(args)

    async def test_qq_group_uses_bound_keyset_and_single_source_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            args = options(Path(folder) / 'export')
            args.wechat, args.qq, args.qq_chat_type = None, 'synthetic-group', 'group'
            fake = FakeGateway({'qq': [native('qq', n) for n in range(5)]})
            with patch.object(exporter, 'Gateway', return_value=fake), contextlib.redirect_stdout(io.StringIO()):
                result = await exporter.export(args)
            self.assertEqual(set(result['sources']), {'qq'})
            self.assertTrue(all(call[2]['chat_type'] == 'group' for call in fake.calls))
            self.assertEqual(fake.calls[1][2]['cursor'], '2')
            self.assertNotIn('offset', fake.calls[1][2])
            self.assertTrue(all(call[2]['include_image_text'] is False for call in fake.calls))

    async def test_media_availability_change_does_not_invalidate_source_history(self):
        with tempfile.TemporaryDirectory() as folder:
            args = options(Path(folder) / 'export')
            args.wechat, args.qq = None, 'synthetic-qq'
            row = native('qq', 1)
            row['media'] = {'images': ['synthetic-original-image-hint']}
            row['qq_media_resolution'] = {'status': 'unavailable'}
            with patch.object(exporter, 'Gateway', side_effect=lambda: FakeGateway({'qq': [row]})), contextlib.redirect_stdout(io.StringIO()):
                await exporter.export(args)
                args.resume = True
                row['images'] = [{'path': 'newly-downloaded.png', 'ocr': {'status': 'ok', 'text': 'local-only derived text'}}]
                row['qq_media_resolution'] = {'status': 'ok'}
                report = await exporter.export(args)
            self.assertTrue(report['complete'])
            self.assertEqual(report['total_records'], 1)

    async def test_dual_merge_keeps_native_id_collisions_and_collapses_only_identical_raw(self):
        with tempfile.TemporaryDirectory() as folder:
            args = options(Path(folder) / 'export'); args.qq = 'synthetic-qq'; args.page_size = 10
            first = native('wechat', 1)
            collision = copy.deepcopy(first); collision['text'] = 'different local record'
            rows = {'wechat': [first, first, collision], 'qq': [native('qq', 0), native('qq', 1)]}
            with patch.object(exporter, 'Gateway', return_value=FakeGateway(rows)), contextlib.redirect_stdout(io.StringIO()):
                report = await exporter.export(args)
            result = list(exporter.iter_jsonl(Path(args.output) / 'merged.jsonl'))
            self.assertEqual(len(result), 4)
            self.assertEqual(len({(row['source'], row['record_id']) for row in result}), 4)
            self.assertEqual(report['sources']['wechat']['duplicate_records'], 1)
            self.assertEqual(report['sources']['wechat']['message_id_collisions'], 1)
            self.assertEqual([(row['timestamp'], row['source']) for row in result], sorted((row['timestamp'], row['source']) for row in result))

    async def test_duplicate_only_advancing_pages_stop_instead_of_looping(self):
        with tempfile.TemporaryDirectory() as folder:
            args = options(Path(folder) / 'export'); args.page_size = 1
            row = native('wechat', 1)
            with patch.object(exporter, 'Gateway', return_value=FakeGateway({'wechat': [row, row, row]})), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, 'previously committed'):
                    await exporter.export(args)
            self.assertEqual(len(list(exporter.iter_jsonl(Path(args.output) / 'wechat.jsonl'))), 1)

    async def test_foreign_chat_cannot_be_relabeled_into_export(self):
        with tempfile.TemporaryDirectory() as folder:
            args = options(Path(folder) / 'export')
            row = native('wechat', 1); row['talker'] = 'different-chat'
            with patch.object(exporter, 'Gateway', return_value=FakeGateway({'wechat': [row]})):
                with self.assertRaisesRegex(RuntimeError, 'different chat'):
                    await exporter.export(args)
            self.assertEqual(list(exporter.iter_jsonl(Path(args.output) / 'wechat.jsonl')), [])

    def test_partial_page_does_not_establish_completeness(self):
        with self.assertRaises(RuntimeError):
            exporter._page({'status': 'partial', 'messages': [], 'query': {'has_more': False}})

    def test_export_date_range_is_exclusive_and_sent_as_epochs_to_both_readers(self):
        scope = {'page_size': 10, 'after': '2026-10-01', 'before': '2026-10-02', 'qq_chat_type': 'private'}
        for source in ('wechat', 'qq'):
            args = exporter._args(source, 'synthetic', scope, {'offset': 0})
            self.assertEqual(int(args['before']) - int(args['after']), 86400)
            self.assertTrue(args['before'].isdigit())


class FakeVoice:
    calls = []
    failure = False
    async def enrich(self, original, transcribe=True):
        self.calls.append(transcribe)
        return {**original, 'voice_transcript': {'status': 'unavailable' if self.failure else 'ok', 'text': 'synthetic speech'}}
    async def close(self):
        pass


class FakeOCR:
    calls = []
    failure = False
    def read(self, path):
        self.calls.append(path)
        return {'status': 'unavailable' if self.failure else 'ok', 'text': 'synthetic OCR'}
    def close(self):
        pass


class MediaWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeVoice.calls, FakeVoice.failure = [], False
        FakeOCR.calls, FakeOCR.failure = [], False
        self.patches = [patch.object(media, 'VoiceService', FakeVoice), patch.object(media, 'ImageTextReader', FakeOCR),
                        patch.dict(media.os.environ, {'QQ_MCP_DATA_ROOT': '', 'QQ_MCP_DB_ROOT': ''})]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    async def test_single_source_transcribe_video_sticker_and_occurrence_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); snapshot = root / 'snapshot'
            rows = [normalized(1, kind='voice'), normalized(2, kind='video'),
                    normalized(3, kind='sticker', images=[{'path': 'synthetic.png'}, {'path': 'synthetic.png'}])]
            write_snapshot(snapshot, rows)
            image = lambda raw: {**raw, 'wechat_media_resolution': {'status': 'readable'}}
            video = lambda raw: {**raw, 'videos': [{'path': 'synthetic.mp4'}], 'wechat_video_resolution': {'status': 'playable_local'}}
            with patch.object(media, 'enrich_wechat_media', side_effect=image), patch.object(media, 'enrich_wechat_video', side_effect=video):
                report = await media.run(snapshot, root / 'output', transcribe=True)
            self.assertEqual(FakeVoice.calls, [True])
            self.assertEqual(report['state'], 'complete')
            self.assertEqual(report['processed'], 3)
            result = list(exporter.iter_jsonl(root / 'output/media-messages.jsonl'))
            self.assertEqual(result[1]['processing_stages']['video']['status'], 'ok')
            self.assertEqual(len(result[2]['image_text']), 2)
            self.assertNotEqual(result[2]['image_text'][0]['media_ref'], result[2]['image_text'][1]['media_ref'])

    async def test_failed_stage_retry_and_model_change_reprocess_without_duplicate_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); snapshot = root / 'snapshot'
            write_snapshot(snapshot, [normalized(1, kind='voice')])
            FakeVoice.failure = True
            report = await media.run(snapshot, root / 'out', transcribe=True)
            self.assertEqual(report['state'], 'complete_with_errors')
            FakeVoice.failure = False
            await media.run(snapshot, root / 'out', resume=True, transcribe=True)
            self.assertEqual(len(FakeVoice.calls), 1)
            report = await media.run(snapshot, root / 'out', resume=True, transcribe=True, retry_failed=True)
            self.assertEqual(report['state'], 'complete')
            self.assertEqual(len(FakeVoice.calls), 2)
            with patch.object(media, '_model_revision', return_value={'synthetic': 'revision-two'}):
                await media.run(snapshot, root / 'out', resume=True, transcribe=True)
            self.assertEqual(len(FakeVoice.calls), 3)
            self.assertEqual(len(list(exporter.iter_jsonl(root / 'out/media-messages.jsonl'))), 1)

    async def test_limit_resume_progress_and_truncated_projection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); snapshot = root / 'snapshot'
            write_snapshot(snapshot, [normalized(n, kind='voice') for n in range(1, 5)])
            first = await media.run(snapshot, root / 'out', limit=2, transcribe=True)
            self.assertEqual(first['state'], 'paused_limit')
            (root / 'out/media-messages.jsonl').write_text('{incomplete', encoding='utf-8')
            second = await media.run(snapshot, root / 'out', resume=True, transcribe=True)
            self.assertEqual(second['processed'], 4)
            self.assertEqual(len(FakeVoice.calls), 4)
            self.assertEqual(len(list(exporter.iter_jsonl(root / 'out/media-messages.jsonl'))), 4)

    async def test_changed_snapshot_rejected_and_qq_only_supported(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); snapshot = root / 'snapshot'
            write_snapshot(snapshot, [normalized(1, source='qq', images=[{'path': 'synthetic.png'}], qq_media_resolution={'status': 'ok'})])
            report = await media.run(snapshot, root / 'out')
            self.assertEqual(report['sources'], ['qq'])
            saved = file_fingerprint(root / 'out/media-messages.jsonl')
            with (snapshot / 'qq.jsonl').open('a', encoding='utf-8') as handle:
                handle.write(canonical(normalized(2, source='qq')) + '\n')
            with self.assertRaisesRegex(ValueError, 'changed'):
                await media.run(snapshot, root / 'out', resume=True)
            self.assertEqual(file_fingerprint(root / 'out/media-messages.jsonl'), saved)

    async def test_ocr_reruns_when_retried_media_finds_new_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); snapshot = root / 'snapshot'
            write_snapshot(snapshot, [normalized(1, images=[{'path': 'first.png'}])])
            with patch.object(media, 'enrich_wechat_media', side_effect=lambda raw: {**raw, 'wechat_media_resolution': {'status': 'partial'}}):
                await media.run(snapshot, root / 'out')
            self.assertEqual(FakeOCR.calls, ['first.png'])
            with patch.object(media, 'enrich_wechat_media', side_effect=lambda raw: {**raw, 'images': [{'path': 'second.png'}], 'wechat_media_resolution': {'status': 'readable'}}):
                await media.run(snapshot, root / 'out', resume=True, retry_failed=True)
            self.assertEqual(FakeOCR.calls, ['first.png', 'second.png'])

    async def test_stage_exception_is_recorded_and_does_not_hide_later_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); snapshot = root / 'snapshot'
            write_snapshot(snapshot, [normalized(1), normalized(2, kind='voice')])
            with patch.object(media, 'enrich_wechat_media', side_effect=RuntimeError('synthetic failure')):
                report = await media.run(snapshot, root / 'out', transcribe=True)
            self.assertEqual(report['processed'], 2)
            self.assertEqual(report['stage_counts']['media:failed'], 1)
            self.assertEqual(report['stage_counts']['voice:ok'], 1)

    async def test_cancel_after_media_commit_resumes_at_ocr(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); snapshot = root / 'snapshot'
            write_snapshot(snapshot, [normalized(1, images=[{'path': 'synthetic.png'}])])
            image = lambda raw: {**raw, 'wechat_media_resolution': {'status': 'readable'}}
            with patch.object(media, 'enrich_wechat_media', side_effect=image) as resolve:
                with patch.object(FakeOCR, 'read', side_effect=asyncio.CancelledError):
                    with self.assertRaises(asyncio.CancelledError):
                        await media.run(snapshot, root / 'out')
                report = await media.run(snapshot, root / 'out', resume=True)
            self.assertEqual(resolve.call_count, 1)
            self.assertEqual(report['state'], 'complete')
            self.assertEqual(report['processed'], 1)

    async def test_incomplete_source_manifest_is_not_accepted_as_full_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); snapshot = root / 'snapshot'
            write_snapshot(snapshot, [normalized(1, kind='voice')])
            (snapshot / 'coverage.json').write_text(canonical({'complete': False, 'sources': {'wechat': {}}}), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'incomplete'):
                await media.run(snapshot, root / 'out')


class PortableAndStreamingTests(unittest.TestCase):
    def test_portable_package_moves_offline_preserves_duplicate_media_and_drops_remote(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); original = root / 'source-image.png'; original.write_bytes(b'synthetic image fixture')
            before = file_fingerprint(original)
            rows = [normalized(1, images=[{'path': str(original)}, {'path': str(original)}, {'path': 'https://example.invalid/secret.png'}])]
            source = root / 'input.jsonl'; source.write_text(canonical(rows[0]), encoding='utf-8')
            package = root / 'package'; package.mkdir()
            report = build(source, package / 'reader.html', portable=True)
            self.assertEqual(report['media_files'], 1)
            self.assertEqual(report['media_references'], 2)
            self.assertEqual(file_fingerprint(original), before)
            moved = root / 'moved'; package.rename(moved)
            content = (moved / 'reader.html').read_text(encoding='utf-8')
            dataset = json.loads(re.search(r'const data=(.*?);document.title', content).group(1))
            images = dataset['items'][0]['images']
            self.assertEqual(len(images), 2)
            self.assertEqual(images[0], images[1])
            self.assertNotIn('https://example.invalid', content)
            self.assertTrue((moved / images[0]).is_file())
            refs = list(exporter.iter_jsonl(moved / 'reader.media.jsonl'))
            self.assertEqual([r['occurrence'] for r in refs], [0, 1])

    def test_chunk_pages_bound_dataset_and_last_link_is_valid(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / 'input.jsonl'
            source.write_text(''.join(canonical(normalized(n, kind='text')) + '\n' for n in range(17)), encoding='utf-8')
            report = build(source, root / 'reader.html', chunk_size=5)
            self.assertEqual(report['pages'], 4)
            self.assertEqual(sum(part['records'] for part in report['parts']), 17)
            index = (root / 'reader.html').read_text(encoding='utf-8')
            self.assertNotIn('const data=', index)
            for i, part in enumerate(report['parts'], 1):
                text = (root / part['path']).read_text(encoding='utf-8')
                data = json.loads(re.search(r'const data=(.*?);document.title', text).group(1))
                self.assertLessEqual(len(data['items']), 5)
                self.assertEqual(bool(data['navigation']['next']), i < 4)
            self.assertIn('当前分片', index)

    def test_portable_chunk_resources_remain_relative_after_package_move(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); original = root / 'voice.wav'; original.write_bytes(b'synthetic audio bytes')
            source = root / 'input.jsonl'
            source.write_text(''.join(canonical(normalized(n, kind='voice', voice_transcript={'status': 'ok', 'text': 'speech', 'audio_path': str(original)})) + '\n' for n in range(5)), encoding='utf-8')
            package = root / 'package'; package.mkdir()
            report = build(source, package / 'reader.html', chunk_size=2, portable=True)
            moved = root / 'moved'; package.rename(moved)
            for part in report['parts']:
                page = moved / part['path']
                content = page.read_text(encoding='utf-8')
                data = json.loads(re.search(r'const data=(.*?);document.title', content).group(1))
                for item in data['items']:
                    self.assertTrue(item['audio'].startswith('../reader.assets/'))
                    self.assertTrue((page.parent / item['audio']).is_file())
            self.assertEqual(report['media_files'], 1)

    def test_100000_record_merge_is_bounded_and_ordered(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for parity, source in enumerate(('wechat', 'qq')):
                with (root / (source + '.jsonl')).open('w', encoding='utf-8') as handle:
                    for n in range(parity, 100000, 2):
                        handle.write(canonical({'timestamp': n, 'source': source, 'text': 'synthetic payload ' * 10}) + '\n')
            tracemalloc.start()
            count = exporter.merge_jsonl([root / 'wechat.jsonl', root / 'qq.jsonl'], root / 'merged.jsonl')
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            self.assertEqual(count, 100000)
            self.assertLess(peak, 4 * 1024 * 1024)
            for n, row in enumerate(exporter.iter_jsonl(root / 'merged.jsonl')):
                self.assertEqual(row['timestamp'], n)


class WholeWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_command_cancel_resume_delivers_reader_and_unchanged_resume_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            args = options(root / 'job')
            args.transcribe, args.portable, args.chunk_size = True, True, 2
            rows = [native('wechat', n) for n in range(5)]
            rows[2]['kind'] = 'voice'
            interrupted = False

            class InterruptVoice(FakeVoice):
                async def enrich(self, original, transcribe=True):
                    nonlocal interrupted
                    if not interrupted:
                        interrupted = True
                        raise asyncio.CancelledError()
                    return await super().enrich(original, transcribe)

            with patch.object(exporter, 'Gateway', side_effect=lambda: FakeGateway({'wechat': rows})), patch.object(media, 'VoiceService', InterruptVoice), patch.object(media, 'ImageTextReader', FakeOCR), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(asyncio.CancelledError):
                    await workflow.run(args)
                paused = json.loads((root / 'job/job.json').read_text(encoding='utf-8'))
                self.assertEqual(paused['state'], 'interrupted')
                self.assertEqual(paused['steps']['export']['records'], 5)
                args.resume = True
                result = await workflow.run(args)
                self.assertEqual(result['state'], 'complete')
                reader = root / 'job' / result['steps']['reader']['path']
                self.assertTrue(reader.is_file())
                self.assertEqual(result['readers'][0]['pages'], 3)
                repeated = await workflow.run(args)
                self.assertEqual(len(repeated['readers']), 1)
                self.assertEqual(repeated['steps']['reader']['path'], result['steps']['reader']['path'])
                reader.write_text('truncated', encoding='utf-8')
                repaired = await workflow.run(args)
                self.assertEqual(len(repaired['readers']), 2)
                self.assertTrue((root / 'job' / repaired['steps']['reader']['path']).is_file())

    async def test_new_voice_missing_cache_refreshes_exact_local_metadata_before_asr(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); snapshot = root / 'snapshot'
            voice = normalized(1, kind='voice')
            voice['chat_id'] = 'synthetic-chat'
            write_snapshot(snapshot, [voice])

            class MissingVoice(FakeVoice):
                async def enrich(self, original, transcribe=True):
                    if not original.get('local_media_metadata'):
                        return {**original, 'voice_transcript': {'status': 'audio_missing'}}
                    return {**original, 'voice_transcript': {'status': 'ok', 'text': 'newly transcribed'}}

            async def metadata_load(self, row, include_paths=False):
                self.assertion = include_paths
                return [{'voice': {'audio_path': 'synthetic.wav'}}], []

            with patch.object(media, 'VoiceService', MissingVoice), patch.object(media, 'ImageTextReader', FakeOCR), patch.object(media.LocalMetadata, 'load', metadata_load):
                result = await media.run(snapshot, root / 'out', transcribe=True)
            self.assertEqual(result['state'], 'complete')
            self.assertEqual(list(exporter.iter_jsonl(root / 'out/media-messages.jsonl'))[0]['original']['voice_transcript']['text'], 'newly transcribed')

    async def test_metadata_refresh_is_exact_bounded_and_ambiguous_rows_fail(self):
        calls = []
        duplicate = False

        async def call(name, args):
            calls.append((name, args))
            if name == 'media_resources':
                return []
            exact = {'id': {'local_id': 2, 'server_id_str': '42'}, 'create_time': 12, 'kind': 'voice'}
            other = {'id': {'local_id': 3, 'server_id_str': '43'}, 'create_time': 12, 'kind': 'voice'}
            return [exact, exact] if duplicate else [other, exact]

        resolver = media.LocalMetadata()
        resolver.gateway = SimpleNamespace(wechat=SimpleNamespace(call=call))
        row = normalized(2, kind='voice')
        row.update(chat_id='synthetic-chat', timestamp=12, message_id='42')
        row['original']['id']['server_id_str'] = '42'
        exact, resources = await resolver.load(row, include_paths=True)
        self.assertEqual(len(exact), 1)
        self.assertEqual(calls[0][1]['server_id_str'], '42')
        self.assertEqual(calls[0][1]['local_id'], 2)
        self.assertTrue(calls[0][1]['include_local_paths'])
        self.assertEqual(calls[1][1]['after'], '12')
        self.assertEqual(calls[1][1]['before'], '13')
        duplicate = True
        with self.assertRaisesRegex(ValueError, 'Ambiguous'):
            await resolver.load(row, include_paths=True)


if __name__ == '__main__':
    unittest.main()
