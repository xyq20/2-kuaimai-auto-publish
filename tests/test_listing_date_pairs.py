import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from douyin_listing import DouyinListing
from field_policies import listing_date_text_value, positional_listing_date_value
from jd_form_listing import JdFormListing
from pdd_form_listing import PddFormListing
from taobao_listing import TaobaoListing, normalize_label
from tmall_form_listing import _field_source
from wxsph_form_listing import WxsphFormListing
from xhs_form_listing import XhsFormListing
from youzan_form_listing import YouzanFormListing


DATE_ROW = {
    "上市时间/上市年份季节/上市时节": "2026/2026年秋季/动态选择当天"
}


class ListingDatePairTests(unittest.IsolatedAsyncioTestCase):
    async def test_xhs_year_season_uses_second_excel_value(self):
        listing = XhsFormListing.__new__(XhsFormListing)
        key = normalize_label("上市年份季节")
        assignments = await listing._attribute_assignments(
            DATE_ROW, {key: ("上市年份季节", None)}
        )
        self.assertEqual(assignments[key][1], "2026年秋季/2026")

    async def test_jd_listing_time_prefers_first_excel_value(self):
        listing = JdFormListing.__new__(JdFormListing)
        key = normalize_label("上市时间")
        assignments = await listing._attribute_assignments(
            DATE_ROW, {key: ("上市时间", None)}
        )
        self.assertEqual(
            assignments[key][1],
            f"2026/2026年秋季/{date.today():%Y.%m.%d}",
        )

    async def test_pdd_listing_season_matches_today_season_candidate(self):
        class FixedDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 9, 24)

        listing = PddFormListing.__new__(PddFormListing)
        key = normalize_label("上市时节")
        with patch("field_policies.date", FixedDate):
            assignments = await listing._attribute_assignments(
                DATE_ROW, {key: ("上市时节", None)}
            )
        self.assertEqual(assignments[key][1], "2026年秋季/2026")

    def test_tmall_year_season_uses_second_excel_value(self):
        self.assertEqual(
            _field_source(DATE_ROW, "上市年份季节"),
            ("上市时间/上市年份季节/上市时节", "2026年秋季/2026"),
        )

    async def test_taobao_year_season_uses_second_excel_value(self):
        listing = TaobaoListing.__new__(TaobaoListing)
        key = normalize_label("上市年份季节")
        assignments = await listing._build_assignments(
            DATE_ROW, {key: ("上市年份季节", None)}
        )
        self.assertEqual(assignments[key][1], "2026年秋季/2026")

    async def test_wechat_listing_time_prefers_first_excel_value(self):
        listing = WxsphFormListing.__new__(WxsphFormListing)
        key = normalize_label("上市时间")
        assignments = await listing._attribute_assignments(
            DATE_ROW, {key: ("上市时间", None)}
        )
        self.assertEqual(assignments[key][1].split("/", 1)[0], "2026")

    async def test_youzan_year_prefers_first_excel_value(self):
        listing = YouzanFormListing.__new__(YouzanFormListing)
        key = normalize_label("年份")
        assignments = await listing._attribute_assignments(
            DATE_ROW, {key: ("年份", None)}
        )
        self.assertEqual(assignments[key][1].split("/", 1)[0], "2026")

    async def test_douyin_listing_time_receives_ordered_fallbacks(self):
        listing = DouyinListing.__new__(DouyinListing)
        listing.logger = None
        listing.apply_first_recommended_category = AsyncMock(return_value="类目")
        listing._attribute_items = AsyncMock(
            return_value={normalize_label("上市时间"): ("上市时间", None)}
        )
        listing.fill_short_title = AsyncMock(return_value="短标题")
        listing.fill_attribute = AsyncMock(return_value=("2026",))
        await listing.apply_category_and_fields(
            SimpleNamespace(attributes=DATE_ROW, short_title="短标题")
        )
        listing.fill_attribute.assert_awaited_once_with(
            "上市时间", f"2026/2026年秋季/{date.today():%Y.%m.%d}"
        )

    def test_date_text_field_only_writes_paired_value(self):
        self.assertEqual(
            listing_date_text_value("上市年份季节", "2026年秋季/2026"),
            "2026年秋季",
        )
        self.assertEqual(
            listing_date_text_value("适用季节", "春季/秋季"),
            "春季/秋季",
        )

    def test_only_listing_date_group_gets_positional_fallback(self):
        self.assertIsNone(positional_listing_date_value(
            "适用季节", "适用季节/上市年份季节/上市时节",
            "四季通用/2026年秋季/动态选择当天",
        ))

    def test_pair_is_first_even_when_earlier_excel_value_also_matches(self):
        self.assertEqual(positional_listing_date_value(
            "上市年份季节", *next(iter(DATE_ROW.items())),
            today=date(2026, 9, 24),
        ), "2026年秋季/2026")

    def test_dynamic_fallback_uses_current_season_for_season_control(self):
        self.assertEqual(positional_listing_date_value(
            "上市年份季节", *next(iter(DATE_ROW.items())),
            today=date(2026, 12, 1),
        ), "2026年秋季/2026/2026年冬季")
