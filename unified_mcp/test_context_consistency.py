"""Source changes between exact location and context passes must remain visible."""
import copy
import unittest

from unified_mcp.analysis_tools import context, message
from unified_mcp.record_identity import extended_record_id
from unified_mcp.timeline import normal_message


CHAT = 'wxid_synthetic_context'


def wx(number, timestamp=100, text=None):
    return {'id': {'server_id_str': str(number), 'local_id': number},
            'create_time': timestamp, 'talker': CHAT, 'kind': 'text',
            'text': text or 'synthetic-' + str(number), 'sender_wxid': 'synthetic-peer',
            'is_from_me': False}


class ChangingHistory:
    """Realistic page boundary, but independently selected versions per pass."""
    def __init__(self, located, before=None, after=None, page_size=None):
        self.versions = [located, before if before is not None else located,
                         after if after is not None else located]
        self.pass_index = -1
        self.page_size = page_size
        self.calls = []

    async def fetch(self, source, name, args):
        self.calls.append(copy.deepcopy(args))
        if args.get('offset', 0) == 0:
            self.pass_index += 1
        rows = copy.deepcopy(self.versions[min(self.pass_index, len(self.versions)-1)])
        start, end = int(args.get('after', 0)), int(args['before'])
        rows = [row for row in rows if start <= row['create_time'] < end]
        # A stable storage position resolves fixture rows sharing all exposed
        # identifiers. The before and after reads reverse that order precisely.
        rows=[row for _,row in sorted(enumerate(rows), key=lambda pair:
              (pair[1]['create_time'],pair[1]['id']['local_id'],pair[0]),reverse=args['order']=='desc')]
        size=min(args['limit'], self.page_size or args['limit'])
        offset=args.get('offset',0)
        result=rows[offset:offset+size]
        return {'messages':result,'query':{'has_more':offset+len(result)<len(rows)}}


