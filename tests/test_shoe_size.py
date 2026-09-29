import unittest
from shoe_size import parse_shoe_measurements, shoe_size
from size_image_recognition import OCRToken, RecognitionError


def token(text, x, y):
    return OCRToken(text, .99, x, y, .06, .02)


class ShoeSizeTests(unittest.TestCase):
    def setUp(self):
        self.tokens = [token('MM', .10, .10), token('推荐脚长', .10, .14),
                       token('欧码', .60, .14), token('39', .60, .25),
                       token('40', .60, .35), token('261–266', .10, .25),
                       token('266–271', .10, .35), token('271', .35, .25),
                       token('276', .35, .35)]

    def test_numeric_size_removes_only_suffix(self):
        self.assertEqual(shoe_size('３９码'), '39')
        self.assertEqual(shoe_size('39.5码'), '39.5')
        with self.assertRaises(RecognitionError):
            shoe_size('39-40码')

    def test_foot_length_uses_recommended_column_and_converts_mm(self):
        values = parse_shoe_measurements(self.tokens, ['40码', '39码'])
        self.assertEqual([(v.size, v.foot_length) for v in values],
                         [('40', '26.6-27.1'), ('39', '26.1-26.6')])

    def test_missing_extra_and_duplicate_sizes_fail(self):
        for expected in (['39'], ['39','40','41'], ['39','39','40']):
            with self.subTest(expected=expected), self.assertRaises(RecognitionError):
                parse_shoe_measurements(self.tokens, expected)

    def test_unknown_unit_fails(self):
        with self.assertRaisesRegex(RecognitionError, '单位不明确'):
            parse_shoe_measurements(self.tokens[1:], ['39','40'])

    def test_missing_length_fails(self):
        with self.assertRaisesRegex(RecognitionError, '无法唯一识别'):
            parse_shoe_measurements([t for t in self.tokens if t.text != '261–266'], ['39','40'])
