"""Pure, conservative context groups. Never infer a financial table's dependencies.

Without verified cross-table/footnote links, a document containing tables is one
atomic group. Smaller budgets omit it; they cannot turn its rows into paragraphs.
"""
from __future__ import annotations

import copy
import hashlib
import json

POLICY = 'verified_dependencies_or_whole_document_v2'
CELL_FIELDS = ['text', 'colspan', 'rowspan', 'tag']


def encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False).encode('utf-8'))


def _structure_valid(evidence, rows):
    tables = [row for row in rows if row.get('kind') == 'table_row']
    if not tables or evidence.get('truncated') or evidence.get('content_status', evidence.get('status')) != 'full_text':
        return False
    if evidence.get('body_text') != '\n'.join(row.get('text', '') for row in rows):
        return False  # A shortened segment cache cannot prove full document context.
    cached = evidence.get('tables')
    if cached and cached != tables:
        return False
    completed, current, number = set(), None, 0
    for row in tables:
        table = row.get('table_id')
        if not isinstance(table, str) or not table or row.get('structure_truncated') or not isinstance(row.get('cells'), list):
            return False
        if table != current:
            if table in completed:
                return False
            completed.add(table); current, number = table, 0
        number += 1
        if row.get('row_index') != number:
            return False
        for cell in row['cells']:
            if (not isinstance(cell, dict) or not isinstance(cell.get('text'), str) or cell.get('tag') not in {'td', 'th'}
                    or any(type(cell.get(key)) is not int or cell[key] < 1 for key in ('colspan', 'rowspan'))):
                return False
        if row.get('text') != ' | '.join(cell['text'] for cell in row['cells']):
            return False  # The text and geometry must describe the same values.
    return True


def context_groups(item, *, include_attachments=True):
    evidence = item.get('evidence', item)
    sources = [('article', evidence, evidence.get('url') or item.get('url', ''))]
    if include_attachments:
        for index, attachment in enumerate(evidence.get('attachments', [])):
            if attachment.get('read') is True:
                sources.append((f'attachment-{index+1}', attachment.get('evidence') or {}, attachment.get('url', '')))
    groups = []
    for source_rank, (source, value, url) in enumerate(sources):
        original = value.get('segments') or []
        has_tables = bool(value.get('tables')) or any(row.get('kind') == 'table_row' for row in original)
        rows = original or ([] if has_tables else [{'id': 'body', 'text': value.get('body_text', ''), 'kind': 'paragraph'}])
        rows = [row for row in rows if row.get('text') or row.get('kind') == 'table_row']
        identity = [source, url, value.get('raw_content_hash') or value.get('content_hash')]
        document = source + ':' + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:16]
        valid_ids = all(isinstance(row.get('id'), str) and row['id'] for row in rows) and len({row['id'] for row in rows}) == len(rows)
        valid = valid_ids and _structure_valid(value, rows) if has_tables else valid_ids
        from .news_verified_table_groups import resolve
        profile = resolve(value, url) if has_tables and valid else None
        if profile:
            known_ids = {row['id'] for row in rows}
            if not all(spec['target_ids'] and set(spec['member_ids']) == set(spec['target_ids'] + spec['dependency_ids'])
                       and set(spec['member_ids']).issubset(known_ids) for spec in profile['groups']):
                profile = None  # A missing dependency cannot turn into a smaller group.
        specs = []
        if profile:
            used = set()
            for spec in profile['groups']:
                members = set(spec['member_ids']); used.update(members)
                specs.append(([row for row in rows if row['id'] in members], spec, None))
            specs.append(([row for row in rows if row['id'] not in used], {'name': 'unverified-remainder'}, 'dependencies_unverified'))
        batches = [rows] if has_tables else [[row] for row in rows]
        if has_tables and not rows:
            batches = [[]]  # Retain the reason; never fall back to flattened body text.
        if not profile:
            specs = [(batch, {}, None if valid else 'table_context_unverified' if has_tables else 'segment_identity_unverified') for batch in batches]
        order = {row['id']: index for index, row in enumerate(rows)} if valid_ids else {}
        for index, (batch, spec, reason) in enumerate(specs):
            group_id = document + (':' + spec['name'] if profile else ':financial-context' if has_tables else f':paragraph-{index+1}')
            selected = []
            for row in batch if valid and not reason else []:
                selected.append({**copy.deepcopy(row), 'id': row['id'] if source == 'article' else source + ':' + row['id'],
                                 'source_segment_id': row['id'], 'document_id': document, 'group_id': group_id, 'url': url,
                                 '_order': (source_rank, order[row['id']]), '_compact': bool(profile)})
            prefix = '' if source == 'article' else source + ':'
            groups.append({'id': group_id, 'document_id': document, 'kind': 'table_context' if has_tables else 'paragraph',
                           'url': url, 'segments': selected, 'source_segment_count': len(batch),
                           'source_segment_ids': [prefix + row['id'] if valid_ids else document + f':unknown-{index}-{i}' for i, row in enumerate(batch)],
                           'profile': {k: profile[k] for k in ('id', 'basis', 'currency_symbol', 'currency_iso', 'scope_limit')} if profile else None,
                           'scope': spec.get('scope'), 'targets': [prefix + key for key in spec.get('target_ids', [])],
                           'dependencies': [prefix + key for key in spec.get('dependency_ids', [])], 'reason': reason})
    return groups