class ContextConsistencyTests(unittest.IsolatedAsyncioTestCase):
    def options(self, **values):
        return {'source':'wechat','chat_id':CHAT,'message_id':'2','after':'1','before':'1000',
                'include_media':False,'before_count':1,'after_count':1,**values}

    def assert_unchanged_partial(self, result):
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['target']['text'],'original target')
        self.assertEqual(result['before'],[])
        self.assertEqual(result['after'],[])
        self.assertTrue(result['warnings'])

    async def test_new_same_base_collision_does_not_replace_original_target(self):
        rows=[wx(1,99),wx(2,text='original target'),wx(3,101)]
        collision={**copy.deepcopy(rows[1]),'text':'different new target'}
        changed=[*rows,collision]
        backend=ChangingHistory(rows,changed,changed)
        self.assert_unchanged_partial(await context(self.options(),backend.fetch))

    async def test_later_pass_changed_body_does_not_override_first_pass(self):
        rows=[wx(1,99),wx(2,text='original target'),wx(3,101)]
        changed=[rows[0],{**copy.deepcopy(rows[1]),'text':'new body in second pass'},rows[2]]
        backend=ChangingHistory(rows,rows,changed)
        self.assert_unchanged_partial(await context(self.options(),backend.fetch))

    async def test_collision_after_requested_neighbors_still_detected_before_return(self):
        rows=[wx(1,99),wx(2,text='original target'),wx(3),wx(4,101)]
        collision={**copy.deepcopy(rows[1]),'text':'late same-second duplicate'}
        # Explicitly supply custom same-second order for the after pass: the
        # first neighbor arrives before a second target with the same base ID.
        backend=ChangingHistory(rows)
        original=backend.fetch
        async def fetch(source,name,args):
            page=await original(source,name,args)
            if backend.pass_index>=2 and args.get('offset',0)==0:
                page={'messages':[copy.deepcopy(rows[1]),copy.deepcopy(rows[2]),collision,copy.deepcopy(rows[3])],
                      'query':{'has_more':False}}
            return page
        self.assert_unchanged_partial(await context(self.options(),fetch))

    async def test_same_second_candidates_across_pages_are_all_checked(self):
        rows=[wx(1,99),wx(2,text='original target'),wx(3),wx(4,101)]
        collision={**copy.deepcopy(rows[1]),'text':'duplicate on another page'}
        backend=ChangingHistory(rows,[*rows,collision],[*rows,collision],page_size=1)
        self.assert_unchanged_partial(await context(self.options(),backend.fetch))

    async def test_exactly_repeated_target_is_ambiguous_even_when_content_same(self):
        rows=[wx(1,99),wx(2,text='original target'),wx(3,101)]
        backend=ChangingHistory(rows,rows+[copy.deepcopy(rows[1])],rows)
        self.assert_unchanged_partial(await context(self.options(),backend.fetch))

    async def test_target_disappearing_in_one_pass_preserves_located_evidence(self):
        rows=[wx(1,99),wx(2,text='original target'),wx(3,101)]
        backend=ChangingHistory(rows,rows,[rows[0],rows[2]])
        self.assert_unchanged_partial(await context(self.options(),backend.fetch))

    async def test_unchanged_same_second_neighbors_remain_in_native_order(self):
        rows=[wx(1,99),wx(2,text='original target'),wx(3),wx(4),wx(5,101)]
        backend=ChangingHistory(rows,page_size=1)
        result=await context(self.options(before_count=1,after_count=3),backend.fetch)
        self.assertEqual(result['status'],'ok')
        self.assertEqual([row['message_id'] for row in result['before']],['1'])
        self.assertEqual([row['message_id'] for row in result['after']],['3','4','5'])
        self.assertEqual(result['target']['text'],'original target')

    async def test_extended_id_can_distinguish_real_same_base_neighbor(self):
        first=wx(2,text='original target')
        collision={**copy.deepcopy(first),'text':'another source record'}
        rows=[wx(1,99),first,collision,wx(3,101)]
        target_id=extended_record_id(normal_message('wechat',first,CHAT)['record_id'],first)
        backend=ChangingHistory(rows)
        result=await context(self.options(record_id=target_id,message_id=None),backend.fetch)
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['target']['record_id'],target_id)
        self.assertEqual(result['target']['text'],'original target')
        self.assertEqual(result['after'][0]['text'],'another source record')
        self.assertEqual(result['before'][0]['message_id'],'1')

    async def test_scan_cap_inside_target_second_cannot_claim_unique_context(self):
        first=wx(2,text='original target')
        # Exact record_id locates only the target second. The original location
        # version has one row; the subsequent version has more same-second rows.
        target_id=normal_message('wechat',first,CHAT)['record_id']
        backend=ChangingHistory([first],[first,wx(3),wx(4)],[first,wx(3),wx(4)],page_size=1)
        result=await context(self.options(message_id=None,record_id=target_id,max_scan=2),backend.fetch)
        self.assert_unchanged_partial(result)

    async def test_both_neighbor_counts_zero_returns_original_without_extra_reads(self):
        rows=[wx(2,text='original target')]
        backend=ChangingHistory(rows)
        result=await context(self.options(before_count=0,after_count=0),backend.fetch)
        self.assertEqual(result['status'],'ok')
        self.assertEqual(len(backend.calls),1)
        self.assertEqual(result['before'],[])
        self.assertEqual(result['after'],[])

    async def test_source_boundaries_allow_fewer_neighbors_with_explicit_end(self):
        rows=[wx(2,text='original target')]
        backend=ChangingHistory(rows)
        result=await context(self.options(before_count=3,after_count=3),backend.fetch)
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['before'],[])
        self.assertEqual(result['after'],[])

    async def test_only_derived_media_can_be_combined_after_both_passes_validate(self):
        rows=[wx(1,99),wx(2,text='original target'),wx(3,101)]
        enriched=copy.deepcopy(rows); enriched[1]['images']=[{'path':'synthetic-only.png'}]
        backend=ChangingHistory(rows,enriched,enriched)
        result=await context(self.options(include_media=True),backend.fetch)
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['target']['text'],'original target')
        self.assertEqual(result['target']['original']['images'][0]['path'],'synthetic-only.png')

    async def test_false_nonstring_or_whitespace_message_id_rejected_without_reads(self):
        for value in (False, True, [], {}, 2, '   '):
            backend=ChangingHistory([wx(2)])
            with self.subTest(value=value),self.assertRaises(ValueError):
                await message(self.options(message_id=value),backend.fetch)
            self.assertEqual(backend.calls,[])

    async def test_record_id_type_errors_rejected_without_reads(self):
        for value in (False, True, [], {}, 2, ' '):
            backend=ChangingHistory([wx(2)])
            with self.subTest(value=value),self.assertRaises(ValueError):
                await message(self.options(message_id=None,record_id=value),backend.fetch)
            self.assertEqual(backend.calls,[])

    async def test_false_other_selector_is_not_silently_ignored(self):
        row=wx(2,text='original target')
        record_id=normal_message('wechat',row,CHAT)['record_id']
        backend=ChangingHistory([row])
        with self.assertRaises(ValueError):
            await message(self.options(message_id=False,record_id=record_id),backend.fetch)
        self.assertEqual(backend.calls,[])

    async def test_scan_cap_at_proved_terminal_target_second_is_complete(self):
        row=wx(2,text='original target')
        record_id=normal_message('wechat',row,CHAT)['record_id']
        backend=ChangingHistory([row],page_size=1)
        result=await context(self.options(message_id=None,record_id=record_id,max_scan=1),backend.fetch)
        self.assertEqual(result['status'],'ok')
        self.assertTrue(result['coverage']['complete'])
        self.assertTrue(all(value['target_second_complete'] for value in result['coverage']['sides'].values()))

    async def test_empty_original_text_cannot_be_replaced_with_new_words(self):
        rows=[wx(1,99),wx(2,text='original target'),wx(3,101)]
        rows[1]['text']=''
        changed=copy.deepcopy(rows)
        changed[1]['text']='new words replacing an empty source body'
        backend=ChangingHistory(rows,rows,changed)
        result=await context(self.options(),backend.fetch)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['target']['text'],'')
        self.assertEqual(result['before'],[])
        self.assertEqual(result['after'],[])

    async def test_inconsistent_same_key_order_does_not_put_one_neighbor_on_both_sides(self):
        first=wx(2,text='original target')
        collision={**copy.deepcopy(first),'text':'another source record'}
        rows=[wx(1,99),first,collision,wx(3,101)]
        target_id=extended_record_id(normal_message('wechat',first,CHAT)['record_id'],first)
        backend=ChangingHistory(rows)
        original=backend.fetch
        async def fetch(source,name,args):
            page=await original(source,name,args)
            if backend.pass_index==1 and args.get('offset',0)==0:
                page['messages']=[first,collision,rows[0]]
            return page
        result=await context(self.options(message_id=None,record_id=target_id),fetch)
        self.assert_unchanged_partial(result)
        self.assertTrue(any('both sides' in warning for warning in result['warnings']))


if __name__=='__main__':
    unittest.main()
