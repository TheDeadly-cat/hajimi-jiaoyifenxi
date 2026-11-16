"""Immutable bindings to existing research evidence, in the news database."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone


def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def prepare(news, store, item_id, *, room_id=None, round_id=None, material_refs=(), version_id=None, as_of=None):
    from .news_market_context import resolve_saved_context, _hash
    from .news_market_replay import historical_item,cutoff_time
    if (version_id is None)!=(as_of is None):
        raise ValueError('market_replay_requires_version_and_as_of')
    historical=version_id is not None
    if historical:
        cutoff=cutoff_time(as_of,news.clock()).isoformat()
        item=historical_item(news,item_id,version_id,cutoff)
    else:
        cutoff=datetime.fromtimestamp(news.clock(),timezone.utc).isoformat()
        item = news.get_item(item_id)
    if not item or not item.get('current_version_id'):
        raise ValueError('versioned_news_item_required')
    context = resolve_saved_context(store,item,room_id=room_id,round_id=round_id,
                                    material_refs=material_refs,as_of=cutoff)
    if not historical and room_id is None:
        context.update(mode='current_news_only', article_version_id=item['current_version_id'])
        context['context_id']=_hash({key:value for key,value in context.items() if key!='context_id'})
    references={(v.get('id') or v.get('material_id'),v['version']) for v in material_refs}
    if historical:
        version=item['event']['current_version']
        context.update(mode='historical_replay',article_version_id=version_id,analysis_allowed=True,
                       news={key:version.get(key) for key in ('version_number','version_seen_at','content_status')},
                       material_refs=[{'material_id':key,'version':version} for key,version in sorted(references)])
        context['boundaries'].append('历史回放只讨论截止时点已知材料，保存行情不是当前行情；准备材料不调用模型。')
        context['context_id']=_hash({key:value for key,value in context.items() if key!='context_id'})
    # Reopening or preparing the same evidence at a later wall-clock time must
    # not create a new paid identity. Only a different evidence binding does.
    identity = {key:context.get(key) for key in ('schema','room_id','round_id','manifest_sha256')}
    identity['material_refs'] = [{'id':key,'version':version} for key,version in sorted(references)]
    if historical:
        identity.update(mode='historical_replay',article_version_id=version_id,as_of=context['as_of'])
    evidence_identity=[item['event']['event_id'],item['evidence_package_id']]
    context_id = hashlib.sha256(_json([evidence_identity,identity]).encode()).hexdigest()
    equivalent=news.equivalent_reading_versions(item_id,item['current_version_id']) if not historical and hasattr(news,'equivalent_reading_versions') else []
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


_HISTORY_CANDIDATES = """
WITH candidates AS (
    SELECT c.*, v.id AS matched_version_id, v.version_number AS matched_version_number,
           v.item_id AS matched_item_id,
           ROW_NUMBER() OVER (PARTITION BY c.id ORDER BY v.version_number DESC, v.id DESC) AS match_rank
    FROM news_market_contexts c
    JOIN news_article_versions original
      ON original.id=c.version_id AND original.item_id=c.item_id
     AND original.id=json_extract(c.item_snapshot,'$.current_version_id')
     AND original.event_id=json_extract(c.item_snapshot,'$.event.event_id')
     AND original.evidence_package_id=json_extract(c.item_snapshot,'$.evidence_package_id')
    JOIN news_article_versions v
      ON v.item_id=:item_id
     AND ((c.item_id=:item_id AND v.id=c.version_id)
          OR (c.item_id!=:item_id
              AND COALESCE(json_extract(c.context,'$.mode'),'current')!='historical_replay'
              AND v.event_id=original.event_id
              AND v.evidence_package_id=original.evidence_package_id))
    WHERE (:version_id IS NULL OR v.id=:version_id)
      AND (:context_id IS NULL OR c.id=:context_id)
)
"""


def _decode_saved(row):
    if row is None:
        return None
    value = {key: row[key] for key in ('id', 'item_id', 'version_id', 'created', 'context', 'item_snapshot')}
    return {**value, 'context': json.loads(value['context']), 'item': json.loads(value['item_snapshot'])}


def _matched_context(db, item_id, context_id, version_id=None):
    """Prove the requested article version shares the original saved scope.

    The immutable origin version must validate the saved snapshot, then the
    requesting article must have the same event and evidence package. Merely
    sharing a title, a present-day link, or an item id cannot establish this.
    Historical replay remains owned by its original article.
    """
    return db.execute(_HISTORY_CANDIDATES + 'SELECT * FROM candidates WHERE match_rank=1',
                      {'item_id': item_id, 'context_id': context_id, 'version_id': version_id}).fetchone()


def get(news, item_id, context_id=None, *, version_id=None, include_historical_equivalents=False):
    with news._db() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='news_market_contexts'").fetchone():
            return None
        if context_id:
            row = _matched_context(db, item_id, context_id, version_id)
            # Workers also call get(). Only explicit read endpoints may reopen
            # a shared ordinary scope after the requesting article has changed;
            # queued work must still be tied to its currently accepted evidence.
            if row and row['item_id'] != item_id and not include_historical_equivalents:
                current = news.get_item(item_id)
                snapshot = json.loads(row['item_snapshot'])
                if (not current or snapshot.get('evidence_package_id') != current.get('evidence_package_id')
                        or snapshot.get('event', {}).get('event_id') != current.get('event', {}).get('event_id')):
                    return None
        else:
            item = news.get_item(item_id)
            if not item:
                return None
            equivalent=news.equivalent_reading_versions(item_id,item['current_version_id']) if hasattr(news,'equivalent_reading_versions') else []
            ids=[v['id'] for v in equivalent]
            clause=' OR (item_id=? AND version_id IN ('+','.join('?' for _ in ids)+'))' if ids else ''
            args=(item.get('evidence_package_id'),item_id,*ids) if ids else (item.get('evidence_package_id'),)
            row = db.execute("SELECT * FROM news_market_contexts WHERE (json_extract(item_snapshot,'$.evidence_package_id')=?"+clause+") AND COALESCE(json_extract(context,'$.mode'),'current')!='historical_replay' ORDER BY created DESC,id DESC LIMIT 1",args).fetchone()
    return _decode_saved(row)


def belongs_to_current_evidence(news,saved,item_id):
    current=news.get_item(item_id)
    if not current:
        return False
    if saved['item'].get('evidence_package_id')==current.get('evidence_package_id'):
        return True
    if saved['item_id']!=item_id or not hasattr(news,'equivalent_reading_versions'):
        return False
    return any(version['id']==saved['version_id'] for version in news.equivalent_reading_versions(item_id,current['current_version_id']))


def belongs_to_replay(news,saved,item_id):
    from .news_event_store import valid_historical_market_binding
    if saved['item_id']!=item_id or saved['context'].get('mode')!='historical_replay':return False
    with news._db() as db:
        return valid_historical_market_binding(db,saved['id'],item_id,saved['version_id'],news.clock())


def public_context(saved):
    context=saved['context']
    return {**context,'evidence_context_id':context['context_id'],'context_id':saved['id']}


def _saved_record(saved, current, equivalent_ids, matched):
    """Describe a saved scope without rewriting its immutable context or result.

    Freshness here concerns the news evidence only. The saved context's market
    and expectation timestamps remain authoritative even when it still matches.
    """
    context = saved['context']
    snapshot = saved['item']
    matches = bool(current and (
        (snapshot.get('evidence_package_id') == current.get('evidence_package_id')
         and snapshot.get('event', {}).get('event_id') == current.get('event', {}).get('event_id'))
        or (saved['item_id'] == current['id'] and saved['version_id'] in equivalent_ids)
    ))
    return {
        'context_id': saved['id'],
        'article_version_id': matched['matched_version_id'],
        'article_version_number': matched['matched_version_number'],
        'source_item_id': saved['item_id'],
        'source_article_version_id': saved['version_id'],
        'is_shared': saved['item_id'] != matched['matched_item_id'],
        'mode': context.get('mode') or 'current',
        'created_at': datetime.fromtimestamp(saved['created'], timezone.utc).isoformat(),
        'as_of': context.get('as_of'),
        'room_id': context.get('room_id'),
        'round_id': context.get('round_id'),
        'evidence_matches_current': matches,
        'requires_review': not matches or bool((current or {}).get('event', {}).get('requires_review')),
        'current_article_version_id': (current or {}).get('current_version_id'),
        'source_url': snapshot.get('url'),
        'title': snapshot.get('title'),
    }


def _current_record_scope(news, item_id):
    current = news.get_item(item_id)
    equivalent = (news.equivalent_reading_versions(item_id, current['current_version_id'])
                  if current and hasattr(news, 'equivalent_reading_versions') else [])
    return current, {version['id'] for version in equivalent}


def saved_record(news, saved, item_id, version_id=None):
    current, equivalent_ids = _current_record_scope(news, item_id)
    with news._db() as db:
        matched = _matched_context(db, item_id, saved['id'], version_id)
    return _saved_record(saved, current, equivalent_ids, matched) if matched is not None else None


def history_list(news, item_id, version_id=None, limit=50, offset=0):
    """List saved scopes for this item and proven equivalent article versions.

    Historical replay is a separate analysis mode, not a replacement for an old
    ordinary result. Reading this list never creates a context or queues work.
    """
    current, equivalent_ids = _current_record_scope(news, item_id)
    with news._db() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='news_market_contexts'").fetchone():
            return {'contexts': [], 'total': 0, 'limit': limit, 'offset': offset}
        args = {'item_id': item_id, 'context_id': None, 'version_id': version_id,
                'limit': limit, 'offset': offset}
        total = db.execute(_HISTORY_CANDIDATES + 'SELECT COUNT(*) FROM candidates WHERE match_rank=1', args).fetchone()[0]
        rows = db.execute(_HISTORY_CANDIDATES + 'SELECT * FROM candidates WHERE match_rank=1'
                          + ' ORDER BY created DESC,id DESC LIMIT :limit OFFSET :offset', args).fetchall()
    contexts = []
    for row in rows:
        contexts.append(_saved_record(_decode_saved(row), current, equivalent_ids, row))
    return {'contexts': contexts, 'total': total, 'limit': limit, 'offset': offset}


def replay_list(news,item_id,version_id=None,limit=50,offset=0):
    with news._db() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='news_market_contexts'").fetchone():
            return {'contexts':[],'total':0,'limit':limit,'offset':offset}
        clause=" WHERE item_id=? AND json_extract(context,'$.mode')='historical_replay'"
        args=[item_id]
        if version_id is not None:clause+=' AND version_id=?';args.append(version_id)
        total=db.execute('SELECT COUNT(*) FROM news_market_contexts'+clause,args).fetchone()[0]
        rows=db.execute('SELECT id,version_id,created,context FROM news_market_contexts'+clause+' ORDER BY created DESC,id DESC LIMIT ? OFFSET ?',(*args,limit,offset)).fetchall()
    contexts=[]
    for row in rows:
        value=json.loads(row['context'])
        contexts.append({'context_id':row['id'],'article_version_id':row['version_id'],'as_of':value['as_of'],
                         'created_at':datetime.fromtimestamp(row['created'],timezone.utc).isoformat(),
                         'room_id':value.get('room_id'),'round_id':value.get('round_id')})
    return {'contexts':contexts,'total':total,'limit':limit,'offset':offset}


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
