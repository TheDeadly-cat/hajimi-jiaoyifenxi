"""Append-only article evidence and durable work in the existing news database.

This module never fetches a URL or invokes an analysis provider. A queued task is
work awaiting the existing authorization/budget gate, not permission to spend.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TERMINAL = frozenset({"completed", "needs_review", "failed", "unknown", "skipped"})
CONTENT_STATES = frozenset({"headline_only", "excerpt_only", "full_text", "partial_text", "fetch_failed", "access_restricted", "retracted"})


def timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.timestamp() if parsed.tzinfo else None
        except (ValueError, OverflowError):
            pass
    return None


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z") if value is not None else None


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def normalized_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def canonical_url(value):
    """Remove only recognized tracking parameters; preserve semantic query data."""
    try:
        parts = urlsplit(str(value or ""))
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            return ""
        query = [(key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True)
                 if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid", "mc_cid", "mc_eid"}]
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", urlencode(query), ""))
    except ValueError:
        return ""


def initialize(db, now):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS news_events(id TEXT PRIMARY KEY, origin_key TEXT NOT NULL UNIQUE, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS news_event_articles(
            item_id TEXT PRIMARY KEY REFERENCES items(id), event_id TEXT NOT NULL REFERENCES news_events(id),
            origin_url TEXT NOT NULL, relation TEXT NOT NULL, linkage_basis TEXT NOT NULL, linked_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS news_article_versions(
            id TEXT PRIMARY KEY, item_id TEXT NOT NULL REFERENCES items(id), event_id TEXT NOT NULL REFERENCES news_events(id),
            version_number INTEGER NOT NULL, content_hash TEXT NOT NULL, evidence_package_id TEXT NOT NULL,
            previous_version_id TEXT, version_seen REAL NOT NULL, snapshot TEXT NOT NULL,
            UNIQUE(item_id,version_number));
        CREATE INDEX IF NOT EXISTS news_versions_item ON news_article_versions(item_id,version_number DESC);
        CREATE TABLE IF NOT EXISTS news_analysis_tasks(
            id TEXT PRIMARY KEY, item_id TEXT NOT NULL REFERENCES items(id), event_id TEXT NOT NULL,
            version_id TEXT NOT NULL REFERENCES news_article_versions(id), evidence_package_id TEXT NOT NULL,
            task_type TEXT NOT NULL, trigger TEXT NOT NULL, version_seen_at REAL NOT NULL, priority INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending', updated_at REAL NOT NULL, result_ref TEXT, reason TEXT);
        CREATE INDEX IF NOT EXISTS news_tasks_pending ON news_analysis_tasks(status,priority DESC,version_seen_at,id);
        CREATE TABLE IF NOT EXISTS news_article_tasks(
            id TEXT PRIMARY KEY,item_id TEXT NOT NULL REFERENCES items(id),version_id TEXT NOT NULL REFERENCES news_article_versions(id),
            source_id TEXT NOT NULL REFERENCES sources(id),status TEXT NOT NULL DEFAULT 'pending',priority INTEGER NOT NULL DEFAULT 0,
            created REAL NOT NULL,updated REAL NOT NULL,next_attempt REAL NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT);
        CREATE INDEX IF NOT EXISTS news_article_tasks_pending ON news_article_tasks(status,priority DESC,created,id);
        CREATE TABLE IF NOT EXISTS news_article_task_attempts(
            seq INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT NOT NULL REFERENCES news_article_tasks(id),
            started REAL NOT NULL,finished REAL,status TEXT NOT NULL,error_code TEXT,version_id TEXT,result_version_id TEXT);
        INSERT OR IGNORE INTO settings(key,value) VALUES('article_reading_enabled','false');
        INSERT OR IGNORE INTO settings(key,value) VALUES('article_reading_attachments','false');
        CREATE TABLE IF NOT EXISTS news_article_fetches(
            seq INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL REFERENCES items(id), fetched REAL NOT NULL, receipt TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS news_article_observations(
            seq INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL REFERENCES items(id), observed_at REAL NOT NULL,
            record_hash TEXT NOT NULL, record TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS news_source_coverage(
            source_id TEXT PRIMARY KEY REFERENCES sources(id), last_success REAL, latest_published REAL,
            coverage_status TEXT NOT NULL DEFAULT 'unknown', cursor TEXT, gap_reason TEXT);
    """)
    attempt_columns={row[1] for row in db.execute('PRAGMA table_info(news_article_task_attempts)')}
    for column in ('version_id','result_version_id'):
        if column not in attempt_columns:
            db.execute(f'ALTER TABLE news_article_task_attempts ADD COLUMN {column} TEXT')
    # Only the current legacy row survives in old databases. Do not invent lost
    # versions, source-updated times or a historical evidence-availability date.
    for row in db.execute("SELECT * FROM items WHERE id NOT IN (SELECT item_id FROM news_article_versions)").fetchall():
        record_version(db, dict(row), now, migrated=True, enqueue=False)


