"""Build offline readers, optional self-contained media packages, and bounded pages."""
import argparse
import html
import json
import os
from pathlib import Path
import shutil
import tempfile
from urllib.parse import quote

from unified_mcp.batch_support import atomic_json, canonical, file_fingerprint, open_store, output_lock
from unified_mcp.enrichment_updates import merge_update
from unified_mcp.message_identity import message_identity


def local_file(value):
    if not isinstance(value, str) or not value or '://' in value or value.startswith(('\\\\', '//')):
        return None
    try:
        path = Path(value).expanduser().resolve()
        if str(path).startswith(('\\\\', '//')):
            return None
        return path if path.is_file() else None
    except (OSError, ValueError):
        return None


def file_uri(value):
    path = local_file(value)
    return path.as_uri() if path else ''


def load_rows(path):
    with Path(path).open(encoding='utf-8-sig') as handle:
        for index, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or 'source' not in row or 'record_id' not in row:
                raise ValueError(f'Line {index} is not a normalized source/record_id message')
            if row['source'] not in ('wechat', 'qq') or not isinstance(row['record_id'], str) or not row['record_id']:
                raise ValueError(f'Line {index} has an invalid source/record_id')
            message_identity(row)  # Reject contradictions between outer/native IDs.
            yield row


class Assets:
    def __init__(self, folder, name, db, portable):
        self.folder, self.name, self.db, self.portable = folder, name, db, portable
        self.count = 0
        self.references = 0
        self.manifest = (folder / (name + '.media.jsonl')).open('x', encoding='utf-8') if portable else None
        db.execute('CREATE TABLE assets(path TEXT PRIMARY KEY,size INTEGER,mtime INTEGER,target TEXT,sha TEXT)')

    def reference(self, value, row, role, occurrence):
        path = local_file(value)
        if path is None:
            return ''
        if not self.portable:
            return path.as_uri()
        stat = path.stat()
        saved = self.db.execute('SELECT size,mtime,target,sha FROM assets WHERE path=?', (str(path),)).fetchone()
        if saved and saved[:2] != (stat.st_size, stat.st_mtime_ns):
            raise ValueError('A media source changed during portable export')
        if not saved:
            fingerprint = file_fingerprint(path)
            suffix = path.suffix.lower()
            if len(suffix) > 12 or not suffix.removeprefix('.').isalnum():
                suffix = '.bin'
            target = self.name + '.assets/' + fingerprint['sha256'] + suffix
            destination = self.folder / target
            destination.parent.mkdir(exist_ok=True)
            if not destination.exists():
                shutil.copyfile(path, destination)
                if file_fingerprint(destination) != fingerprint or file_fingerprint(path) != fingerprint:
                    raise ValueError('Media changed during copying; output package was not published')
                self.count += 1
            self.db.execute('INSERT INTO assets VALUES(?,?,?,?,?)', (str(path), stat.st_size, stat.st_mtime_ns, target, fingerprint['sha256']))
            saved = (stat.st_size, stat.st_mtime_ns, target, fingerprint['sha256'])
        self.references += 1
        self.manifest.write(canonical({'source': row['source'], 'record_id': row['record_id'], 'role': role,
                                      'occurrence': occurrence, 'path': saved[2], 'sha256': saved[3], 'bytes': saved[0]}) + '\n')
        return quote(saved[2], safe='/._-')

    def close(self):
        if self.manifest:
            self.manifest.close()


