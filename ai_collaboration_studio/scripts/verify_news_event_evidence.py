"""Create an inspectable, strictly offline product-route example in a NEW folder.

No installed service, real publisher, model or personal database is used. Fixture
announcements are explicitly synthetic; this is not a live-source quality test.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import socket
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.news_platform import NewsPlatform
from backend.news_platform_ai import NewsAnalysis, MODEL
from backend.news_feed_transport import FeedResponse
from backend.news_platform_routes import news_response


def verify(destination):
    destination.mkdir(parents=True,exist_ok=False)
    clock=[time.time()-60]
    model_inputs=[]
    release={'excerpt':'Synthetic revenue was USD 100 million for fiscal 2026, subject to final audit.',
             'body':'Synthetic revenue was USD 100 million for fiscal 2026, subject to final audit.'}
    def feed(*args):
        text=f'''<rss><channel><item><guid>synthetic-announcement</guid><title>SYNTHETIC official-announcement path example</title>
        <link>https://www.federalreserve.gov/newsevents/synthetic-fixture.htm</link><description>{release['excerpt']}</description></item></channel></rss>'''
        return FeedResponse(200,text.encode(),{'ETag':'synthetic'})
    def body(*args):
        text=f'''<html><head><title>Explicitly synthetic announcement</title></head><body><article>
        <p>{release['body']}</p><table><tr><th>Period</th><th>Revenue</th></tr><tr><td>Fiscal 2026</td><td>USD 100 million (synthetic)</td></tr></table>
        <p>This fixture is not actual company or central-bank data.</p><a href="/synthetic-not-read.pdf">Synthetic attachment</a></article></body></html>'''
        return FeedResponse(200,text.encode(),{'content-type':'text/html'})
    def generate(key,text):
        material=json.loads(text)
        model_inputs.append(material)
        source=material['source_text']; field='body' if 'body' in source else 'excerpt'
        quote=source[field].split('\n')[0]
        value={'summary':'合成公告示例：所列收入仍待最终审计。',
               'key_points':[{'claim':'该合成材料中的收入数字附有待最终审计条件。','evidence_field':field,'quote':quote}],
               'uncertainties':['这是离线合成材料，未核实真实市场情况。','未读附件不纳入判断。']}
        return SimpleNamespace(ok=True,model=MODEL,content=json.dumps(value,ensure_ascii=False))
    with patch.object(socket.socket,'connect',side_effect=AssertionError('real_network_forbidden')):
        news=NewsPlatform(destination/'synthetic-news.sqlite3',transport=feed,article_transport=body,clock=lambda:clock[0])
        ai=NewsAnalysis(destination/'synthetic-analysis.sqlite3',news,generate=generate)
        news.ai=ai
        def fetch():
            with news._db() as db:
                source=db.execute("SELECT * FROM sources WHERE id='fed'").fetchone()
            news._fetch(source)
        fetch()
        item_id=news.list_items()['items'][0]['id']
        before=news.get_item(item_id)
        response,status=news_response(news,f'/api/news/items/{item_id}/evidence',method='POST',payload={'include_attachments':False})
        assert status==200,(status,response)
        ai.configure('PUBLIC_SYNTHETIC_FIXTURE_ONLY',False,True)
        task=news.analysis_task_for_item(item_id)
        ai._analyze(item_id,task_id=task['id'])
        full=news.get_item(item_id); reading=ai.result(full)
        assert reading['status']=='completed',reading
        assert reading['evidence'][0]['locator']['segment_id']
        assert full['evidence']['tables']
        assert full['evidence']['attachments'][0]['read'] is False
        reserved=ai.status()['calls_reserved']
        ai._analyze(item_id)
        assert ai.status()['calls_reserved']==reserved
        old_version=full['current_version_id']
        clock[0]=time.time()-1
        release['excerpt']='Corrected synthetic revenue was USD 90 million for fiscal 2026, subject to final audit.'
        fetch()
        current=news.get_item(item_id)
        assert current['current_version_id']!=old_version
        assert current['discovered_at']==before['discovered_at']
        assert current['evidence']['content_status']=='excerpt_only'
        task=news.analysis_task_for_item(item_id)
        ai._analyze(item_id,task_id=task['id'])
        corrected=ai.result(news.get_item(item_id))
        assert corrected['status']=='completed',corrected
        assert len(model_inputs)==2
        historical=news.event_versions(item_id)
        assert any(v['id']==old_version and '100 million' in v['evidence']['body_text'] for v in historical)
        receipt={'schema':'news_event_offline_route_example_v1','status':'passed','synthetic':True,
                 'real_source_reads':0,'real_model_calls':0,'simulated_model_calls':len(model_inputs),
                 'started_background_services':0,'production_access':False,
                 'checks':{'body_and_table_saved':True,'attachment_not_read_label':True,'located_chinese_reading':True,
                           'reopen_no_new_reservation':True,'older_article_revision_queued':True,
                           'old_body_version_preserved':True,'revision_result_bound':True},
                 'first_reading':reading,'corrected_reading':corrected,'versions':historical,
                 'queue':news.overview().get('analysis_queue'),
                 'limitations':['示例是合成公告及模拟模型，不能证明真实来源、模型质量或真人验收。']}
        ai.stop()
    for name,data in [('receipt.json',receipt),('model-inputs.json',model_inputs)]:
        with (destination/name).open('x',encoding='utf-8') as stream:
            json.dump(data,stream,ensure_ascii=False,indent=2)
    return {'status':receipt['status'],'receipt':str(destination/'receipt.json'),
            'sha256':hashlib.sha256((destination/'receipt.json').read_bytes()).hexdigest()}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(verify(args.output),ensure_ascii=False))
