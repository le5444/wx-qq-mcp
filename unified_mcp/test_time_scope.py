"""Reject ambiguous/empty ranges before they can widen a history read."""
import base64
import json
import unittest
from unittest.mock import AsyncMock

from unified_mcp import analysis_tools
from unified_mcp.server import Gateway
from unified_mcp.timeline import date_scope, end_bound, fingerprint, merged_timeline, read_cursor, start_bound
from unified_mcp.time_scope import parse_time_bound, validate_time_range
import qq_mcp_server


class TimeGrammarTests(unittest.TestCase):
    def test_single_day_keeps_local_midnight_and_excludes_next_midnight(self):
        scope = date_scope({"date": "2026-10-09"})
        start, end = validate_time_range(scope['after'], scope['before'])
        self.assertEqual(end - start, 86400)
        self.assertEqual(start, parse_time_bound('2026-10-08T16:00:00Z'))

    def test_epoch_units_match_in_native_and_unified_parsers(self):
        for value in ('1791504000', '1791504000000', '0', '-1', '2026-10-09T08:00:00+08:00'):
            self.assertEqual(int(start_bound(value)), qq_mcp_server.parse_time_bound(value))
        self.assertEqual(parse_time_bound('2026-10-09T08:30+08:00'), parse_time_bound('2026-10-09T08:30:00+08:00'))

    def test_ambiguous_compact_date_rejected_by_both_entrypoints(self):
        for value in ('20261009', '2026W415', '', '  ', True, 1.5):
            for parser in (parse_time_bound, qq_mcp_server.parse_time_bound, start_bound):
                with self.subTest(value=value, parser=parser.__name__):
                    with self.assertRaises(ValueError):
                        parser(value)

    def test_explicit_invalid_date_never_means_all_history(self):
        for value in ('', '  ', None, False, '20261009', '2026-02-30'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    date_scope({'date': value})

    def test_reversed_user_range_rejected_but_empty_intersection_is_distinct(self):
        with self.assertRaisesRegex(ValueError, 'later'):
            date_scope({'after': '2026-10-09', 'before': '2026-10-07'})
        a, b = validate_time_range('2026-10-09T09:00:00+08:00', '2026-10-09T09:00:00+08:00')
        self.assertEqual(a, b)
        scope = date_scope({'date': '2099-01-01'})
        self.assertLess(int(end_bound(scope['before'], '2026-10-10T00:00:00+08:00')), int(start_bound(scope['after'])))

    def test_cli_end_date_remains_exclusive(self):
        start, end = validate_time_range('2026-10-09', '2026-10-10', end_date_inclusive=False)
        self.assertEqual(end - start, 86400)

    def test_broken_cursor_structure_rejected_without_guessing_offsets(self):
        args = {'wechat_chat': 'wxid_test'}
        base = {'v': 1, 'scope': fingerprint(args), 'offsets': {'wechat': 1, 'qq': 0}, 'snapshot_before': '2026-10-10T00:00:00+08:00'}
        for changed in ({'offsets': {}}, {'offsets': []}, {'offsets': {'wechat': True, 'qq': 0}},
                        {'snapshot_before': '2026-10-10'}, {'canonical_chats': []}, {'v': True}):
            encoded = base64.urlsafe_b64encode(json.dumps({**base, **changed}).encode()).decode()
            with self.subTest(changed=changed):
                with self.assertRaises(ValueError):
                    read_cursor(encoded, args)


class TimeReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_future_date_is_empty_without_invalid_clipped_backend_query(self):
        fetch = AsyncMock(side_effect=AssertionError('No time interval to query'))
        result = await merged_timeline({'wechat_chat': 'wxid_test', 'date': '2099-01-01'}, fetch)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['messages'], [])
        fetch.assert_not_awaited()

    async def test_invalid_ranges_make_no_backend_call(self):
        calls = []
        async def fetch(*args):
            calls.append(args)
            return {'messages': [], 'query': {'has_more': False}}
        for scope in ({'date': ''}, {'after': '20261009'}, {'after': '2026-10-09', 'before': '2026-10-07'}):
            with self.subTest(scope=scope):
                with self.assertRaises(ValueError):
                    await merged_timeline({'wechat_chat': 'wxid_test', **scope}, fetch)
                with self.assertRaises(ValueError):
                    await analysis_tools.group_stats({'source': 'wechat', 'chat_id': 'test@chatroom', **scope}, fetch)
                self.assertFalse(calls)

    async def test_native_wechat_invalid_range_rejected_before_reader(self):
        gateway = Gateway()
        gateway.wechat.call = AsyncMock()
        try:
            result = await gateway.call('chat_timeline', {'talker': 'wxid_test', 'after': '2026-10-09', 'before': '2026-10-07'})
            self.assertTrue(result.isError)
            gateway.wechat.call.assert_not_awaited()
        finally:
            await gateway.close()

    async def test_invalid_source_timestamp_is_not_rounded_into_scope(self):
        for value in (True, -1, 1700000000.9, '1700000000'):
            async def fetch(*_):
                return {'messages': [{'id': {'local_id': 1}, 'create_time': value}], 'query': {'has_more': False}}
            with self.subTest(value=value):
                result = await merged_timeline({'wechat_chat': 'wxid_test'}, fetch)
                self.assertEqual(result['status'], 'partial')
                self.assertFalse(result['available_source_pages'])


if __name__ == '__main__':
    unittest.main()