def _current(db, item_id, as_of=None):
    clause, args = (" AND version_seen<=?", (item_id, timestamp(as_of))) if as_of is not None else ("", (item_id,))
    row = db.execute("SELECT * FROM news_article_versions WHERE item_id=?" + clause + " ORDER BY version_number DESC LIMIT 1", args).fetchone()
    return dict(row) if row else None


def _version(row):
    value = json.loads(row["snapshot"])
    value.update(id=row["id"], event_id=row["event_id"], version_number=row["version_number"],
                 evidence_package_id=row["evidence_package_id"], previous_version_id=row["previous_version_id"])
    return value


def _association(db, item, evidence, now):
    existing = db.execute("SELECT * FROM news_event_articles WHERE item_id=?", (item["id"],)).fetchone()
    origin = canonical_url(evidence.get("origin_url")) if evidence.get("origin_confirmed") is True else ""
    url = canonical_url(item.get("url"))
    if existing and not origin:
        return dict(existing)
    key = origin or url or "article:" + item["id"]
    event_id = digest(["news_event_v1", key])
    relation = "reprint" if origin and origin != url else "original"
    basis = "confirmed_original_source" if origin else "same_canonical_url" if url else "source_article_identity"
    db.execute("INSERT OR IGNORE INTO news_events(id,origin_key,created) VALUES(?,?,?)", (event_id, key, now))
    db.execute("""INSERT INTO news_event_articles(item_id,event_id,origin_url,relation,linkage_basis,linked_at) VALUES(?,?,?,?,?,?)
        ON CONFLICT(item_id) DO UPDATE SET event_id=excluded.event_id,origin_url=excluded.origin_url,
        relation=excluded.relation,linkage_basis=excluded.linkage_basis,linked_at=excluded.linked_at""",
               (item["id"], event_id, origin or url, relation, basis, now))
    if existing and existing['event_id'] != event_id:
        # An explicit reprint attribution retires unsent duplicate work. Saved or
        # unconfirmed results remain untouched and stay tied to their old input.
        db.execute("UPDATE news_analysis_tasks SET status='skipped',updated_at=?,reason='confirmed_reprint_linked' WHERE item_id=? AND status='pending' AND event_id!=?",
                   (now,item['id'],event_id))
    return dict(db.execute("SELECT * FROM news_event_articles WHERE item_id=?", (item["id"],)).fetchone())


def observe(db, item_id, entry, now):
    record = {key: entry.get(key) for key in ('identity','title','url','excerpt','published','source_updated_at','published_time_source','symbols','filing_date')}
    record_hash = digest(record)
    previous = db.execute('SELECT record_hash FROM news_article_observations WHERE item_id=? ORDER BY seq DESC LIMIT 1',(item_id,)).fetchone()
    if previous is None or previous['record_hash'] != record_hash:
        db.execute('INSERT INTO news_article_observations(item_id,observed_at,record_hash,record) VALUES(?,?,?,?)',
                   (item_id,now,record_hash,_json(record)))


