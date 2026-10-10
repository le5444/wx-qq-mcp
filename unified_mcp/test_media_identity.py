"""Identity regressions use synthetic media, never personal conversations."""
import asyncio
import datetime as dt
import hashlib
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from PIL import Image
from unified_mcp.message_identity import message_identity, compatible_identity, IdentityError
from unified_mcp.voice_transcription import audio_path_from_message, resolve_audio_from_message, VoiceService
from unified_mcp.wechat_media import WeChatMediaResolver, _identity
from unified_mcp.qq_media import resolve_message_images
from unified_mcp.video_media import WeChatVideoResolver


class IdentityMediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def image(self, name, color='blue'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (8, 8), color).save(path)
        return path

    def audio(self, name, content=b'synthetic audio'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def row(self, chat='chat_A'):
        return {'kind': 'voice', 'talker': chat, 'id': {'server_id_str': '777', 'local_id': 7}, 'create_time': 17}

    def cached(self, chat='chat_A', account='synthetic-self', content=b'audio', suffix='1600'):
        return self.audio('cache/' + account + '/voice-' + hashlib.md5(chat.encode()).hexdigest() + '-7-17-777-' + suffix + '.silk', content)

    def test_identity_merges_outer_scope_and_rejects_conflicting_original(self):
        raw = self.row(); raw.pop('talker')
        wrapper = {'source': 'wechat', 'chat_id': 'chat_A', 'timestamp': 17, 'original': raw}
        identity = message_identity(wrapper)
        self.assertEqual(identity['chat_id'], 'chat_A')
        self.assertEqual(identity['local_id'], '7')
        self.assertEqual(_identity(wrapper)[0], 'chat_A')
        raw['talker'] = 'chat_B'
        with self.assertRaises(IdentityError):
            message_identity(wrapper)
        self.assertFalse(compatible_identity(message_identity(self.row()), message_identity(self.row('chat_B'))))

    def test_sparse_identity_and_zero_native_values_are_not_guessed(self):
        value = message_identity({'source': 'qq', 'record_id': 'opaque'})
        self.assertIsNone(value['chat_id'])
        value = message_identity({'talker': 'a', 'local_id': 0, 'timestamp': 0, 'server_id_str': '0'})
        self.assertEqual((value['local_id'], value['timestamp'], value['message_id']), ('0', '0', None))

    def test_voice_ignores_quoted_audio_before_own_voice_path(self):
        other = self.audio('B.wav', b'B'); own = self.audio('A.wav', b'A')
        row = {**self.row(), 'quoted_message': {'talker': 'chat_B', 'audio_path': str(other)},
               'voice': {'audio_path': str(own)}}
        self.assertEqual(audio_path_from_message(row, self.root / 'cache'), own)
        row.pop('voice')
        self.assertIsNone(audio_path_from_message(row, self.root / 'cache'))

    def test_voice_rejects_foreign_declared_resource_identity(self):
        other = self.audio('B.wav')
        row = {**self.row(), 'voice': {'talker': 'chat_B', 'audio_path': str(other)}}
        self.assertIsNone(audio_path_from_message(row, self.root / 'cache'))
        row['local_media_metadata'] = {'audio_path': str(other)}
        self.assertIsNone(audio_path_from_message(row, self.root / 'cache'))

    def test_voice_outer_chat_drives_exact_cache_and_wrong_chat_is_missing(self):
        own = self.cached(); other = self.cached('chat_B')
        raw = self.row(); raw.pop('talker')
        row = {'source': 'wechat', 'chat_id': 'chat_A', 'timestamp': 17, 'original': raw}
        self.assertEqual(audio_path_from_message(row, self.root / 'cache'), own)
        own.unlink()
        self.assertIsNone(audio_path_from_message(row, self.root / 'cache'))
        self.assertTrue(other.exists())

    def test_voice_cache_requires_scope_not_just_unique_server_id(self):
        self.cached('chat_B')
        result = resolve_audio_from_message({'server_id_str': '777', 'local_id': 7, 'create_time': 17}, self.root / 'cache')
        self.assertEqual(result['status'], 'identity_unavailable')
        self.assertNotIn('path', result)

    def test_voice_rejects_exact_named_file_with_other_talker(self):
        other = self.cached('chat_B')
        result = resolve_audio_from_message({**self.row(), 'voice': {'audio_path': str(other)}}, self.root / 'empty-cache')
        self.assertNotIn('path', result)
        self.assertEqual(result['reason'], 'cache_identity_mismatch')

    def test_voice_duplicate_formats_and_same_audio_supported_but_conflicts_refused(self):
        own = self.audio('A.wav', b'A'); same = self.audio('copy.wav', b'A'); silk = self.audio('decoded-source.silk', b'different encoding')
        row = {**self.row(), 'voice': {'local_paths': [str(silk), str(own), str(same)]}}
        self.assertEqual(audio_path_from_message(row, self.root / 'cache'), own)
        same.write_bytes(b'other audio')
        self.assertEqual(resolve_audio_from_message(row, self.root / 'cache')['status'], 'identity_ambiguous')

    def test_voice_account_id_scopes_cache_when_present(self):
        own = self.cached(account='selfA', content=b'A'); self.cached(account='selfB', content=b'B')
        row = {**self.row(), 'account_id': 'selfA'}
        self.assertEqual(audio_path_from_message(row, self.root / 'cache'), own)
        row.pop('account_id')
        self.assertEqual(resolve_audio_from_message(row, self.root / 'cache')['status'], 'identity_ambiguous')

    def resolver(self):
        return WeChatMediaResolver(account_root=self.root / 'account', data_root=self.root / 'data',
                                  metadata_dir=self.root / 'metadata', output_dir=self.root / 'out', config_path=self.root / 'none.json')

    def test_wechat_wrapper_uses_outer_chat_and_cannot_borrow_unscoped_metadata(self):
        path = self.image('data/Emojis/B.png'); md5 = hashlib.md5(path.read_bytes()).hexdigest()
        raw = self.row(); raw.pop('talker'); raw['kind'] = 'sticker'
        wrapper = {'source': 'wechat', 'chat_id': 'chat_A', 'kind': 'sticker', 'message_id': '777', 'original': raw}
        with patch('unified_mcp.wechat_media.RUNTIME', self.root):
            resolver = self.resolver()
            result = resolver.enrich_payload(wrapper, metadata_rows=[{**raw, 'message_content_parsed': {'md5': md5}}])
            self.assertNotIn('images', result)
            self.assertEqual(result['wechat_media_resolution']['status'], 'metadata_missing')
            valid = {**raw, 'talker': 'chat_A', 'message_content_parsed': {'md5': md5}}
            positive = resolver.enrich_payload(wrapper, metadata_rows=[valid])
            self.assertEqual(positive['images'][0]['path'], str(path))

    def test_wechat_missing_chat_and_conflicting_content_fail_closed(self):
        a = self.image('data/Emojis/A.png', 'red'); b = self.image('data/Emojis/B.png', 'blue')
        raw = {**self.row(), 'kind': 'sticker'}
        with patch('unified_mcp.wechat_media.RUNTIME', self.root):
            resolver = self.resolver()
            missing = dict(raw); missing.pop('talker')
            self.assertEqual(resolver.resolve(missing)['status'], 'identity_unavailable')
            result = resolver.enrich_payload(raw, metadata_rows=[
                {**raw, 'message_content_parsed': {'md5': hashlib.md5(a.read_bytes()).hexdigest()}},
                {**raw, 'message_content_parsed': {'md5': hashlib.md5(b.read_bytes()).hexdigest()}}])
            self.assertEqual(result['wechat_media_resolution']['status'], 'identity_ambiguous')
            self.assertNotIn('images', result)

    def test_wechat_foreign_explicit_image_removed_from_failed_result(self):
        path = self.image('foreign.png')
        row = {**self.row(), 'kind': 'image', 'images': [{'path': str(path), 'chat_id': 'chat_B'}]}
        with patch('unified_mcp.wechat_media.RUNTIME', self.root):
            result = self.resolver().enrich_payload(row)
        self.assertEqual(result['wechat_media_resolution']['status'], 'identity_unavailable')
        self.assertNotIn('images', result)
        self.assertIn('images', row)

    def test_wechat_current_md5_must_not_be_overridden_by_conflicting_sidecar(self):
        a = self.image('data/Emojis/A.png', 'red'); b = self.image('data/Emojis/B.png', 'blue')
        raw = {**self.row(), 'kind': 'sticker', 'message_content_parsed': {'md5': hashlib.md5(a.read_bytes()).hexdigest()}}
        with patch('unified_mcp.wechat_media.RUNTIME', self.root):
            result = self.resolver().enrich_payload(raw, metadata_rows=[{**raw, 'message_content_parsed': {'md5': hashlib.md5(b.read_bytes()).hexdigest()}}])
        self.assertEqual(result['wechat_media_resolution']['status'], 'identity_ambiguous')

    def test_video_missing_and_conflicting_identity_returns_explicit_failure(self):
        resolver = WeChatVideoResolver(account_root=self.root / 'account', metadata_dir=self.root / 'video-metadata', config_path=self.root / 'none.json')
        raw = {**self.row(), 'kind': 'video'}
        missing = dict(raw); missing.pop('talker')
        self.assertEqual(resolver.resolve(missing)['status'], 'identity_unavailable')
        wrapper = {'kind': 'video', 'message_id': '777', 'chat_id': 'chat_B', 'original': raw, 'videos': [{'path': 'wrong.mp4'}]}
        result = resolver.enrich_payload(wrapper)
        self.assertEqual(result['wechat_video_resolution']['status'], 'identity_unavailable')
        self.assertNotIn('videos', result)
        self.assertIn('videos', wrapper)
        invalid_resource = {**missing, 'resources': [{'resource_family': 'video', 'md5': 'a' * 32}]}
        self.assertEqual(resolver.resolve(raw, resource_rows=[invalid_resource])['status'], 'identity_unavailable')

    def test_video_outer_identity_matches_scoped_resources_without_zero_time_fallback(self):
        account = self.root / 'account'; folder = account / 'msg/video/1970-01'; folder.mkdir(parents=True)
        (folder / ('a' * 32 + '.mp4')).write_bytes(b'synthetic video')
        resolver = WeChatVideoResolver(account_root=account, metadata_dir=self.root / 'video-metadata', config_path=self.root / 'none.json')
        raw = {**self.row(), 'kind': 'video'}; raw.pop('talker')
        wrapper = {'kind': 'video', 'message_id': '777', 'chat_id': 'chat_A', 'original': raw}
        resource = {**raw, 'talker': 'chat_A', 'resources': [{'resource_family': 'video', 'md5': 'a' * 32}]}
        with patch('unified_mcp.video_media.probe_video', return_value={'valid': True}):
            result = resolver.resolve(wrapper, resource_rows=[resource])
        self.assertEqual(result['status'], 'playable_local')

    def qq(self, name, month='2026-01'):
        return {'chat_id': 'chat_A', 'msg_id': '1', 'time': month + '-10T12:00:00+08:00', 'media': {'images': [name]}}

    def test_qq_current_month_conflict_is_not_first_hit_success(self):
        name = 'a' * 32 + '.png'
        self.image('qq/Pic/2026-10/Ori/' + name, 'blue')
        self.image('qq/Pic/2025-12/Ori/' + name, 'red')
        now = dt.datetime(2026, 10, 10, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        result = resolve_message_images(self.qq(name), self.root / 'qq', now=now)
        self.assertEqual(result['images'], [])
        self.assertEqual(result['qq_media_resolution']['items'][0]['status'], 'ambiguous_cache_files')

    def test_qq_message_month_conflict_also_rejected_and_same_copies_are_ok(self):
        name = 'b' * 32 + '.png'
        a = self.image('qq/Pic/2026-01/Ori/' + name, 'red')
        b = self.image('qq/Pic/2026-10/Ori/' + name, 'blue')
        now = dt.datetime(2026, 10, 10, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        self.assertEqual(resolve_message_images(self.qq(name), self.root / 'qq', now=now)['images'], [])
        b.write_bytes(a.read_bytes())
        result = resolve_message_images(self.qq(name), self.root / 'qq', now=now)
        self.assertEqual(result['images'][0]['path'], str(a))

    def test_qq_thumbnail_is_not_required_to_match_original_hash(self):
        name = 'c' * 32 + '.png'
        original = self.image('qq/Pic/2026-01/Ori/' + name, 'red')
        thumb = self.image('qq/Pic/2026-01/Thumb/' + name, 'blue')
        result = resolve_message_images(self.qq(name), self.root / 'qq')
        self.assertEqual(result['images'][0]['path'], str(original))
        original.unlink()
        result = resolve_message_images(self.qq(name), self.root / 'qq')
        self.assertEqual(result['images'][0]['path'], str(thumb))
        self.assertEqual(result['images'][0]['variant'], 'thumbnail')

    def test_qq_ordinary_basename_cannot_fallback_to_current_month(self):
        self.image('qq/Pic/2026-10/Ori/photo.png')
        result = resolve_message_images(self.qq('photo.png'), self.root / 'qq', now=dt.datetime(2026, 10, 10))
        self.assertEqual(result['images'], [])
        own = self.image('qq/Pic/2026-01/Ori/photo.png', 'red')
        result = resolve_message_images(self.qq('photo.png'), self.root / 'qq')
        self.assertEqual(result['images'][0]['path'], str(own))

    def test_qq_repeated_references_retain_occurrences(self):
        name = 'd' * 32 + '.png'; self.image('qq/Pic/2026-01/Ori/' + name)
        row = self.qq(name); row['media']['images'].append(name)
        result = resolve_message_images(row, self.root / 'qq')
        self.assertEqual([r['media_occurrence'] for r in result['images']], [0, 1])


if __name__ == '__main__':
    unittest.main()
