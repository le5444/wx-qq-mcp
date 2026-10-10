"""Stable source fingerprints and exported collision record IDs.

The native record ID remains usable. When multiple distinct native rows share
it, an exported collision record carries a content discriminator. Recognition
and recovered media fields do not change that discriminator.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re


DERIVED_FIELDS = frozenset({
    'images', 'cached_images', 'videos', 'cover_images', 'thumbnails', 'image_text',
    'voice_transcript', 'qq_media_resolution', 'wechat_media_resolution',
    'wechat_video_resolution', 'media_validation_summary', 'media_validation',
    'processing_stages', 'local_media_metadata', 'local_media_resources',
    'media_read_hints', 'local_path_details',
})
HASH = re.compile(r'^[a-f0-9]{64}$')


def stable_raw(row):
    """Return source content only; never mutate a supplied message."""
    value = copy.deepcopy(row)
    for key in DERIVED_FIELDS | {'warnings'}:
        value.pop(key, None)
    original_text = value.pop('original_voice_text', None)
    if original_text is not None:
        value['text'] = original_text
    if isinstance(value.get('voice'), dict):
        for key in ('transcript', 'warnings', 'audio_path', 'decoded_audio_path', 'decoded_path',
                    'cache_path', 'cache_hit', 'automatic', 'human_verified'):
            value['voice'].pop(key, None)
        if not value['voice']:
            value.pop('voice')
    return value


def raw_fingerprint(row):
    encoded = json.dumps(stable_raw(row), ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def extended_record_id(base_record_id, row):
    return base_record_id + ':sha256:' + raw_fingerprint(row)


def split_record_id(source, chat_id, record_id):
    """Return (native base ID, optional digest), accepting v0.3 legacy suffixes."""
    if source not in {'wechat', 'qq'} or not isinstance(record_id, str) or not record_id.startswith(str(chat_id) + ':'):
        raise ValueError('record_id belongs to a different chat or source')
    suffix = None
    if ':sha256:' in record_id:
        base, suffix = record_id.rsplit(':sha256:', 1)
        if not HASH.fullmatch(suffix):
            raise ValueError('Invalid record_id fingerprint')
    else:
        base = record_id
        pieces = record_id[len(str(chat_id)) + 1:].split(':')
        native_parts = 4 if source == 'wechat' else 1
        if len(pieces) == native_parts + 1 and HASH.fullmatch(pieces[-1]):
            base, suffix = record_id.rsplit(':', 1)
    pieces = base[len(str(chat_id)) + 1:].split(':')
    if len(pieces) != (4 if source == 'wechat' else 1):
        raise ValueError('Invalid native record_id')
    if source == 'wechat':
        int(pieces[2])  # timestamp is required for exact scoped lookup.
    return base, suffix


def matches_record_id(source, chat_id, row, record_id):
    from unified_mcp.timeline import normal_message
    base, suffix = split_record_id(source, chat_id, record_id)
    if normal_message(source, row, chat_id)['record_id'] != base:
        return False
    if suffix is None:
        return True
    if raw_fingerprint(row) == suffix:
        return True
    # Versions through 0.3.0 appended an unlabelled hash of this shallower
    # projection. Retain lookup compatibility for already-exported snapshots.
    if ':sha256:' not in record_id:
        old_derived = {'images', 'cached_images', 'videos', 'image_text', 'voice_transcript',
                       'qq_media_resolution', 'wechat_media_resolution', 'wechat_video_resolution'}
        legacy = {key: value for key, value in row.items() if key not in old_derived}
        encoded = json.dumps(legacy, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(encoded.encode('utf-8')).hexdigest() == suffix
    return False
