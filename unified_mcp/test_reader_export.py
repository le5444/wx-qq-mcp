import json
from pathlib import Path
import tempfile
import unittest
from unified_mcp.reader_export import build, file_uri


class ReaderExportTests(unittest.TestCase):
    def test_arbitrary_count_and_script_content_remain_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'input.jsonl';output=root/'reader.html'
            records=[{'source':'wechat','record_id':'demo:1','kind':'text','text':'</script><script>bad()</script>','original':{}},
                     {'source':'qq','record_id':'demo:2','kind':'text','text':'synthetic example','original':{}}]
            source.write_text('\n'.join(json.dumps(r) for r in records),encoding='utf-8')
            report=build(source,output,title='Synthetic reader')
            self.assertEqual(report['records'],2)
            content=output.read_text(encoding='utf-8')
            self.assertNotIn('</script><script>bad()',content)
            self.assertIn('\\u003c/script>',content)
            with self.assertRaises(FileExistsError):build(source,output)

    def test_update_uses_source_and_local_record_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'input.jsonl';update=root/'updates.jsonl'
            a={'source':'wechat','record_id':'local:1','message_id':'same','kind':'text','text':'first','original':{}}
            b={**a,'record_id':'local:2','text':'second'}
            source.write_text(json.dumps(a)+'\n'+json.dumps(b),encoding='utf-8')
            update.write_text(json.dumps({**a,'text':'updated'}),encoding='utf-8')
            output=root/'reader.html';build(source,output,updates=update)
            self.assertIn('updated',output.read_text(encoding='utf-8'))
            self.assertIn('second',output.read_text(encoding='utf-8'))

    def test_remote_or_missing_files_do_not_become_live_media_urls(self):
        self.assertEqual(file_uri('https://example.com/image.png'),'')
        self.assertEqual(file_uri('\\\\server\\private.png'),'')
        self.assertEqual(file_uri('not-a-real-file.jpg'),'')
