"""Resolve one message identity without traversing quoted/forwarded messages."""
from __future__ import annotations


class IdentityError(ValueError):
    """A message has contradictory identity fields or lacks a required scope."""


FIELDS = ('source', 'chat_id', 'message_id', 'local_id', 'timestamp', 'account_id')
ALIASES = {
    'source': ('source',),
    'chat_id': ('chat_id', 'talker'),
    'message_id': ('message_id', 'server_id_str', 'server_id', 'msg_id', 'voice_server_id_str'),
    'local_id': ('local_id', 'voice_local_id'),
    'timestamp': ('timestamp', 'create_time', 'voice_create_time'),
    'account_id': ('account_id', 'self_account_id'),
}


def _value(field, value):
    if value is None or value == '':
        return None
    if isinstance(value, bool) or isinstance(value, (dict, list)):
        raise IdentityError('Invalid message identity field: ' + field)
    if field == 'source':
        # Raw readers also use source for provenance such as fts_only.
        return str(value) if value in ('wechat', 'qq') else None
    if field in ('message_id', 'local_id', 'timestamp'):
        raw = str(value)
        if raw.lstrip('-').isdigit():
            raw = str(int(raw))
        if field == 'message_id' and raw == '0':
            return None
        return raw
    return str(value)


def message_identity(row):
    """Merge current node/id/original only; conflicting values raise IdentityError.

    Missing fields are None. No account is inferred from a contact or nickname.
    Quoted messages, resource lists and arbitrary nested dictionaries are ignored.
    """
    result = dict.fromkeys(FIELDS)
    node = row
    seen = set()
    for _ in range(16):
        if not isinstance(node, dict):
            raise IdentityError('Message identity requires a dictionary')
        if id(node) in seen:
            raise IdentityError('Cyclic original message identity')
        seen.add(id(node))
        nodes = (node, node.get('id')) if isinstance(node.get('id'), dict) else (node,)
        for current in nodes:
            for field, aliases in ALIASES.items():
                for alias in aliases:
                    value = _value(field, current.get(alias))
                    if value is None:
                        continue
                    if result[field] is not None and result[field] != value:
                        raise IdentityError('Conflicting message identity field: ' + field)
                    result[field] = value
        if not isinstance(node.get('original'), dict):
            return result
        node = node['original']
    raise IdentityError('Original message identity nesting is too deep')


def identity_complete(identity):
    """A resource join needs a chat, time and at least one native record ID."""
    return bool(identity.get('chat_id') and identity.get('timestamp') is not None and
                (identity.get('message_id') is not None or identity.get('local_id') is not None))


def compatible_identity(expected, candidate, *, require_complete=False):
    """Compare normalized identity dictionaries. Never infer matching missing IDs."""
    if require_complete and not (identity_complete(expected) and identity_complete(candidate)):
        return False
    if any(expected.get(key) is not None and candidate.get(key) is not None and
           expected[key] != candidate[key] for key in FIELDS):
        return False
    if require_complete:
        return expected['chat_id'] == candidate['chat_id'] and expected['timestamp'] == candidate['timestamp'] and any(
            expected.get(key) is not None and expected.get(key) == candidate.get(key) for key in ('message_id', 'local_id'))
    return True
