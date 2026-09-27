import unittest
from backend.news_review_quality_scorecard import score_quality,RUBRIC


class QualityScorecardTests(unittest.TestCase):
    def case(self,approved=False):
        return {'id':'Q01','reference_draft':{'human_reviewer':'synthetic-reviewer' if approved else None,
            'human_approved_at':'synthetic-timestamp' if approved else None}}

    def rating(self,**updates):
        return {'id':'Q01','reviewer':'synthetic-reviewer','reviewed_at':'synthetic-timestamp',
            'scores':RUBRIC.copy(),'critical_failures':[]}|updates

    def test_unapproved_reference_never_yields_semantic_score(self):
        self.assertIsNone(score_quality([self.case()],[self.rating()])['mean_score'])
        self.assertIsNone(score_quality([self.case(True)],[])['quality_gate_passed'])

    def test_critical_error_fails_even_with_full_points(self):
        result=score_quality([self.case(True)],[self.rating(critical_failures=['wrong_unit'])])
        self.assertEqual(result['mean_score'],10)
        self.assertFalse(result['quality_gate_passed'])

    def test_complete_human_scores_do_not_grant_release(self):
        result=score_quality([self.case(True)],[self.rating()])
        self.assertTrue(result['quality_gate_passed'])
        self.assertFalse(result['release_approved'])
        self.assertFalse(result['natural_event_acceptance'])

    def test_duplicate_or_invented_case_and_invalid_score_rejected(self):
        for ratings in ([self.rating(),self.rating()],[self.rating(id='unknown')],[self.rating(scores=RUBRIC|{'importance':3})]):
            with self.assertRaises(ValueError): score_quality([self.case(True)],ratings)
