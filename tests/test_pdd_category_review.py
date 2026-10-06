import tempfile
import unittest
from pathlib import Path

from attribute_runtime import AttributeRuntime, ReviewBatchRequired
from learning_models import canonical_sha256
from learning_store import LearningStore
from pdd_form_listing import PddFormListing, PddFormListingError


class CategoryLearningClient:
    def __init__(self, learned_path=None):
        self.events = []
        self.learned_path = learned_path

    def post_event(self, key, event_type, payload):
        self.events.append((event_type, payload))
        return {'event_id': key}

    def review_learning(self, request):
        if self.learned_path is None:
            return {'status': 'review_required', 'reason_code': 'review_learning_insufficient'}
        return {
            'status': 'auto_fill_ready', 'source': 'review_learning',
            'snapshot_version': request['snapshot_version'],
            'support_count': 3, 'calibrated_acceptance_rate': 1.0,
            'value_ids': ['path:' + canonical_sha256(self.learned_path)[:24]],
            'value_labels': [self.learned_path],
        }


class PddCategoryReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = LearningStore(Path(self.temp.name) / 'learning.sqlite3')
        self.store.migrate()
        self.client = CategoryLearningClient()
        self.runtime = AttributeRuntime(self.store, self.client, 'run', 'product')
        self.runtime.begin_review_collection('pdd')
        self.listing = PddFormListing(None, None, None, attribute_runtime=self.runtime,
                                     category_hints=('牛仔裤',))
        self.paths = ('男装 > 牛仔裤 > 牛仔裤', '女装 > 牛仔裤 > 牛仔裤')

    async def asyncTearDown(self):
        await self.runtime.drain()
        self.store.close()
        self.temp.cleanup()

    async def test_uncertain_category_submits_paths_and_stops_until_approved(self):
        with self.assertRaises(ReviewBatchRequired):
            await self.listing._resolve_category_uncertainty(self.paths)
        await self.runtime.drain()
        review = self.runtime.deferred_reviews[0]
        self.assertEqual(review.request.field_label, '商品类目')
        self.assertEqual(review.request.control_type, 'category')
        self.assertEqual({c.label for c in review.request.candidates}, set(self.paths))
        self.assertTrue(any(event == 'review.created' for event, _ in self.client.events))
        chosen = next(c for c in review.request.candidates if c.label == self.paths[0])
        self.store.save_review_resolution(product_version='product', platform_id='pdd',
                                         snapshot_version=review.snapshot_version,
                                         review_id=review.review_id, final_value_id=chosen.value_id)
        self.runtime.begin_review_collection('pdd')
        result = await self.listing._resolve_category_uncertainty(tuple(reversed(self.paths)))
        self.assertEqual(result, self.paths[0])
        self.assertFalse(self.runtime.has_deferred_reviews)

    async def test_mature_knowledge_selects_only_a_current_full_path(self):
        self.client.learned_path = self.paths[0]
        self.assertEqual(await self.listing._resolve_category_uncertainty(self.paths), self.paths[0])
        self.assertFalse(self.runtime.has_deferred_reviews)

    async def test_stale_knowledge_path_is_sent_to_review(self):
        self.client.learned_path = '男装 > 休闲裤'
        with self.assertRaises(ReviewBatchRequired):
            await self.listing._resolve_category_uncertainty(self.paths)
        self.assertEqual(self.runtime.deferred_reviews[0].reason_code, 'review_learning_invalid_response')

    async def test_parent_path_scopes_same_named_children(self):
        with self.assertRaises(ReviewBatchRequired):
            await self.listing._resolve_category_uncertainty(self.paths, parent_path=('男装',))
        first = self.runtime.deferred_reviews[0].request.category_leaf_id
        self.runtime.begin_review_collection('pdd')
        with self.assertRaises(ReviewBatchRequired):
            await self.listing._resolve_category_uncertainty(self.paths, parent_path=('童装',))
        self.assertNotEqual(first, self.runtime.deferred_reviews[0].request.category_leaf_id)

    async def test_no_candidates_does_not_invent_a_category(self):
        with self.assertRaisesRegex(PddFormListingError, '未返回可审核候选'):
            await self.listing._resolve_category_uncertainty(())
        self.assertFalse(self.runtime.has_deferred_reviews)