def selected_rows(selected):
    """Shared headers appear once; every admitted group still lists all dependencies."""
    rows = {}
    for group in selected:
        for row in group['segments']:
            rows.setdefault(row['id'], row)
    packed = []
    for row in sorted(rows.values(), key=lambda value: value['_order']):
        if row['_compact']:
            value = {key: copy.deepcopy(row[key]) for key in ('id', 'text', 'kind', 'source_segment_id', 'document_id', 'table_id', 'row_index') if key in row}
            if 'cells' in row:
                value['cells'] = [[cell[key] for key in CELL_FIELDS] for cell in row['cells']]
                value['cell_encoding'] = 'text_colspan_rowspan_tag_v1'
        else:
            value = {key: copy.deepcopy(v) for key, v in row.items() if key not in {'_order', '_compact'}}
        packed.append(value)
    return packed


def decoded_cells(row):
    if row.get('cell_encoding') == 'text_colspan_rowspan_tag_v1':
        return [dict(zip(CELL_FIELDS, cell)) for cell in row.get('cells', [])]
    return row.get('cells', [])


def source_url(row, package):
    return row.get('url') or next((group['url'] for group in package['groups'] if group['document_id'] == row.get('document_id')), '')


def summary(groups, selected, reasons, detail_limit=16):
    selected_ids = {group['id'] for group in selected}
    omitted = [group for group in groups if group['id'] not in selected_ids]
    reason_counts = {}
    for group in omitted:
        reason = group['reason'] or reasons.get(group['id'], 'budget')
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    all_segments = {key for group in groups for key in group['source_segment_ids']}
    kept_segments = {row['id'] for group in selected for row in group['segments']}
    profiles = {group['profile']['id']: group['profile'] for group in selected if group['profile']}
    return {'selection_policy': POLICY,
            'groups': [{'id': group['id'], 'document_id': group['document_id'], 'kind': group['kind'], 'url': group['url'],
                        'required_segment_ids': [row['id'] for row in group['segments']],
                        **({'profile_id': group['profile']['id'], 'scope': group['scope'], 'targets': group['targets'],
                            'dependencies': group['dependencies']} if group['profile'] else {})} for group in selected],
            'verified_profiles': list(profiles.values()),
            'compact_cell_fields': CELL_FIELDS if profiles else [],
            'omitted_groups': [{'id': group['id'], 'kind': group['kind'], 'reason': group['reason'] or reasons.get(group['id'], 'budget'),
                                'segment_count': group['source_segment_count']} for group in omitted[:detail_limit]],
            'omitted_group_count': len(omitted), 'omitted_group_reasons': reason_counts,
            'omitted_segments': len(all_segments - kept_segments),
            'has_table_context': any(group['kind'] == 'table_context' for group in groups),
            'omitted_table_groups': sum(group['kind'] == 'table_context' for group in omitted)}


def select_groups(groups, render, max_bytes, *, fits=None):
    """Budget the final serialized output, never an estimate of text alone."""
    maximum = max(0, int(max_bytes))
    reasons, selected = {}, []
    detail_limit = 16
    result = render(selected, reasons, detail_limit)
    for limit in (4, 0):
        if encoded_size(result) <= maximum:
            break
        detail_limit = limit
        result = render(selected, reasons, detail_limit)
    if encoded_size(result) > maximum:
        raise ValueError('evidence_budget_smaller_than_metadata')
    for group in groups:
        if group['reason']:
            continue
        proposal = render(selected + [group], reasons, detail_limit)
        # Spend bytes on evidence before verbose omission diagnostics. Counts
        # and reasons remain present even when individual omitted IDs are hidden.
        for limit in (4, 0):
            if encoded_size(proposal) <= maximum or detail_limit <= limit:
                continue
            proposal = render(selected + [group], reasons, limit)
            if encoded_size(proposal) <= maximum:
                detail_limit = limit
                break
        if encoded_size(proposal) <= maximum and (fits is None or fits(proposal)):
            selected.append(group)
        else:
            reasons[group['id']] = 'budget'
    result = render(selected, reasons, detail_limit)
    if encoded_size(result) > maximum:
        raise ValueError('evidence_budget_smaller_than_metadata')
    return result