def _item(row, assets):
    original = row.get('original') or {}
    transcript = original.get('voice_transcript') or (original.get('voice') or {}).get('transcript', {})
    if not isinstance(transcript, dict):
        transcript = {'status': 'unverified', 'text': str(transcript)}
    # Preserve repeated occurrences. Content-addressed copying may deduplicate
    # files, but it must not silently delete a repeated image within a message.
    images = [assets.reference(ref.get('path') or ref.get('cache_path'), row, 'image', i)
              for i, ref in enumerate(original.get('images') or []) if isinstance(ref, dict)]
    videos = [assets.reference(ref.get('path'), row, 'video', i)
              for i, ref in enumerate(original.get('videos') or []) if isinstance(ref, dict)]
    images, videos = list(filter(None, images)), list(filter(None, videos))
    audio = assets.reference(transcript.get('decoded_audio_path') or transcript.get('audio_path'), row, 'audio', 0)
    kind = row.get('kind') or ''
    category = 'voice' if kind == 'voice' else 'video' if kind == 'video' else 'sticker' if kind in ('sticker', 'emoji') else 'image' if images or 'image' in kind else 'text'
    text = transcript.get('text') if category == 'voice' and transcript.get('status') == 'ok' else row.get('text', '')
    if category == 'voice' and transcript.get('status') == 'no_speech':
        text = '自动识别未检测到语音内容，请播放原音核对。'
    ocr = row.get('image_text', []) or [ref.get('ocr') or {} for ref in original.get('images') or [] if isinstance(ref, dict)]
    covers = [assets.reference(path, row, 'cover', i) for i, path in enumerate(original.get('wechat_video_resolution', {}).get('cover_paths', []))]
    return {'id': row['record_id'], 'source': {'wechat': '微信', 'qq': 'QQ'}.get(row['source'], row['source']),
            'category': category, 'time': row.get('time') or '', 'sender': row.get('sender_name') or '发送者未核实',
            'text': text or '', 'status': original.get('wechat_media_resolution', original.get('qq_media_resolution', {})).get('status', 'from_input'),
            'images': images, 'videos': videos, 'audio': audio, 'covers': list(filter(None, covers)),
            'ocr': '\n\n'.join(value.get('text', '') for value in ocr if value.get('status') in ('ok', 'partial')),
            'model': transcript.get('model'), 'only_thumbnail': bool(original.get('wechat_media_resolution', {}).get('only_thumbnail')),
            'review_required': bool(transcript.get('review_required')),
            'available': bool(images or videos or audio or category == 'text')}


def _render(path, report, items, navigation=None, relative_prefix=''):
    if relative_prefix:
        # Relative paths in part pages point up one level to the shared assets.
        for item in items:
            for key in ('images', 'videos', 'covers'):
                item[key] = [relative_prefix + value if not value.startswith('file:') else value for value in item[key]]
            if item['audio'] and not item['audio'].startswith('file:'):
                item['audio'] = relative_prefix + item['audio']
    dataset = json.dumps({'report': report, 'items': items, 'navigation': navigation or {}}, ensure_ascii=False).replace('<', '\\u003c')
    template = Path(__file__).with_name('reader_template.html').read_text(encoding='utf-8')
    path.write_text(template.replace('__DATA__', dataset), encoding='utf-8')


