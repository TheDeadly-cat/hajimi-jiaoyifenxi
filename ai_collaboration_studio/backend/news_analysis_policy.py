"""Deterministic news focus and admission rules; never fetches or calls a model."""
from __future__ import annotations

import copy
import hashlib
import json
import re

POLICY_VERSION = 'news_focus_policy_v1'
COMPANIES = (
    {'id':'micron','label':'美光','symbol':'US.MU','aliases':['micron','美光'],'industries':['semiconductors','storage']},
    {'id':'sandisk','label':'闪迪','symbol':'US.SNDK','aliases':['sandisk','闪迪'],'industries':['semiconductors','storage']},
    {'id':'western_digital','label':'西部数据','symbol':'US.WDC','aliases':['western digital','西部数据'],'industries':['storage']},
    {'id':'seagate','label':'希捷','symbol':'US.STX','aliases':['seagate','希捷'],'industries':['storage']},
)
INDUSTRIES = (
    {'id':'semiconductors','label':'半导体','terms':['semiconductor','semiconductors','半导体']},
    {'id':'storage','label':'存储','terms':['memory','dram','nand','hard drive','data storage','存储','内存']},
    {'id':'banking','label':'银行','terms':['bank','banks','banking','银行']},
)
MACRO_TOPICS = (
    {'id':'inflation','label':'物价与通胀','terms':['inflation','consumer price','producer price','cpi','pce','通胀','物价']},
    {'id':'employment','label':'就业与工资','terms':['employment','payroll','labor market','unemployment','wages','就业','工资']},
    {'id':'monetary','label':'货币与利率','terms':['fomc','monetary','interest rate','federal funds','policy rate','利率','货币政策']},
    {'id':'banking','label':'银行与监管','terms':['supervision','regulation','enforcement','银行','监管']},
)
DEFAULT_FOCUS = {'all_registered':True,'companies':[],'symbols':[],'industries':[],
                 'macro_topics':[],'automatic_scope':'facts'}


def catalog():
    from .news_sources import source_registry
    sources=source_registry()
    companies=[{**{k:v for k,v in row.items() if k in {'id','label','symbol','industries'}},
                'source_ids':[s['id'] for s in sources if row['symbol'] in s.get('symbols',[])]} for row in COMPANIES]
    industries=[{'id':row['id'],'label':row['label'],'source_ids':sorted({source for company in companies
        if row['id'] in company['industries'] for source in company['source_ids']} | ({'fed'} if row['id']=='banking' else set()))}
        for row in INDUSTRIES]
    macro_sources={'inflation':['fed','bls'],'employment':['bls','fed'],'monetary':['fed'],'banking':['fed']}
    return {'companies':companies,'industries':industries,
            'macro_topics':[{'id':row['id'],'label':row['label'],'source_ids':macro_sources[row['id']]} for row in MACRO_TOPICS],
            'coverage_notice':'只映射已登记来源；启用、联系方式、可访问性和持续覆盖需分别核对。'}


def normalize(value):
    if not isinstance(value,dict) or set(value)-set(DEFAULT_FOCUS):
        raise ValueError('invalid_news_focus')
    result = copy.deepcopy(DEFAULT_FOCUS)
    result.update(value)
    if (type(result['all_registered']) is not bool or not isinstance(result['automatic_scope'],str)
            or result['automatic_scope'] not in {'facts','market'}):
        raise ValueError('invalid_news_focus')
    choices = {'companies':{x['id'] for x in COMPANIES},'industries':{x['id'] for x in INDUSTRIES},
               'macro_topics':{x['id'] for x in MACRO_TOPICS}}
    for key in ('companies','symbols','industries','macro_topics'):
        values=result[key]
        if not isinstance(values,list) or len(values)>30 or any(not isinstance(v,str) for v in values):
            raise ValueError('invalid_news_focus')
        if key=='symbols':
            values=[v.upper() if v.upper().startswith('US.') else 'US.'+v.upper() for v in values]
            if any(not re.fullmatch(r'US\.[A-Z][A-Z0-9.\-]{0,11}',v) for v in values):
                raise ValueError('invalid_news_focus_symbol')
        elif any(v not in choices[key] for v in values):
            raise ValueError('invalid_news_focus_choice')
        result[key]=sorted(set(values))
    if not result['all_registered'] and not any(result[k] for k in ('companies','symbols','industries','macro_topics')):
        raise ValueError('news_focus_selection_required')
    return result


