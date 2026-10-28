"""Immutable bindings to existing research evidence, in the news database."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone


def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def prepare(news, store, item_id, *, room_id=None, round_id=None, material_refs=()):
    from .news_market_context import resolve_saved_context
    item = news.get_item(item_id)
    if not item or not item.get('current_version_id'):
        raise ValueError('versioned_news_item_required')
    context = resolve_saved_context(store,item,room_id=room_id,round_id=round_id,
                                    material_refs=material_refs,as_of=datetime.fromtimestamp(news.clock(),timezone.utc).isoformat())
    # Reopening or preparing the same evidence at a later wall-clock time must
    # not create a new paid identity. Only a different evidence binding does.
    identity = {key:context.get(key) for key in ('schema','room_id','round_id','manifest_sha256')}
    references={(v.get('material_id') or v.get('id'),v['version']) for v in material_refs}
    identity['material_refs'] = [{'id':key,'version':version} for key,version in sorted(references)]
    evidence_identity=[item['event']['event_id'],item['evidence_package_id']]
    context_id = hashlib.sha256(_json([evidence_identity,identity]).encode()).hexdigest()
    equivalent=news.equivalent_reading_versions(item_id,item['current_version_id']) if hasattr(news,'equivalent_reading_versions') else []
    compatible_ids={context_id}
    for version in equivalent:
        compatible_ids.add(hashlib.sha256(_json([[version['event_id'],version['evidence_package_id']],identity]).encode()).hexdigest())
    with news._db() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS news_market_contexts(
            id TEXT PRIMARY KEY,item_id TEXT NOT NULL,version_id TEXT NOT NULL,created REAL NOT NULL,
            context TEXT NOT NULL,item_snapshot TEXT NOT NULL)''')
        marks=','.join('?' for _ in compatible_ids)
        existing=db.execute('SELECT id,context FROM news_market_contexts WHERE id IN ('+marks+') ORDER BY created,id LIMIT 1',tuple(compatible_ids)).fetchone()
        if existing:
            value=json.loads(existing['context'])
            return {**value,'evidence_context_id':value['context_id'],'context_id':existing['id']}
        db.execute('INSERT OR IGNORE INTO news_market_contexts VALUES(?,?,?,?,?,?)',
                   (context_id,item_id,item['current_version_id'],news.clock(),_json(context),_json(item)))
        stored=db.execute('SELECT context FROM news_market_contexts WHERE id=?',(context_id,)).fetchone()
    value=json.loads(stored['context'])
    return {**value,'evidence_context_id':value['context_id'],'context_id':context_id}


def get(news, item_id, context_id=None):
    with news._db() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='news_market_contexts'").fetchone():
            return None
        if context_id:
            current=news.get_item(item_id)
            row = db.execute('SELECT * FROM news_market_contexts WHERE id=?',(context_id,)).fetchone()
            if row and row['item_id'] != item_id and (not current or json.loads(row['item_snapshot']).get('evidence_package_id') != current.get('evidence_package_id')):
                return None
        else:
            item = news.get_item(item_id)
            if not item:
                return None
            equivalent=news.equivalent_reading_versions(item_id,item['current_version_id']) if hasattr(news,'equivalent_reading_versions') else []
            ids=[v['id'] for v in equivalent]
            clause=' OR (item_id=? AND version_id IN ('+','.join('?' for _ in ids)+'))' if ids else ''
            args=(item.get('evidence_package_id'),item_id,*ids) if ids else (item.get('evidence_package_id'),)
            row = db.execute("SELECT * FROM news_market_contexts WHERE json_extract(item_snapshot,'$.evidence_package_id')=?"+clause+" ORDER BY created DESC,id DESC LIMIT 1",args).fetchone()
    return {**dict(row),'context':json.loads(row['context']),'item':json.loads(row['item_snapshot'])} if row else None


def belongs_to_current_evidence(news,saved,item_id):
    current=news.get_item(item_id)
    if not current:
        return False
    if saved['item'].get('evidence_package_id')==current.get('evidence_package_id'):
        return True
    if saved['item_id']!=item_id or not hasattr(news,'equivalent_reading_versions'):
        return False
    return any(version['id']==saved['version_id'] for version in news.equivalent_reading_versions(item_id,current['current_version_id']))


def fingerprint(context_id):
    return hashlib.sha256(('news_market_impact_v1:'+context_id).encode()).hexdigest()


def task(news, saved):
    context_id=saved['id']
    task_id=fingerprint(context_id)
    event_id=saved['item']['event']['event_id']
    with news._db() as db:
        db.execute('''INSERT OR IGNORE INTO news_analysis_tasks
            (id,item_id,event_id,version_id,evidence_package_id,task_type,trigger,version_seen_at,priority,status,updated_at)
            VALUES(?,?,?,?,?,'market_impact','explicit_market_request',?,0,'pending',?)''',
            (task_id,saved['item_id'],event_id,saved['version_id'],context_id,news.clock(),news.clock()))
    return task_id
