"""Strict structured market facts inside the existing versioned room materials."""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlsplit

from .ir_source_catalog import IR_FEEDS


def _https_url(value, field):
    if not isinstance(value, str) or not 1 <= len(value) <= 2000 or any(ord(char) <= 32 for char in value) or '\\' in value:
        raise ValueError('market_material_' + field + '_invalid')
    try:
        parsed = urlsplit(value)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443):
            raise ValueError
    except ValueError:
        raise ValueError('market_material_' + field + '_invalid') from None
    return value


def clean_market_context_metadata(value):
    if not isinstance(value, dict):
        raise ValueError('market_material_metadata_invalid')
    required = {'kind','symbol','metric','period','unit','currency','comparison_basis','value','known_at','quote'}
    actual = value.get('kind') == 'reported_actual'
    if actual:
        required |= {'source_type','event_url'}
    if set(value) != required:
        raise ValueError('market_material_metadata_fields_invalid')
    if value['kind'] not in {'consensus','old_guidance','reported_actual'}:
        raise ValueError('market_material_kind_invalid')
    if value['symbol'] not in IR_FEEDS:
        raise ValueError('market_material_symbol_invalid')
    clean = {'kind':value['kind'],'symbol':value['symbol']}
    for field, limit in (('metric',80),('period',80),('unit',60),('currency',8),('comparison_basis',100),('quote',2000)):
        raw = value[field]
        if not isinstance(raw,str) or not raw.strip() or len(raw) > limit or any(ord(c)<32 and (field!='quote' or c not in '\n\r\t') for c in raw):
            raise ValueError('market_material_' + field + '_invalid')
        clean[field] = raw.strip()
    clean['currency'] = clean['currency'].upper()
    if re.fullmatch(r'[A-Z]{3}|NONE',clean['currency']) is None:
        raise ValueError('market_material_currency_invalid')
    number = value['value']
    try:
        numeric_ok = type(number) in (int,float) and math.isfinite(number) and abs(number) <= 10**18
    except OverflowError:
        numeric_ok = False
    if not numeric_ok:
        raise ValueError('market_material_value_invalid')
    clean['value'] = number
    known = value['known_at']
    if not isinstance(known,str) or len(known)>80:
        raise ValueError('market_material_known_at_invalid')
    try:
        parsed = datetime.fromisoformat(known.replace('Z','+00:00'))
        if parsed.tzinfo is None:
            raise ValueError
        clean['known_at'] = parsed.astimezone(timezone.utc).isoformat().replace('+00:00','Z')
    except (ValueError,OverflowError):
        raise ValueError('market_material_known_at_requires_timezone') from None
    if actual:
        if value['source_type'] not in {'company_ir','regulatory_filing'}:
            raise ValueError('market_material_actual_source_type_invalid')
        clean['source_type'] = value['source_type']
        clean['event_url'] = _https_url(value['event_url'],'event_url')
    return clean


def validate_market_material_content(metadata, content, source_url):
    value = metadata.get('market_context')
    if value is None:
        return
    _https_url(source_url,'source_url')
    if value['quote'] not in content:
        raise ValueError('market_material_quote_not_in_content')
    quoted = {Decimal(token.replace(',','')) for token in re.findall(r'-?\d+(?:,\d{3})*(?:\.\d+)?',value['quote'])}
    if Decimal(str(value['value'])) not in quoted:
        raise ValueError('market_material_value_not_in_quote')
    if value['kind']=='reported_actual':
        from .news_sources import allowed_url, source_registry
        if not any(value['symbol'] in source['symbols'] and allowed_url(source_url,source['id']) for source in source_registry()):
            raise ValueError('market_material_actual_source_not_official')


def prepare_market_material_payload(*, kind, symbol, metric, period, unit, currency, comparison_basis,
        value, known_at, quote, source_url, event_url=None, source_type=None, title=None, content=None):
    """Construct ordinary Store.add_material payload; never accepts saved times."""
    structured = dict(kind=kind,symbol=symbol,metric=metric,period=period,unit=unit,currency=currency,
                      comparison_basis=comparison_basis,value=value,known_at=known_at,quote=quote)
    if kind=='reported_actual':
        structured.update(event_url=event_url,source_type=source_type)
    structured = clean_market_context_metadata(structured)
    metadata = {'market_context':structured,'symbols':[symbol],
                'source_type':source_type or ('analyst_research' if kind=='consensus' else 'company_ir'),
                'event_type':'guidance' if kind=='old_guidance' else 'earnings',
                'extraction_method':'manual_structured_market_context'}
    text = quote if content is None else content
    if not isinstance(text,str) or not 1<=len(text.strip())<=50000:
        raise ValueError('market_material_content_invalid')
    validate_market_material_content(metadata,text,source_url)
    label = {'consensus':'市场预期','old_guidance':'旧指引','reported_actual':'已公布实际值'}[kind]
    return {'title':str(title or f'{symbol} {period} {metric} {label}')[:120], 'kind':'note',
            'source_url':source_url,'content':text,'metadata':metadata}