def record_version(db, item, now, *, evidence=None, metadata=None, migrated=False, enqueue=True):
    """Snapshot material before the caller overwrites its legacy current row."""
    item, metadata = dict(item), metadata or {}
    previous = _current(db, item["id"])
    old = _version(previous) if previous else None
    evidence = dict(evidence or {})
    if not evidence:
        # A new RSS revision does not prove a previously read body is current.
        evidence = {"content_status": "excerpt_only" if item.get("excerpt") else "headline_only",
                    "body_text": "", "segments": [], "attachments": [], "extractor_version": "rss-v1"}
    state = evidence.get("content_status", evidence.get("status"))
    if state not in CONTENT_STATES:
        raise ValueError("invalid_article_content_status")
    if old and old['content_status']=='retracted':
        # Fetch success is not a reviewed reversal of a publisher withdrawal.
        # Keep the notice bound to all later observations until an explicit,
        # separately reviewed reversal workflow exists.
        evidence['latest_read_content_status']=state
        evidence['withdrawal_evidence']=old['evidence'].get('withdrawal_evidence')
        evidence['retraction_basis']=old['evidence'].get('retraction_basis')
        state='retracted'
        evidence['status']='retracted'
    evidence["content_status"] = state
    evidence["canonical_url"] = canonical_url(evidence.get("canonical_url") or item.get("url"))
    association = _association(db, item, evidence, now)
    material = {"title": normalized_text(item.get("title")), "excerpt": normalized_text(item.get("excerpt")),
                "url": canonical_url(item.get("url")), "body_text": normalized_text(evidence.get("body_text")),
                "content_status": state,
                "attachments": [{"url": canonical_url(a.get("url")), "status": a.get("status", a.get("content_status")),
                                 "text": normalized_text(a.get("body_text", a.get("text", a.get("evidence", {}).get("body_text"))))}
                                for a in evidence.get("attachments", []) if isinstance(a, dict)]}
    content_hash = digest(material)
    if previous and previous["content_hash"] == content_hash and previous["event_id"] == association["event_id"]:
        return _version(previous), False
    number = previous["version_number"] + 1 if previous else 1
    version_id = digest([item["id"], number, content_hash])
    package_id = digest(["news_evidence_v1", association["event_id"], material])
    changes = []
    if old:
        for field in ("title", "excerpt", "content_status"):
            value = state if field == "content_status" else item.get(field, "")
            if normalized_text(old.get(field)) != normalized_text(value):
                changes.append({"field": field, "before": old.get(field), "after": value})
        if normalized_text(old.get("evidence", {}).get("body_text")) != material["body_text"]:
            changes.append({"field": "body_text", "before": old.get("evidence", {}).get("body_text", ""), "after": evidence.get("body_text", "")})
    source_updated = timestamp(metadata.get("source_updated_at", evidence.get("source_updated_at")))
    publication=timestamp(item.get('published'))
    historical_material=(metadata.get('discovery_kind')=='historical_backfill'
                         or (not old and publication is not None and publication<now-86400)
                         or (bool(evidence.get('body_text')) and bool(old) and not old.get('evidence',{}).get('body_text')
                             and (old.get('discovery_kind')=='historical_backfill' or old.get('migrated_legacy')
                                  or publication is not None and publication<now-86400)))
    snapshot = {"item_id": item["id"], "title": item.get("title", ""), "url": item.get("url", ""),
                "excerpt": item.get("excerpt", ""), "source_id": item["source_id"],
                "published_at": iso(timestamp(evidence.get("published_at")) or timestamp(item.get("published"))), "source_updated_at": iso(source_updated),
                "first_seen_at": iso(timestamp(item.get("discovered", now))), "version_seen_at": iso(now),
                "fetched_at": iso(timestamp(evidence.get("fetched_at")) or now), "content_status": state,
                "evidence": evidence, "changes": changes, "migrated_legacy": migrated,
                "discovery_kind":"historical_backfill" if historical_material else "live_discovery",
                "time_provenance": {"published_at": metadata.get("published_time_source", "legacy_unqualified" if migrated else "source_publication"),
                                    "source_updated_at": "source_modified" if source_updated is not None else "unknown",
                                    "version_seen_at": "legacy_snapshot_recorded_now" if migrated else "local_observation"},
                "origin_url": association["origin_url"], "relation": association["relation"],
                "linkage_basis": association["linkage_basis"], "symbols": evidence.get("symbols", metadata.get("symbols", []))}
    db.execute("""INSERT INTO news_article_versions(id,item_id,event_id,version_number,content_hash,evidence_package_id,
        previous_version_id,version_seen,snapshot) VALUES(?,?,?,?,?,?,?,?,?)""",
               (version_id,item["id"],association["event_id"],number,content_hash,package_id,previous["id"] if previous else None,now,_json(snapshot)))
    if previous:
        db.execute("UPDATE news_analysis_tasks SET status='skipped',updated_at=?,reason='superseded_version' WHERE item_id=? AND status='pending' AND version_id!=?",
                   (now,item['id'],version_id))
        db.execute("UPDATE news_article_tasks SET status='skipped',updated=?,last_error='superseded_version' WHERE item_id=? AND status='pending' AND version_id!=?",
                   (now,item['id'],version_id))
    analysis = json.loads(item.get('analysis') or '{}') if isinstance(item.get('analysis'),str) else item.get('analysis') or {}
    important = analysis.get('importance') == 'attention' or analysis.get('category') == 'company' or bool(metadata.get('symbols'))
    if enqueue and important and state in {'headline_only','excerpt_only'} and canonical_url(item.get('url')):
        body_task = digest(['news_article_task_v1',version_id])
        db.execute("""INSERT OR IGNORE INTO news_article_tasks(id,item_id,version_id,source_id,priority,created,updated)
            VALUES(?,?,?,?,?,?,?)""",(body_task,item['id'],version_id,item['source_id'],10 if old else 0,now,now))
    # Reprints preserve their own versions/attribution, but do not constitute new
    # independent evidence or silently authorize another explanation of one origin.
    prior_event_task = db.execute("SELECT 1 FROM news_analysis_tasks WHERE event_id=? LIMIT 1", (association["event_id"],)).fetchone()
    if enqueue and state not in {"headline_only", "fetch_failed", "access_restricted"} and not (association["relation"] == "reprint" and prior_event_task):
        trigger = "retracted" if state == "retracted" else "body_evidence" if evidence.get("body_text") else "content_revision" if old else "new_article"
        if historical_material and state != 'retracted':
            trigger='historical_backfill'
        task_id = digest(["news_task_v1", association["event_id"], package_id, "fact_reading"])
        db.execute("""INSERT OR IGNORE INTO news_analysis_tasks(id,item_id,event_id,version_id,evidence_package_id,task_type,
            trigger,version_seen_at,priority,status,updated_at,reason) VALUES(?,?,?,?,?,'fact_reading',?,?,?,?,?,?)""",
                   (task_id,item["id"],association["event_id"],version_id,package_id,trigger,now,10 if old else 0,
                    'needs_review' if state == 'retracted' else 'pending',now,'source_retracted' if state == 'retracted' else None))
    return _version(dict(db.execute("SELECT * FROM news_article_versions WHERE id=?", (version_id,)).fetchone())), True