def fingerprint(value):
    return hashlib.sha256(json.dumps(normalize(value),sort_keys=True,separators=(',',':')).encode()).hexdigest()


def load(db):
    row=db.execute("SELECT value FROM settings WHERE key='analysis_focus'").fetchone()
    return normalize(json.loads(row[0])) if row else copy.deepcopy(DEFAULT_FOCUS)


def _matches(text, term):
    return bool(re.search(r'(?<!\w)'+re.escape(term)+r'(?!\w)',text)) if term.isascii() else term in text


def _quarterly_directory_basis(item, version, source):
    """Recognize only the registered parser's retained quarterly provenance.

    A directory label is evidence of the document category, never its original
    headline, publication time, financial figures, or availability of the body.
    """
    from .news_micron_directory import MICRON_DIRECTORY_URL, PARSER_VERSION, _link
    from .news_sources import canonical_url
    provenance=item.get('discovery_evidence') or version.get('discovery_evidence') or {}
    period=item.get('reference_period') or version.get('reference_period')
    if (not source or source.get('id')!='ir_mu' or source.get('kind')!='micron_directory'
            or not isinstance(provenance,dict) or provenance.get('schema')!=PARSER_VERSION
            or provenance.get('directory_url')!=MICRON_DIRECTORY_URL or provenance.get('title_basis')!='directory_label'
            or not isinstance(period,str) or not re.fullmatch(r'FY(?:19|20|21)\d{2}-Q[1-4]',period)
            or provenance.get('reference_period')!=period
            or not re.fullmatch(r'[0-9a-f]{64}',str(provenance.get('directory_sha256') or ''))
            or not re.fullmatch(r'documentCategoryData\[\d+\]\.data\[\d+\]\.documents\[\d+\]',str(provenance.get('json_path') or ''))):
        return False
    link,allowed,_reason=_link(provenance.get('observed_url'),announcement=True)
    return bool(allowed and canonical_url(link)==canonical_url(item.get('url') or version.get('url')))


