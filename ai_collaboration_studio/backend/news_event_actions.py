"""Explicit human evidence annotations; never infer an event from topic similarity."""
from __future__ import annotations

import copy
import re


def confirm_relation(news,item_id,origin_item_id):
    if item_id==origin_item_id:
        raise ValueError('different_original_required')
    before=news.get_item(item_id)
    origin=news.get_item(origin_item_id)
    if not before or not origin or not origin.get('url') or not origin.get('current_version_id'):
        raise ValueError('saved_original_required')
    evidence=copy.deepcopy(before.get('evidence') or {})
    evidence.update(origin_url=(origin.get('event') or {}).get('current_version',{}).get('origin_url') or origin['url'],
                    origin_confirmed=True,
                    relation_confirmation={'basis':'user_confirmed_same_original','original_item_id':origin_item_id,
                       'original_version_id':origin['current_version_id'],'independently_verified':False})
    updated=news.save_article_evidence(item_id,evidence,relation_annotation=True)
    ai=getattr(news,'ai',None)
    if ai:
        ai.retain_equivalent_reading(before,updated)
    return updated


def confirm_withdrawal(news,item_id,evidence_item_id,quote):
    target=news.get_item(item_id)
    source=news.get_item(evidence_item_id)
    if not target or not source or not isinstance(quote,str) or not 8<=len(quote)<=2000:
        raise ValueError('saved_withdrawal_evidence_required')
    if source['source_id']!=target['source_id']:
        raise ValueError('withdrawal_requires_same_publisher')
    fields=[source.get('excerpt',''),(source.get('evidence') or {}).get('body_text','')]
    if not any(quote in text for text in fields) or not re.search(r'\b(?:withdraw(?:n|s|al)?|retract(?:ed|s|ion)?)\b|撤回|撤销',quote,re.I):
        raise ValueError('withdrawal_quote_not_supported')
    evidence=copy.deepcopy(target.get('evidence') or {})
    evidence.update(content_status='retracted',status='retracted',
       withdrawal_evidence={'item_id':evidence_item_id,'version_id':source['current_version_id'],
                            'url':source['url'],'quote':quote,'basis':'user_confirmed_publisher_notice',
                            'independently_verified':False})
    return news.save_article_evidence(item_id,evidence,withdrawal_annotation=True)
