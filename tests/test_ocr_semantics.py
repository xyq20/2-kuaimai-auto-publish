import dataclasses
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import size_image_recognition as recognition
from attribute_runtime import AttributeRuntime, ReviewBatchRequired
from learning_store import LearningStore
from local_review.config import Settings
from tests.test_size_image_recognition import _wool_clothing_tokens


def unfamiliar_tokens():
    return tuple(dataclasses.replace(token, text="衣身丈") if index == 0 else token
                 for index, token in enumerate(_wool_clothing_tokens()))


class OCRSemanticTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.chart = Path(self.temporary.name) / "size.jpg"
        self.chart.write_bytes(b"chart")
        self.store = LearningStore(Path(self.temporary.name) / "learning.sqlite3")
        self.store.migrate()
        self.addCleanup(self.store.close)
        self.client = Mock()
        self.client.post_event.return_value = {"ok": True}
        self.client.review_learning.return_value = {"status": "review_required"}
        self.client.ocr_judge.return_value = {
            "status": "suggested", "source": "deepseek",
            "suggestions": [{"token_id": 0, "meaning": "length", "confidence": .99}],
        }
        self.runtime = AttributeRuntime(self.store, self.client, "run", "product")

    async def failure(self, session):
        from ocr_semantics import OCRJudgmentRequired
        with session.activate(), patch.object(recognition, "vision_ocr", return_value=unfamiliar_tokens()):
            with self.assertRaises(OCRJudgmentRequired) as caught:
                recognition.recognize_size_names(self.chart, "clothing")
        return caught.exception

    async def test_rule_fast_path_does_not_request_model(self):
        from ocr_semantics import OCRSemanticSession
        session = OCRSemanticSession()
        with session.activate(), patch.object(recognition, "vision_ocr", return_value=_wool_clothing_tokens()):
            self.assertEqual(recognition.recognize_size_names(self.chart, "clothing"), ("M", "L", "XL", "2XL"))
        self.client.ocr_judge.assert_not_called()

    async def test_model_only_changes_label_and_all_size_readers_reuse_it(self):
        from ocr_semantics import OCRSemanticSession
        from ocr_review import resolve_ocr_failure
        session = OCRSemanticSession()
        error = await self.failure(session)
        await resolve_ocr_failure(self.runtime, self.client, session, error, logging.getLogger(__name__))
        with session.activate(), patch.object(recognition, "vision_ocr", return_value=unfamiliar_tokens()):
            self.assertEqual(recognition.recognize_size_names(self.chart, "clothing"), ("M", "L", "XL", "2XL"))
            values = recognition.recognize_size_lengths(self.chart, ("M", "L", "XL", "2XL"), "clothing")
            self.assertEqual([row.length for row in values], [68, 70, 72, 74])
        self.client.ocr_judge.assert_called_once()
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM review_resolutions").fetchone()[0], 0,
                         "模型预测不能伪装成人工审核学习样本")

    async def test_unconfigured_or_timeout_model_enters_review_with_original_text(self):
        from ocr_semantics import OCRSemanticSession
        from ocr_review import resolve_ocr_failure
        from learning_client import RetryableCloudError
        for response in ({"status": "review_required", "reason_code": "deepseek_not_configured"}, RetryableCloudError("cloud status 503")):
            with self.subTest(response=type(response).__name__):
                self.client.ocr_judge.side_effect = response if isinstance(response, Exception) else None
                self.client.ocr_judge.return_value = response
                session = OCRSemanticSession()
                error = await self.failure(session)
                with self.assertRaises(ReviewBatchRequired) as caught:
                    await resolve_ocr_failure(self.runtime, self.client, session, error, logging.getLogger(__name__))
                request = caught.exception.reviews[0].request
                self.assertIn("衣身丈", request.evidence["summary"])
                self.assertEqual(request.platform_id, "base")
                self.assertFalse(request.custom_allowed)

    async def test_fabricated_numeric_token_or_meaning_is_rejected(self):
        from ocr_semantics import OCRSemanticSession
        from ocr_review import resolve_ocr_failure
        for suggestion in ({"token_id": 6, "meaning": "length", "confidence": 1},
                           {"token_id": 999, "meaning": "length", "confidence": 1},
                           {"token_id": 0, "meaning": "length", "confidence": 1, "value": 99},
                           {"token_id": 0, "meaning": "sleeve", "confidence": 1}):
            with self.subTest(suggestion=suggestion):
                self.client.ocr_judge.return_value = {"status": "suggested", "source": "deepseek", "suggestions": [suggestion]}
                session = OCRSemanticSession()
                error = await self.failure(session)
                with self.assertRaises(ReviewBatchRequired):
                    await resolve_ocr_failure(self.runtime, self.client, session, error, logging.getLogger(__name__))

    async def test_human_confirmation_wins_and_is_reused_without_model(self):
        from ocr_semantics import OCRSemanticSession
        from ocr_review import resolve_ocr_failure
        self.client.ocr_judge.return_value = {"status": "review_required"}
        session = OCRSemanticSession()
        error = await self.failure(session)
        with self.assertRaises(ReviewBatchRequired) as caught:
            await resolve_ocr_failure(self.runtime, self.client, session, error, logging.getLogger(__name__))
        review = caught.exception.reviews[0]
        self.store.save_review_resolution(review_id=review.review_id, product_version="product",
                                         platform_id="base", snapshot_version=review.snapshot_version,
                                         final_value_id="length")
        self.client.ocr_judge.reset_mock()
        await resolve_ocr_failure(self.runtime, self.client, session, error, logging.getLogger(__name__))
        self.client.ocr_judge.assert_not_called()
        with session.activate(), patch.object(recognition, "vision_ocr", return_value=unfamiliar_tokens()):
            self.assertEqual(recognition.recognize_size_names(self.chart, "clothing"), ("M", "L", "XL", "2XL"))