def save_evidence(db, item, evidence, now):
    evidence = dict(evidence)
    state = evidence.get("content_status", evidence.get("status"))
    if state not in CONTENT_STATES:
        raise ValueError("invalid_article_content_status")
    # Every attempt is retained; a transient failure never replaces saved text.
    receipt = {key: evidence.get(key) for key in ("content_status", "status", "http_status", "error_code", "error", "fetched_at", "canonical_url")}
    db.execute("INSERT INTO news_article_fetches(item_id,fetched,receipt) VALUES(?,?,?)", (item["id"],now,_json(receipt)))
    if state in {"fetch_failed", "access_restricted"}:
        return None, False
    return record_version(db, item, now, evidence=evidence)


def versions(db, item_id, as_of=None):
    clause, args = (" AND version_seen<=?", (item_id, timestamp(as_of))) if as_of is not None else ("", (item_id,))
    return [_version(dict(row)) for row in db.execute("SELECT * FROM news_article_versions WHERE item_id=?" + clause + " ORDER BY version_number DESC", args)]


def queue_summary(db, event_id=None):
    where, args = (" WHERE event_id=?", (event_id,)) if event_id else ("", ())
    value = {state: 0 for state in ("pending", "running", "completed", "needs_review", "failed", "unknown", "skipped")}
    value.update(dict(db.execute("SELECT status,COUNT(*) FROM news_analysis_tasks" + where + " GROUP BY status", args)))
    value["total"] = sum(value.values())
    return value


