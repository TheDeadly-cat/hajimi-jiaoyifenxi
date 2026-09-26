"""Unregistered, synthetic-only future event contract. No source/paid authority.

This is intentionally not an adapter in the source registry. Publisher-native
identity must be preserved; URLs/headlines are not universal dedupe keys.
"""
import copy
import hashlib
import json
import re


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def validate_event(value):
    fields={'version','market','publisher','native_id','revision_id','previous_revision_id',
        'published_at_ms','observed_at_ms','content_sha256','status','synthetic'}
    if type(value) is not dict or set(value)!=fields or value['version']!='news_event_contract_draft_v1' or value['synthetic'] is not True:
        raise ValueError('draft_synthetic_contract_only')
    for key in ('market','publisher','native_id','revision_id'):
        if type(value[key]) is not str or not re.fullmatch('[A-Za-z0-9_.:-]{1,128}',value[key]):
            raise ValueError('invalid_native_identity')
    previous=value['previous_revision_id']
    if previous is not None and (type(previous) is not str or not re.fullmatch('[A-Za-z0-9_.:-]{1,128}',previous)):
        raise ValueError('invalid_previous_revision')
    for key in ('published_at_ms','observed_at_ms'):
        if type(value[key]) is not int or value[key]<0:
            raise ValueError('invalid_event_time')
    if value['status'] not in {'published','retracted'}:
        raise ValueError('invalid_event_status')
    content=value['content_sha256']
    if value['status']=='published' and (type(content) is not str or not re.fullmatch('[0-9a-f]{64}',content)):
        raise ValueError('missing_body_identity')
    if value['status']=='retracted' and content is not None:
        raise ValueError('retraction_has_no_new_body')
    return copy.deepcopy(value)


class MockEventAdapter:
    def __init__(self, events):
        self.events=[validate_event(e) for e in events]

    def poll(self):
        return copy.deepcopy(self.events)


class DraftEventHistory:
    def __init__(self):
        self.history={}

    def observe(self, event):
        e=validate_event(event)
        key=digest({k:e[k] for k in ('market','publisher','native_id')})
        rows=self.history.setdefault(key,[])
        immutable={k:v for k,v in e.items() if k!='observed_at_ms'}
        old=next((r for r in rows if r['event']['revision_id']==e['revision_id']),None)
        if old:
            if {k:v for k,v in old['event'].items() if k!='observed_at_ms'}!=immutable:
                raise ValueError('publisher_revision_identity_collision')
            return {'event_key':key,'duplicate':True,'current':copy.deepcopy(rows[-1]),'new_paid_request_authorized':False}
        if e['previous_revision_id']!=(rows[-1]['event']['revision_id'] if rows else None):
            raise ValueError('revision_gap_or_out_of_order')
        if rows and e['observed_at_ms']<rows[-1]['event']['observed_at_ms']:
            raise ValueError('observation_clock_unconfirmed')
        # Link a restored body to its earlier review key, never create paid
        # authority. Strategy/model versions belong in the execution contract.
        review_key=digest({'event':key,'body':e['content_sha256']}) if e['content_sha256'] else None
        row={'event':e,'review_content_key':review_key,'publication_clock_unconfirmed':e['published_at_ms']>e['observed_at_ms']}
        rows.append(row)
        return {'event_key':key,'duplicate':False,'current':copy.deepcopy(row),'new_paid_request_authorized':False}