class DeepSeekOCRAPITests(unittest.TestCase):
    def test_official_text_api_uses_json_and_bounded_timeout(self):
        from local_review.ocr import judge_ocr_text
        settings = Settings(Path("unused"), "device", deepseek_api_key="private-key")
        payload = {"product_version": "p", "garment_kind": "clothing", "tokens": [dataclasses.asdict(t) for t in unfamiliar_tokens()], "label_token_ids": [0], "reason": "unknown label"}
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"suggestions": [{"token_id": 0, "meaning": "length", "confidence": .99}]})}}]}
        with patch("local_review.ocr.learning_examples", return_value=[{"text": "衣身丈", "meaning": "衣长", "source": "human"}]), patch("local_review.ocr.httpx.post", return_value=response) as post:
            result = judge_ocr_text(settings, payload)
        self.assertEqual(result["status"], "suggested")
        self.assertEqual(post.call_args.args[0], "https://api.deepseek.com/chat/completions")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertNotIn("input_image", json.dumps(body))
        self.assertIn("衣身丈", body["messages"][1]["content"])
        self.assertLessEqual(post.call_args.kwargs["timeout"], 30)
        self.assertNotIn("private-key", json.dumps(result))

    def test_credentials_timeout_and_truncated_json_fall_back_safely(self):
        import httpx
        from local_review.ocr import judge_ocr_text
        payload = {"product_version": "p", "garment_kind": "clothing", "tokens": [dataclasses.asdict(t) for t in unfamiliar_tokens()], "label_token_ids": [0], "reason": "unknown"}
        settings = Settings(Path("unused"), "device")
        with patch("local_review.ocr.learning_examples", return_value=[]), patch("local_review.ocr.httpx.post") as post:
            self.assertEqual(judge_ocr_text(settings, payload)["reason_code"], "deepseek_not_configured")
            post.assert_not_called()
            settings = dataclasses.replace(settings, deepseek_api_key="private-key")
            post.side_effect = httpx.ReadTimeout("private-key")
            result = judge_ocr_text(settings, payload)
            self.assertEqual(result["status"], "review_required")
            self.assertNotIn("private-key", json.dumps(result))
            post.side_effect = None
            post.return_value.json.return_value = {"choices": [{"finish_reason": "length", "message": {"content": "{"}}]}
            self.assertEqual(judge_ocr_text(settings, payload)["status"], "review_required")


if __name__ == "__main__":
    unittest.main()
