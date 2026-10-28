import copy
import json
import unittest
from backend.news_sec_history import advance_history
from backend.news_feed_transport import FeedResponse


class SecHistoryTests(unittest.TestCase):
    def setUp(self):
        self.cik='0000723125'
        self.name='CIK'+self.cik+'-submissions-001.json'
        self.primary={'filings':{'files':[{'name':self.name,'filingCount':601,'filingFrom':'2020-01-01','filingTo':'2025-01-01'}]}}
        self.columns={'accessionNumber':[f'0000723125-25-{i:06d}' for i in range(601)],
                      'form':['8-K']*601,'filingDate':['2025-01-01']*601,
                      'primaryDocument':['report.htm']*601,'primaryDocDescription':['Old filing']*601}
        self.calls=[]
    def get(self,url,headers):
        self.calls.append(url)
        return FeedResponse(200,json.dumps(self.columns).encode())
    def test_pages_all_archive_rows_and_persisted_cursor_resumes(self):
        first,state=advance_history(self.cik,self.primary,None,self.get,1800000000)
        self.assertEqual(len(first),500)
        self.assertTrue(all(row['discovery_kind']=='historical_backfill' for row in first))
        second,state2=advance_history(self.cik,None,json.loads(json.dumps(state)),self.get,1800000010)
        self.assertEqual(len(second),101)
        self.assertEqual(state['files'][self.name]['offset'],500)
        self.assertEqual(state2['pending_files'],0)
        self.assertFalse(state2['continuous_coverage_proven'])
        self.assertEqual(len(self.calls),2)
        third,_=advance_history(self.cik,None,state2,self.get,1800000020)
        self.assertEqual(third,[])
        self.assertEqual(len(self.calls),2)
    def test_failure_retains_prior_cursor_and_new_body_restarts_overlap(self):
        _,state=advance_history(self.cik,self.primary,None,self.get,1800000000)
        original=copy.deepcopy(state)
        def failing(*args):
            raise TimeoutError('fixture_timeout')
        with self.assertRaises(TimeoutError):
            advance_history(self.cik,None,state,failing,1800000010)
        self.assertEqual(state,original)
        self.columns['primaryDocDescription'][0]='Corrected old filing'
        rows,new=advance_history(self.cik,None,state,self.get,1800000010)
        self.assertEqual(rows[0]['description'],'Corrected old filing')
        self.assertEqual(new['files'][self.name]['offset'],500)
    def test_history_index_cannot_inject_another_url_or_cik(self):
        for name in ('../../secrets','https://example.invalid/a.json','CIK0000000000-submissions-001.json'):
            with self.subTest(name=name),self.assertRaises(ValueError):
                advance_history(self.cik,{'filings':{'files':[{'name':name}]}},None,self.get,1800000000)
        self.assertEqual(self.calls,[])


if __name__=='__main__':
    unittest.main()
