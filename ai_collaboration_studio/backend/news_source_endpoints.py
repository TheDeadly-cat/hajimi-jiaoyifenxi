"""Endpoint changes retain source identity, pressure, and historical receipts."""
import json
from .news_runtime_evidence import iso


def synchronize(db, definitions, now):
    db.execute('''CREATE TABLE IF NOT EXISTS news_source_endpoint_changes(
        seq INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL,
        previous_url TEXT NOT NULL, current_url TEXT NOT NULL, changed_at REAL NOT NULL,
        previous_state TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS news_source_endpoint_confirmations(
        seq INTEGER PRIMARY KEY AUTOINCREMENT, change_seq INTEGER NOT NULL, confirmed_at REAL NOT NULL)''')
    for definition in definitions:
        row=db.execute('SELECT * FROM sources WHERE id=?',(definition['id'],)).fetchone()
        if row is None:
            continue
        if row['url'] != definition['url']:
            old=dict(row)
            coverage=db.execute('SELECT * FROM news_source_coverage WHERE source_id=?',(row['id'],)).fetchone()
            old['coverage']=dict(coverage) if coverage else None
            db.execute('''INSERT INTO news_source_endpoint_changes(source_id,previous_url,current_url,changed_at,previous_state)
                VALUES(?,?,?,?,?)''',(row['id'],row['url'],definition['url'],now,json.dumps(old,ensure_ascii=False,sort_keys=True)))
            # Old enablement was for a different entry. Keep shared cooldowns,
            # but require the existing source toggle to enable this new URL.
            db.execute('''UPDATE sources SET name=?,url=?,enabled=0,refresh_queued=0,
                etag=NULL,last_modified=NULL,status='never',last_success=NULL,last_attempt=NULL,error=NULL WHERE id=?''',
                (definition['name'],definition['url'],row['id']))
            db.execute("UPDATE news_source_coverage SET cursor=NULL,last_success=NULL,latest_published=NULL,coverage_status='unknown',gap_reason='source_endpoint_changed' WHERE source_id=?",(row['id'],))
        else:
            db.execute('UPDATE sources SET name=? WHERE id=?',(definition['name'],row['id']))


def confirm_enabled(db, source_id, now):
    row=db.execute('SELECT seq FROM news_source_endpoint_changes WHERE source_id=? ORDER BY seq DESC LIMIT 1',(source_id,)).fetchone()
    if row and not db.execute('SELECT 1 FROM news_source_endpoint_confirmations WHERE change_seq=?',(row['seq'],)).fetchone():
        db.execute('INSERT INTO news_source_endpoint_confirmations(change_seq,confirmed_at) VALUES(?,?)',(row['seq'],now))


def latest_change(db, source_id):
    row=db.execute('SELECT * FROM news_source_endpoint_changes WHERE source_id=? ORDER BY seq DESC LIMIT 1',(source_id,)).fetchone()
    if not row:return None
    confirmed=db.execute('SELECT 1 FROM news_source_endpoint_confirmations WHERE change_seq=?',(row['seq'],)).fetchone()
    return {'previous_url':row['previous_url'],'current_url':row['current_url'],'changed_at':iso(row['changed_at']),
            'explicit_reenable_required':not bool(confirmed),'previous_receipt_preserved':True}


def latest_check(db, source_id):
    row=db.execute('SELECT * FROM poll_runs WHERE source_id=? ORDER BY started DESC,rowid DESC LIMIT 1',(source_id,)).fetchone()
    return ({**{key:row[key] for key in ('outcome','parsed_items','new_items','updated_items','http_status')},
             'finished_at':iso(row['finished']),'started_at':iso(row['started'])} if row else None)
