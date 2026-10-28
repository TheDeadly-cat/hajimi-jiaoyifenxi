"""Bounded SEC archive paging; caller commits cursor with the corresponding rows."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone

PAGE_ROWS=500
REVISIT_SECONDS=6*3600


def advance_history(cik, primary, previous, get, now):
    from .market.sec_edgar import SecEdgarAdapter, SEC_DEFAULT_FORMS
    state=copy.deepcopy(previous) if isinstance(previous,dict) and previous.get('cik')==cik else {'cik':cik,'files':{},'schema':'sec_history_cursor_v1'}
    files=state.setdefault('files',{})
    if not isinstance(files,dict):
        raise ValueError('invalid_sec_history_cursor')
    if primary is not None:
        advertised=(primary.get('filings') or {}).get('files') or []
        if not isinstance(advertised,list) or len(advertised)>1000:
            raise ValueError('invalid_sec_history_index')
        for meta in advertised:
            name=meta.get('name') if isinstance(meta,dict) else None
            if not isinstance(name,str) or re.fullmatch('CIK'+cik+r'-submissions-\d{3}\.json',name) is None:
                raise ValueError('invalid_sec_history_file')
            identity={key:meta.get(key) for key in ('name','filingCount','filingFrom','filingTo')}
            signature=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
            old=files.get(name,{})
            if old.get('index_hash')!=signature:
                files[name]={'index_hash':signature,'offset':0,'complete':False,'last_checked':None,'advertised_range':identity}
    candidates=[(name,row) for name,row in files.items() if not row.get('complete') or (row.get('last_checked') or 0)+REVISIT_SECONDS<=now]
    candidates.sort(key=lambda entry:(bool(entry[1].get('complete')),entry[1].get('last_checked') or 0,entry[0]))
    state['continuous_coverage_proven']=False
    if not candidates:
        state['pending_files']=0
        state['completed_files']=sum(bool(row.get('complete')) for row in files.values())
        return [],state
    name,entry=candidates[0]
    # Revalidate persisted cursor filenames too; never trust state as a URL.
    if re.fullmatch('CIK'+cik+r'-submissions-\d{3}\.json',name) is None:
        raise ValueError('invalid_sec_history_file')
    response=get('https://data.sec.gov/submissions/'+name,{})
    if response.status!=200:
        raise ValueError('sec_history_body_required')
    columns=json.loads(response.body)
    if not isinstance(columns,dict) or not isinstance(columns.get('accessionNumber'),list):
        raise ValueError('invalid_sec_history_body')
    content_hash=hashlib.sha256(response.body).hexdigest()
    offset=entry.get('offset',0)
    if type(offset) is not int or offset<0:
        raise ValueError('invalid_sec_history_offset')
    if entry.get('content_hash')!=content_hash or entry.get('complete'):
        offset=0
    total=len(columns['accessionNumber'])
    end=min(total,offset+PAGE_ROWS)
    page={key:value[offset:end] for key,value in columns.items() if isinstance(value,list)}
    rows=SecEdgarAdapter._normalize_recent_filings({'filings':{'recent':page}},cik=cik,forms=SEC_DEFAULT_FORMS,
         limit=PAGE_ROWS,today=datetime.fromtimestamp(now,timezone.utc).date())
    for row in rows:
        row['discovery_kind']='historical_backfill'
    entry.update(offset=end,complete=end>=total,last_checked=now,content_hash=content_hash,total_rows=total)
    state.update(last_history_scan_at=datetime.fromtimestamp(now,timezone.utc).isoformat(),
                 pending_files=sum(not row.get('complete') for row in files.values()),
                 completed_files=sum(bool(row.get('complete')) for row in files.values()),
                 coverage_note='仅确认已列出归档文件的读取；不证明所有申报类型或来源持续完整覆盖。')
    return rows,state
