"""Synthetic export/update association boundaries; no local account access."""
import argparse
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unified_mcp import export_snapshot, process_media
from unified_mcp.batch_support import canonical, digest
from unified_mcp.enrichment_updates import merge_update
from unified_mcp.message_identity import IdentityError
from unified_mcp.reader_export import build
from unified_mcp.record_identity import extended_record_id, matches_record_id, raw_fingerprint, split_record_id
from unified_mcp.timeline import normal_message


def qq(chat='u_synthetic_a', text='source text'):
    raw = {'msg_id': '101', 'chat_id': chat, 'timestamp': 100, 'sender_uid': 'sender-a',
           'sender': 'Synthetic', 'direction': 'from_contact', 'kind': 'image', 'text': text}
    return normal_message('qq', raw, chat)


def write_lines(path, rows):
    path.write_text(''.join(canonical(row) + '\n' for row in rows), encoding='utf-8')


class UpdateContractTests(unittest.TestCase):
    def test_sparse_derived_update_preserves_native_body_and_identity(self):
        base = qq()
        update = {'source': 'qq', 'record_id': base['record_id'], 'image_text': [{'status': 'ok', 'text': 'derived text'}],
                  'original': {'images': [{'path': 'synthetic.png'}]}}
        merged = merge_update(base, update)
        self.assertEqual(merged['text'], base['text'])
        self.assertEqual(merged['original']['text'], base['original']['text'])
        self.assertEqual(merged['sender_id'], base['sender_id'])
        self.assertEqual(merged['original']['images'], update['original']['images'])
        self.assertNotIn('images', base['original'])

    def test_replacement_text_is_not_authoritative_even_with_exact_record_key(self):
        base = qq()
        update = {**copy.deepcopy(base), 'text': 'untrusted normalized replacement'}
        update['original']['text'] = 'untrusted original replacement'
        merged = merge_update(base, update)
        self.assertEqual(merged['text'], 'source text')
        self.assertEqual(merged['original']['text'], 'source text')

    def test_update_conflicting_outer_identity_is_rejected(self):
        base = qq()
        for field, wrong in [('chat_id', 'u_synthetic_b'), ('message_id', '999'), ('timestamp', 101),
                             ('sender_id', 'sender-b'), ('direction', 'outgoing'), ('kind', 'voice')]:
            with self.subTest(field=field), self.assertRaises(IdentityError):
                merge_update(base, {'source': 'qq', 'record_id': base['record_id'], field: wrong})

    def test_update_conflicting_native_identity_is_rejected(self):
        base = qq()
        for field, wrong in [('chat_id', 'u_synthetic_b'), ('msg_id', '999'), ('timestamp', 101),
                             ('sender_uid', 'sender-b'), ('direction', 'from_me')]:
            with self.subTest(field=field), self.assertRaises(IdentityError):
                merge_update(base, {'source': 'qq', 'record_id': base['record_id'], 'original': {field: wrong}})

    def test_source_mismatch_cannot_be_hidden_inside_original(self):
        base = qq()
        update = {'source': 'qq', 'record_id': base['record_id'], 'original': {'source': 'wechat'}}
        with self.assertRaises(IdentityError):
            merge_update(base, update)

    def test_reader_rejects_conflict_without_publishing_and_leaves_input_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, updates, output = root/'source.jsonl', root/'updates.jsonl', root/'reader.html'
            base = qq(); write_lines(source, [base]); before = source.read_bytes()
            write_lines(updates, [{'source': 'qq', 'record_id': base['record_id'], 'chat_id': 'u_synthetic_b'}])
            with self.assertRaises(IdentityError):
                build(source, output, updates=updates)
            self.assertFalse(output.exists())
            self.assertFalse((root/'reader.manifest.json').exists())
            self.assertEqual(source.read_bytes(), before)

    def test_reader_conflicting_duplicate_updates_are_not_last_write_wins(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); base = qq()
            write_lines(root/'source.jsonl', [base])
            first = {'source': 'qq', 'record_id': base['record_id'], 'image_text': []}
            write_lines(root/'updates.jsonl', [first, {**first, 'image_text': [{'status':'ok','text':'second'}]}])
            with self.assertRaisesRegex(ValueError, 'Conflicting duplicate'):
                build(root/'source.jsonl', root/'reader.html', updates=root/'updates.jsonl')
            self.assertFalse((root/'reader.html').exists())


class QuietVoice:
    async def close(self):
        pass


class QuietOCR:
    def read(self, path):
        return {'status':'ok','text':'synthetic OCR'}
    def close(self):
        pass


class ExternalQQAssociationTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, updates):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        snapshot = root/'snapshot'; snapshot.mkdir()
        base = qq()
        write_lines(snapshot/'qq.jsonl', [base])
        (snapshot/'coverage.json').write_text(canonical({'complete':True,'sources':{'qq':{'complete':True}}}),encoding='utf-8')
        write_lines(root/'updates.jsonl', updates)
        with patch.object(process_media, 'VoiceService', QuietVoice), patch.object(process_media, 'ImageTextReader', QuietOCR):
            report = await process_media.run(snapshot, root/'output', qq_enriched=root/'updates.jsonl', stages=['ocr'])
        row = next(export_snapshot.iter_jsonl(root/'output/media-messages.jsonl'))
        return report, row

    async def test_same_message_id_other_chat_never_fills_image(self):
        raw = qq('u_synthetic_b')['original']; raw['images'] = [{'path':'foreign.png'}]
        report, row = await self.run_case([raw])
        self.assertNotIn('images', row['original'])
        self.assertEqual(report['qq_enrichment']['applied'], 0)

    async def test_unscoped_legacy_message_id_is_skipped_explicitly(self):
        report, row = await self.run_case([{'msg_id':'101','images':[{'path':'unscoped.png'}]}])
        self.assertNotIn('images', row['original'])
        self.assertEqual(report['qq_enrichment']['skipped_unscoped'], 1)

    async def test_legacy_native_row_with_complete_scope_adds_media_only(self):
        raw = qq()['original']; raw['text'] = 'external body not authoritative'; raw['images'] = [{'path':'exact.png'}]
        report, row = await self.run_case([raw])
        self.assertEqual(row['original']['images'][0]['path'], 'exact.png')
        self.assertEqual(row['original']['text'], 'source text')
        self.assertEqual(report['qq_enrichment']['applied'], 1)

    async def test_precise_derived_only_record_patch_is_compatible(self):
        base = qq()
        report, row = await self.run_case([{'source':'qq','record_id':base['record_id'],
                                         'original':{'images':[{'path':'precise.png'}]}}])
        self.assertEqual(row['original']['images'][0]['path'], 'precise.png')
        self.assertEqual(row['chat_id'], base['chat_id'])
        self.assertEqual(report['qq_enrichment']['applied'], 1)

    async def test_duplicate_same_scope_candidates_are_ambiguous(self):
        left = qq()['original']; left['images'] = [{'path':'first.png'}]
        right = copy.deepcopy(left); right['images'][0]['path'] = 'second.png'
        with self.assertRaisesRegex(IdentityError, 'Ambiguous'):
            await self.run_case([left, right])

    async def test_wrong_platform_in_external_qq_file_is_rejected(self):
        with self.assertRaisesRegex(IdentityError, 'different platform'):
            await self.run_case([{'source':'wechat','record_id':'other','msg_id':'101','images':[{'path':'wrong.png'}]}])

    async def test_matching_record_id_does_not_override_conflicting_chat(self):
        row = qq(); update = {'source':'qq','record_id':row['record_id'],'chat_id':'u_synthetic_b',
                              'original':{'images':[{'path':'wrong.png'}]}}
        with self.assertRaises(IdentityError):
            await self.run_case([update])


class CollisionIdentityTests(unittest.TestCase):
    def test_hash_ignores_enrichment_and_voice_display_text(self):
        raw = {'id':{'local_id':1,'server_id_str':'7'},'create_time':100,'kind':'voice','text':'[voice]',
               'voice':{'duration_ms':1000}}
        enriched = copy.deepcopy(raw)
        enriched.update(text='[语音·本地自动识别] generated',original_voice_text='[voice]',
                        voice_transcript={'status':'ok','text':'generated'},images=[{'path':'new.png'}])
        enriched['voice']['transcript'] = enriched['voice_transcript']
        enriched['voice']['decoded_audio_path'] = 'derived-cache.wav'
        enriched['media_read_hints'] = [{'transcript': {'status':'ok','text':'generated'}}]
        self.assertEqual(raw_fingerprint(raw), raw_fingerprint(enriched))

    def test_collision_suffix_matches_exact_raw_content_and_legacy_suffix(self):
        raw = {'id':{'local_id':1,'server_id_str':'7'},'create_time':100,'kind':'text','text':'source A'}
        other = {**raw,'text':'source B'}
        base = normal_message('wechat',raw,'synthetic-chat')['record_id']
        record = extended_record_id(base,other)
        self.assertEqual(split_record_id('wechat','synthetic-chat',record)[0],base)
        self.assertFalse(matches_record_id('wechat','synthetic-chat',raw,record))
        self.assertTrue(matches_record_id('wechat','synthetic-chat',other,record))
        self.assertTrue(matches_record_id('wechat','synthetic-chat',other,base+':'+digest(other)))

    def test_hash_suffix_never_changes_chat_scope(self):
        row=qq(); identity=extended_record_id(row['record_id'],row['original'])
        with self.assertRaises(ValueError):
            split_record_id('qq','u_synthetic_b',identity)


class ExportRoundtripTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_three_collision_ids_can_select_their_exact_source_rows(self):
        raw = {'id':{'local_id':1,'server_id_str':'7'},'create_time':100,'kind':'text','text':'source A'}
        rows = [raw, {**raw,'text':'source B'}, {**raw,'text':'source C'}]
        class Fake:
            async def fetch(self, source, name, args):
                offset=args.get('offset',0); selected=rows[offset:offset+args['limit']]
                more=offset+len(selected)<len(rows)
                return {'messages':selected,'query':{'has_more':more,'next_offset':offset+len(selected)}}
            async def close(self):
                pass
        with tempfile.TemporaryDirectory() as folder:
            options=argparse.Namespace(output=str(Path(folder)/'out'),wechat='synthetic-chat',qq=None,
                                       qq_chat_type='private',after=None,before='1000',page_size=1,resume=False)
            with patch.object(export_snapshot,'Gateway',Fake),contextlib.redirect_stdout(io.StringIO()):
                report=await export_snapshot.export(options)
            exported=list(export_snapshot.iter_jsonl(Path(options.output)/'wechat.jsonl'))
            self.assertEqual(report['total_records'],3)
            self.assertEqual(len({r['record_id'] for r in exported}),3)
            for index, record in enumerate(exported):
                self.assertIn(':sha256:',record['record_id'])
                self.assertEqual([matches_record_id('wechat','synthetic-chat',candidate,record['record_id']) for candidate in rows],
                                 [position==index for position in range(3)])
                from unified_mcp.analysis_tools import message
                recovered = await message({'source':'wechat','chat_id':'synthetic-chat',
                                           'record_id':record['record_id'],'include_media':False}, Fake().fetch)
                self.assertEqual(recovered['status'],'ok')
                self.assertEqual(recovered['message']['text'],rows[index]['text'])
                self.assertEqual(recovered['message']['record_id'],record['record_id'])

    async def test_export_refuses_complete_claim_when_qq_index_source_unavailable(self):
        class Fake:
            async def fetch(self,*_):
                return {'messages':[],'query':{'has_more':False},'warnings':['QQ FTS unavailable; main payloads only']}
            async def close(self):
                pass
        with tempfile.TemporaryDirectory() as folder:
            options=argparse.Namespace(output=str(Path(folder)/'out'),wechat=None,qq='u_synthetic_a',
                                       qq_chat_type='private',after=None,before='1000',page_size=1,resume=False)
            with patch.object(export_snapshot,'Gateway',Fake),self.assertRaisesRegex(RuntimeError,'coverage is incomplete'):
                await export_snapshot.export(options)
            report=json.loads((Path(options.output)/'coverage.json').read_text(encoding='utf-8'))
            self.assertFalse(report['complete'])

    async def test_metadata_refresh_refuses_conflicting_outer_and_native_ids(self):
        raw={'id':{'local_id':1,'server_id_str':'7'},'create_time':100,'kind':'voice','text':'voice'}
        row=normal_message('wechat',raw,'synthetic-chat')
        row['message_id']='8'
        metadata=process_media.LocalMetadata()
        with self.assertRaises(IdentityError):
            await metadata.load(row)
        self.assertIsNone(metadata.gateway)

    async def test_verified_numeric_qq_alias_exports_under_canonical_uid(self):
        row=qq()['original']
        class Fake:
            async def fetch(self,*_):
                return {'chat':{'uid':'u_synthetic_a','uin':'10001','chat_type':'private',
                                'canonical_id':'u_synthetic_a','aliases':['u_synthetic_a','10001'],'identity_verified':True},
                        'messages':[row],'query':{'has_more':False}}
            async def close(self):
                pass
        with tempfile.TemporaryDirectory() as folder:
            options=argparse.Namespace(output=str(Path(folder)/'out'),wechat=None,qq='10001',
                                       qq_chat_type='private',after=None,before='1000',page_size=2,resume=False)
            with patch.object(export_snapshot,'Gateway',Fake),contextlib.redirect_stdout(io.StringIO()):
                report=await export_snapshot.export(options)
            record=next(export_snapshot.iter_jsonl(Path(options.output)/'qq.jsonl'))
            self.assertEqual(record['chat_id'],'u_synthetic_a')
            self.assertEqual(record['record_id'],'u_synthetic_a:101')
            self.assertEqual(report['sources']['qq']['canonical_chat_id'],'u_synthetic_a')

    async def test_same_numeric_qq_identity_cannot_switch_group_type(self):
        raw={'msg_id':'101','chat_id':'10001','chat_type':'private','timestamp':100,'kind':'text','text':'private text'}
        class Fake:
            async def fetch(self,*_):
                return {'chat':{'uid':'10001','uin':'10001','chat_type':'private','canonical_id':'10001',
                                'aliases':['10001'],'identity_verified':True},'messages':[raw],'query':{'has_more':False}}
            async def close(self):
                pass
        with tempfile.TemporaryDirectory() as folder:
            options=argparse.Namespace(output=str(Path(folder)/'out'),wechat=None,qq='10001',
                                       qq_chat_type='group',after=None,before='1000',page_size=2,resume=False)
            with patch.object(export_snapshot,'Gateway',Fake),self.assertRaises(RuntimeError):
                await export_snapshot.export(options)
            self.assertEqual(list(export_snapshot.iter_jsonl(Path(options.output)/'qq.jsonl')),[])


if __name__ == '__main__':
    unittest.main()