def build(input_path, output_path, title='微信与 QQ · 本地聊天阅读', updates=None, media_only=False, *, portable=False, chunk_size=2000):
    output_path = Path(output_path).resolve()
    if type(chunk_size) is not int or not 1 <= chunk_size <= 20000:
        raise ValueError('chunk_size must be between 1 and 20000')
    with output_lock(output_path.parent):
        names = [output_path.name, output_path.stem + '.assets', output_path.stem + '.pages',
                 output_path.stem + '.manifest.json', output_path.stem + '.media.jsonl']
        if any((output_path.parent / name).exists() for name in names):
            raise FileExistsError('Output or package artifacts already exist; choose a new file')
        with tempfile.TemporaryDirectory(prefix='wxqq-reader-', dir=output_path.parent) as temporary:
            folder = Path(temporary)
            db = open_store(folder / 'build.sqlite3')
            db.execute('CREATE TABLE updates(source TEXT,record_id TEXT,payload TEXT,PRIMARY KEY(source,record_id))')
            try:
                if updates:
                    with db:
                        for row in load_rows(updates):
                            encoded = canonical(row)
                            prior = db.execute('SELECT payload FROM updates WHERE source=? AND record_id=?',
                                               (row['source'], row['record_id'])).fetchone()
                            if prior and prior[0] != encoded:
                                raise ValueError('Conflicting duplicate media updates for one source/record_id')
                            db.execute('INSERT OR IGNORE INTO updates VALUES(?,?,?)', (row['source'], row['record_id'], encoded))
            except BaseException:
                db.close()
                raise
            assets = Assets(folder, output_path.stem, db, portable)
            total, displayed, items, parts = 0, 0, [], []
            part_dir = folder / (output_path.stem + '.pages')
            part_dir.mkdir()
            def flush_part():
                if not items:
                    return
                number = len(parts) + 1
                name = f'part-{number:05d}.html'
                part_report = {'title': title + f' · 分片 {number}', 'records': len(items), 'displayed': len(items),
                               'scope': '当前分片；搜索和筛选仅覆盖本页。返回目录查看其他时间范围。'}
                _render(part_dir / name, part_report, items,
                        {'index': '../' + quote(output_path.name), 'previous': f'part-{number-1:05d}.html' if number > 1 else '',
                         'next': f'part-{number+1:05d}.html'}, '../' if portable else '')
                parts.append({'path': part_dir.name + '/' + name, 'records': len(items), 'first_time': items[0]['time'], 'last_time': items[-1]['time']})
                items.clear()
            try:
                for incoming in load_rows(input_path):
                    total += 1
                    replacement = db.execute('SELECT payload FROM updates WHERE source=? AND record_id=?', (incoming['source'], incoming['record_id'])).fetchone()
                    row = merge_update(incoming, json.loads(replacement[0])) if replacement else incoming
                    item = _item(row, assets)
                    if media_only and item['category'] == 'text':
                        continue
                    # Delay the first flush until an additional item arrives,
                    # preserving the old single-file shape for small readers.
                    if len(items) == chunk_size:
                        flush_part()
                    items.append(item)
                    displayed += 1
                report = {'title': title, 'records': total, 'displayed': displayed, 'portable': portable,
                          'scope': 'Input file only; history completeness and semantic media understanding are not established by this reader.',
                          'chunk_size': chunk_size, 'parts': parts, 'media_files': assets.count,
                          'media_references': assets.references}
                if parts:
                    flush_part()
                    # Remove the speculative next-page link on the final page.
                    last = folder / parts[-1]['path']
                    content = last.read_text(encoding='utf-8')
                    content = content.replace(f'"next": "part-{len(parts)+1:05d}.html"', '"next": ""')
                    last.write_text(content, encoding='utf-8')
                    links = ''.join(f'<li><a href="{quote(part["path"])}">第 {i+1} 片：{html.escape(part["first_time"])} ～ {html.escape(part["last_time"])} · {part["records"]} 条</a></li>' for i, part in enumerate(parts))
                    index = f'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>{html.escape(title)}</title><body style="font:17px/1.8 sans-serif;max-width:960px;margin:40px auto;padding:20px"><h1>{html.escape(title)}</h1><p>输入共 {total} 条，展示 {displayed} 条。每个分片独立离线打开；搜索和筛选仅覆盖当前分片，请按日期范围进入。</p><ol>{links}</ol></body></html>'
                    (folder / output_path.name).write_text(index, encoding='utf-8')
                else:
                    _render(folder / output_path.name, report, items)
                    part_dir.rmdir()
                report['pages'] = len(parts) if parts else 1
                report['media_manifest'] = output_path.stem + '.media.jsonl' if portable else None
                atomic_json(folder / (output_path.stem + '.manifest.json'), report)
            finally:
                assets.close()
                db.close()
            # The private build database remains temporary. Publish only the
            # reader, explicit package assets, and bounded manifests.
            for name in names[1:]:
                artifact = folder / name
                if artifact.exists():
                    artifact.rename(output_path.parent / name)
            (folder / output_path.name).rename(output_path)
            return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--title', default='微信与 QQ · 本地聊天阅读')
    parser.add_argument('--media-updates', type=Path)
    parser.add_argument('--media-only', action='store_true')
    parser.add_argument('--portable', action='store_true', help='Copy available local media into a relative-path offline package')
    parser.add_argument('--chunk-size', type=int, default=2000)
    args = parser.parse_args()
    print(json.dumps(build(args.input, args.output, args.title, args.media_updates, args.media_only,
                           portable=args.portable, chunk_size=args.chunk_size), ensure_ascii=False))


if __name__ == '__main__':
    main()
