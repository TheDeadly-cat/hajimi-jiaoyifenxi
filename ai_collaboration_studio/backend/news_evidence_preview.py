"""Read-only evidence preview for UI and research drafts; never an analysis run."""
import copy
import hashlib
import re

from .news_evidence_reading import build_input
from .news_evidence_groups import decoded_cells, source_url


def evidence_preview(item, *, checked_at=None):
    evidence = item.get('evidence') or {}
    if not evidence.get('body_text') and not evidence.get('tables'):
        return None
    version = item.get('current_version_id')
    content_hash = evidence.get('content_hash')
    result = {'schema': 'news_evidence_preview_v1', 'status': 'unavailable',
              'version_id': version, 'content_hash': content_hash, 'preview_only': True,
              'model_called': False, 'segments': [], 'material': None}
    if not isinstance(version, str) or not version:
        return {**result, 'reason': 'version_required'}
    if not isinstance(content_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', content_hash):
        return {**result, 'reason': 'content_hash_missing'}
    body = evidence.get('body_text')
    if not isinstance(body, str) or hashlib.sha256(body.encode('utf-8')).hexdigest() != content_hash:
        return {**result, 'reason': 'content_hash_mismatch'}
    if evidence.get('content_status', evidence.get('status')) not in {'full_text', 'partial_text'}:
        return {**result, 'reason': 'source_not_readable'}
    documents = [evidence] + [attachment.get('evidence') or {} for attachment in evidence.get('attachments', [])
                              if attachment.get('read') is True]
    for document in documents:
        segments = document.get('segments')
        if segments and (not isinstance(segments, list)
                         or any(not isinstance(row, dict) or not isinstance(row.get('text'), str) for row in segments)
                         or '\n'.join(row['text'] for row in segments) != document.get('body_text')):
            return {**result, 'reason': 'source_segment_mismatch'}
    try:
        material = build_input(item, checked_at=checked_at)
        package = material['evidence_package']
        selected_body = material['source_text']['body']
        cursor, segments = 0, []
        for selected in package['selection']:
            length = selected['end'] - selected['start']
            text = selected_body[cursor:cursor + length]
            if len(text) != length:
                raise ValueError('preview_segment_mismatch')
            row = {**copy.deepcopy(selected), 'text': text, 'url': source_url(selected, package)}
            if 'cells' in row:
                row['cells'] = decoded_cells(row)
                row.pop('cell_encoding', None)
            segments.append(row)
            cursor += length + 1
        if segments and cursor - 1 != len(selected_body):
            raise ValueError('preview_body_mismatch')
        return {**result, 'status': 'ready' if segments else 'no_selected_evidence',
                'reason': None if segments else 'no_complete_groups_within_budget',
                'package_hash': package['package_hash'], 'segments': segments, 'material': material}
    except (KeyError, TypeError, ValueError, OverflowError):
        return {**result, 'reason': 'preview_material_unavailable'}
