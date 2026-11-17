import unittest
import tempfile
import logging
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from attribute_runtime import AttributeRuntime, ResolvedAttribute
from learning_store import LearningStore
from kuaimai_erp import AutomationError, sync_image_group, wait_for_image_uploads
from xhs_data import XhsFields, parse_xhs_fields
from xhs_form_listing import XhsFormListing, title_without_neigborl


class XhsDataTests(unittest.TestCase):
    def test_unit_only_money_template_does_not_override_real_price(self):
        from xhs_form_listing import _required_excel_value, XhsFormListingError
        fields = {'吊牌价/价格/基本售价': '元', '售卖价/售价/价格': '298',
                  '商品分类': '服装 > 服装配饰 > 腰带/皮带/腰链'}
        self.assertEqual(_required_excel_value(fields, ('售价', '价格', '基本售价'), '售价', money=True), '298')
        with self.assertRaisesRegex(XhsFormListingError, '缺少小红书售价'):
            _required_excel_value({'价格': '元', '商品分类': fields['商品分类']}, ('价格',), '售价', money=True)
        with self.assertRaisesRegex(XhsFormListingError, '不是有效金额'):
            _required_excel_value(dict(fields, 商品分类='男装 > 休闲裤'), ('价格',), '售价', money=True)

    def test_first_exact_sweatshirt_alternative_is_not_replaced_by_last_alias(self):
        self.assertEqual(XhsFormListing._choose_category_text(
            ('男装 > 卫衣',), ('卫衣/打底衫/男士套头卫衣/套头卫衣',)),
            ('男装 > 卫衣', 'full_ordered_path'))
        self.assertTrue(XhsFormListing._category_selection_matches(
            '男装 > 卫衣', ('卫衣/打底衫/男士套头卫衣/套头卫衣',)))
    def test_shoe_category_rejects_root_only_search_result(self):
        from xhs_form_listing import XhsFormListingError
        path = ('流行男鞋', '时尚单鞋', '休闲皮鞋', '正装皮鞋', '男士德比鞋', '皮鞋')
        with self.assertRaises(XhsFormListingError):
            XhsFormListing._choose_category_text(('流行男鞋',), path)
        chosen, _ = XhsFormListing._choose_category_text(
            ('流行男鞋', '流行男鞋 > 时尚单鞋 > 休闲皮鞋'), path)
        self.assertEqual(chosen, '流行男鞋 > 时尚单鞋 > 休闲皮鞋')

    def test_category_slashes_are_alternatives_within_one_level(self):
        fields = parse_xhs_fields(
            {
                "商品分类": "休闲裤/男士休闲直筒裤/工装休闲裤",
                "价格": 586.0,
            }
        )

        self.assertEqual(
            fields.category_path,
            ("休闲裤/男士休闲直筒裤/工装休闲裤",),
        )
        self.assertEqual(fields.fields["价格"], "586")

    def test_title_removes_only_forbidden_brand_token(self):
        self.assertEqual(
            title_without_neigborl("【绿巨人】NEIGBORL 钊叔制工装休闲裤"),
            "【绿巨人】钊叔制工装休闲裤",
        )


