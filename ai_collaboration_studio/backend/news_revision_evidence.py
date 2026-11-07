"""Bounded comparisons of two immutable saved versions, never a live lookup.

Both saved bodies (or both original excerpts) form one atomic comparison. No
paragraph alignment, table-row matching, numerical inference or partial old
context is used. A large/unknown comparison is explicitly unavailable.
"""
import copy
import hashlib
import json

from .news_evidence_groups import _structure_valid, encoded_size

UNKNOWN = '缺少预算内完整、可比的旧版与新版原文，无法核验本次新增或更正。'


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def candidate(item, checked_at):
    from .news_event_store import timestamp
    event = item.get('event') or {}
    current = event.get('current_version') or {}
    previous_id = current.get('previous_version_id')
    if not previous_id:
        return None
    result = {'schema': 'saved_article_revision_comparison_v1', 'status': 'unavailable',
              'previous_version_id': previous_id, 'current_version_id': item.get('current_version_id'),
              'reason': 'previous_snapshot_unavailable', 'unknown': UNKNOWN}
    previous = event.get('previous_version')
    if not isinstance(previous, dict):
        return result
    before_time, after_time, cutoff = (timestamp(value) for value in
        (previous.get('version_seen_at'), current.get('version_seen_at'), checked_at))
    if (current.get('id') != item.get('current_version_id') or previous.get('id') != previous_id
            or previous.get('item_id') != current.get('item_id') or current.get('item_id') != item.get('id')
            or type(current.get('version_number')) is not int or type(previous.get('version_number')) is not int
            or previous['version_number'] + 1 != current['version_number']
            or None in (before_time, after_time, cutoff) or not before_time <= after_time <= cutoff):
        return {**result, 'reason': 'revision_binding_unverified'}
    # The selected item's current source must itself match the frozen version.
    if any(item.get(key) != current.get(key) for key in ('url', 'title', 'excerpt', 'evidence')):
        return {**result, 'reason': 'current_snapshot_mismatch'}
    documents, body, selection = [], '', []
    for role, version in (('before', previous), ('after', current)):
        evidence = version.get('evidence') or {}
        state = evidence.get('content_status', evidence.get('status'))
        if state == 'full_text' and not evidence.get('truncated'):
            text = evidence.get('body_text')
            rows = evidence.get('segments') or [{'id': 'body', 'kind': 'paragraph', 'text': text}]
            has_tables = bool(evidence.get('tables')) or any(row.get('kind') == 'table_row' for row in rows)
            if (not isinstance(text, str) or not text.strip()
                    or hashlib.sha256(text.encode()).hexdigest() != evidence.get('content_hash')
                    or text != '\n'.join(row.get('text', '') for row in rows)
                    or has_tables and not _structure_valid(evidence, rows)):
                return {**result, 'reason': 'complete_body_unverified'}
            scope = 'saved_body_only_attachments_not_compared'
        elif state in {'excerpt_only', 'headline_only'} and isinstance(version.get('excerpt'), str) and version['excerpt'].strip():
            rows = [{'id': 'excerpt', 'kind': 'paragraph', 'text': version['excerpt']}]
            scope = 'original_excerpt_only_full_body_unknown'
        else:
            return {**result, 'reason': 'incomplete_or_retracted_source'}
        if (any(not isinstance(row.get('id'), str) or not row['id'] for row in rows)
                or len({row['id'] for row in rows}) != len(rows)):
            return {**result, 'reason': 'segment_identity_unverified'}
        body += ('\n' if body else '') + ('[已保存旧版本原文]\n' if role == 'before' else '[所选新版本原文]\n')
        ids = []
        for row in rows:
            identity = role + ':' + row['id']
            start = len(body)
            body += row['text'] + '\n'
            ids.append(identity)
            selection.append({**{key: copy.deepcopy(row[key]) for key in ('kind', 'table_id', 'row_index', 'cells') if key in row},
                              'id': identity, 'source_segment_id': row['id'], 'role': role,
                              'version_id': version['id'], 'start': start, 'end': len(body) - 1})
        documents.append({'role': role, 'version_id': version['id'], 'version_number': version['version_number'],
                          'version_seen_at': version['version_seen_at'], 'url': version['url'],
                          'scope': scope, 'required_segment_ids': ids})
    if documents[0]['scope'] != documents[1]['scope']:
        return {**result, 'reason': 'reading_scope_changed_not_comparable'}
    # Title/provenance-only revisions do not imply that the underlying body changed.
    split = len(documents[0]['required_segment_ids'])
    before_rows, after_rows = selection[:split], selection[split:]
    def content(rows):
        return [{**{key: value for key, value in row.items() if key not in {'id', 'role', 'version_id', 'start', 'end'}},
                 'text': body[row['start']:row['end']]} for row in rows]
    if content(before_rows) == content(after_rows):
        return {**result, 'status': 'source_text_unchanged', 'reason': 'no_comparable_body_change'}
    result.update(status='available', reason=None, documents=documents, selection=selection,
                  comparison_scope='complete_saved_source_pair_not_independent_fact_check',
                  text_sha256=hashlib.sha256(body.encode()).hexdigest())
    result['comparison_id'] = _hash({'manifest': result, 'text': body})
    return {**result, '_text': body}