def article_queue_summary(db):
    value = {status:0 for status in ('pending','running','completed','failed','skipped')}
    value.update(dict(db.execute('SELECT status,COUNT(*) FROM news_article_tasks GROUP BY status')))
    value['total']=sum(value.values())
    return value


def _distribution(values):
    values = sorted(values)
    def percentile(q):
        position = (len(values)-1)*q
        lower = math.floor(position)
        upper = math.ceil(position)
        return round(values[lower]+(values[upper]-values[lower])*(position-lower),3)
    return {"sample_count":len(values),"p50_seconds":percentile(.5) if values else None,
            "p95_seconds":percentile(.95) if values else None,
            "unavailable_reason":None if values else "no_eligible_time_observations",
            "quantile_method":"linear_interpolation"}


def quality_metrics(db, now):
    """Observed counters only. No inferred full coverage or fabricated labels."""
    start = now-86400
    source_counts = []
    for source in db.execute('SELECT id,name FROM sources ORDER BY id'):
        outcomes = dict(db.execute('SELECT outcome,COUNT(*) FROM poll_runs WHERE source_id=? AND started>=? AND started<=? GROUP BY outcome',
                                  (source['id'],start,now)))
        success = outcomes.get('success',0)+outcomes.get('not_modified',0)
        failed = outcomes.get('failed',0)
        denominator = success+failed
        source_counts.append({'source_id':source['id'],'name':source['name'],'attempts':sum(outcomes.values()),
                              'successful':success,'failed':failed,'not_modified':outcomes.get('not_modified',0),
                              'unknown_or_in_progress':sum(value for key,value in outcomes.items() if key not in {'success','not_modified','failed'}),
                              'success_rate':success/denominator if denominator else None,
                              'success_rate_denominator':denominator,
                              'denominator_definition':'observed_success_or_failure_only',
                              'unavailable_reason':None if denominator else 'no_confirmed_poll_outcomes'})
    body_counts = {state:0 for state in sorted(CONTENT_STATES)}
    for row in db.execute('SELECT receipt FROM news_article_fetches WHERE fetched>=? AND fetched<=?',(start,now)):
        receipt = json.loads(row['receipt'])
        state = receipt.get('content_status') or receipt.get('status')
        body_counts[state if state in CONTENT_STATES else 'fetch_failed'] += 1
    body_total = sum(body_counts.values())
    discovery, evidence_ready, analysis = [], [], []
    excluded = {'legacy':0,'historical_backfill':0,'missing_source_publication':0,'invalid_time_order':0}
    for row in db.execute('SELECT * FROM news_article_versions WHERE version_number=1 AND version_seen>=? AND version_seen<=?',(start,now)):
        snap = json.loads(row['snapshot'])
        published, first_seen = timestamp(snap.get('published_at')),timestamp(snap.get('first_seen_at'))
        if snap.get('migrated_legacy'):
            excluded['legacy']+=1
        elif snap.get('discovery_kind')=='historical_backfill':
            excluded['historical_backfill']+=1
        elif published is None:
            excluded['missing_source_publication']+=1
        elif first_seen is None or not published<=first_seen<=now:
            excluded['invalid_time_order']+=1
        else:
            discovery.append(first_seen-published)
    ready_by_article = {}
    for row in db.execute('SELECT * FROM news_article_versions WHERE version_seen>=? AND version_seen<=? ORDER BY version_seen,version_number',(start,now)):
        snap = json.loads(row['snapshot'])
        published, first_seen = timestamp(snap.get('published_at')),timestamp(snap.get('first_seen_at'))
        if (snap.get('migrated_legacy') or snap.get('discovery_kind')=='historical_backfill' or published is None or first_seen is None or not published<=first_seen<=row['version_seen']<=now):
            continue
        if snap['content_status'] in {'full_text','partial_text'} and row['item_id'] not in ready_by_article:
            ready_by_article[row['item_id']]=row['version_seen']-first_seen
    evidence_ready.extend(ready_by_article.values())
    for row in db.execute("""SELECT t.version_seen_at,t.updated_at,v.snapshot FROM news_analysis_tasks t JOIN news_article_versions v ON v.id=t.version_id
        WHERE t.status IN ('completed','needs_review','failed') AND t.result_ref IS NOT NULL AND t.updated_at>=? AND t.updated_at<=?""",(start,now)):
        snap = json.loads(row['snapshot'])
        published, seen = timestamp(snap.get('published_at')),timestamp(snap.get('first_seen_at'))
        if (not snap.get('migrated_legacy') and snap.get('discovery_kind')!='historical_backfill' and published is not None and seen is not None
                and published<=seen<=row['version_seen_at']<=row['updated_at']<=now):
            analysis.append(row['updated_at']-row['version_seen_at'])
    unavailable = {
        'duplicate_alert_rate':'no_observed_notification_outcomes_and_deduplicated_denominator',
        'wrong_merge_rate':'no_independently_judged_event_associations',
        'evidence_support_rate':'no_independently_judged_claim_denominator',
        'major_misstatement_count':'no_independently_judged_model_outputs',
        'real_cost_per_effective_event':'supplier_bill_and_judged_effective_events_unavailable',
        'relative_coverage_rate':'no_frozen_external_comparison_event_set',
    }
    return {'window_start':iso(start),'window_end':iso(now),'window_seconds':86400,
            'source_checks':source_counts,'body_reads':{'attempts':body_total,'statuses':body_counts,
                'full_text_rate':body_counts['full_text']/body_total if body_total else None,
                'readable_text_rate':(body_counts['full_text']+body_counts['partial_text'])/body_total if body_total else None,
                'unavailable_reason':None if body_total else 'no_body_read_attempts'},
            'latency':{'publication_to_discovery':_distribution(discovery),'discovery_to_body_ready':_distribution(evidence_ready),
                       'evidence_to_analysis_result':_distribution(analysis),'excluded_discovery_samples':excluded,
                       'scope':'live_discovery_article_versions_with_valid_source_publication_and_nonnegative_local_time_order'},
            'coverage':{'continuous_coverage':False,'status':'unknown','reason':'successful_discovery_or_archive_pages_do_not_prove_complete_continuous_coverage',
                        'known_monitor_gap_count':db.execute('SELECT COUNT(*) FROM monitor_gaps WHERE ended>=? AND started<=?',(start,now)).fetchone()[0],
                        'known_source_gap_count':db.execute("SELECT COUNT(*) FROM news_source_coverage WHERE gap_reason='check_gap_without_verified_backfill'").fetchone()[0]},
            'analysis_queue':queue_summary(db),
            'judged_metrics':{key:{'value':None,'unavailable_reason':reason} for key,reason in unavailable.items()},
            'not_supplier_billing':True,'not_independent_quality_acceptance':True}