class XhsFormListingTests(unittest.IsolatedAsyncioTestCase):
    async def test_belt_presale_is_seven_days_and_other_categories_keep_fifteen(self):
        from xhs_form_listing import XhsFormListingError
        for path, expected in (('服装 > 服装配饰 > 腰带/皮带/腰链', '7'),
                               ('男装 > 休闲裤', '15'), ('男鞋 > 正装皮鞋', '15')):
            with self.subTest(path=path):
                listing = await self._listing()
                listing.category_hints = (path,)
                self.assertEqual((await listing.apply_full_payment_presale())['付款后'], expected)
                listing._verify_persisted_identity = AsyncMock(return_value={})
                listing._verify_persisted_attributes = AsyncMock(return_value={})
                listing._persisted_radio_is_checked = AsyncMock(return_value=True)
                listing._sku_table_snapshot = AsyncMock(return_value={})
                listing._validate_sku_snapshot = Mock(return_value=({},))
                listing._visible_validation_errors = AsyncMock(return_value=())
                fields = parse_xhs_fields({'商品分类': path, '价格': '298', '数量': '100'})
                if expected == '7':
                    listing.category_hints = ()  # fresh persisted-readback adapter
                with patch('xhs_form_listing.sync_store_freight', new=AsyncMock(return_value={})):
                    report = await listing.verify_persisted_values(
                        fields, title='商品', style_code='test', expected_attributes={})
                    self.assertEqual(report['presale']['付款后'], expected)
                    await self.page.locator('[data-xhs-presale-days]').fill('15' if expected == '7' else '7')
                    with self.assertRaisesRegex(XhsFormListingError, '保存后预售天数回读失败'):
                        await listing.verify_persisted_values(
                            fields, title='商品', style_code='test', expected_attributes={})

    async def test_plain_category_text_input_fills_and_verifies_persisted_value(self):
        from xhs_form_listing import XhsFormListingError
        listing = await self._listing()
        listing.category_hints = ('服装 > 服装配饰 > 腰带/皮带/腰链',)
        await self.page.set_content('''<div id="width" class="el-form-item">
          <label class="el-form-item__label">宽度</label><div><input></div></div>''')
        item = self.page.locator('#width')
        self.assertEqual(await listing._fill_attribute('宽度', item, '3.8cm/3.8'), ('3.8cm',))
        listing._attribute_items = AsyncMock(return_value={'宽度': ('宽度', item)})
        self.assertEqual(await listing._verify_persisted_attributes({'宽度': ('3.8cm',)}),
                         {'宽度': ('3.8cm',)})
        await item.locator('input').fill('3.0cm')
        with self.assertRaisesRegex(XhsFormListingError, '保存后类目属性回读失败'):
            await listing._verify_persisted_attributes({'宽度': ('3.8cm',)})
        listing.category_hints = ('男装 > 休闲裤',)
        await item.locator('input').fill('')
        with self.assertRaisesRegex(XhsFormListingError, '下拉框不是唯一项'):
            await listing._fill_attribute('宽度', item, '3.8cm')
        self.assertEqual(await item.locator('input').input_value(), '')

    async def _empty_saved_attribute_listing(self, payload, *, status=200):
        await self.page.route(
            "https://api.test/",
            lambda route: route.fulfill(content_type="text/html", body="<body></body>"),
        )
        await self.page.goto("https://api.test/")
        listing = await self._listing()
        listing.base_item_id = "saved-product"
        items = await listing._attribute_items()
        for _label, item in reversed(tuple(items.values())):
            await item.evaluate("node => node.remove()")
        await listing.panel.evaluate(
            "node => node.insertAdjacentHTML('beforeend', '<div>暂无属性</div>')"
        )
        requests = []

        async def detail(route):
            requests.append(route.request)
            await route.fulfill(status=status, json=payload)

        await self.page.route("**/xhs/detail.json*", detail)
        return listing, requests

    async def test_empty_saved_attributes_use_detail_api_with_single_multi_and_no_fill(self):
        listing, requests = await self._empty_saved_attribute_listing({
            "result": 1,
            "data": {
                "baseItemId": "saved-product",
                "attributeList": [
                    {"name": "细分风格", "value": "时尚都市", "valueList": None},
                    {"name": "适用季节", "value": None, "valueList": [
                        {"valueId": "winter", "value": "冬季"},
                        {"valueId": "autumn", "value": "秋季"},
                    ]},
                    {"name": "工艺处理", "value": None, "valueList": None},
                ],
            },
        })

        actual = await listing._verify_persisted_attributes({
            "细分风格": ("时尚都市",), "适用季节": ("秋季", "冬季"), "工艺处理": (),
        })

        self.assertEqual(actual["细分风格"], ("时尚都市",))
        self.assertCountEqual(actual["适用季节"], ("秋季", "冬季"))
        self.assertEqual(actual["工艺处理"], ())
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].method, "GET")
        self.assertIn("baseItemId=saved-product", requests[0].url)

    async def test_empty_saved_attributes_reject_api_value_mismatch_and_no_fill_residue(self):
        listing, _requests = await self._empty_saved_attribute_listing({
            "result": 1,
            "data": {"baseItemId": "saved-product", "attributeList": [
                {"name": "细分风格", "value": "休闲风"},
                {"name": "工艺处理", "value": "做旧"},
            ]},
        })

        with self.assertRaisesRegex(Exception, "细分风格=.*休闲风.*工艺处理=.*做旧"):
            await listing._verify_persisted_attributes({
                "细分风格": ("时尚都市",), "工艺处理": (),
            })

    async def test_empty_saved_attributes_reject_wrong_product_missing_and_duplicate_fields(self):
        for data, error in (
            ({"baseItemId": "other-product", "attributeList": []}, "商品身份"),
            ({"baseItemId": "saved-product", "attributeList": []}, "工艺处理=.*不唯一"),
            ({"baseItemId": "saved-product", "attributeList": [
                {"name": "工艺处理", "value": None},
                {"name": "工艺处理", "value": None},
            ]}, "工艺处理=.*不唯一"),
        ):
            with self.subTest(error=error):
                listing, _requests = await self._empty_saved_attribute_listing(
                    {"result": 1, "data": data}
                )
                with self.assertRaisesRegex(Exception, error):
                    await listing._verify_persisted_attributes({"工艺处理": ()})

    async def test_empty_saved_attributes_reject_failed_api_instead_of_accepting_empty(self):
        listing, _requests = await self._empty_saved_attribute_listing({
            "result": 500, "message": "请求失败，请和系统管理员联系", "data": None,
        })

        with self.assertRaisesRegex(Exception, "详情接口.*请求失败"):
            await listing._verify_persisted_attributes({"工艺处理": ()})

    async def test_empty_saved_attributes_reject_http_failure_and_missing_value_shape(self):
        for payload, status, error in (
            ({"result": 1, "data": {}}, 503, "详情接口.*503"),
            ({"result": 1, "data": {"baseItemId": "saved-product"}}, 200, "缺少 attributeList"),
            ({"result": 1, "data": {"baseItemId": "saved-product", "attributeList": [
                {"name": "工艺处理", "valueList": "invalid"},
            ]}}, 200, "工艺处理=.*格式错误"),
        ):
            with self.subTest(error=error):
                listing, _requests = await self._empty_saved_attribute_listing(payload, status=status)
                with self.assertRaisesRegex(Exception, error):
                    await listing._verify_persisted_attributes({"工艺处理": ()})

    async def test_candidate_business_failure_logs_code_without_caching_empty_options(self):
        listing = await self._listing()
        listing.logger = logging.getLogger("xhs-api-test")
        listing._xhs_api_generation = 1
        response = Mock(status=200)
        response.request = Mock(url="https://api.test/xhs/getAttributeValues.json?attributeId=sleeve&shopId=")
        response.json = AsyncMock(return_value={
            "result": 500, "message": "请求失败，请和系统管理员联系", "data": None,
        })

        with self.assertLogs("xhs-api-test", level="WARNING") as logs:
            await listing._consume_xhs_attribute_response(response, ("values", 1, "sleeve"))

        self.assertNotIn((1, "sleeve"), listing._xhs_api_options)
        self.assertIn("HTTP=200", logs.output[0])
        self.assertIn("result=500", logs.output[0])
        self.assertIn("请求失败", logs.output[0])
        self.assertIn("shopId_present=False", logs.output[0])

    async def test_attribute_request_failure_logs_transport_error_and_parameter_presence(self):
        listing = await self._listing()
        listing.logger = logging.getLogger("xhs-api-test")
        await self.page.route(
            "**/xhs/getAttributeList.json*", lambda route: route.abort("connectionreset")
        )

        with self.assertLogs("xhs-api-test", level="WARNING") as logs:
            await self.page.evaluate("""async () => {
              try { await fetch('https://api.test/xhs/getAttributeList.json?leafCategoryId=101&shopId=9'); }
              catch (_) {}
            }""")

        self.assertIn("ERR_CONNECTION_RESET", logs.output[0])
        self.assertIn("shopId_present=True", logs.output[0])

    async def test_shoe_specification_waits_for_category_rerender(self):
        listing = await self._listing()

        async def delayed_category(_path):
            await listing.panel.evaluate('''panel => setTimeout(() => {
              panel.insertAdjacentHTML('beforeend', `<div class="block-specification">
                <div><div class="title-bg"><div class="el-select"><input class="el-input__inner" value="颜色分类"></div></div></div>
                <div><div class="title-bg"><div class="el-select"><input class="el-input__inner" value="尺码"></div></div>
                  <div class="specification-value"><input class="el-input__inner" value="39码"></div></div>
              </div>`);
            }, 150)''')
            return {}

        listing.apply_category = AsyncMock(side_effect=delayed_category)
        for name in (
            'fill_identity', 'fill_category_attributes', 'apply_full_payment_presale',
            'fill_price_inventory_batch', 'sync_main_images',
        ):
            setattr(listing, name, AsyncMock(return_value={}))
        listing._select_values = AsyncMock(return_value=('鞋码',))
        listing._visible_validation_errors = AsyncMock(return_value=())
        with patch('xhs_form_listing.sync_store_freight', new_callable=AsyncMock):
            await listing.apply_excel_fields(
                XhsFields(fields={}, category_path=('流行男鞋', '休闲皮鞋')),
                title='皮鞋', style_code='MY019-1', portrait_paths=(),
                timeout_seconds=5, uploader=AsyncMock(),
            )

        listing._select_values.assert_awaited_once()
        self.assertEqual(
            await listing.panel.locator(
                '.block-specification .specification-value input'
            ).first.input_value(),
            '39码',
        )

    async def test_main_upload_survives_new_validation_label_after_hover(self):
        listing = await self._listing()
        await listing.panel.evaluate('''panel => {
          panel.innerHTML = `<div id="main-upload">
            <label>主图：</label>
            <div class="sc-upload" style="width:100px;height:100px">
              <div class="file-img"><button class="del-btn" onclick="this.parentElement.remove()">删除</button></div>
              <input type="file" multiple accept="image/*">
            </div>
          </div><div id="transparent"><label>透明素材图：</label><input type="file"></div>`;
          const root = panel.querySelector('#main-upload');
          root.querySelector('.sc-upload').addEventListener('mousemove', () => {
            if (!root.querySelector('.file-img') && !panel.querySelector('#validation-summary')) {
              const summary = document.createElement('div');
              summary.id = 'validation-summary';
              summary.innerHTML = '<span>主图</span>';
              panel.prepend(summary);
            }
          });
          root.querySelector('input').addEventListener('change', event => {
            window.uploadedNames = Array.from(event.target.files).map(file => file.name);
            for (const file of event.target.files) {
              const image = document.createElement('div');
              image.className = 'file-img';
              image.innerHTML = '<img class="originImg" src="data:image/png;base64,AA==">';
              root.querySelector('.sc-upload').append(image);
            }
          });
        }''')
        self.page.set_default_timeout(700)
        with tempfile.TemporaryDirectory() as directory:
            paths = (Path(directory) / "portrait-1.jpg", Path(directory) / "portrait-2.jpg")
            for path in paths:
                path.write_bytes(b"fixture-image")
            result = await listing.sync_main_images(paths, timeout_seconds=2, uploader=sync_image_group)
        self.assertEqual(result["action"], "replaced")
        self.assertEqual(await self.page.evaluate("window.uploadedNames"), ["portrait-1.jpg", "portrait-2.jpg"])
        self.assertEqual(await listing.panel.locator('#main-upload .file-img').count(), 2)
        self.assertEqual(await listing.panel.locator('#transparent input').evaluate('e => e.files.length'), 0)

    async def test_main_upload_does_not_count_empty_drop_slot_as_uploaded_image(self):
        await self.page.set_content('''
          <div id="main"><div class="sc-upload">
            <div class="file-img"><img class="originImg" src="one.png"></div>
            <div class="file-img"><img class="originImg" src="two.png"></div>
            <div class="file-img"><img class="originImg" src="three.png"></div>
            <div class="file-img"><img class="originImg" src="four.png"></div>
            <div class="file-img empty-slot"></div>
          </div>
          </div>
        ''')
        with self.assertRaisesRegex(AutomationError, "4/5"):
            await wait_for_image_uploads(
                self.page.locator("#main"), 5, "小红书3:4主图", 1
            )

    async def test_main_image_validation_waits_for_pending_result(self):
        listing = await self._listing()
        await listing.panel.evaluate('''panel => {
          panel.innerHTML = `<div class="el-form-item">
            <label class="el-form-item__label">主图：</label>
            <div data-xhs-image-group="main" class="sc-upload" style="width:100px;height:100px">
              <div class="file-img"><img class="originImg" src="one.png"></div>
            </div>
            <div class="el-form-item__error">至少选择1张图片</div>
          </div>`;
          const item = panel.querySelector('.el-form-item');
          item.__vue__ = {
            prop: 'imageList', fieldValue: [{url: 'one.png'}],
            validateState: 'validating', validateMessage: '至少选择1张图片'
          };
          setTimeout(() => {
            item.__vue__.validateState = 'success';
            item.__vue__.validateMessage = '';
            item.querySelector('.el-form-item__error').remove();
          }, 150);
        }''')
        for name, result in (
            ("apply_category", {}), ("fill_identity", {}),
            ("fill_category_attributes", {}), ("apply_full_payment_presale", {}),
            ("fill_price_inventory_batch", {}),
            ("sync_main_images", {"source": "3:4主图", "count": 1}),
        ):
            setattr(listing, name, AsyncMock(return_value=result))
        with patch("xhs_form_listing.sync_store_freight", new_callable=AsyncMock) as freight:
            freight.return_value = {}
            result = await listing.apply_excel_fields(
                XhsFields(fields={}, category_path=()), title="商品", style_code="SKU",
                portrait_paths=(Path("one.png"),), timeout_seconds=5,
                uploader=AsyncMock(),
            )
        self.assertEqual(result["deferred_validation_errors"], ())
        self.assertEqual(result["main_images"]["count"], 1)

    async def test_uploaded_main_image_warning_defers_to_save_but_other_errors_block(self):
        from xhs_form_listing import XhsFormListingError
        listing=await self._listing()
        for name, result in (
            ('apply_category',{}),('fill_identity',{}),('fill_category_attributes',{}),
            ('apply_full_payment_presale',{}),('fill_price_inventory_batch',{}),
            ('sync_main_images',{'source':'3:4主图','count':5}),
            ('_wait_for_main_image_validation',None),
        ):
            setattr(listing,name,AsyncMock(return_value=result))
        warning='主图：：至少选择1张图片'
        listing._visible_validation_errors=AsyncMock(return_value=(warning,))
        async def apply():
            return await listing.apply_excel_fields(XhsFields(fields={},category_path=()),
                title='商品',style_code='SKU',portrait_paths=(Path('one.png'),),
                timeout_seconds=5,uploader=AsyncMock())
        with patch('xhs_form_listing.sync_store_freight',new_callable=AsyncMock,return_value={}):
            result=await apply()
            self.assertEqual(result['save_validation_pending'],(warning,))
            listing._visible_validation_errors.return_value=(warning,'售价：必填')
            with self.assertRaisesRegex(XhsFormListingError,'售价：必填'):
                await apply()
            listing.sync_main_images.return_value={'count':0}
            listing._visible_validation_errors.return_value=(warning,)
            with self.assertRaisesRegex(XhsFormListingError,'至少选择1张图片'):
                await apply()

    def test_category_hints_accept_one_exact_outerwear_leaf(self):
        self.assertEqual(
            XhsFormListing._choose_category_text(
                (
                    "服饰 > 男装 > 男士休闲夹克",
                    "服饰 > 男装 > 夹克",
                ),
                ("夹克", "外套", "男士休闲夹克", "其他夹克"),
            ),
            ("服饰 > 男装 > 夹克", "unique_exact_hint_leaf"),
        )

    def test_category_leaf_cannot_match_root_hint_from_wrong_branch(self):
        from xhs_form_listing import XhsFormListingError

        with self.assertRaises(XhsFormListingError):
            XhsFormListing._choose_category_text(
                (
                    "模玩/动漫/周边/娃圈三坑/桌游 > 卡通/动漫周边 > 动漫T恤/鞋帽/服装",
                ),
                (
                    "服装",
                    "男装",
                    "针织衫/毛衣/羊毛衫/毛针织开衫/开衫毛衣",
                ),
            )

    async def asyncSetUp(self):
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            channel="chrome", headless=True
        )
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def _listing(self):
        async def fulfill_category_query(route):
            await route.fulfill(
                content_type="application/json",
                headers={"access-control-allow-origin": "*"},
                json={
                    "data": {
                        "records": [
                            {
                                "categoryNameList": [
                                    "男装",
                                    "休闲裤",
                                    "工装休闲裤",
                                ]
                            }
                        ]
                    }
                },
            )

        await self.page.route(
            "**/category/base/queryCategoryList.json", fulfill_category_query
        )
        await self.page.set_content(
            """
            <button role="tab" aria-selected="false" onclick="
              this.setAttribute('aria-selected', 'true');
              document.querySelector('[role=tabpanel]').style.display='block';">小红书资料</button>
            <div role="tabpanel" aria-label="小红书资料" style="display:none">
              <h3>基础信息</h3>
              <div><span>商品分类：</span><button onclick="openCategory()">修改类目</button></div>
              <div data-xhs-field="商品标题"><span>商品标题：</span><input value="【绿巨人】NEIGBORL钊叔制工装休闲裤"></div>
              <div data-xhs-field="货号"><span>货号：</span><input value=""></div>
              <h3>商品属性：</h3>
              <div class="el-form-item"><label class="el-form-item__label">面料</label><div class="el-form-item__content">${select(['棉'])}</div></div>
              <div class="el-form-item"><label class="el-form-item__label">厚薄</label><div class="el-form-item__content">${select(['常规'])}</div></div>
              <div class="el-form-item"><label class="el-form-item__label">适用场景</label><div class="el-form-item__content">${select(['毕业答辩'])}</div></div>
              <div class="el-form-item is-required"><label class="el-form-item__label">上市年份季节</label><div class="el-form-item__content">${select(['2026年秋季'])}</div></div>
              <div class="el-form-item"><label class="el-form-item__label">服装版型</label><div class="el-form-item__content">${select(['直筒'])}</div></div>
              <div class="el-form-item"><label class="el-form-item__label">基础风格</label><div class="el-form-item__content">${select(['时尚都市'])}</div></div>
              <div class="el-form-item"><label class="el-form-item__label">裤长</label><div class="el-form-item__content">${select(['长裤'])}</div></div>
              <h3>价格库存</h3>
              <section id="delivery">
                <label class="el-radio"><input type="radio" name="delivery"><span class="el-radio__label">现货模式</span></label>
                <label class="el-radio"><input type="radio" name="delivery" onclick="showPresale()"><span class="el-radio__label">全款预售模式</span></label>
                <div id="presale" style="display:none">
                  <label class="el-radio"><input type="radio" name="presale"><span class="el-radio__label">时段预售</span></label>
                  <div><span>付款后：</span><input data-xhs-presale-days value=""><span>天发货</span></div>
                </div>
              </section>
              <section>
                <div data-xhs-batch-field="售价"><span>售价：</span><input value=""></div>
                <div data-xhs-batch-field="市场价"><span>市场价：</span><input value=""></div>
                <div data-xhs-batch-field="库存"><span>库存：</span><input value=""></div>
                <button onclick="applyBatch()">批量设置</button>
              </section>
              <table><thead><tr><th>颜色分类</th><th>售价</th><th>市场价</th><th>库存</th></tr></thead>
                <tbody><tr><td>黑灰</td><td><input></td><td><input></td><td><input></td></tr>
                  <tr><td>军绿色</td><td><input></td><td><input></td><td><input></td></tr></tbody></table>
              <h3>图文信息</h3>
              <div data-xhs-image-group="main"><span>主图：</span></div>
            </div>
            <div class="el-dialog" style="display:none"><div class="el-dialog__title">修改类目</div>
              <input oninput="showRoot(this.value)"><div id="nodes"><span class="el-cascader-node__label">不应扫描的整树节点</span></div><div id="chosen">已选：</div>
              <button onclick="closeCategory()">取 消</button><button onclick="confirmCategory()">确 定</button></div>
            <div id="portal-results" class="el-autocomplete-suggestion" style="display:none"></div>
            <script>
              function select(values) {
                return '<div class="el-select"><input class="el-input__inner" readonly onclick="openSelect(this)"><div class="el-select-dropdown" style="display:none"><ul>' + values.map(v => '<li class="el-select-dropdown__item" onclick="choose(this)">'+v+'</li>').join('') + '</ul></div></div>';
              }
              document.querySelectorAll('.el-form-item__content').forEach(node => {
                if (!node.innerHTML.trim()) node.innerHTML = select([]);
              });
              function openSelect(input) { input.closest('.el-select').querySelector('.el-select-dropdown').style.display = 'block'; }
              function choose(node) { const select = node.closest('.el-select'); select.querySelector('input').value = node.textContent.trim(); select.querySelector('.el-select-dropdown').style.display = 'none'; }
              document.addEventListener('keydown', event => {
                if (event.key === 'Escape') {
                  document.querySelectorAll('.el-select-dropdown').forEach(node => node.style.display = 'none');
                }
              });
              let path=[];
              function openCategory(){ document.querySelector('.el-dialog').style.display='block'; path=[]; document.getElementById('chosen').textContent='已选：'; }
              function closeCategory(){ document.querySelector('.el-dialog').style.display='none'; }
              function showRoot(value){
                const results = document.getElementById('portal-results');
                results.style.display = value === '工装休闲裤' ? 'block' : 'none';
                results.innerHTML = value === '工装休闲裤'
                  ? '<ul><li onclick="choosePortalCategory(this)">男装 > 休闲裤 > 工装休闲裤</li></ul>'
                  : '';
                if (value === '工装休闲裤') fetch('https://category.test/category/base/queryCategoryList.json');
              }
              function choosePortalCategory(node){
                path=['男装', '休闲裤', '工装休闲裤'];
                document.getElementById('chosen').textContent='已选：'+path.join(' > ');
                document.getElementById('portal-results').style.display='none';
              }
              function node(label, handler){ const n=document.createElement('span'); n.className='el-cascader-node__label'; n.textContent=label; n.onclick=handler; const root=document.getElementById('nodes'); root.innerHTML=''; root.appendChild(n); }
              function confirmCategory(){ document.querySelector('.el-dialog').style.display='none'; }
              function showPresale(){ document.getElementById('presale').style.display='block'; }
              function applyBatch(){ const get=l => document.querySelector('[data-xhs-batch-field="'+l+'"] input').value; document.querySelectorAll('tbody tr').forEach(row => { const cells=row.querySelectorAll('input'); cells[0].value=get('售价'); cells[1].value=get('市场价'); cells[2].value=get('库存'); }); }
            </script>
            """.replace("${select(['棉'])}", "<div class='el-select'><input class='el-input__inner' readonly onclick='openSelect(this)'><div class='el-select-dropdown' style='display:none'><ul><li class='el-select-dropdown__item' onclick='choose(this)'>棉</li></ul></div></div>")
            .replace("${select(['常规'])}", "<div class='el-select'><input class='el-input__inner' readonly onclick='openSelect(this)'><div class='el-select-dropdown' style='display:none'><ul><li class='el-select-dropdown__item' onclick='choose(this)'>常规</li></ul></div></div>")
            .replace("${select(['毕业答辩'])}", "<div class='el-select'><input class='el-input__inner' readonly onclick='openSelect(this)'><div class='el-select-dropdown' style='display:none'><ul><li class='el-select-dropdown__item' onclick='choose(this)'>毕业答辩</li></ul></div></div>")
            .replace("${select(['2026年秋季'])}", "<div class='el-select'><input class='el-input__inner' readonly onclick='openSelect(this)'><div class='el-select-dropdown' style='display:none'><ul><li class='el-select-dropdown__item' onclick='choose(this)'>2026年秋季</li></ul></div></div>")
            .replace("${select(['直筒'])}", "<div class='el-select'><input class='el-input__inner' readonly onclick='openSelect(this)'><div class='el-select-dropdown' style='display:none'><ul><li class='el-select-dropdown__item' onclick='choose(this)'>直筒</li></ul></div></div>")
            .replace("${select(['时尚都市'])}", "<div class='el-select'><input class='el-input__inner' readonly onclick='openSelect(this)'><div class='el-select-dropdown' style='display:none'><ul><li class='el-select-dropdown__item' onclick='choose(this)'>时尚都市</li></ul></div></div>")
            .replace("${select(['长裤'])}", "<div class='el-select'><input class='el-input__inner' readonly onclick='openSelect(this)'><div class='el-select-dropdown' style='display:none'><ul><li class='el-select-dropdown__item' onclick='choose(this)'>长裤</li></ul></div></div>")
        )
        listing = XhsFormListing(self.page, self.page.locator("body"), None)
        await listing.open()
        await self.page.evaluate("""() => {
          const chosen=document.getElementById('chosen');
          if (!chosen) return;
          const menus=document.createElement('div');chosen.after(menus);
          new MutationObserver(()=>{
            const parts=chosen.textContent.replace(/^已选[：:]\s*/, '').split(' > ').filter(Boolean);
            menus.innerHTML=parts.map((name,i)=>
              '<div class="el-cascader-menu"><div class="el-cascader-node" '+
              (i<parts.length-1?'aria-haspopup="true"':'')+'><span class="el-cascader-node__label">'+name+'</span></div></div>').join('');
          }).observe(chosen, {childList:true, characterData:true, subtree:true});
        }""")
        return listing

    async def _ordered_category_listing(self, records_by_term):
        listing = await self._listing()
        await self.page.unroute("**/category/base/queryCategoryList.json")
        self.searches = []

        async def query(route):
            term = route.request.post_data_json["queryTxt"]
            self.searches.append(term)
            await route.fulfill(headers={"access-control-allow-origin": "*"}, json={
                "result": 1, "data": {"records": [
                    {"categoryNameList": parts} for parts in records_by_term.get(term, [])
                ]},
            })

        await self.page.route("**/category/base/queryCategoryList.json", query)
        await self.page.evaluate("""() => {
          window.showRoot = async value => {
            const results = document.getElementById('portal-results');
            results.innerHTML = '';
            const payload = await (await fetch('https://category.test/category/base/queryCategoryList.json', {
              method: 'POST', body: JSON.stringify({queryTxt: value, platformType: 'xhs'})
            })).json();
            results.style.display = 'block';
            const list = document.createElement('ul');
            for (const record of payload.data.records) {
              const row = document.createElement('li');
              row.textContent = record.categoryNameList.join(' > ');
              row.onclick = () => {
                document.getElementById('chosen').textContent = '已选：' + row.textContent;
                results.style.display = 'none';
              };
              list.appendChild(row);
            }
            results.appendChild(list);
          };
        }""")
        return listing

    async def test_category_search_respects_first_available_slash_alternative(self):
        listing = await self._ordered_category_listing({
            "休闲裤": [["男装", "休闲裤", "休闲裤"]],
            "男士休闲直筒裤": [["男装", "休闲裤", "男士休闲直筒裤"]],
            "工装休闲裤": [["男装", "休闲裤", "工装休闲裤"]],
        })
        report = await listing.apply_category(("休闲裤/男士休闲直筒裤/工装休闲裤",))
        self.assertEqual(
            self.searches,
            ["休闲裤"],
        )
        self.assertEqual(report["selected"], "男装 > 休闲裤 > 休闲裤")

    async def test_category_validation_mismatch_uses_model_to_choose_real_full_path(self):
        listing = await self._ordered_category_listing({
            '针织衫': [['全新奢侈品', '贸易男装/男士精品', '针织衫/毛衣']],
            '毛衣': [['服装', '男装', '羊毛开衫']],
        })
        with tempfile.TemporaryDirectory() as directory:
            store = LearningStore(Path(directory)/'learning.sqlite3'); store.migrate()
            client = Mock(); client.post_event.return_value = {'ok':True}
            client.review_learning.return_value = {'status':'review_required'}
            client.decide.return_value = {'status':'review_required'}
            def judgment(payload):
                choice = next(o for o in payload['options'] if o['label'] == '服装 > 男装 > 羊毛开衫')
                return {'status':'suggested','source':'deepseek','api_called':True,'model':'deepseek-flash',
                    'snapshot_version':payload['snapshot_version'],'value_ids':[choice['value_id']],
                    'confidence':.99,'evidence_kinds':['excel','product'],
                    'assessment':{'relation':'inferred','source_text':payload['excel_value'],
                                  'assumptions':[],'conflicts':[],'competing_value_ids':[]},
                    'grounded_support':[{'source':'product','text':'羊毛开衫'}],
                    'reason':'羊毛开衫符合 Excel 毛衣类目，奢侈品分支不符合商品信息'}
            client.semantic_judge.side_effect = judgment
            listing.attribute_runtime = AttributeRuntime(store,client,'run','product')
            listing.category_hints = ('服装','男装','针织衫/毛衣')
            try:
                report = await listing.apply_category(('服装','男装','针织衫/毛衣'))
                await listing.attribute_runtime.drain()
                self.assertEqual(report['selected'], '服装 > 男装 > 羊毛开衫')
                trace = listing.attribute_runtime.model_decisions[0]
                self.assertEqual(trace['status'], 'adopted')
                self.assertEqual(trace['input_evidence']['category_source'], 'excel_search_ambiguous')
                self.assertIn('全新奢侈品 > 贸易男装/男士精品 > 针织衫/毛衣', trace['input_evidence']['candidate_paths'])
                self.assertEqual(self.searches[:2], ['针织衫','毛衣'])
            finally:
                store.close()

    async def test_flattened_category_alias_does_not_accept_wrong_branch(self):
        listing = await self._ordered_category_listing({
            "休闲裤": [["全新奢侈品", "贸易男装/男士精品", "休闲裤"]],
            "男士休闲直筒裤": [["男装", "休闲裤", "男士休闲直筒裤"]],
            "工装休闲裤": [["男装", "休闲裤", "工装休闲裤"]],
        })

        report = await listing.apply_category(
            ("休闲裤/男士休闲直筒裤/工装休闲裤",)
        )

        self.assertEqual(
            report["selected"], "男装 > 休闲裤 > 男士休闲直筒裤"
        )
        self.assertEqual(
            self.searches,
            ["休闲裤", "男士休闲直筒裤"],
        )

    async def test_category_fallback_uses_xhs_learning_review(self):
        listing = await self._ordered_category_listing({
            "休闲裤": [["男装", "休闲裤", "休闲裤"], ["女装", "休闲裤", "休闲裤"]],
        })
        requests = []

        class Runtime:
            async def resolve(self, request):
                requests.append(request)
                return type("Resolved", (), {
                    "value_id": "path:learned",
                    "label": "男装 > 休闲裤 > 休闲裤",
                    "is_no_fill": False,
                })()

            def raise_deferred_reviews(self):
                raise AssertionError("review should have resolved")

        listing.attribute_runtime = Runtime()

        report = await listing.apply_category(("休闲裤/直筒裤/工装休闲裤",))

        self.assertEqual(report["strategy"], "review_learning")
        self.assertEqual(requests[0].platform_id, "xhs")
        self.assertEqual(requests[0].control_type, "category")

    async def test_category_search_advances_after_empty_or_unmatched_results(self):
        listing = await self._ordered_category_listing({
            "休闲裤": [["男装", "牛仔裤"]],
            "工装休闲裤": [["男装", "休闲裤", "工装休闲裤"]],
        })
        report = await listing.apply_category(("休闲裤/男士休闲直筒裤/工装休闲裤",))
        self.assertEqual(self.searches, ["休闲裤", "男士休闲直筒裤", "工装休闲裤"])
        self.assertEqual(report["selected"], "男装 > 休闲裤 > 工装休闲裤")

    async def test_category_search_advances_when_unmatched_json_has_no_dom_rows(self):
        listing = await self._ordered_category_listing({
            "休闲裤": [["男装", "牛仔裤"]],
            "工装休闲裤": [["男装", "休闲裤", "工装休闲裤"]],
        })
        await self.page.evaluate("""() => {
          const show = window.showRoot;
          window.showRoot = async value => {
            await show(value);
            if (value === '休闲裤') document.getElementById('portal-results').replaceChildren();
          };
        }""")
        report = await asyncio.wait_for(
            listing.apply_category(("休闲裤/工装休闲裤",)), timeout=10,
        )
        self.assertEqual(self.searches, ["休闲裤", "工装休闲裤"])
        self.assertEqual(report["selected"], "男装 > 休闲裤 > 工装休闲裤")

    def test_category_api_parent_result_is_not_a_selectable_leaf(self):
        self.assertEqual(XhsFormListing._category_paths_from_json({"data": {"records": [
            {"categoryNameList": ["男装", "休闲裤"], "leaf": 0},
            {"categoryNameList": ["男装", "休闲裤", "工装休闲裤"], "leaf": 1},
        ]}}), ("男装 > 休闲裤 > 工装休闲裤",))

    async def test_category_search_ignores_late_response_from_previous_keyword(self):
        from category_search import CategorySearchCapture, category_response_paths

        entered = asyncio.Event()
        release = asyncio.Event()

        async def query(route):
            term = route.request.post_data_json["queryTxt"]
            if term == "休闲裤":
                entered.set()
                await release.wait()
            await route.fulfill(headers={"access-control-allow-origin": "*"}, json={
                "result": 1, "data": {"records": [
                    {"categoryNameList": ["男装", term], "leaf": 1},
                ]},
            })

        await self.page.route("**/category/base/queryCategoryList.json", query)
        async with CategorySearchCapture(self.page, category_response_paths) as capture:
            capture.start("休闲裤")
            old = asyncio.create_task(self.page.evaluate("""async () => {
              await fetch('https://category.test/category/base/queryCategoryList.json', {
                method:'POST', body:JSON.stringify({queryTxt:'休闲裤'})
              });
            }"""))
            await asyncio.wait_for(entered.wait(), 5)
            capture.start("工装休闲裤")
            await self.page.evaluate("""async () => {
              await fetch('https://category.test/category/base/queryCategoryList.json', {
                method:'POST', body:JSON.stringify({queryTxt:'工装休闲裤'})
              });
            }""")
            release.set()
            await old
        self.assertEqual(capture.paths, ["男装 > 工装休闲裤"])

    async def test_category_api_failure_is_not_treated_as_no_results(self):
        listing = await self._ordered_category_listing({})
        await self.page.route("**/category/base/queryCategoryList.json", lambda route: route.fulfill(
            headers={"access-control-allow-origin": "*"},
            json={"result": 500, "message": "接口失败", "data": {"records": []}},
        ))
        with self.assertRaisesRegex(Exception, "类目搜索接口失败.*500.*接口失败"):
            await listing.apply_category(("休闲裤/工装休闲裤",))

    def test_category_path_alternatives_choose_first_exact_leaf(self):
        self.assertEqual(XhsFormListing._choose_category_text(
            ("服装 > 男装 > 毛衣", "服装 > 男装 > 针织衫"),
            ("服装", "男装", "针织衫/毛衣"),
        ), ("服装 > 男装 > 针织衫", "full_ordered_path"))

    async def test_fills_ordered_category_title_attributes_presale_batch_and_34_images(self):
        listing = await self._listing()
        uploads = []

        async def uploader(page, item, paths, label, timeout_seconds, **kwargs):
            uploads.append((tuple(paths), label, timeout_seconds, kwargs.get("force_replace")))
            return "replaced"

        report = await listing.apply_excel_fields(
            XhsFields(
                fields={
                    "面料材质/面料": "棉（100%）",
                    "厚度": "常规款",
                    "厚薄": "常规",
                    "适用场景": "日常",
                    "上市时间/上市年份季节": "2026/2026年秋季",
                    "服饰版型/版型": "直筒",
                    "风格/细分风格/基础风格": "休闲风/时尚都市",
                    "裤长": "长裤",
                    "价格/售价": "586",
                    "数量": "100",
                },
                category_path=("休闲裤", "男士休闲直筒裤", "工装休闲裤"),
            ),
            title="【绿巨人】NEIGBORL钊叔制工装休闲裤",
            style_code="NGBL-10588",
            portrait_paths=(Path("three-four-1.jpg"), Path("three-four-2.jpg")),
            timeout_seconds=30,
            uploader=uploader,
        )

        self.assertEqual(
            report["category"]["clicked_segments"],
            ("休闲裤", "男士休闲直筒裤", "工装休闲裤"),
        )
        self.assertEqual(report["category"]["strategy"], "first_and_leaf_exact")
        self.assertEqual(report["category"]["json_candidate_count"], 1)
        self.assertEqual(report["category"]["search_term"], "工装休闲裤")
        self.assertEqual(
            report["category"]["selected"],
            "男装 > 休闲裤 > 工装休闲裤",
        )
        self.assertEqual(report["identity"], {"商品标题": "【绿巨人】钊叔制工装休闲裤", "货号": "NGBL-10588"})
        self.assertEqual(await self.page.locator('[data-xhs-field="商品标题"] input').input_value(), "【绿巨人】钊叔制工装休闲裤")
        self.assertEqual(await self.page.locator('[data-xhs-field="货号"] input').input_value(), "NGBL-10588")
        self.assertEqual(report["attributes"]["attributes"]["面料"], ("棉",))
        self.assertEqual(report["attributes"]["attributes"]["厚薄"], ("常规",))
        self.assertEqual(
            report["attributes"]["skipped_no_exact_candidate"],
            {"适用场景": "日常"},
        )
        self.assertEqual(report["presale"], {"发货模式": "全款预售模式", "预售类型": "时段预售", "付款后": "15"})
        self.assertEqual(report["sku_batch"]["values"], {"售价": "586", "库存": "100"})
        self.assertEqual(report["sku_batch"]["row_count"], 2)
        self.assertEqual(
            uploads,
            [((Path("three-four-1.jpg"), Path("three-four-2.jpg")), "小红书3:4主图", 30, True)],
        )

    async def test_identity_waits_for_category_rerender(self):
        listing = await self._listing()
        await listing.panel.evaluate('''panel => {
          panel.querySelector('[data-xhs-field="商品标题"]').remove();
          setTimeout(() => panel.insertAdjacentHTML('beforeend', '<div class="el-form-item"><label>商品标题：</label><textarea></textarea></div>'), 600);
        }''')
        report = await listing.fill_identity('NEIGBORL 工装裤', 'NGBL-2068')
        self.assertEqual(report['商品标题'], '工装裤')

    async def test_identity_waits_until_category_rerender_makes_title_editable(self):
        listing = await self._listing()
        await listing.panel.evaluate('''panel => {
          const title = panel.querySelector('[data-xhs-field="商品标题"] input');
          title.setAttribute('readonly', 'readonly');
          setTimeout(() => title.removeAttribute('readonly'), 600);
        }''')

        report = await listing.fill_identity('NEIGBORL 工装裤', 'NGBL-2068')

        self.assertEqual(report['商品标题'], '工装裤')
        self.assertEqual(
            await self.page.locator('[data-xhs-field="商品标题"] input').input_value(),
            '工装裤',
        )

    async def test_identity_requeries_when_title_row_is_replaced_after_lookup(self):
        listing = await self._listing()
        original_find = listing._find_named_input
        replaced = False

        async def find_then_replace(label):
            nonlocal replaced
            control = await original_find(label)
            if label == '商品标题' and not replaced:
                replaced = True
                await listing.panel.evaluate('''panel => {
                  const row = panel.querySelector('[data-xhs-field="商品标题"]');
                  row.outerHTML = `
                    <div class="el-form-item">
                      <div class="label-wrap"><label>商品标题：</label></div>
                      <div class="control-wrap"><input value="【绿巨人】NEIGBORL钊叔制工装休闲裤"></div>
                    </div>`;
                }''')
            return control

        self.page.set_default_timeout(500)
        with patch.object(listing, '_find_named_input', side_effect=find_then_replace):
            report = await listing.fill_identity('NEIGBORL 工装裤', 'NGBL-2068')

        self.assertEqual(report['商品标题'], '工装裤')

    async def test_identity_finds_control_beyond_five_parent_levels(self):
        listing = await self._listing()
        await listing.panel.evaluate('''panel => {
          const row = panel.querySelector('[data-xhs-field="商品标题"]');
          row.outerHTML = `
            <div class="el-form-item">
              <div><div><div><div><div><div><label>商品标题：</label></div></div></div></div></div></div>
              <div class="el-form-item__content"><input value="旧标题"></div>
            </div>`;
        }''')

        control = await listing._find_named_input_once('商品标题')

        self.assertIsNotNone(control)
        self.assertEqual(await control.input_value(timeout=500), '旧标题')

    async def test_identity_scrolls_editor_to_load_field(self):
        listing = await self._listing()
        await listing.panel.evaluate('''panel => {
          panel.innerHTML = '<div style="height:1200px"></div>';
          panel.style.cssText = 'height:200px;overflow-y:auto';
          panel.addEventListener('scroll', () => {
            if (panel.scrollTop > 0 && !panel.querySelector('textarea')) panel.insertAdjacentHTML('beforeend', '<div class="el-form-item"><label>商品标题：</label><textarea></textarea></div>');
          });
        }''')
        control = await listing._find_named_input('商品标题')
        await control.fill('工装裤')
        self.assertEqual(await control.input_value(), '工装裤')

    async def test_verifies_persisted_identity_attributes_presale_and_sku_values(self):
        listing = await self._listing()
        fields = XhsFields(
            fields={
                "面料材质/面料": "棉（100%）",
                "厚薄": "常规",
                "上市时间/上市年份季节": "2026/2026年秋季",
                "服饰版型/版型": "直筒",
                "风格/细分风格/基础风格": "休闲风/时尚都市",
                "裤长": "长裤",
                "价格/售价": "586",
                "数量": "100",
            },
            category_path=("休闲裤", "男士休闲直筒裤", "工装休闲裤"),
        )

        async def uploader(*_args, **_kwargs):
            return "replaced"

        before_save = await listing.apply_excel_fields(
            fields,
            title="【绿巨人】NEIGBORL钊叔制工装休闲裤",
            style_code="NGBL-10588",
            portrait_paths=(Path("three-four-1.jpg"),),
            timeout_seconds=30,
            uploader=uploader,
        )

        report = await listing.verify_persisted_values(
            fields,
            title="【绿巨人】NEIGBORL钊叔制工装休闲裤",
            style_code="NGBL-10588",
            expected_attributes=before_save["attributes"]["attributes"],
        )

        self.assertEqual(report["identity"]["商品标题"], "【绿巨人】钊叔制工装休闲裤")
        self.assertEqual(report["attributes"]["裤长"], ("长裤",))
        self.assertEqual(report["presale"]["付款后"], "15")
        self.assertEqual(report["sku_values"], {"售价": "586", "库存": "100"})
        self.assertEqual(report["row_count"], 2)

        await self.page.locator('[data-xhs-field="货号"] input').fill("wrong")
        with self.assertRaisesRegex(Exception, "保存后身份字段回读失败"):
            await listing.verify_persisted_values(
                fields,
                title="【绿巨人】NEIGBORL钊叔制工装休闲裤",
                style_code="NGBL-10588",
                expected_attributes=before_save["attributes"]["attributes"],
            )

    def test_adapter_has_no_save_or_publish_entrypoints(self):
        self.assertFalse(hasattr(XhsFormListing, "save"))
        self.assertFalse(hasattr(XhsFormListing, "publish"))

    async def test_required_attribute_without_exact_candidate_still_fails(self):
        listing = await self._listing()
        items = await listing._attribute_items()
        item = items["上市年份季节"][1]
        await item.locator(".el-select-dropdown__item").first.evaluate(
            "node => node.textContent = '2025年春季'"
        )

        with self.assertRaisesRegex(Exception, "没有 Excel 值的精确候选"):
            await listing._fill_attribute(
                "上市年份季节",
                item,
                "2026年秋季",
                required=True,
            )

    async def test_xhs_uses_unique_exact_dom_option_when_shared_select_skips_it(self):
        listing = await self._listing()
        items = await listing._attribute_items()
        item = items["厚薄"][1]

        with patch.object(
            listing, "_select_values", new=AsyncMock(return_value=None)
        ):
            actual = await listing._fill_attribute(
                "厚薄",
                item,
                "常规",
                required=False,
            )

        self.assertEqual(actual, ("常规",))
        self.assertEqual(
            await item.locator("input.el-input__inner").input_value(),
            "常规",
        )

    async def test_learning_uses_xhs_api_ids_and_dom_cross_check(self):
        class Runtime:
            def __init__(self):
                self.requests = []

            async def resolve(self, request):
                self.requests.append(request)
                chosen = next(
                    candidate
                    for candidate in request.candidates
                    if candidate.label == request.excel_value
                )
                return ResolvedAttribute(
                    chosen.value_id,
                    chosen.label,
                    "explicit_text",
                    "snapshot-xhs-1",
                )

        async def fulfill_fields(route):
            await route.fulfill(
                content_type="application/json",
                headers={"access-control-allow-origin": "*"},
                json={
                    "attributeV3s": [
                        {
                            "id": "thickness",
                            "name": "厚薄",
                            "isRequired": True,
                            "isMulti": False,
                            "customizable": False,
                        }
                    ]
                },
            )

        async def fulfill_values(route):
            await route.fulfill(
                content_type="application/json",
                headers={"access-control-allow-origin": "*"},
                json={
                    "attributeValueV3s": [
                        {"valueId": "regular", "valueName": "常规"},
                        {"valueId": "thick", "valueName": "加厚"},
                    ]
                },
            )

        runtime = Runtime()
        listing = await self._listing()
        listing.attribute_runtime = runtime
        await self.page.route("**/xhs/getAttributeList.json*", fulfill_fields)
        await self.page.route("**/xhs/getAttributeValues.json*", fulfill_values)
        await self.page.evaluate(
            "fetch('https://api.test/xhs/getAttributeList.json?leafCategoryId=101')"
        )
        await self.page.evaluate(
            "fetch('https://api.test/xhs/getAttributeValues.json?attributeId=thickness')"
        )
        items = await listing._attribute_items()
        item = items["厚薄"][1]
        await item.locator("ul").evaluate(
            """node => {
              const option = document.createElement('li');
              option.className = 'el-select-dropdown__item';
              option.textContent = '加厚';
              option.onclick = () => choose(option);
              node.append(option);
            }"""
        )
        await item.locator(".el-select-dropdown__item").evaluate_all(
            """nodes => nodes.forEach(node => {
              const label = node.textContent.trim();
              node.__vue__ = {value: label, created: false};
            })"""
        )

        actual = await listing._fill_attribute(
            "厚薄",
            item,
            "常规",
            required=True,
        )

        self.assertEqual(actual, ("常规",))
        self.assertEqual(len(runtime.requests), 1)
        request = runtime.requests[0]
        self.assertEqual(request.platform_id, "xhs")
        self.assertEqual(request.category_leaf_id, "101")
        self.assertEqual(request.field_id, "thickness")
        self.assertEqual(request.excel_value, "常规")
        self.assertEqual(
            tuple((item.value_id, item.label) for item in request.candidates),
            (("regular", "常规"), ("thick", "加厚")),
        )

    async def _reviewed_season_listing(
        self, runtime, *, multi=True, choices=None
    ):
        choices = choices or tuple(
            ("season-" + label, label) for label in ("春", "夏", "秋", "冬")
        )
        listing = await self._listing()
        listing.attribute_runtime = runtime
        await self.page.locator("[role=tabpanel]").evaluate(
            """(root, {multi, choices}) => {
              const item = document.createElement('div');
              item.className = 'complex-item';
              item.id = 'reviewed-season';
              item.innerHTML = `<div class="el-form-item"><label class="el-form-item__label">适用季节</label>
                <div class="el-form-item__content"><div class="el-select">
                  ${multi ? '<div class="el-select__tags"><input class="el-select__input" onclick="openSelect(this)"></div>' : ''}
                  <input class="el-input__inner" readonly onclick="openSelect(this)">
                  <div class="el-select-dropdown" style="display:none"><ul></ul></div>
                </div></div></div>`;
              root.appendChild(item);
              for (const [value, label] of choices) {
                const row = document.createElement('li');
                row.className = 'el-select-dropdown__item';
                row.textContent = label;
                row.__vue__ = {value, created: false};
                row.onclick = () => {
                  if (!multi) { choose(row); return; }
                  const tags = item.querySelector('.el-select__tags');
                  if ([...tags.querySelectorAll('.el-tag')].some(tag => tag.textContent === label)) return;
                  const tag = document.createElement('span');
                  tag.className = 'el-tag';
                  tag.textContent = label;
                  const close = document.createElement('i');
                  close.className = 'el-tag__close';
                  close.onclick = () => tag.remove();
                  tag.appendChild(close);
                  tags.prepend(tag);
                };
                item.querySelector('ul').appendChild(row);
              }
            }""",
            {"multi": multi, "choices": choices},
        )
        await self.page.route(
            "**/xhs/getAttributeList.json*",
            lambda route: route.fulfill(
                headers={"access-control-allow-origin": "*"},
                json={"attributeV3s": [{
                    "id": "season", "name": "适用季节", "isMulti": multi,
                    "isRequired": True, "customizable": False,
                }]},
            ),
        )
        await self.page.route(
            "**/xhs/getAttributeValues.json*",
            lambda route: route.fulfill(
                headers={"access-control-allow-origin": "*"},
                json={"attributeValueV3s": [
                    {"valueId": value_id, "valueName": label}
                    for value_id, label in choices
                ]},
            ),
        )
        await self.page.evaluate("""async () => {
          await fetch('https://api.test/xhs/getAttributeList.json?leafCategoryId=377119554');
          await fetch('https://api.test/xhs/getAttributeValues.json?attributeId=season');
        }""")
        return listing, self.page.locator("#reviewed-season")

    async def test_reviewed_multiple_seasons_are_refilled_as_separate_choices(self):
        class Client:
            def post_event(self, key, event_type, payload):
                return {"event_id": key}

            def review_learning(self, request):
                return {
                    "status": "review_required",
                    "reason_code": "review_learning_insufficient",
                }

        with tempfile.TemporaryDirectory() as directory:
            store = LearningStore(Path(directory) / "learning.sqlite3")
            store.migrate()
            runtime = AttributeRuntime(store, Client(), "run", "NGBL-2097")
            try:
                runtime.begin_review_collection("xhs")
                listing, item = await self._reviewed_season_listing(runtime)
                self.assertIsNone(
                    await listing._fill_attribute("适用季节", item, "四季通用")
                )
                self.assertEqual(len(runtime.deferred_reviews), 1)
                review = runtime.deferred_reviews[0]
                store.save_review_resolution(
                    product_version="NGBL-2097", platform_id="xhs",
                    snapshot_version=review.snapshot_version, review_id=review.review_id,
                    final_value_id="season-春,season-夏,season-冬,season-秋",
                )
                runtime.begin_review_collection("xhs")
                actual = await listing._fill_attribute("适用季节", item, "四季通用")
                self.assertCountEqual(actual, ("春", "夏", "冬", "秋"))
                self.assertFalse(runtime.has_deferred_reviews)
                self.assertEqual(review.request.control_type, "multi_select")
            finally:
                await runtime.drain()
                store.close()

    async def test_single_select_keeps_comma_in_reviewed_option_label(self):
        runtime = Mock()
        runtime.reusable_choice.return_value = ResolvedAttribute(
            "combined", "春，夏", "human", "snapshot-season"
        )
        runtime.resolve = AsyncMock(return_value=runtime.reusable_choice.return_value)
        listing, item = await self._reviewed_season_listing(
            runtime, multi=False, choices=(("combined", "春，夏"), ("winter", "冬"))
        )

        actual = await listing._fill_attribute("适用季节", item, "四季通用")

        self.assertEqual(actual, ("春，夏",))
        self.assertEqual(runtime.resolve.call_args.args[0].control_type, "select")

    async def test_multiselect_deduplicates_reviewed_choices_across_excel_groups(self):
        runtime = Mock()
        runtime.reusable_choice.return_value = ResolvedAttribute(
            "season-春,season-夏", "春,夏", "human", "snapshot-season"
        )
        runtime.resolve = AsyncMock(return_value=runtime.reusable_choice.return_value)
        listing, item = await self._reviewed_season_listing(runtime)

        actual = await listing._fill_attribute("适用季节", item, "春,夏")

        self.assertCountEqual(actual, ("春", "夏"))
        self.assertEqual(await item.locator(".el-tag").count(), 2)
        self.assertEqual(runtime.reusable_choice.call_count, 2)
        self.assertTrue(all(
            call.args[0].control_type == "multi_select"
            for call in runtime.reusable_choice.call_args_list
        ))

    async def test_xhs_clicks_one_exact_created_remote_option_before_it_disappears(self):
        listing = await self._listing()
        items = await listing._attribute_items()
        item = items["厚薄"][1]
        await item.locator(".el-select-dropdown__item").first.evaluate(
            "node => node.__vue__ = {created: true, value: '常规'}"
        )

        with patch.object(
            listing,
            "_select_unique_exact_dom_option",
            new=AsyncMock(side_effect=AssertionError("fallback should not run")),
        ):
            actual = await listing._fill_attribute(
                "厚薄",
                item,
                "常规",
                required=False,
            )

        self.assertEqual(actual, ("常规",))


if __name__ == "__main__":
    unittest.main()
