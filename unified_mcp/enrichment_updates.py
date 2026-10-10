"""Validate a media update against its source message, then merge derived fields.

External updates are data, not authority to replace a message's identity or
content. Partial media/OCR patches are supported through exact source/record_id
keys. Raw legacy QQ updates require complete chat/time/native-ID evidence.
"""
from __future__ import annotations

import copy

from unified_mcp.message_identity import IdentityError, compatible_identity, message_identity
from unified_mcp.record_identity import DERIVED_FIELDS


TOP_DERIVED = frozenset({'image_text', 'processing_stages'})
RAW_DERIVED = DERIVED_FIELDS - {'local_media_metadata', 'local_media_resources', 'processing_stages'}
OUTER_IMMUTABLE = ('chat_type', 'sender_id', 'direction', 'kind', 'time')
RAW_IMMUTABLE = ('chat_type', 'sender_wxid', 'sender_uid', 'sender_uin', 'is_from_me',
                 'direction', 'kind', 'kind_name', 'base_kind', 'type_code', 'msg_code', 'seq')


def _agree(base, update, fields):
    for field in fields:
        if field in base and field in update and base[field] is not None and update[field] is not None:
            if str(base[field]) != str(update[field]):
                raise IdentityError('Conflicting media update field: ' + field)


def _semantic_claims(row, source):
    values = {}
    node = row
    while isinstance(node, dict):
        sender_fields = ('sender_id', 'sender_wxid') if source == 'wechat' else ('sender_id', 'sender_uid')
        claims = [('sender', node.get(field)) for field in sender_fields]
        direction = node.get('direction')
        direction = {'from_me': 'outgoing', 'from_contact': 'incoming', 'from_member': 'incoming'}.get(direction, direction)
        if direction not in (None, '', 'unknown'):
            claims.append(('direction', direction))
        if type(node.get('is_from_me')) is bool:
            claims.append(('direction', 'outgoing' if node['is_from_me'] else 'incoming'))
        for name, value in claims:
            if value not in (None, ''):
                if name in values and values[name] != str(value):
                    raise IdentityError('Conflicting message ' + name + ' across normalized/native fields')
                values[name] = str(value)
        node = node.get('original')
    return values


def validate_update(base, update, *, raw_qq=False):
    """Raises for contradictions; returns normalized identities for diagnostics."""
    if not isinstance(base, dict) or not isinstance(update, dict):
        raise IdentityError('Media update must be a dictionary')
    expected = message_identity(base)
    candidate = message_identity(update)
    if not compatible_identity(expected, candidate):
        raise IdentityError('Media update belongs to a different message')
    source = expected.get('source') or candidate.get('source')
    _agree(_semantic_claims(base, source), _semantic_claims(update, source), ('sender', 'direction'))
    if raw_qq:
        if expected.get('source') != 'qq' or candidate.get('source') not in (None, 'qq'):
            raise IdentityError('QQ enrichment source mismatch')
        if not compatible_identity(expected, candidate, require_complete=True):
            raise IdentityError('Legacy QQ enrichment requires matching chat, time and native message ID')
    else:
        if base.get('source') != update.get('source') or base.get('record_id') != update.get('record_id'):
            raise IdentityError('Media update source/record_id mismatch')
    if not raw_qq:
        _agree(base, update, OUTER_IMMUTABLE)
    old_raw = base.get('original') if isinstance(base.get('original'), dict) else {}
    new_raw = update.get('original') if isinstance(update.get('original'), dict) else update if raw_qq else {}
    _agree(old_raw, new_raw, RAW_IMMUTABLE)
    return expected, candidate


def merge_update(base, update, *, raw_qq=False):
    """Return a copy with derived additions, preserving all original source data."""
    validate_update(base, update, raw_qq=raw_qq)
    result = copy.deepcopy(base)
    for key in TOP_DERIVED:
        if key in update:
            result[key] = copy.deepcopy(update[key])
    raw = update.get('original') if isinstance(update.get('original'), dict) else update if raw_qq else {}
    original = result.setdefault('original', {})
    for key in RAW_DERIVED:
        if key in raw:
            original[key] = copy.deepcopy(raw[key])
    # ASR stores its structured output in both supported locations. Retain
    # duration/identity/source text and copy only the computed transcript field.
    if isinstance(raw.get('voice'), dict) and 'transcript' in raw['voice']:
        if not isinstance(original.get('voice'), dict):
            original['voice'] = {}
        original['voice']['transcript'] = copy.deepcopy(raw['voice']['transcript'])
    if 'warnings' in raw:
        old_warnings = original.get('warnings', [])
        old_warnings = old_warnings if isinstance(old_warnings, list) else [old_warnings]
        new_warnings = raw['warnings'] if isinstance(raw['warnings'], list) else [raw['warnings']]
        original['warnings'] = old_warnings + [value for value in new_warnings if value not in old_warnings]
    return result
