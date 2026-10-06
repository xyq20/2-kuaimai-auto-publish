import unittest
from jd_form_listing import JdFormListing, JdFormListingError, parse_jd_attribute_fields


def observation(category, field, sequence, label='面料'):
    return {'category_id': category, 'request_sequence': sequence,
            'attribute_fields': parse_jd_attribute_fields({'properties': [{
                'propId': field, 'propertyName': label,
                'propertyValues': [{'valueId': 123, 'valueName': '棉布'}]}]})}


class JdCategorySchemaTests(unittest.IsolatedAsyncioTestCase):
    def listing(self, observations):
        listing = object.__new__(JdFormListing)
        listing._api_observations = observations
        listing._api_capture_tasks = []
        return listing

    async def test_new_category_does_not_merge_old_same_name_fields(self):
        listing = self.listing([observation('old', '132917', 1), observation('new', '155163', 2)])
        field, category = await listing._captured_api_field('面料')
        self.assertEqual((field.source_id, category), ('155163', 'new'))

    async def test_delayed_old_response_cannot_replace_new_category(self):
        listing = self.listing([observation('new', '155163', 2), observation('old', '132917', 1)])
        field, category = await listing._captured_api_field('面料')
        self.assertEqual((field.source_id, category), ('155163', 'new'))

    async def test_empty_auxiliary_response_keeps_same_category_schema(self):
        listing = self.listing([observation('new', '155163', 2),
                               {'category_id': 'new', 'request_sequence': 3, 'attribute_fields': ()}])
        field, category = await listing._captured_api_field('面料')
        self.assertEqual((field.source_id, category), ('155163', 'new'))

    async def test_empty_new_category_cannot_reuse_old_schema(self):
        listing = self.listing([observation('old', '132917', 1),
                               {'category_id': 'new', 'request_sequence': 2, 'attribute_fields': ()}])
        with self.assertRaises(JdFormListingError):
            await listing._captured_api_field('面料')

    async def test_missing_field_in_current_category_cannot_reuse_old(self):
        listing = self.listing([observation('old', '132917', 1), observation('new', '155163', 2, '裤长')])
        with self.assertRaises(JdFormListingError):
            await listing._captured_api_field('面料')

    async def test_duplicate_fields_within_current_category_still_rejected(self):
        latest = observation('new', '155163', 2)
        latest['attribute_fields'] += observation('new', '155164', 2)['attribute_fields']
        listing = self.listing([latest])
        with self.assertRaises(JdFormListingError):
            await listing._captured_api_field('面料')