def detail(db, item_id, as_of=None):
    history = versions(db, item_id, as_of)
    if not history:
        return None
    current = history[0]
    # Historical membership comes from immutable snapshots, not current links.
    cutoff=timestamp(as_of) if as_of is not None else float('inf')
    rows = db.execute("""SELECT v.*,s.name AS source_name FROM news_article_versions v JOIN items i ON i.id=v.item_id
        JOIN sources s ON s.id=i.source_id WHERE v.event_id=? AND v.version_seen<=?
        AND NOT EXISTS(SELECT 1 FROM news_article_versions newer WHERE newer.item_id=v.item_id AND newer.version_number>v.version_number AND newer.version_seen<=?)
        ORDER BY v.version_seen DESC,v.version_number DESC,v.id DESC""",(current['event_id'],cutoff,cutoff)).fetchall()
    members = {}
    withdrawn = False
    for row in rows:
        if row["item_id"] not in members:
            version = _version(dict(row))
            withdrawn = withdrawn or version['content_status'] == 'retracted'
            members[row["item_id"]] = {key: version.get(key) for key in ("item_id", "source_id", "url", "origin_url", "relation", "linkage_basis")}
            members[row["item_id"]]["source_name"] = row["source_name"]
    last_fetch = db.execute("SELECT receipt FROM news_article_fetches WHERE item_id=? AND fetched<=? ORDER BY seq DESC LIMIT 1",
        (item_id,timestamp(as_of) if as_of is not None else float("inf"))).fetchone()
    observations = [{'observed_at':iso(row['observed_at']),**json.loads(row['record'])} for row in db.execute(
        'SELECT observed_at,record FROM news_article_observations WHERE item_id=? AND observed_at<=? ORDER BY seq DESC',
        (item_id,timestamp(as_of) if as_of is not None else float('inf')))]
    return {"event_id":current["event_id"], "current_version":current, "version_count":len(history), "versions":history,
            "sources":list(members.values()), "independent_evidence_count":len({m["origin_url"] or m["item_id"] for m in members.values()}),
            "requires_review":withdrawn or any(c["field"] in {"excerpt", "body_text"} for c in current["changes"]),
            "retracted":withdrawn, "queue":queue_summary(db,current["event_id"]) if as_of is None else None,
            "last_fetch_attempt":json.loads(last_fetch["receipt"]) if last_fetch else None,
            "observations":observations,
            "historical_as_of": iso(timestamp(as_of)) if as_of is not None else None}


