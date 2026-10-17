import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from failure_diagnosis import diagnose_failure
from local_review.deepseek import JudgmentUnavailable
from model_judgments import summarize


class FailureDiagnosisTests(unittest.TestCase):
    def run_diagnosis(self, answer=None, error=None):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / 'run.log').write_text('HTTP 403 api_key=private-secret')
            session = SimpleNamespace(model_events=[])
            settings = SimpleNamespace(deepseek_api_key='private-secret', model_api_key='', device_token='')
            with patch('failure_diagnosis.Settings.from_env', return_value=settings), patch(
                'failure_diagnosis.request_json', return_value=answer, side_effect=error
            ) as request:
                result = diagnose_failure(ValueError('属性未加载'), directory, Mock(), session)
            self.assertTrue((directory / 'failure-diagnosis.json').is_file())
            self.assertNotIn('private-secret', str(request.call_args.args[2]))
            self.assertEqual(len(session.model_events), 1)
            self.assertEqual(summarize([], session.model_events)['not_called_reason'], '')
            return result

    def test_success_is_advisory_and_reports_missing_evidence(self):
        result = self.run_diagnosis(dict(observed='HTTP 403', hypothesis='网关拒绝（推测）', missing_evidence='服务器日志', next_step='检查接口'))
        self.assertEqual(result['status'], 'diagnosed')
        self.assertIn('服务器日志', result['reason'])

    def test_timeout_is_reported(self):
        result = self.run_diagnosis(error=JudgmentUnavailable('deepseek_timeout'))
        self.assertIn('超时', result['reason'])

    def test_invalid_response_cannot_become_diagnosis(self):
        result = self.run_diagnosis({'observed': '已修复'})
        self.assertEqual(result['status'], 'unavailable')

    def test_unexpected_diagnostic_failure_keeps_original_failure(self):
        result = self.run_diagnosis(error=RuntimeError('private-secret'))
        self.assertIn('RuntimeError', result['reason'])
        self.assertNotIn('private-secret', result['reason'])