def public(value, *, reason=None):
    if not value:
        return None
    if reason:
        return {key: copy.deepcopy(value[key]) for key in ('schema', 'previous_version_id', 'current_version_id', 'unknown')} | {
            'status': 'unavailable', 'reason': reason}
    return {key: copy.deepcopy(value) for key, value in value.items() if key != '_text'}


def attach_reading(build_current, item, *, checked_at, text_budget, max_bytes):
    comparison = candidate(item, checked_at)
    if comparison is None:
        return build_current(max_bytes)
    unavailable = public(comparison, reason='comparison_budget' if comparison['status'] == 'available' else None)
    reserve = encoded_size({'revision_comparison': unavailable})
    try:
        material = build_current(max(0, max_bytes - reserve))
    except ValueError as exc:
        if str(exc) != 'evidence_budget_smaller_than_metadata':
            raise
        # The current evidence has priority. Absence of a comparison never
        # grants permission to infer changes or to increase the input budget.
        return build_current(max_bytes)
    if encoded_size(material) > max_bytes - reserve:
        # Frozen legacy RSS inputs deliberately reach the existing wire-size
        # rejection unchanged. A comparison must not alter that spent identity
        # or turn an oversized legacy request into a new truncation policy.
        return build_current(max_bytes)
    material['revision_comparison'] = unavailable
    if comparison['status'] == 'available':
        proposed = copy.deepcopy(material)
        proposed['revision_comparison'] = public(comparison)
        proposed['source_text']['revision'] = comparison['_text']
        current_bytes = len(str(proposed['source_text'].get('body') or proposed['source_text'].get('excerpt') or '').encode())
        if encoded_size(proposed) <= max_bytes and current_bytes + len(comparison['_text'].encode()) <= text_budget:
            material = proposed
    assert encoded_size(material) <= max_bytes
    return material


def locate(point, material):
    comparison = material.get('revision_comparison') or {}
    if comparison.get('status') != 'available':
        raise ValueError('revision_comparison_unavailable')
    body = material['source_text'].get('revision', '')
    quote = point['quote']
    # Repeated text must have one unambiguous original segment and version.
    matches = []
    for row in comparison['selection']:
        source = body[row['start']:row['end']]
        if quote in source:
            start = source.index(quote)
            matches.append({**row, 'start': start, 'end': start + len(quote)})
    if len(matches) != 1:
        raise ValueError('revision_quote_ambiguous_or_crosses_segments')
    row = matches[0]
    document = next(doc for doc in comparison['documents'] if doc['role'] == row['role'])
    return {'segment_id': row['id'], 'source_segment_id': row['source_segment_id'],
            'article_version_id': row['version_id'], 'revision_role': row['role'], 'url': document['url'],
            'start': row['start'], 'end': row['end'], 'comparison_id': comparison['comparison_id']}