def pending_tasks(db, limit=100, offset=0, after=None):
    limit, offset = max(1,min(500,int(limit))), max(0,int(offset))
    clause, args = (" AND version_seen_at>=?", (timestamp(after),)) if after is not None else ("", ())
    total = db.execute("SELECT COUNT(*) FROM news_analysis_tasks WHERE status='pending'" + clause,args).fetchone()[0]
    rows = db.execute("SELECT * FROM news_analysis_tasks WHERE status='pending'" + clause + " ORDER BY priority DESC,version_seen_at,id LIMIT ? OFFSET ?",(*args,limit,offset)).fetchall()
    return {"tasks":[dict(row) for row in rows],"total":total,"limit":limit,"offset":offset}


def transition(db, task_id, status, now, *, expected_status=None, result_ref=None, reason=None):
    if status not in TERMINAL | {"running"}:
        raise ValueError("invalid_analysis_task_status")
    row = db.execute("SELECT status FROM news_analysis_tasks WHERE id=?", (task_id,)).fetchone()
    if row is None or row["status"] in TERMINAL or (expected_status is not None and row["status"] != expected_status):
        return False
    if status == "running" and row["status"] != "pending":
        return False
    return bool(db.execute("UPDATE news_analysis_tasks SET status=?,updated_at=?,result_ref=?,reason=? WHERE id=? AND status=?",
        (status,now,result_ref,reason,task_id,row["status"])).rowcount)
