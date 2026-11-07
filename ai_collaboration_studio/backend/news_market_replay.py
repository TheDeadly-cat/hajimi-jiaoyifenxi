"""Historical news/material selection. Local saved data only; no providers."""
import copy
from datetime import datetime, timezone

from .news_market_context import _time, _iso, _milliseconds


def cutoff_time(value, now):
    cutoff=_time(value)
    if cutoff is None:
        raise ValueError('market_as_of_requires_timezone')
    if cutoff.timestamp()>now:
        raise ValueError('market_as_of_in_future')
    return cutoff


def historical_item(news,item_id,version_id,as_of):
    cutoff=cutoff_time(as_of,news.clock())
    detail=news.event_detail(item_id,as_of=_iso(cutoff))
    if not detail:
        raise ValueError('news_version_not_known_at_as_of')
    version=detail['current_version']
    if version['id']!=version_id:
        raise ValueError('news_version_not_current_at_as_of')
    item={key:copy.deepcopy(version.get(key)) for key in (
        'title','url','excerpt','published_at','source_updated_at','version_seen_at','evidence',
        'evidence_package_id','symbols','source_id','discovery_kind','discovery_evidence','reference_period','automatic_reading_eligible')}
    item.update(id=item_id,current_version_id=version_id,discovered_at=version['first_seen_at'],
                updated_at=version['version_seen_at'],source_name=version.get('source_name') or version['source_id'],
                published_time_source=(version.get('time_provenance') or {}).get('published_at'),
                event={key:copy.deepcopy(value) for key,value in detail.items() if key!='versions'})
    return item


def round_options(store,room_id,as_of,now,limit=50,offset=0):
    cutoff=cutoff_time(as_of,now)
    if store is None or not callable(getattr(store,'news_market_round_page',None)):
        raise ValueError('market_round_history_unavailable')
    page=store.news_market_round_page(room_id,int(cutoff.timestamp()*1000),limit=limit,offset=offset)
    if page is None:
        raise ValueError('market_room_not_found')
    choices=[]
    for summary in page['rounds']:
        checkpoint=store.get_round_checkpoint(room_id,summary['id'])
        if not isinstance(checkpoint,dict) or checkpoint.get('protocol_integrity_ok') is not True:
            continue
        known=_milliseconds(checkpoint.get('updated_at'))
        if (checkpoint.get('room_id')!=room_id or checkpoint.get('round_id')!=summary['id']
                or known is None or known>cutoff):
            continue
        state=checkpoint.get('state') or {};manifest=state.get('round_evidence_manifest')
        if not isinstance(manifest,dict):continue
        snapshot=state.get('market_snapshot')
        if snapshot is not None:
            captured=_time(snapshot.get('captured_at')) if isinstance(snapshot,dict) else None
            if captured is None or captured>cutoff or captured>known:
                continue
        try:
            store.validate_round_evidence_manifest(room_id,manifest,shared_context=state.get('shared_context') or '',market_snapshot=state.get('market_snapshot'))
        except ValueError:
            continue
        materials=[]
        for ref in manifest.get('materials',[]):
            if not isinstance(ref,dict) or not ref.get('id') or type(ref.get('version')) is not int:continue
            material=store.get_material_version(room_id,ref['id'],ref['version'])
            recorded=_milliseconds(material.get('changed_at')) if isinstance(material,dict) else None
            if (recorded is not None and recorded<=cutoff and recorded<=known
                    and store._material_snapshot_sha256(material)==ref.get('snapshot_sha256')):
                materials.append({'id':ref['id'],'version':ref['version'],'title':str(material.get('title') or '未命名资料')[:120]})
        choices.append({'id':summary['id'],'created_at':summary['created_at'],'known_at':_iso(known),'materials':materials})
    end=offset+len(page['rounds'])
    return {'rounds':choices,'scope':'saved_rounds_as_of','as_of':_iso(cutoff),'total':page['total'],
            'total_basis':'time_eligible_checkpoint_candidates','limit':limit,'offset':offset,
            'next_offset':end if end<page['total'] else None,'has_more':end<page['total'],
            'skipped_unverified':len(page['rounds'])-len(choices),
            'message':'只列出截止时点前已保存且可核对的轮次；不完整记录略过，没有合格轮次时可仅冻结新闻。'}
