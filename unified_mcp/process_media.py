"""Enrich an existing local snapshot without re-exporting or altering sources."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
from pathlib import Path
import time

from unified_mcp.image_text import ImageTextReader, image_reference_rank
from unified_mcp.voice_transcription import VoiceService, atomic_json
from unified_mcp.wechat_media import enrich_wechat_media


def image_paths(value, best_only=False):
    paths = []
    def visit(node):
        if isinstance(node, dict):
            for key, child in node.items():
                if key in ('images', 'cached_images') and isinstance(child, list):
                    choices = child
                    if best_only and len(child) > 1:
                        choices = [max(child, key=image_reference_rank)]
                    for item in choices:
                        if isinstance(item, dict):
                            path = item.get('path') or item.get('cache_path')
                            if path and path not in paths:
                                paths.append(path)
                if isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)
    visit(value)
    return paths


async def run(snapshot, output, qq_enriched=None, limit=None, resume=False):
    output.mkdir(parents=True, exist_ok=True)
    result_file = output / 'media-messages.jsonl'
    summary_file = output / 'summary.json'
    if result_file.exists() and not resume:
        raise FileExistsError('Use a new media output directory; previous records are preserved')
    qq_lookup = {}
    if qq_enriched and qq_enriched.exists():
        with qq_enriched.open(encoding='utf-8') as stream:
            for line in stream:
                row = json.loads(line)
                raw = row.get('original', row)
                qq_lookup[str(raw.get('msg_id', row.get('message_id', '')))] = raw
    reader = ImageTextReader()
    voices = VoiceService()
    counts = Counter()
    ocr_counts = Counter()
    completed_ids = set()
    started = time.monotonic()
    report = {'state': 'running', 'processed': 0, 'counts': {}, 'ocr': {},
              'snapshot': str(snapshot), 'output': str(result_file),
              'audio_transcription_mode': 'success_cache_only; independent batch handles missing transcripts',
              'semantic_scope': 'Local media availability and OCR/ASR. OCR does not describe all visual content.'}
    if resume and result_file.exists():
        previous = json.loads(summary_file.read_text(encoding='utf-8'))
        if Path(previous['snapshot']).resolve() != snapshot.resolve():
            raise ValueError('Resume snapshot does not match the existing output')
        with result_file.open(encoding='utf-8') as stream:
            for line in stream:
                item = json.loads(line)
                completed_ids.add((item['source'], item['record_id']))
                original = item['original']
                resolution = original.get('wechat_media_resolution' if item['source'] == 'wechat' else 'qq_media_resolution', {})
                status = resolution.get('status')
                if item['kind'] == 'voice':
                    status = original.get('voice_transcript', {}).get('status', 'not_transcribed')
                counts[f"{item['source']}:{item['kind']}:{status or 'unresolved'}"] += 1
                for ocr in item.get('image_text', []):
                    ocr_counts[ocr.get('status', 'unknown')] += 1
        report.update(processed=len(completed_ids), resumed=True,
                      previous_elapsed_seconds=previous.get('elapsed_seconds', 0))
    atomic_json(summary_file, report)
    try:
        with result_file.open('a' if resume else 'x', encoding='utf-8') as destination:
            for source in ('wechat', 'qq'):
                with (snapshot / (source + '.jsonl')).open(encoding='utf-8') as stream:
                    for line in stream:
                        row = json.loads(line)
                        if (source, row['record_id']) in completed_ids:
                            continue
                        original = row['original']
                        if source == 'wechat':
                            if row.get('kind') not in ('image', 'sticker', 'voice', 'video'):
                                continue
                            enriched = await asyncio.to_thread(enrich_wechat_media, original)
                            enriched = await voices.enrich(enriched, transcribe=False)
                            status = enriched.get('wechat_media_resolution', {}).get('status')
                            if row.get('kind') == 'voice':
                                status = enriched.get('voice_transcript', {}).get('status', 'not_transcribed')
                        else:
                            original = qq_lookup.get(row['message_id'], original)
                            resolution = original.get('qq_media_resolution', {})
                            if resolution.get('status', 'no_image_hints') == 'no_image_hints':
                                continue
                            enriched = original
                            status = resolution.get('status', 'unknown')
                        image_text = []
                        for path in image_paths(enriched, best_only=source == 'wechat'):
                            result = await asyncio.to_thread(reader.read, path)
                            image_text.append({'path': path, **result})
                            ocr_counts[result.get('status', 'unknown')] += 1
                        result_row = {**row, 'original': enriched, 'image_text': image_text,
                                      'image_text_scope': 'largest_available_variant' if source == 'wechat' else 'each_available_image'}
                        destination.write(json.dumps(result_row, ensure_ascii=False) + '\n')
                        destination.flush()
                        counts[f"{source}:{row.get('kind')}:{status or 'unresolved'}"] += 1
                        report['processed'] += 1
                        report.update(counts=dict(counts), ocr=dict(ocr_counts), elapsed_seconds=round(time.monotonic()-started, 2))
                        atomic_json(summary_file, report)
                        if report['processed'] % 100 == 0:
                            print(json.dumps({'processed': report['processed'], 'ocr': dict(ocr_counts)}, ensure_ascii=False), flush=True)
                        if limit and report['processed'] >= limit:
                            raise RuntimeError('Limited diagnostic run; full source coverage was not completed')
        report['state'] = 'complete'
    except Exception as exc:
        report.update(state='failed', error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        atomic_json(summary_file, report)
        await voices.close()
        await asyncio.to_thread(reader.close)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--qq-enriched', type=Path)
    parser.add_argument('--resume', action='store_true')
    options = parser.parse_args()
    asyncio.run(run(options.snapshot, options.output, options.qq_enriched, resume=options.resume))
