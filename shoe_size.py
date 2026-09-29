"""鞋码规范化及尺码图中的推荐脚长读取。"""
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from size_image_recognition import MIN_OCR_CONFIDENCE, RecognitionError, vision_ocr


def shoe_size(value):
    text = unicodedata.normalize('NFKC', str(value)).strip()
    text = re.sub(r'码$', '', text).strip()
    if not re.fullmatch(r'\d+(?:\.5)?', text):
        raise RecognitionError(f'无法识别鞋码：{value!r}')
    return format(Decimal(text).normalize(), 'f')


@dataclass(frozen=True)
class ShoeMeasurement:
    size: str
    foot_length: str


def parse_shoe_measurements(tokens, expected_sizes):
    tokens = tuple(t for t in tokens if t.confidence >= MIN_OCR_CONFIDENCE)
    def cx(t): return t.x + t.width / 2
    def cy(t): return t.y + t.height / 2
    def header(names):
        found = [t for t in tokens if t.text.strip() in names]
        if len(found) != 1:
            raise RecognitionError(f'鞋类尺码图表头不唯一：{names}')
        return found[0]
    size_head = header(('欧码',))
    foot_head = header(('推荐脚长', '脚长'))
    units = [t.text.upper().strip() for t in tokens
             if abs(cx(t)-cx(foot_head)) < .12 and abs(cy(t)-cy(foot_head)) < .09
             and t.text.upper().strip() in ('MM', 'CM')]
    if len(set(units)) != 1:
        raise RecognitionError('推荐脚长单位不明确，不能推测毫米或厘米')
    divisor = Decimal(10) if units[0] == 'MM' else Decimal(1)
    sizes = [t for t in tokens if cy(t) > cy(size_head) and abs(cx(t)-cx(size_head)) < .06
             and re.fullmatch(r'\d+(?:\.5)?', t.text.strip())]
    expected = tuple(shoe_size(s) for s in expected_sizes)
    actual = tuple(shoe_size(t.text) for t in sizes)
    if len(set(actual)) != len(actual) or len(set(expected)) != len(expected) or set(actual) != set(expected):
        raise RecognitionError(f'鞋类尺码图与商品尺码不一致：图片 {actual}，商品 {expected}')
    result = {}
    for t in sizes:
        cells = [v for v in tokens if abs(cy(v)-cy(t)) < .018 and abs(cx(v)-cx(foot_head)) < .12]
        values = []
        for v in cells:
            m = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(?:[-–—~～至]\s*(\d+(?:\.\d+)?))?\s*', v.text)
            if m:
                numbers = [Decimal(x)/divisor for x in m.groups() if x is not None]
                if not all(8 <= x <= 40 for x in numbers) or numbers != sorted(numbers):
                    raise RecognitionError(f'脚长数值异常：{v.text}')
                values.append('-'.join(format(x.normalize(), 'f') for x in numbers))
        if len(values) != 1:
            raise RecognitionError(f'鞋码 {t.text} 的推荐脚长无法唯一识别')
        result[shoe_size(t.text)] = values[0]
    return tuple(ShoeMeasurement(s, result[s]) for s in expected)


def recognize_shoe_measurements(path, expected_sizes):
    return parse_shoe_measurements(vision_ocr(path), expected_sizes)