def classify(item, focus=None, *, check_selected=False, checked_at=None):
    """Project four independent dimensions, with no mutation of saved evidence."""
    from .news_sources import source_definition
    focus=normalize(focus or DEFAULT_FOCUS)
    event=item.get('event') or {}
    version=event.get('current_version') or {}
    previous=event.get('previous_version') or {}
    evidence=item.get('evidence') or version.get('evidence') or {}
    source=source_definition(item.get('source_id',version.get('source_id','')))
    text=' '.join(str(item.get(k) or '') for k in ('title','excerpt')).lower()
    symbols=set(item.get('symbols') or version.get('symbols') or (source or {}).get('symbols') or [])
    named={row['id'] for row in COMPANIES if row['symbol'] in symbols or any(_matches(text,t) for t in row['aliases'])}
    symbols.update(row['symbol'] for row in COMPANIES if row['id'] in named)
    industries={row['id'] for row in INDUSTRIES if any(_matches(text,t) for t in row['terms'])}
    industries.update(industry for row in COMPANIES if row['id'] in named for industry in row['industries'])
    topics={row['id'] for row in MACRO_TOPICS if item.get('category')==row['id'] or any(_matches(text,t) for t in row['terms'])}
    direct=bool(set(focus['symbols']) & symbols or set(focus['companies']) & named or set(focus['macro_topics']) & topics)
    indirect=bool(set(focus['industries']) & industries)
    relevance='direct' if direct or focus['all_registered'] and source else 'indirect' if indirect else 'unrelated'
    quarterly_directory=_quarterly_directory_basis(item,version,source)
    if quarterly_directory:
        kind='earnings'
    elif any(_matches(text,t) for t in ('outlook','guidance','指引','预期调整')):
        kind='guidance'
    elif any(_matches(text,t) for t in ('earnings','financial results','quarterly results','财报','业绩')):
        kind='earnings'
    elif any(_matches(text,t) for t in ('enforcement','regulation','regulatory','10-k','10-q','8-k','监管','处罚')):
        kind='regulatory'
    elif topics:
        kind='macro'
    elif industries:
        kind='industry'
    else:
        kind='commentary'
    changes={change.get('field') for change in version.get('changes',[]) if isinstance(change,dict)}
    historical=(version.get('discovery_kind',item.get('discovery_kind'))=='historical_backfill'
                or version.get('automatic_reading_eligible',item.get('automatic_reading_eligible',True)) is False)
    repeated=version.get('relation')=='reprint'
    attachments_changed=bool(previous and previous.get('evidence',{}).get('attachments')!=evidence.get('attachments'))
    substantive=bool(changes & {'excerpt','body_text','table_structure','content_status'} or attachments_changed)
    novelty=('historical_backfill' if historical else 'duplicate_confirmation' if repeated else
             'material_revision' if version.get('previous_version_id') and substantive else
             'duplicate_confirmation' if version.get('previous_version_id') else 'new_event')
    status=evidence.get('content_status',evidence.get('status')) or ('excerpt_only' if item.get('excerpt') else 'headline_only')
    conflict=bool(version.get('structure_review') or event.get('retracted') or status=='retracted'
                  or evidence.get('conflict') or evidence.get('conflicting_views'))
    refs=(item.get('discovery_evidence') or {}).get('attachment_references') or []
    read_urls={a.get('url') for a in evidence.get('attachments',[]) if a.get('read') is True}
    unread=any(a.get('read') is not True for a in evidence.get('attachments',[])) or any(r.get('url') not in read_urls for r in refs)
    important=item.get('importance')=='attention' or kind in {'earnings','guidance','regulatory','macro'}
    reasons=[]
    if relevance=='unrelated':reasons.append('outside_focus')
    if not important:reasons.append('not_important')
    if novelty not in {'new_event','material_revision'}:reasons.append(novelty)
    if conflict:reasons.append('evidence_requires_review')
    if status in {'headline_only','fetch_failed','access_restricted','retracted'} or not (evidence.get('body_text') or str(item.get('excerpt') or '').strip()):
        reasons.append('evidence_missing')
    selected_complete=None
    if check_selected and status in {'full_text','partial_text'} and evidence.get('body_text'):
        from . import news_evidence_reading as reading
        projected={**item,'evidence':evidence,'current_version_id':item.get('current_version_id') or version.get('id')}
        selected_complete=reading.insufficient_article_result(projected,checked_at=checked_at) is None
        if not selected_complete:reasons.append(reading.NO_SELECTED_EVIDENCE)
    priority=(100 if novelty=='material_revision' else 50 if novelty=='new_event' else 0)+(20 if important else 0)+(10 if relevance=='direct' else 0)
    return {'policy_version':POLICY_VERSION,'relevance':relevance,'novelty':novelty,'event_type':kind,
            'event_type_basis':'registered_quarterly_directory' if quarterly_directory else 'local_text_rules',
            'evidence':{'status':status,'attachments_unread':bool(unread),'time_unknown':not bool(item.get('published_at') or version.get('published_at')),
                        'conflict':conflict,'selected_complete_groups':selected_complete},
            'importance':'important' if important else 'ordinary','automatic_eligible':not reasons,
            'automatic_scope':focus['automatic_scope'],'reasons':reasons,'priority':priority,
            'matched':{'companies':sorted(named),'symbols':sorted(symbols),'industries':sorted(industries),'macro_topics':sorted(topics)}}
