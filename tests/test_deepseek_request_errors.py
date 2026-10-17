import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from local_review.config import Settings
from local_review.deepseek import request_json, JudgmentUnavailable

class DeepSeekRequestErrorTests(unittest.TestCase):
    def test_failures_keep_their_stage(self):
        req=httpx.Request('POST','https://api.deepseek.com/chat/completions')
        cases=[
            ('deepseek_server_error',httpx.Response(503,request=req),None),
            ('deepseek_invalid_response',httpx.Response(200,request=req,text='invalid'),None),
            ('deepseek_invalid_json_output',httpx.Response(200,request=req,json={'choices':[{'finish_reason':'stop','message':{'content':'invalid'}}]}),None),
            ('deepseek_connection_failed',None,httpx.ConnectError('secret',request=req)),
            ('deepseek_timeout',None,httpx.ReadTimeout('secret',request=req)),
        ]
        for expected,response,error in cases:
            with self.subTest(expected=expected),patch('local_review.deepseek.httpx.post',return_value=response,side_effect=error):
                with self.assertRaises(JudgmentUnavailable) as caught:
                    request_json(Settings(Path('/tmp'),'',deepseek_api_key='secret'),'system',{})
                self.assertEqual(str(caught.exception),expected)
                self.assertNotIn('secret',str(caught.exception))
