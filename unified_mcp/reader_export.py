"""Render normalized message JSONL as a local searchable HTML reader."""
import argparse
import json
from pathlib import Path


def file_uri(value):
    if not isinstance(value, str) or not value or '://' in value or value.startswith(('\\\\', '//')):
        return ''
    try:
        path = Path(value).expanduser().resolve()
        return path.as_uri() if path.is_file() else ''
    except (OSError, ValueError):
        return ''


def load_rows(path):
    with Path(path).open(encoding='utf-8-sig') as handle:
        for index, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or 'source' not in row or 'record_id' not in row:
                raise ValueError(f'Line {index} is not a normalized source/record_id message')
            yield row


def build(input_path, output_path, title='微信与 QQ · 本地聊天阅读', updates=None, media_only=False):
    output_path = Path(output_path)
    if output_path.exists():
        raise FileExistsError('Output already exists; choose a new file')
    replacements = {(r['source'], r['record_id']): r for r in load_rows(updates)} if updates else {}
    items = []
    total = 0
    for original_row in load_rows(input_path):
        total += 1
        row = replacements.get((original_row['source'], original_row['record_id']), original_row)
        original = row.get('original', {})
        transcript = original.get('voice_transcript') or original.get('voice', {}).get('transcript', {})
        images = [file_uri(ref.get('path')) for ref in original.get('images', []) if isinstance(ref, dict)]
        videos = [file_uri(ref.get('path')) for ref in original.get('videos', []) if isinstance(ref, dict)]
        images, videos = list(dict.fromkeys(filter(None, images))), list(dict.fromkeys(filter(None, videos)))
        audio = file_uri(transcript.get('decoded_audio_path') or transcript.get('audio_path'))
        kind = row.get('kind', '')
        category = 'voice' if kind == 'voice' else 'video' if kind == 'video' else 'sticker' if kind in ('sticker','emoji') else 'image' if images or 'image' in kind else 'text'
        if media_only and category == 'text':
            continue
        text = transcript.get('text') if category == 'voice' and transcript.get('status') == 'ok' else row.get('text', '')
        ocr = row.get('image_text', []) or [ref.get('ocr', {}) for ref in original.get('images', []) if isinstance(ref, dict)]
        items.append({'id': row['record_id'], 'source': {'wechat':'微信','qq':'QQ'}.get(row['source'],row['source']),
                      'category':category, 'time':row.get('time') or '', 'sender':row.get('sender_name') or '发送者未核实',
                      'text':text or '', 'status':original.get('wechat_media_resolution',{}).get('status','from_input'),
                      'images':images, 'videos':videos, 'audio':audio,
                      'covers':list(filter(None,(file_uri(p) for p in original.get('wechat_video_resolution',{}).get('cover_paths',[])))),
                      'ocr':'\n\n'.join(value.get('text','') for value in ocr if value.get('status') in ('ok','partial')),
                      'model':transcript.get('model'), 'only_thumbnail':bool(original.get('wechat_media_resolution',{}).get('only_thumbnail')),
                      'review_required':bool(transcript.get('review_required')),
                      'available':bool(images or videos or audio or category=='text')})
    report={'title':title,'records':total,'displayed':len(items),'scope':'Input file only; this reader does not establish history completeness or revalidate media contents.'}
    dataset=json.dumps({'report':report,'items':items},ensure_ascii=False).replace('<',chr(92)+'u003c')
    template=Path(__file__).with_name('reader_template.html').read_text(encoding='utf-8')
    output_path.parent.mkdir(parents=True,exist_ok=True)
    with output_path.open('x',encoding='utf-8') as handle:
        handle.write(template.replace('__DATA__',dataset))
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--title',default='微信与 QQ · 本地聊天阅读')
    parser.add_argument('--media-updates',type=Path)
    parser.add_argument('--media-only',action='store_true')
    args=parser.parse_args()
    print(json.dumps(build(args.input,args.output,args.title,args.media_updates,args.media_only),ensure_ascii=False))


if __name__=='__main__':
    main()
