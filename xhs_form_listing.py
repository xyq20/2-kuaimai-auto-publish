"""DOM writer for FastMai's Xiaohongshu product-information tab.

The adapter stops after filling and validating the form.  It intentionally has
no save or publish method; the common runner owns the final write gate.
"""

from __future__ import annotations

from field_policies import positional_listing_date_value, without_color_attributes

from store_freight import sync_store_freight

import asyncio
import json
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from attribute_runtime import AttributeRequest, split_multi_choice
from category_profile import (
    category_gender_roots,
    category_input_hints,
    category_input_levels,
    category_input_variants,
    category_path_levels,
    category_search_terms,
    is_footwear_category,
)
from category_search import CategorySearchCapture
from canonical_fields import is_learning_managed_field
from learning_models import CandidateValue, canonical_sha256
from money_values import MoneyValueError, normalize_money_value
from platform_discovery import ApiClient, EndpointSpec, PlatformDiscoveryError
from platform_candidate_source import (
    CandidateSourceError,
    DomCandidate,
    reconcile_candidates,
)
from platform_schema import FieldOption, field_options, sanitize_message
from taobao_listing import (
    TaobaoListing,
    TaobaoListingError,
    excel_aliases,
    material_name_groups,
    normalize_label,
    normalize_option,
    parse_taobao_fabrics,
    parse_taobao_materials,
    material_value_groups,
    selection_value_groups,
    selection_value_groups_for_control,
)
from xhs_data import XhsFields
from xhs_listing import XhsListing, normalize_xhs_attribute_options


class XhsFormListingError(RuntimeError):
    """A Xiaohongshu form problem that can be shown directly to the operator."""


XHS_INHERITED_FIELDS = frozenset(
    normalize_label(value)
    for value in ("商品分类", "商品标题", "商品名称", "商品英文名", "品牌", "货号")
)
XHS_FIELD_ALIASES: Mapping[str, Tuple[str, ...]] = {
    normalize_label("服装版型"): (
        normalize_label("服饰版型"),
        normalize_label("版型"),
    ),
    normalize_label("服饰版型"): (
        normalize_label("服装版型"),
        normalize_label("版型"),
    ),
    normalize_label("厚薄"): (normalize_label("厚度"),),
    normalize_label("面料"): (
        normalize_label("面料材质"),
        normalize_label("面料俗称"),
    ),
    normalize_label("材质成分"): (normalize_label("材质"),),
    normalize_label("上市年份季节"): (normalize_label("上市时节"),),
}
XHS_BATCH_ORDER = ("售价", "市场价", "库存")
XHS_ATTRIBUTE_END_HEADINGS = re.compile(r"^\s*价格库存\s*[：:]?\s*$")
XHS_CATEGORY_QUERY_PATH = "/category/base/queryCategoryList.json"
XHS_ATTRIBUTE_LIST_PATH = "/xhs/getAttributeList.json"
XHS_ATTRIBUTE_VALUES_PATH = "/xhs/getAttributeValues.json"
XHS_CATEGORY_RESULT_SELECTOR = (
    "[data-xhs-category]:visible, "
    ".el-popover.el-popper:visible .categoryList-wrap > .text.item:visible, "
    ".el-autocomplete-suggestion:visible li:visible, "
    ".el-autocomplete-suggestion:visible [role=option]:visible, "
    ".el-autocomplete-suggestion:visible .el-autocomplete-suggestion__item:visible"
)
XHS_IDENTITY_CONTROL_SELECTOR = (
    'input:not([type="hidden"]):not([readonly]):not([disabled]):visible, '
    'textarea:not([readonly]):not([disabled]):visible'
)


def _numeric_equal(actual: str, expected: str) -> bool:
    try:
        return Decimal(actual.replace(",", "")) == Decimal(expected.replace(",", ""))
    except (InvalidOperation, ValueError):
        return False


def _is_nonnegative_decimal(value: str) -> bool:
    try:
        return Decimal(value) >= 0
    except (InvalidOperation, ValueError):
        return False


def _required_excel_value(
    fields: Mapping[str, str], aliases: Sequence[str], label: str, *, money: bool = False
) -> str:
    wanted = {normalize_label(alias) for alias in aliases}
    matches = [
        (str(key), str(value).strip())
        for key, value in fields.items()
        if wanted.intersection(excel_aliases(key)) and str(value).strip()
    ]
    if not matches:
        raise XhsFormListingError(
            "Excel 中缺少小红书{0}字段（可识别：{1}）".format(
                label, "/".join(aliases)
            )
        )
    if money:
        try:
            matches = [
                (key, normalize_money_value(value)) for key, value in matches
            ]
        except MoneyValueError as exc:
            raise XhsFormListingError(f"Excel 小红书{label}{exc}") from exc
    distinct = {value for _key, value in matches}
    if len(distinct) != 1:
        raise XhsFormListingError(
            "小红书{0}匹配到多个 Excel 字段：{1}".format(
                label, "、".join(key for key, _value in matches)
            )
        )
    return matches[0][1]


def title_without_neigborl(value: object) -> str:
    """Remove only the brand token forbidden by Xiaohongshu's title rule."""
    title = str(value or "").strip()
    cleaned = re.sub(r"neigborl", "", title, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"([【\[（(])\s+", r"\1", cleaned)
    cleaned = re.sub(r"\s+([】\]）)])", r"\1", cleaned)
    cleaned = re.sub(r"([】\]）)])\s+", r"\1", cleaned)
    return cleaned.strip()


def _category_normalized(value: object) -> str:
    return re.sub(r"[\s>＞/／,，、;；]+", "", str(value or "")).casefold()


def _category_parts(value: object) -> Tuple[str, ...]:
    """Return the visible platform path as exact, ordered route segments."""
    return tuple(
        normalize_label(part)
        for part in re.split(r"[>＞/／]", str(value or ""))
        if normalize_label(part)
    )


def _contains_ordered_parts(
    candidate_parts: Sequence[str], expected_parts: Sequence[str]
) -> bool:
    """Exact segment matching only; never infer a category from a substring."""
    cursor = 0
    for expected in expected_parts:
        while cursor < len(candidate_parts) and candidate_parts[cursor] != expected:
            cursor += 1
        if cursor == len(candidate_parts):
            return False
        cursor += 1
    return True


class XhsFormListing(TaobaoListing):
    """Fill Xiaohongshu category data, presale, batch fields and 3:4 main art."""

    attribute_platform_id = "xhs"
    freight_platform_id = "xhs"

    # XHS remote selects render an exact server-search result as an Element
    # ``created`` option even though the control is not free-form.  It is safe
    # to click only because the inherited matcher still requires one exact,
    # visible and enabled DOM option, followed by value readback.
    allow_created_exact_dom_option = True

    def __init__(
        self,
        page: Any,
        drawer: Any,
        logger: Any,
        *,
        attribute_runtime: Optional[Any] = None,
        category_hints: Sequence[str] = (),
    ) -> None:
        super().__init__(
            page,
            drawer,
            logger,
            attribute_runtime=attribute_runtime,
            category_hints=category_hints,
        )
        self.base_item_id = ""
        self._xhs_attribute_readback_source = "dom"
        self._xhs_api_generation = 0
        self._xhs_api_category_id = ""
        self._xhs_api_shop_id = ""
        self._xhs_api_fields: Dict[str, Tuple[Mapping[str, Any], ...]] = {}
        self._xhs_api_options: Dict[Tuple[int, str], Tuple[FieldOption, ...]] = {}
        self._xhs_active_option_attempts: set[Tuple[int, str]] = set()
        self._xhs_request_contexts: Dict[int, Tuple[str, int, str]] = {}
        self._xhs_response_tasks: set[asyncio.Task[Any]] = set()
        self._install_xhs_attribute_listener()

    @staticmethod
    def _request_parameter(request: Any, name: str) -> str:
        try:
            query = parse_qs(urlsplit(str(request.url)).query, keep_blank_values=True)
            values = [str(value).strip() for value in query.get(name, ()) if str(value).strip()]
        except Exception:
            values = []
        if len(values) == 1:
            return values[0]
        try:
            form = parse_qs(str(request.post_data or ""), keep_blank_values=True)
            values = [str(value).strip() for value in form.get(name, ()) if str(value).strip()]
        except Exception:
            values = []
        if len(values) == 1:
            return values[0]
        try:
            payload = getattr(request, "post_data_json", None)
            if callable(payload):
                payload = payload()
            if not isinstance(payload, Mapping):
                payload = json.loads(str(request.post_data or "{}"))
            candidates = []
            if isinstance(payload, Mapping):
                candidates.append(payload.get(name))
                for container_name in ("params", "data", "body"):
                    container = payload.get(container_name)
                    if isinstance(container, Mapping):
                        candidates.append(container.get(name))
            values = [
                str(value).strip()
                for value in candidates
                if value not in (None, "") and str(value).strip()
            ]
        except Exception:
            values = []
        return values[0] if len(values) == 1 else ""

    def _install_xhs_attribute_listener(self) -> None:
        if not hasattr(self.page, "on"):
            return

        def on_request(request: Any) -> None:
            try:
                path = urlsplit(str(request.url)).path
            except Exception:
                return
            if path == XHS_ATTRIBUTE_LIST_PATH:
                category_id = self._request_parameter(request, "leafCategoryId")
                if not category_id:
                    return
                self._xhs_api_generation += 1
                self._xhs_api_category_id = category_id
                self._xhs_api_shop_id = self._request_parameter(request, "shopId")
                self._xhs_api_fields = {}
                self._xhs_api_options = {}
                self._xhs_active_option_attempts = set()
                self._xhs_request_contexts[id(request)] = (
                    "fields",
                    self._xhs_api_generation,
                    category_id,
                )
            elif path == XHS_ATTRIBUTE_VALUES_PATH:
                attribute_id = next(
                    (
                        self._request_parameter(request, key)
                        for key in ("attributeId", "attributeV3Id", "attrId", "id")
                        if self._request_parameter(request, key)
                    ),
                    "",
                )
                if self._xhs_api_generation and attribute_id:
                    self._xhs_request_contexts[id(request)] = (
                        "values",
                        self._xhs_api_generation,
                        attribute_id,
                    )
                elif self.logger is not None:
                    try:
                        query_keys = sorted(
                            parse_qs(
                                urlsplit(str(request.url)).query,
                                keep_blank_values=True,
                            ).keys()
                        )
                    except Exception:
                        query_keys = []
                    try:
                        body = getattr(request, "post_data_json", None)
                        if callable(body):
                            body = body()
                        body_keys = sorted(body.keys()) if isinstance(body, Mapping) else []
                    except Exception:
                        body_keys = []
                    self.logger.warning(
                        "小红书候选接口请求未识别字段 ID：method=%s，query_keys=%s，body_keys=%s",
                        getattr(request, "method", ""),
                        query_keys,
                        body_keys,
                    )

        def on_response(response: Any) -> None:
            request = getattr(response, "request", None)
            context = self._xhs_request_contexts.pop(id(request), None)
            if context is None:
                return
            task = asyncio.create_task(self._consume_xhs_attribute_response(response, context))
            self._xhs_response_tasks.add(task)
            task.add_done_callback(self._xhs_response_tasks.discard)

        def on_request_failed(request: Any) -> None:
            context = self._xhs_request_contexts.pop(id(request), None)
            if context is not None and self.logger is not None:
                kind, generation, identity = context
                if generation == self._xhs_api_generation:
                    self.logger.warning(
                        "小红书属性接口连接失败：kind=%s，字段/类目=%s，shopId_present=%s，failure=%s",
                        kind, identity, bool(self._request_parameter(request, "shopId")),
                        sanitize_message(getattr(request, "failure", "")),
                    )

        self.page.on("request", on_request)
        self.page.on("response", on_response)
        self.page.on("requestfailed", on_request_failed)

    async def _consume_xhs_attribute_response(
        self,
        response: Any,
        context: Tuple[str, int, str],
    ) -> None:
        kind, generation, identity = context
        if generation != self._xhs_api_generation:
            return
        try:
            payload = await response.json()
        except Exception:
            if self.logger is not None:
                self.logger.warning(
                    "小红书属性接口未返回 JSON：kind=%s，字段/类目=%s，HTTP=%s",
                    kind, identity, getattr(response, "status", None),
                )
            return
        if generation != self._xhs_api_generation:
            return
        error = self._xhs_api_error(payload, getattr(response, "status", 200))
        if error:
            if self.logger is not None:
                self.logger.warning(
                    "小红书属性接口失败：kind=%s，字段/类目=%s，shopId_present=%s，%s",
                    kind, identity,
                    bool(self._request_parameter(getattr(response, "request", None), "shopId")),
                    error,
                )
            return
        data = self._xhs_payload_data(payload)
        if not isinstance(data, Mapping):
            return
        if kind == "fields":
            raw_fields = data.get("attributeV3s")
            if not isinstance(raw_fields, Sequence) or isinstance(raw_fields, (str, bytes)):
                return
            records: Dict[str, List[Mapping[str, Any]]] = {}
            for raw_field in raw_fields:
                if not isinstance(raw_field, Mapping):
                    continue
                field_id = str(raw_field.get("id") or "").strip()
                label = str(raw_field.get("name") or raw_field.get("label") or "").strip()
                if not field_id or not label:
                    continue
                records.setdefault(normalize_label(label), []).append(
                    {
                        "id": field_id,
                        "label": label,
                        "custom_allowed": raw_field.get("customizable") is True,
                    }
                )
                inline_values = raw_field.get("values")
                if isinstance(inline_values, Sequence) and not isinstance(
                    inline_values, (str, bytes)
                ):
                    self._xhs_api_options[(generation, field_id)] = field_options(
                        normalize_xhs_attribute_options(inline_values),
                        source="api",
                    )
            self._xhs_api_fields = {
                key: tuple(value) for key, value in records.items()
            }
            return

        raw_values = data.get("values")
        if not isinstance(raw_values, Sequence) or isinstance(
            raw_values, (str, bytes)
        ):
            raw_values = data.get("attributeValueV3s")
        if not isinstance(raw_values, Sequence) or isinstance(raw_values, (str, bytes)):
            raw_values = ()
        options = field_options(
            normalize_xhs_attribute_options(raw_values),
            source="api",
        )
        self._xhs_api_options[(generation, identity)] = options
        if not options and self.logger is not None:
            self.logger.warning(
                "小红书候选接口返回未含候选数组：字段 %s，data_keys=%s",
                identity,
                sorted(data.keys()),
            )

    @staticmethod
    def _xhs_api_error(payload: Any, http_status: int = 200) -> str:
        current = payload
        for _attempt in range(4):
            if not isinstance(current, Mapping):
                break
            failed = (
                ("result" in current and str(current["result"]) not in {"1", "True"})
                or current.get("success") is False
                or ("code" in current and "data" in current
                    and str(current["code"]).upper() not in {"0", "1", "200", "OK", "SUCCESS"})
            )
            if failed or not 200 <= http_status < 300:
                return "HTTP={0}，result={1}，code={2}，message={3}".format(
                    http_status, sanitize_message(current.get("result")),
                    sanitize_message(current.get("code")),
                    sanitize_message(current.get("message") or current.get("msg") or current.get("errorMessage")),
                )
            current = current.get("data")
        return "HTTP={0}".format(http_status) if not 200 <= http_status < 300 else ""

    @staticmethod
    def _xhs_payload_data(payload: Any) -> Mapping[str, Any]:
        current = payload
        for _attempt in range(4):
            if not isinstance(current, Mapping):
                return {}
            if "result" in current and "data" in current:
                if current.get("result") is True or str(current.get("result")) == "1":
                    current = current.get("data")
                    continue
                return {}
            if isinstance(current.get("success"), bool) and "data" in current:
                if current.get("success"):
                    current = current.get("data")
                    continue
                return {}
            if "code" in current and "data" in current:
                if str(current.get("code")).upper() in {
                    "0", "1", "200", "OK", "SUCCESS"
                }:
                    current = current.get("data")
                    continue
                return {}
            if "data" in current and isinstance(current.get("data"), Mapping):
                current = current.get("data")
                continue
            return current
        return current if isinstance(current, Mapping) else {}

    async def _request_xhs_attribute_options(
        self, field_id: str
    ) -> Tuple[FieldOption, ...]:
        """Read one missing option list without depending on a UI refetch."""
        try:
            payload = await self.page.evaluate(
                """async ({shopId, attributeId}) => {
                  const url = new URL('/xhs/getAttributeValues.json', window.location.origin);
                  url.searchParams.set('shopId', shopId || '');
                  url.searchParams.set('attributeId', attributeId);
                  url.searchParams.set('api_name', 'xhs_getAttributeValues');
                  const response = await fetch(url.toString(), {
                    credentials: 'same-origin',
                    headers: {'Accept': 'application/json'}
                  });
                  if (!response.ok) throw new Error(`HTTP ${response.status}`);
                  return await response.json();
                }""",
                {
                    "shopId": self._xhs_api_shop_id,
                    "attributeId": field_id,
                },
            )
        except Exception as exc:
            if self.logger is not None:
                self.logger.warning(
                    "小红书属性候选主动读取失败（字段 %s）：%s",
                    field_id,
                    exc,
                )
            return ()
        error = self._xhs_api_error(payload)
        if error:
            if self.logger is not None:
                self.logger.warning(
                    "小红书候选主动读取接口失败：字段 %s，%s", field_id, error,
                )
            return ()
        data = self._xhs_payload_data(payload)
        raw_values = data.get("values")
        if not isinstance(raw_values, Sequence) or isinstance(
            raw_values, (str, bytes)
        ):
            raw_values = data.get("attributeValueV3s")
        if not isinstance(raw_values, Sequence) or isinstance(
            raw_values, (str, bytes)
        ):
            raw_values = ()
        options = field_options(
            normalize_xhs_attribute_options(raw_values), source="api"
        )
        if options:
            self._xhs_api_options[(self._xhs_api_generation, field_id)] = options
        elif self.logger is not None:
            self.logger.warning(
                "小红书候选主动读取未含候选数组：字段 %s，data_keys=%s",
                field_id,
                sorted(data.keys()),
            )
        return options

    async def _drain_xhs_attribute_tasks(self) -> None:
        await asyncio.sleep(0)
        while self._xhs_response_tasks:
            await asyncio.gather(*tuple(self._xhs_response_tasks), return_exceptions=True)

    async def _captured_xhs_field(
        self,
        page_label: str,
        *,
        timeout_seconds: float = 3.0,
    ) -> Tuple[Mapping[str, Any], Tuple[FieldOption, ...], str]:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            await self._drain_xhs_attribute_tasks()
            records = self._xhs_api_fields.get(normalize_label(page_label), ())
            if len(records) == 1:
                field_id = str(records[0].get("id") or "")
                options = self._xhs_api_options.get(
                    (self._xhs_api_generation, field_id),
                    (),
                )
                attempt_key = (self._xhs_api_generation, field_id)
                if (
                    not options
                    and field_id
                    and self._xhs_api_category_id
                    and attempt_key not in self._xhs_active_option_attempts
                ):
                    self._xhs_active_option_attempts.add(attempt_key)
                    options = await self._request_xhs_attribute_options(field_id)
                if options and self._xhs_api_category_id:
                    return records[0], options, self._xhs_api_category_id
            await asyncio.sleep(0.05)
        records = self._xhs_api_fields.get(normalize_label(page_label), ())
        raise XhsFormListingError(
            "小红书属性“{0}”缺少唯一且完整的接口 JSON 候选：字段数 {1}".format(
                page_label,
                len(records),
            )
        )

    async def _wait_for_loading_masks(self, timeout_seconds: float = 45) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            try:
                if await self.drawer.locator(".el-loading-mask:visible").count() == 0:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.1)
        raise XhsFormListingError("小红书资料加载遮罩在 45 秒内未消失")

    async def open(self) -> "XhsFormListing":
        tab = self.drawer.get_by_role("tab", name="小红书资料", exact=True)
        try:
            await tab.wait_for(state="visible", timeout=30_000)
            if (await tab.get_attribute("aria-selected") or "").casefold() != "true":
                await tab.click(timeout=30_000)
            panel = self.drawer.get_by_role("tabpanel", name="小红书资料", exact=True)
            await panel.wait_for(state="visible", timeout=30_000)
        except Exception as exc:
            raise XhsFormListingError("找不到可切换的“小红书资料”页签") from exc
        self.panel = panel
        await self._wait_for_loading_masks()
        if self.logger is not None:
            self.logger.info("小红书资料页签已打开")
        return self

    async def _category_dialog(self) -> Any:
        visible_dialogs = self.page.locator(".el-dialog:visible")
        matches = []
        for index in range(await visible_dialogs.count()):
            dialog = visible_dialogs.nth(index)
            if normalize_label(await dialog.inner_text()).startswith(normalize_label("修改类目")):
                matches.append(dialog)
        if len(matches) == 1:
            return matches[0]

        role_dialogs = self.page.get_by_role("dialog", name="修改类目", exact=True)
        visible_roles = []
        for index in range(await role_dialogs.count()):
            dialog = role_dialogs.nth(index)
            if await dialog.is_visible():
                visible_roles.append(dialog)
        if len(visible_roles) == 1:
            return visible_roles[0]
        raise XhsFormListingError("小红书修改类目弹窗不是唯一项：{0}".format(len(matches)))

    async def _category_search_input(self, dialog: Any) -> Any:
        inputs = dialog.locator('input:not([type="hidden"]):visible')
        if await inputs.count() != 1:
            raise XhsFormListingError(
                "小红书修改类目弹窗搜索框不是唯一项：{0}".format(await inputs.count())
            )
        return inputs.first

    async def _category_result_nodes(self) -> List[Tuple[Any, str]]:
        """Read category search rows from the page-level Element portal.

        Element mounts the result list next to ``body`` rather than beneath the
        ``修改类目`` dialog.  Looking only inside the dialog made a visible
        result list appear empty to the automation.  Keep this list narrow to
        real option rows, rather than searching arbitrary dialog text.
        """
        nodes = self.page.locator(XHS_CATEGORY_RESULT_SELECTOR)
        found: List[Tuple[Any, str]] = []
        seen = set()
        for index in range(await nodes.count()):
            node = nodes.nth(index)
            text = " ".join((await node.inner_text()).split())
            key = _category_normalized(text)
            if not key or key in seen:
                continue
            seen.add(key)
            found.append((node, text))
        return found

    @staticmethod
    def _choose_category_text(
        result_texts: Sequence[str], category_path: Sequence[str]
    ) -> Tuple[str, str]:
        """Select a single exact platform route from the Excel category path.

        A page may return a ready-made platform path such as
        ``男装 > 休闲裤 > 工装休闲裤``.  Prefer a complete ordered match.  Some
        source workbooks include an internal middle category that Xiaohongshu
        does not expose; in that case an exact first segment plus an exact,
        unique final leaf is still deterministic and is explicitly recorded in
        the report.  Any duplicate or incomplete result remains an error.
        """
        expression = " > ".join(
            str(part).strip() for part in category_path if str(part).strip()
        )
        expected_levels = category_input_levels(expression)
        expected_variants = tuple(
            tuple(normalize_label(part) for part in variant)
            for variant in category_input_variants(expression)
        )
        if len(expected_levels) > 1 and any(
            "男鞋" in normalize_label(option) for option in expected_levels[0]
        ):
            result_texts = tuple(
                text for text in result_texts if len(_category_parts(text)) > 1
            )

        def candidate_levels(text: str):
            return category_path_levels(text)

        def contains_ordered(levels, expected):
            cursor = 0
            for wanted in expected:
                while cursor < len(levels) and wanted not in levels[cursor]:
                    cursor += 1
                if cursor == len(levels):
                    return False
                cursor += 1
            return True

        gender_roots = {
            normalize_label(root)
            for root in category_gender_roots(category_input_hints(expression))
        }

        # Some workbooks flatten a real category route into one slash-delimited
        # cell (for example ``休闲裤/男士休闲直筒裤/工装休闲裤``).  Slash still
        # means an authored alternative in general, but when the first leaf is
        # contained by the final leaf it is a broad alias for the same family.
        # Do not stop at that broad alias; wait for the concrete final leaf so
        # a search result from an unrelated branch cannot win early.
        preferred_flat_leaf = None
        if len(expected_levels) == 1 and len(expected_levels[0]) > 1:
            first_leaf = normalize_label(expected_levels[0][0])
            final_leaf = normalize_label(expected_levels[0][-1])
            if first_leaf and final_leaf != first_leaf and first_leaf in final_leaf:
                preferred_flat_leaf = final_leaf

        def has_gender(levels):
            return not gender_roots or any(
                root in option
                for level in levels
                for option in level
                for root in gender_roots
            )

        for expected in expected_variants:
            complete = [
                text for text in result_texts
                if has_gender(candidate_levels(text))
                and candidate_levels(text)
                and expected[-1] in candidate_levels(text)[-1]
                and (
                    preferred_flat_leaf is None
                    or expected[-1] == preferred_flat_leaf
                )
                and contains_ordered(candidate_levels(text), expected)
            ]
            if len(complete) == 1:
                return complete[0], "full_ordered_path"
            if len(complete) > 1:
                raise XhsFormListingError(
                    "小红书类目完整路径不是唯一精确项：{0}".format(len(complete))
                )

        # Platform taxonomies can omit a supplier-internal middle layer.  This
        # fallback still requires both Excel endpoints to be exact and unique;
        # it never selects merely because a string contains a category word.
        if len(expected_levels) >= 2:
            for leaf in expected_levels[-1]:
                root_leaf = [
                    text
                    for text in result_texts
                    if candidate_levels(text)
                    and has_gender(candidate_levels(text))
                    and (
                        preferred_flat_leaf is None
                        or normalize_label(leaf) == preferred_flat_leaf
                    )
                    and any(
                        normalize_label(option) in candidate_level
                        for candidate_level in candidate_levels(text)
                        for option in expected_levels[0]
                    )
                    and normalize_label(leaf) in candidate_levels(text)[-1]
                ]
                if len(root_leaf) == 1:
                    return root_leaf[0], "first_and_leaf_exact"
                if len(root_leaf) > 1:
                    raise XhsFormListingError(
                        "小红书类目首段和末级叶子不是唯一精确项：{0}".format(
                            len(root_leaf)
                        )
                    )

        # New-category workbooks may list cross-platform category hints rather
        # than one marketplace path.  Try exact leaves in authored order;
        # every accepted leaf must still be unique in the platform JSON.
        # New Excel expressions keep slash alternatives in the final level.
        # A legacy caller may still pass an already-expanded tuple; only that
        # compatibility shape searches every hint, while real expressions stay
        # constrained to their declared leaf alternatives.
        has_peer_alternatives = any(
            "/" in str(part) or "／" in str(part)
            for part in category_path
            if str(part).strip()
        )
        if has_peer_alternatives and expected_levels:
            # In the new representation the final ``>`` level contains the
            # authored same-level alternatives.  Keep that order so the first
            # exact candidate wins deterministically.
            fallback_hints = expected_levels[-1]
        elif expected_levels:
            # Legacy callers pass a flat hint tuple. Keep its authored order.
            fallback_hints = category_search_terms(category_path)
        else:
            fallback_hints = category_path
        for hint in fallback_hints:
            wanted = normalize_label(hint)
            leaf_matches = [
                text
                for text in result_texts
                if candidate_levels(text)
                and has_gender(candidate_levels(text))
                and (
                    preferred_flat_leaf is None
                    or wanted == preferred_flat_leaf
                )
                and wanted in candidate_levels(text)[-1]
            ]
            # Excel 类目有时携带“男士/女士”等供应商层级，而平台结果
            # 只保留“男装/女装”根类目。用人群根类目消除同名末级项，
            # 例如“卫衣”同时出现在女装、童装、运动和男装下。
            if len(leaf_matches) > 1 and gender_roots:
                narrowed = [
                    text for text in leaf_matches
                    if any(
                        root in option
                        for level in candidate_levels(text)
                        for option in level
                        for root in gender_roots
                    )
                ]
                if len(narrowed) == 1:
                    return narrowed[0], "gender_root_exact_hint_leaf"
            if len(leaf_matches) == 1:
                return leaf_matches[0], "unique_exact_hint_leaf"
            if len(leaf_matches) > 1:
                raise XhsFormListingError(
                    "小红书类目提示词末级不是唯一精确项：{0}={1}".format(
                        hint, len(leaf_matches)
                    )
                )
        raise XhsFormListingError("小红书类目搜索结果中没有 Excel 的完整路径或唯一精确提示词")

    @staticmethod
    def _category_paths_from_json(payload: Any) -> Tuple[str, ...]:
        """Extract only display paths from FastMai's read-only category JSON."""
        if not isinstance(payload, Mapping):
            return ()
        data = payload.get("data")
        if not isinstance(data, Mapping):
            return ()
        records = data.get("records")
        if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
            return ()
        paths: List[str] = []
        for record in records:
            if not isinstance(record, Mapping):
                continue
            if record.get("leaf") in (0, "0", False):
                continue
            raw_parts = record.get("categoryNameList")
            if not isinstance(raw_parts, Sequence) or isinstance(
                raw_parts, (str, bytes)
            ):
                continue
            parts = tuple(str(part).strip() for part in raw_parts if str(part).strip())
            if parts:
                rendered = " > ".join(parts)
                if rendered not in paths:
                    paths.append(rendered)
        return tuple(paths)

    async def _wait_for_category_result(
        self, category_path: Sequence[str], capture: CategorySearchCapture
    ) -> Tuple[Any, str, str]:
        timeout_seconds = getattr(self, "category_search_timeout_seconds", 12)
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        last_results: List[Tuple[Any, str]] = []
        while asyncio.get_running_loop().time() < deadline:
            if capture.error:
                raise XhsFormListingError("小红书类目搜索接口失败：" + capture.error)
            json_paths = capture.paths
            if capture.received and not json_paths:
                raise XhsFormListingError("小红书类目搜索接口返回空候选")
            last_results = await self._category_result_nodes()
            if json_paths:
                try:
                    target_text, strategy = self._choose_category_text(
                        json_paths, category_path
                    )
                except XhsFormListingError as exc:
                    # JSON ambiguity is already complete evidence: do not
                    # wait for a DOM race to choose an arbitrary row.
                    if "不是唯一" in str(exc):
                        raise
                    raise XhsFormListingError("小红书类目当前搜索没有匹配候选") from exc
                matches = [
                    (node, text)
                    for node, text in last_results
                    if _category_normalized(text) == _category_normalized(target_text)
                ]
                if len(matches) == 1:
                    node, text = matches[0]
                    return node, text, strategy
                if len(matches) > 1:
                    raise XhsFormListingError(
                        "小红书类目 JSON 对应的 DOM 行不是唯一项：{0}".format(
                            len(matches)
                        )
                    )
            await asyncio.sleep(0.15)
        if not capture.received:
            raise XhsFormListingError("小红书类目搜索 JSON 在 {0} 秒内未返回候选".format(timeout_seconds))
        rendered = "；".join(text for _node, text in last_results[:12])
        raise XhsFormListingError(
            "小红书类目 JSON 已定位，但对应 DOM 行未出现：{0}".format(
                rendered or "页面未返回候选"
            )
        )

    async def _click_category_segment(self, segment: str) -> None:
        """Fallback for a true cascader: click one exact visible child label."""
        deadline = asyncio.get_running_loop().time() + 45
        wanted = normalize_label(segment)
        while asyncio.get_running_loop().time() < deadline:
            results = await self._category_result_nodes()
            matches = [
                node
                for node, text in results
                if normalize_label(text) == wanted
            ]
            if len(matches) == 1:
                await matches[0].scroll_into_view_if_needed()
                await matches[0].click(timeout=10_000)
                await self._wait_for_loading_masks()
                return
            if len(matches) > 1:
                raise XhsFormListingError(
                    "小红书类目层级“{0}”不是唯一精确项：{1}".format(
                        segment, len(matches)
                    )
                )
            await asyncio.sleep(0.15)
        raise XhsFormListingError(
            "小红书类目搜索/下级菜单未出现精确项：{0}".format(segment)
        )

    @staticmethod
    def _selected_category_text(dialog_text: str) -> str:
        for line in reversed(dialog_text.splitlines()):
            match = re.match(r"^\s*已选\s*[：:]\s*(.*?)\s*$", line)
            if match:
                return match.group(1)
        return ""

    @staticmethod
    def _category_selection_matches(selected, path):
        """Use the same route check before clicking and before confirming."""
        levels = category_path_levels(selected)
        expression = " > ".join(path)
        variants = category_input_variants(expression)
        for variant in variants:
            cursor = 0
            for part in variant:
                wanted = normalize_label(part)
                while cursor < len(levels) and wanted not in levels[cursor]:
                    cursor += 1
                if cursor == len(levels):
                    break
                cursor += 1
            else:
                return True
        if not levels:
            return False
        expected = category_input_levels(expression)
        leaves = {normalize_label(option) for option in (expected[-1] if expected else ())}
        gender = {normalize_label(root) for root in category_gender_roots(category_input_hints(expression))}
        actual = {option for level in levels for option in level}
        root_match = bool(expected and any(normalize_label(option) in levels[0] for option in expected[0]))
        return bool(leaves & set(levels[-1])) and (root_match or not gender or bool(gender & actual))

    @staticmethod
    def _category_tree_scope(candidates, path):
        """Require an actual gender root, rather than a word in a luxury branch."""
        roots = {normalize_label(root) for root in category_gender_roots(category_input_hints(path))}
        if not roots:
            return tuple(candidates)
        # Explicit Excel roots distinguish men's clothing from men's shoes.
        hints = {normalize_label(hint) for hint in category_input_hints(path)}
        explicit_roots = roots.intersection(hints)
        if explicit_roots:
            roots = explicit_roots
        return tuple(
            candidate for candidate in candidates
            if roots.intersection(category_path_levels(candidate.path[:1])[0])
        )

    @staticmethod
    def _category_tree_review_candidates(candidates, path):
        """Keep all real child leaves of a matching family for semantic review."""
        levels = category_input_levels(path)
        terms = {normalize_label(term) for term in (levels[-1] if levels else ())}
        exact, related = [], []
        for candidate in candidates:
            labels = {option for level in category_path_levels(candidate.path[1:]) for option in level}
            if terms.intersection(labels):
                exact.append(candidate)
            elif any(term in label or label in term for term in terms for label in labels):
                related.append(candidate)
        # A broad parent such as 针织衫/毛衣 is evidence for the family,
        # never a substitute for the platform's final leaf.
        return tuple(exact or related or candidates)

    async def _click_category_tree_path(self, dialog: Any, path: Sequence[str]) -> None:
        for level, label in enumerate(path):
            menu = dialog.locator(".el-cascader-panel:visible .el-cascader-menu:visible").nth(level)
            try:
                await menu.wait_for(state="visible", timeout=12_000)
            except PlaywrightTimeoutError as exc:
                raise XhsFormListingError("小红书类目第 {0} 级菜单未显示：{1}".format(level + 1, label)) from exc
            deadline = asyncio.get_running_loop().time() + 12
            while True:
                matches = []
                nodes = menu.locator(".el-cascader-node:visible")
                for index in range(await nodes.count()):
                    node = nodes.nth(index)
                    node_label = node.locator(".el-cascader-node__label")
                    text = await (node_label if await node_label.count() == 1 else node).inner_text()
                    text = re.sub(r"\s*[(（]灰度[)）]\s*$", "", text.strip())
                    if normalize_label(text) == normalize_label(label):
                        if "is-disabled" not in (await node.get_attribute("class") or "") and await node.get_attribute("aria-disabled") != "true":
                            matches.append(node)
                if len(matches) > 1:
                    raise XhsFormListingError("小红书类目第 {0} 级同名节点不唯一：{1}".format(level + 1, label))
                if len(matches) == 1:
                    await matches[0].scroll_into_view_if_needed()
                    await matches[0].click(timeout=10_000)
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    raise XhsFormListingError("小红书类目树 JSON 对应的菜单节点未出现：" + label)
                await asyncio.sleep(.15)

    async def _confirm_category_path(self, dialog: Any, expected: str) -> str:
        deadline = asyncio.get_running_loop().time() + 10
        selected = ""
        while asyncio.get_running_loop().time() < deadline:
            selected = self._selected_category_text(await dialog.inner_text())
            if self._normalize_category_path(selected) == self._normalize_category_path(expected):
                break
            await asyncio.sleep(.1)
        else:
            raise XhsFormListingError("小红书类目确认前 JSON+DOM 路径不匹配：目标 {0}，页面为 {1!r}".format(expected, selected))
        confirm = dialog.get_by_role("button", name=re.compile(r"^\s*确\s*定\s*$"))
        if await confirm.count() != 1:
            raise XhsFormListingError("小红书修改类目弹窗找不到唯一“确定”按钮")
        await confirm.click(timeout=10_000)
        await dialog.wait_for(state="hidden", timeout=30_000)
        self.category_clicked = True
        await self._wait_for_loading_masks()
        return selected

    async def _apply_category_from_tree(
        self, dialog: Any, search: Any, path: Sequence[str]
    ) -> Optional[Mapping[str, Any]]:
        # Reuse discovery's API parser, including lazy-tree and budget guards.
        # Limit total discovery time as well as each individual API request.
        try:
            candidates = await asyncio.wait_for(XhsListing(panel=self.page).read_category_tree(), timeout=20)
        except (asyncio.TimeoutError, PlatformDiscoveryError) as exc:
            if self.logger is not None:
                self.logger.info("小红书完整类目树暂不可用，使用关键词搜索兼容：%s", sanitize_message(str(exc)))
            return None
        scoped = self._category_tree_scope(candidates, path)
        candidate_paths = tuple(" > ".join(candidate.path) for candidate in scoped)
        target_text, strategy = "", ""
        try:
            target_text, strategy = self._choose_category_text(candidate_paths, path)
            if not self._category_selection_matches(target_text, path):
                target_text = ""
        except XhsFormListingError:
            pass
        if target_text:
            runtime = self.attribute_runtime
            if runtime is not None and hasattr(runtime, "model_events"):
                from model_judgments import record_event, reason_text
                record_event(runtime, {
                    "key": "skip:xhs-category-tree:" + canonical_sha256((tuple(path), target_text)),
                    "platform_id": "xhs", "field_id": "category_path", "field_label": "商品类目",
                    "excel_value": " > ".join(path), "status": "skipped", "api_called": False,
                    "reason_code": "explicit_text", "reason": reason_text("explicit_text"),
                    "selected_labels": [target_text],
                }, sink=runtime.model_decision_sink, logger=self.logger)
        else:
            review_candidates = self._category_tree_review_candidates(scoped or candidates, path)
            target_text = await self._resolve_category_review(
                tuple(" > ".join(candidate.path) for candidate in review_candidates),
                source="xhs_category_tree", excel_value=" > ".join(path),
                evidence={"api_category_tree": True, "api_path": "/xhs/getCategoryTree.json",
                          "category_tree_complete": True, "live_dom": False,
                          "excel_category_hints": list(path),
                          "category_tree_leaf_count": len(candidates),
                          "category_tree_candidate_count": len(review_candidates),
                          "gender_root_not_found": bool(not scoped),
                          "selection_only": bool(not scoped)},
            )
            if not target_text:
                raise XhsFormListingError("小红书完整类目树无法确定末级类目，需要审核后继续")
            strategy = "review_learning"
        matched = [candidate for candidate in candidates if " > ".join(candidate.path) == target_text]
        if len(matched) != 1:
            raise XhsFormListingError("小红书类目判断结果不在唯一真实叶子范围内")
        chosen = matched[0]
        # No tree-wide DOM scan or Vue mutation: click only the selected route.
        if await search.input_value():
            await search.fill("", timeout=10_000)
        await search.blur()
        await self._click_category_tree_path(dialog, chosen.path)
        selected = await self._confirm_category_path(dialog, target_text)
        if self.logger is not None:
            self.logger.info("小红书完整类目树 JSON+DOM 校验通过：%s；规则=%s；真实叶子=%s", selected, strategy, chosen.leaf_id)
        return {"path": tuple(path), "clicked_segments": chosen.path, "selected": selected,
                "matched_result": target_text, "search_term": "", "strategy": strategy,
                "json_candidate_count": len(candidate_paths), "category_tree_id": chosen.leaf_id,
                "category_source": "api_tree"}

    async def apply_category(self, category_path: Sequence[str]) -> Mapping[str, Any]:
        """Resolve real tree leaves first; retain ordered keyword compatibility."""
        if self.panel is None:
            raise XhsFormListingError("请先调用 open() 打开小红书资料")
        path = tuple(str(item).strip() for item in category_path if str(item).strip())
        if not path:
            raise XhsFormListingError("Excel 小红书商品分类没有可选择的层级")
        modify = self.panel.get_by_role("button", name="修改类目", exact=True)
        if await modify.count() != 1:
            raise XhsFormListingError("小红书页面找不到唯一的“修改类目”按钮")
        started = asyncio.get_running_loop().time()
        reviewed_path = ""
        validation_errors = []
        await modify.click(timeout=30_000)
        dialog = await self._category_dialog()
        search = await self._category_search_input(dialog)
        tree_report = await self._apply_category_from_tree(dialog, search, path)
        if tree_report is not None:
            return tree_report
        async with CategorySearchCapture(self.page, self._category_paths_from_json) as capture:
            # Only the actual search-result portal is scanned.  The page also
            # keeps a large cascader tree in the DOM; enumerating that tree was
            # the source of a 25+ second delay and is not valid click evidence.
            expected_levels = category_input_levels(" > ".join(path))
            leaf_options = expected_levels[-1] if expected_levels else path
            search_terms = category_search_terms(leaf_options)
            observed_paths: List[str] = []
            observed_origins: Dict[str, str] = {}
            for search_term in search_terms:
                capture.start(search_term)
                await search.fill(search_term, timeout=10_000)
                search_filled_at = asyncio.get_running_loop().time()
                if self.logger is not None:
                    self.logger.info("小红书类目按 Excel 顺序搜索：%s", search_term)
                try:
                    target, target_text, strategy = await self._wait_for_category_result(path, capture)
                    if not self._category_selection_matches(target_text, path):
                        validation_errors.append({"expected": " > ".join(path), "actual": target_text,
                                                  "reason": "完整类目路径校验不匹配"})
                        if self.logger is not None:
                            self.logger.info("小红书类目候选路径校验不匹配：%s；继续 Excel 备选，全部无法确定时请求模型辅助校验", target_text)
                        raise XhsFormListingError("小红书类目当前搜索没有匹配候选")
                    break
                except XhsFormListingError as exc:
                    for candidate_path in capture.paths:
                        normalized_path = self._normalize_category_path(candidate_path)
                        if normalized_path and normalized_path not in observed_origins:
                            observed_paths.append(candidate_path)
                            observed_origins[normalized_path] = search_term
                    ambiguous = "不是唯一" in str(exc)
                    if ambiguous:
                        validation_errors.append({"expected": " > ".join(path), "actual": list(capture.paths),
                                                  "reason": str(exc)})
                    timed_out = str(exc).startswith("小红书类目搜索 JSON 在")
                    if not ambiguous and not timed_out and str(exc) not in (
                        "小红书类目搜索接口返回空候选", "小红书类目当前搜索没有匹配候选",
                    ):
                        raise
                    if self.logger is not None:
                        self.logger.info("小红书类目搜索“%s”无匹配，尝试下一项", search_term)
            else:
                reviewed = await self._resolve_category_review(
                    tuple(observed_paths), source="excel_validation_mismatch" if validation_errors else "excel_search_ambiguous",
                    excel_value=" > ".join(path),
                    evidence={"validation_error": validation_errors} if validation_errors else {},
                )
                if not reviewed:
                    raise XhsFormListingError(
                        "小红书按 Excel 顺序搜索全部类目后仍无匹配候选："
                        + " / ".join(search_terms)
                    )
                origin = observed_origins.get(
                    self._normalize_category_path(reviewed)
                )
                if not origin:
                    raise XhsFormListingError(
                        "小红书类目审核结果不在当前搜索候选范围内"
                    )
                capture.start(origin)
                await search.fill(origin, timeout=10_000)
                target, target_text, _strategy = await self._wait_for_category_result(
                    (reviewed,), capture
                )
                strategy = "review_learning"
                reviewed_path = reviewed
                search_term = origin
            json_paths = tuple(capture.paths)
            result_ready_at = asyncio.get_running_loop().time()
            await target.scroll_into_view_if_needed()
            await target.click(timeout=10_000)
            clicked = tuple(path)

        selected = await self._confirm_category_path(dialog, reviewed_path or target_text)
        if self.logger is not None:
            self.logger.info(
                "已按 Excel 层级选择小红书类目：%s；页面路径：%s；规则：%s；"
                "耗时：弹窗/输入 %.2fs，JSON+DOM %.2fs，总计 %.2fs",
                " / ".join(path),
                selected,
                strategy,
                search_filled_at - started,
                result_ready_at - search_filled_at,
                asyncio.get_running_loop().time() - started,
            )
        return {
            "path": path,
            "clicked_segments": tuple(clicked),
            "selected": selected,
            "matched_result": target_text,
            "search_term": search_term,
            "strategy": strategy,
            "json_candidate_count": len(json_paths),
        }

    async def _heading_vertical_bounds(self) -> Tuple[Optional[float], Optional[float]]:
        if self.panel is None:
            raise XhsFormListingError("请先调用 open() 打开小红书资料")
        start: Optional[float] = None
        end: Optional[float] = None
        headings = self.panel.get_by_text(
            re.compile(r"^\s*(?:商品属性|价格库存)\s*[：:]?\s*$")
        )
        for index in range(await headings.count()):
            heading = headings.nth(index)
            if not await heading.is_visible():
                continue
            text = re.sub(r"\s*[：:]\s*$", "", (await heading.inner_text()).strip())
            box = await heading.bounding_box()
            if box is None:
                continue
            if text == "商品属性":
                start = box["y"]
            elif XHS_ATTRIBUTE_END_HEADINGS.fullmatch(text):
                if start is not None and box["y"] > start:
                    end = box["y"] if end is None else min(end, box["y"])
        return start, end

    async def _attribute_items(self, *, allow_empty: bool = False) -> Dict[str, Tuple[str, Any]]:
        if self.panel is None:
            raise XhsFormListingError("请先调用 open() 打开小红书资料")
        top, end = await self._heading_vertical_bounds()
        items = self.panel.locator(".el-form-item:visible")
        result: Dict[str, Tuple[str, Any]] = {}
        counts: Dict[str, int] = {}
        for index in range(await items.count()):
            item = items.nth(index)
            label_node = item.locator(":scope > .el-form-item__label").first
            if not await label_node.count():
                label_node = item.locator(".el-form-item__label").first
            if not await label_node.count() or not await label_node.is_visible():
                continue
            box = await item.bounding_box()
            if box is not None:
                if top is not None and box["y"] <= top:
                    continue
                if end is not None and box["y"] >= end:
                    continue
            label = re.sub(r"^\s*\*\s*", "", (await label_node.inner_text()).strip())
            normalized = normalize_label(label)
            if not normalized or normalized in XHS_INHERITED_FIELDS:
                continue
            counts[normalized] = counts.get(normalized, 0) + 1
            key = normalized if counts[normalized] == 1 else "{0}#{1}".format(
                normalized, counts[normalized]
            )
            result[key] = (label, item)
        if not result and not allow_empty:
            raise XhsFormListingError("小红书商品属性区域为空或未渲染")
        return result

    @staticmethod
    async def _attribute_is_required(item: Any) -> bool:
        """Read Element's required marker without relying on CSS pseudo text."""
        classes = set((await item.get_attribute("class") or "").split())
        if "is-required" in classes:
            return True
        label = item.locator(":scope > .el-form-item__label").first
        if not await label.count():
            label = item.locator(".el-form-item__label").first
        return bool(
            await label.count()
            and re.match(r"^\s*\*", (await label.inner_text()).strip())
        )

    async def _find_attribute_item_anywhere(self, page_label: str) -> Any:
        """Find a uniquely labelled attribute even when it is outside the
        normal 商品属性/价格库存 bounds.

        XHS moves some category-dependent fields (notably 裤长) into a
        separate SKU/details block.  The bounded attribute scan intentionally
        ignores that block, so use an exact visible label lookup as a
        compatibility fallback rather than treating the missing field as an
        optional omission.
        """
        if self.panel is None:
            raise XhsFormListingError("请先调用 open() 打开小红书资料")
        target = normalize_label(page_label)
        matches = []
        for index in range(await self.panel.locator(".el-form-item:visible").count()):
            item = self.panel.locator(".el-form-item:visible").nth(index)
            label = item.locator(".el-form-item__label").first
            if not await label.count() or not await label.is_visible():
                continue
            text = re.sub(r"^\s*\*\s*", "", (await label.inner_text()).strip())
            if normalize_label(text) == target:
                matches.append(item)
        if len(matches) > 1:
            raise XhsFormListingError(
                "小红书属性“{0}”输入项不是唯一项：{1}".format(page_label, len(matches))
            )
        return matches[0] if matches else None

    async def _attribute_assignments(
        self,
        fields: Mapping[str, str],
        page_items: Mapping[str, Tuple[str, Any]],
    ) -> Dict[str, Tuple[str, str]]:
        # An explicitly named Excel column wins over an alias.  For example,
        # a source sheet can contain both ``厚度=常规款`` and ``厚薄=常规``;
        # Xiaohongshu's field is ``厚薄`` and must use the latter rather than
        # treating the two source concepts as conflicting values.
        direct_sources: Dict[str, List[Tuple[str, str]]] = {}
        page_items = without_color_attributes(page_items)
        alias_sources: Dict[str, List[Tuple[str, str]]] = {}
        for excel_key, raw_value in fields.items():
            aliases = set(excel_aliases(excel_key))
            for normalized_page, (page_label, _item) in page_items.items():
                base = normalized_page.split("#", 1)[0]
                if base in aliases:
                    direct_sources.setdefault(normalized_page, []).append(
                        (str(excel_key), str(raw_value).strip())
                    )
                elif aliases.intersection(XHS_FIELD_ALIASES.get(base, ())):
                    alias_sources.setdefault(normalized_page, []).append(
                        (str(excel_key), str(raw_value).strip())
                    )
        assignments: Dict[str, Tuple[str, str]] = {}
        for normalized_page, (page_label, _item) in page_items.items():
            matches = direct_sources.get(normalized_page) or alias_sources.get(
                normalized_page, ()
            )
            if not matches:
                continue
            values = {value for _key, value in matches}
            if len(values) != 1:
                raise XhsFormListingError(
                    "小红书属性“{0}”匹配到多个 Excel 字段：{1}".format(
                        page_label, "、".join(key for key, _value in matches)
                    )
                )
            source_key, source_value = matches[0]
            paired = positional_listing_date_value(page_label, source_key, source_value)
            assignments[normalized_page] = (
                page_label, paired if paired is not None else source_value
            )
        return assignments

    @staticmethod
    def _special_attribute_values(
        fields: Mapping[str, str], page_label: str, expected: str
    ) -> Optional[Tuple[str, ...]]:
        normalized = normalize_label(page_label)
        if normalized in {normalize_label("面料"), normalize_label("材质成分")}:
            values = tuple(
                "/".join(group) for group in material_name_groups(expected)
            )
            return values or None
        if normalized == normalize_label("是否加绒"):
            mapped = {"是": "加绒", "否": "不加绒"}.get(str(expected).strip())
            return (mapped,) if mapped else None
        return None

    async def _resolve_learning_select_groups(
        self,
        page_label: str,
        select: Any,
        groups: Sequence[Sequence[str]],
    ) -> Optional[Tuple[str, ...]]:
        runtime = self.attribute_runtime
        managed = is_learning_managed_field("xhs", page_label)
        if runtime is None:
            raise XhsFormListingError("小红书属性学习运行器未启用")
        multi = await select.locator(".el-select__tags").count() > 0
        control_type = "multi_select" if multi else "select"
        await self._open_select(select, multi=multi)
        try:
            _dropdown, dom_options = await self._visible_dom_options(select)
            try:
                field, api_options, category_id = await self._captured_xhs_field(
                    page_label
                )
            except XhsFormListingError as exc:
                if self.logger is not None:
                    self.logger.info(
                        "小红书属性“%s”：接口字段或候选未定位，"
                        "改用当前 DOM 匹配/直接输入与回读：%s",
                        page_label,
                        exc,
                    )
                return None
        finally:
            try:
                await self._dismiss_select_dropdown(select)
            except Exception:
                pass
        try:
            candidates = reconcile_candidates(
                api_options,
                tuple(
                    DomCandidate(
                        (
                            ""
                            if normalize_option(option.get("value") or "")
                            == normalize_option(option.get("name") or "")
                            else str(option.get("value") or "")
                        ),
                        str(option.get("name") or ""),
                        not bool(option.get("disabled")),
                    )
                    for option in dom_options
                ),
            )
        except CandidateSourceError as exc:
            if not managed:
                return None
            if self.logger is not None:
                self.logger.error(
                    "小红书属性“%s”候选校验明细：API=%s，DOM=%s",
                    page_label,
                    [
                        (value.value_id, value.label)
                        for value in api_options
                    ],
                    [
                        (
                            str(option.get("value") or ""),
                            str(option.get("name") or ""),
                            not bool(option.get("disabled")),
                        )
                        for option in dom_options
                    ],
                )
            raise XhsFormListingError(
                "小红书属性“{0}”接口候选与页面候选不一致：{1}".format(
                    page_label,
                    exc.reason_code,
                )
            ) from exc
        field_id = str(field.get("id") or "").strip()
        if not field_id or not str(category_id).strip():
            if self.logger is not None:
                self.logger.info(
                    "小红书属性“%s”：接口字段 ID 或类目 ID 不完整，"
                    "改用当前 DOM 匹配与回读",
                    page_label,
                )
            return None
        schema_version = canonical_sha256(
            {
                "platform_id": "xhs",
                "category_leaf_id": category_id,
                "field_id": field_id,
                "options": [
                    {"value_id": value.value_id, "label": value.label}
                    for value in candidates
                ],
            }
        )
        resolved_values = []
        for group in groups:
            request_candidates, excel_value = await self._excel_before_review(
                select, candidates, group, label=page_label,
                preflight_request=AttributeRequest(
                    platform_id="xhs", category_leaf_id=category_id,
                    field_id=field_id, field_label=page_label,
                    candidates=tuple(CandidateValue(v.value_id, v.label) for v in candidates),
                    excel_value="/".join(group), evidence={},
                    custom_allowed=bool(field.get("custom_allowed")), schema_version=schema_version,
                    control_type=control_type,
                ),
            )
            resolved = await runtime.resolve(
                AttributeRequest(
                    platform_id="xhs",
                    category_leaf_id=category_id,
                    field_id=field_id,
                    field_label=page_label,
                    candidates=request_candidates,
                    excel_value=excel_value,
                    evidence={"excel": bool(excel_value.strip())},
                    custom_allowed=bool(field.get("custom_allowed")),
                    schema_version=schema_version,
                    control_type=control_type,
                )
            )
            if resolved is None or getattr(resolved, "is_no_fill", False):
                return ()
            resolved_values.extend(
                split_multi_choice(resolved.label) if multi else (resolved.label,)
            )
        return tuple(dict.fromkeys(resolved_values))

    async def _fill_attribute(
        self,
        page_label: str,
        item: Any,
        expected: str,
        *,
        exact_values: Optional[Sequence[str]] = None,
        required: bool = True,
    ) -> Optional[Tuple[str, ...]]:
        await item.scroll_into_view_if_needed()
        selects = item.locator(".el-select:visible")
        if await selects.count() != 1:
            raise XhsFormListingError(
                "小红书属性“{0}”下拉框不是唯一项：{1}".format(
                    page_label, await selects.count()
                )
            )
        select = selects.first
        multi = await select.locator(".el-select__tags").count() > 0
        groups = (
            material_value_groups(page_label, exact_values)
            if exact_values is not None
            else selection_value_groups_for_control(
                page_label, expected, multi=multi
            )
        )
        if not groups:
            raise XhsFormListingError("小红书属性“{0}”期望值为空".format(page_label))
        if self.attribute_runtime is not None:
            resolved_values = await self._resolve_learning_select_groups(
                page_label,
                select,
                groups,
            )
            if resolved_values == ():
                if self.logger is not None:
                    self.logger.info(
                        "小红书属性“%s”已加入本平台待审核汇总，继续填写后续字段",
                        page_label,
                    )
                return None
            if resolved_values is not None:
                groups = tuple((value,) for value in resolved_values)
        try:
            actual = await self._select_values(select, groups, label=page_label, multi=multi)
        except TaobaoListingError as exc:
            raise XhsFormListingError(
                "小红书属性“{0}”选择失败：{1}".format(
                    page_label, str(exc).replace("淘宝", "小红书")
                )
            ) from exc
        if (
            actual is None
            and self.attribute_runtime is not None
            and self.attribute_runtime.review_pending_or_omitted(
                self.attribute_platform_id, page_label
            )
        ):
            return None
        if actual is None and len(groups) == 1:
            # Xiaohongshu's remote selects can expose an exact, clickable DOM
            # option as ``created`` even when Element's caninputcustom flag is
            # false.  The shared Taobao helper correctly refuses that shape;
            # for XHS we may still click it when the visible text is uniquely
            # exact, then require a successful control readback.
            actual = await self._select_unique_exact_dom_option(
                select,
                groups[0],
                page_label=page_label,
                multi=multi,
            )
        if actual is None:
            candidates = " / ".join("/".join(group) for group in groups)
            if not required:
                if self.logger is not None:
                    self.logger.warning(
                        "小红书可选属性“%s”没有 Excel 值的精确候选，已跳过：%s",
                        page_label,
                        candidates,
                    )
                return None
            raise XhsFormListingError(
                "小红书属性“{0}”没有 Excel 值的精确候选：{1}".format(
                    page_label, candidates
                )
            )
        return actual

    async def _select_unique_exact_dom_option(
        self,
        select: Any,
        candidates: Sequence[str],
        *,
        page_label: str,
        multi: bool,
    ) -> Optional[Tuple[str, ...]]:
        try:
            await self._open_select(select, multi=multi)
            dropdown, options = await self._visible_dom_options(
                select, timeout_seconds=2
            )
        except TaobaoListingError:
            return None
        matches = [
            (candidate, option)
            for candidate in candidates
            for option in options
            if normalize_option(option.get("name", ""))
            == normalize_option(candidate)
        ]
        if len(matches) != 1:
            await self._dismiss_select_dropdown(select)
            return None
        candidate, option = matches[0]
        clicked = await dropdown.evaluate(
            """(element, index) => {
              const option = element.querySelectorAll(
                '.el-select-dropdown__item'
              )[index];
              if (!option || option.classList.contains('is-disabled')) return false;
              option.scrollIntoView({block: 'nearest'});
              option.click();
              return true;
            }""",
            int(option["index"]),
        )
        if not clicked:
            return None
        deadline = asyncio.get_running_loop().time() + 2
        while asyncio.get_running_loop().time() < deadline:
            actual = await self._read_select_values(select, multi=multi)
            if any(
                normalize_option(value) == normalize_option(candidate)
                for value in actual
            ):
                await self._dismiss_select_dropdown(select)
                if self.logger is not None:
                    self.logger.info(
                        "小红书属性“%s”已通过唯一精确 DOM 候选填写：%s",
                        page_label,
                        candidate,
                    )
                return actual
            await asyncio.sleep(0.05)
        await self._dismiss_select_dropdown(select)
        return None

    async def fill_category_attributes(self, fields: XhsFields) -> Mapping[str, Any]:
        page_items = without_color_attributes(await self._attribute_items())
        assignments = await self._attribute_assignments(fields.fields, page_items)
        applied: Dict[str, Tuple[str, ...]] = {}
        skipped: Dict[str, str] = {}
        for normalized_page, (page_label, item) in page_items.items():
            assignment = assignments.get(normalized_page)
            if assignment is None:
                continue
            _source_label, expected = assignment
            exact_values = self._special_attribute_values(
                fields.fields, page_label, expected
            )
            required = await self._attribute_is_required(item)
            fabric_label = normalize_label(page_label)
            fabric_values = exact_values
            if fabric_label in {
                normalize_label("面料分类"),
            } and (not fabric_values or len(fabric_values) < 2):
                fabric_values = self._special_attribute_values(
                    fields.fields, "材质成分", expected
                )
            if (
                fabric_label in {
                    normalize_label("面料"),
                    normalize_label("面料分类"),
                }
                and fabric_values is not None
                and len(fabric_values) >= 2
            ):
                actual = await self._fill_fabric_attribute(
                    page_label,
                    fabric_values,
                    item=item,
                    raw_value=expected,
                    writer=lambda value, values: self._fill_attribute(
                        page_label,
                        item,
                        value,
                        exact_values=values,
                        required=required,
                    ),
                )
            else:
                actual = await self._fill_attribute(
                    page_label,
                    item,
                    expected,
                    exact_values=exact_values,
                    required=required,
                )
            if actual is None:
                skipped[page_label] = expected
            else:
                applied[page_label] = actual

        # Some XHS pants categories render 裤长 outside the bounded category
        # attribute section.  It still remains a required page field, so fill
        # it by its unique label when the normal scan did not see it.
        pants_label = "裤长"
        pants_value = next(
            (
                str(value).strip()
                for key, value in fields.fields.items()
                if normalize_label(str(key)) == normalize_label(pants_label)
                and str(value).strip()
            ),
            "",
        )
        if pants_value and normalize_label(pants_label) not in {
            normalize_label(label) for label in applied
        }:
            pants_item = await self._find_attribute_item_anywhere(pants_label)
            if pants_item is not None:
                actual = await self._fill_attribute(
                    pants_label,
                    pants_item,
                    pants_value,
                    exact_values=self._special_attribute_values(
                        fields.fields, pants_label, pants_value
                    ),
                    required=True,
                )
                if actual is not None:
                    applied[pants_label] = actual
                    if self.logger is not None:
                        self.logger.info(
                            "小红书属性“裤长”已通过跨区域兼容定位填写：%s",
                            "/".join(actual),
                        )
        if self.logger is not None:
            self.logger.info("小红书类目属性填写完成：已填 %s 项", len(applied))
        return {
            "attributes": applied,
            "skipped_no_exact_candidate": skipped,
            "unmatched_page_fields": tuple(
                page_label
                for normalized_page, (page_label, _item) in page_items.items()
                if normalized_page not in assignments
            ),
        }

    async def _find_named_input(self, label: str) -> Any:
        """Re-query after category rendering and scroll only the editor for lazy fields."""
        deadline = asyncio.get_running_loop().time() + 12
        step = 0
        while True:
            control = await self._find_named_input_once(label)
            if control is not None:
                try:
                    await control.scroll_into_view_if_needed(timeout=1500)
                    return control
                except PlaywrightTimeoutError:
                    # The category confirmation can replace the whole form row
                    # between DOM discovery and the first locator action.
                    control = None
            if asyncio.get_running_loop().time() >= deadline:
                raise XhsFormListingError(
                    "小红书字段“{0}”等待渲染并滚动查找后仍未找到输入框".format(label)
                )
            if step == 0 and self.logger is not None:
                self.logger.info("小红书字段“%s”暂未出现，等待渲染并在编辑区域滚动查找", label)
            await self.panel.evaluate('''(panel, step) => {
              const roots = [panel, ...panel.querySelectorAll('*')];
              for (let p = panel.parentElement; p && !['BODY','HTML'].includes(p.tagName); p = p.parentElement) roots.push(p);
              for (const el of roots) {
                if (!/(auto|scroll)/.test(getComputedStyle(el).overflowY) || el.clientHeight <= 0 || el.scrollHeight <= el.clientHeight) continue;
                const bottom = el.scrollHeight - el.clientHeight;
                const next = step === 0 || el.scrollTop >= bottom - 2 ? 0 : Math.min(bottom, el.scrollTop + el.clientHeight * 0.75);
                el.scrollTo({top: next, behavior: 'instant'});
              }
            }''', step)
            step += 1
            await asyncio.sleep(0.4)

    async def _find_named_input_once(self, label: str) -> Any:
        if self.panel is None:
            raise XhsFormListingError("请先调用 open() 打开小红书资料")
        match_count = await self.panel.evaluate(
            """(panel, label) => {
              const marker = 'data-xhs-identity-control';
              const controlSelector = 'input:not([type="hidden"]):not([readonly]):not([disabled]), textarea:not([readonly]):not([disabled])';
              const normalize = value => String(value || '')
                .replace(/[\\s：:*]/g, '')
                .trim();
              const isVisible = element => {
                if (!element || !element.isConnected) return false;
                const style = getComputedStyle(element);
                return style.display !== 'none'
                  && style.visibility !== 'hidden'
                  && style.opacity !== '0'
                  && element.getClientRects().length > 0;
              };
              const controlsWithin = root => Array.from(root.querySelectorAll(controlSelector))
                .filter(isVisible);

              panel.querySelectorAll('[' + marker + ']').forEach(element => {
                element.removeAttribute(marker);
              });

              const matches = new Set();
              for (const field of panel.querySelectorAll('[data-xhs-field]')) {
                if (normalize(field.getAttribute('data-xhs-field')) !== normalize(label)) continue;
                const controls = controlsWithin(field);
                if (controls.length === 1) matches.add(controls[0]);
              }

              if (matches.size === 0) {
                const labelNodes = Array.from(panel.querySelectorAll(
                  'label, .el-form-item__label, [class*="label"], span, div'
                )).filter(element => isVisible(element) && normalize(element.textContent) === normalize(label));
                labelNodes.sort((left, right) => {
                  const score = element => (element.tagName === 'LABEL' ? 0 : 2)
                    + (element.classList.contains('el-form-item__label') ? 0 : 1)
                    + element.children.length;
                  return score(left) - score(right);
                });
                for (const labelNode of labelNodes) {
                  let root = labelNode.closest('.el-form-item') || labelNode.parentElement;
                  while (root && panel.contains(root)) {
                    const controls = controlsWithin(root);
                    if (controls.length === 1) {
                      matches.add(controls[0]);
                      break;
                    }
                    if (root === panel) break;
                    root = root.parentElement;
                  }
                }
              }

              if (matches.size === 1) {
                matches.values().next().value.setAttribute(marker, '1');
              }
              return matches.size;
            }""",
            label,
        )
        if match_count > 1:
            raise XhsFormListingError(
                "小红书字段“{0}”输入框不是唯一项：{1}".format(label, match_count)
            )
        if match_count == 0:
            return None
        return self.panel.locator('[data-xhs-identity-control="1"]')

    async def fill_identity(self, title: str, style_code: str) -> Mapping[str, str]:
        expected_title = title_without_neigborl(title)
        if not expected_title:
            raise XhsFormListingError("移除 NEIGBORL 后小红书商品标题为空")
        if re.search(r"neigborl", expected_title, re.IGNORECASE):
            raise XhsFormListingError("小红书商品标题仍包含 NEIGBORL")
        if len(expected_title) > 60:
            raise XhsFormListingError(
                "移除 NEIGBORL 后小红书商品标题仍超过 60 字：{0}".format(len(expected_title))
            )
        expected = {"商品标题": expected_title, "货号": str(style_code).strip()}
        actual: Dict[str, str] = {}
        for label, value in expected.items():
            if not value:
                raise XhsFormListingError("小红书{0}不能为空".format(label))
            current = ""
            last_timeout: Optional[Exception] = None
            for attempt in range(3):
                control = await self._find_named_input(label)
                try:
                    current = (await control.input_value(timeout=1500)).strip()
                    if current != value:
                        await control.fill(value, timeout=3000)
                        await control.press("Tab", timeout=1500)
                    current = (await control.input_value(timeout=1500)).strip()
                    if current == value:
                        break
                except PlaywrightTimeoutError as exc:
                    last_timeout = exc
                    if self.page.is_closed():
                        raise
                if attempt < 2:
                    if self.logger is not None:
                        self.logger.info("小红书%s输入框发生重绘，重新定位后填写", label)
                    await asyncio.sleep(0.2)
            else:
                if last_timeout is not None:
                    raise XhsFormListingError(
                        "小红书{0}输入框连续重绘，3 次短等待后仍无法填写".format(label)
                    ) from last_timeout
            if current != value:
                raise XhsFormListingError(
                    "小红书{0}回读失败：期望 {1!r}，页面为 {2!r}".format(
                        label, value, current
                    )
                )
            actual[label] = current
        if re.search(r"neigborl", actual["商品标题"], re.IGNORECASE):
            raise XhsFormListingError("小红书商品标题回读仍包含 NEIGBORL")
        return actual

    async def _ensure_radio(self, option_text: str) -> str:
        if self.panel is None:
            raise XhsFormListingError("请先调用 open() 打开小红书资料")
        options = self.panel.locator("label.el-radio:visible")
        matches = []
        for index in range(await options.count()):
            option = options.nth(index)
            if normalize_label(await option.inner_text()) == normalize_label(option_text):
                matches.append(option)
        if len(matches) != 1:
            raise XhsFormListingError(
                "小红书单选项“{0}”不是唯一项：{1}".format(option_text, len(matches))
            )
        option = matches[0]
        radio = option.locator('input[type="radio"]').first
        if not await radio.is_checked():
            await option.click()
        if not await radio.is_checked():
            raise XhsFormListingError("小红书单选项未生效：{0}".format(option_text))
        return option_text

    async def _presale_day_control(self) -> Tuple[Any, bool]:
        if self.panel is None:
            raise XhsFormListingError("请先调用 open() 打开小红书资料")
        direct = self.panel.locator('[data-xhs-presale-days]:visible')
        if await direct.count() == 1:
            select = direct.first.locator(".el-select:visible")
            return (select.first if await select.count() == 1 else direct.first, await select.count() == 1)
        matches: List[Tuple[Any, bool]] = []
        labels = self.panel.get_by_text(re.compile(r"^\s*(?:\*?\s*)?(?:预售发货时间|付款后)\s*[：:]?\s*$"))
        for index in range(await labels.count()):
            label = labels.nth(index)
            if not await label.is_visible():
                continue
            root = label
            for _depth in range(5):
                selects = root.locator(".el-select:visible")
                inputs = root.locator('input:not([type="hidden"]):visible')
                if await selects.count() == 1:
                    matches.append((selects.first, True))
                    break
                if await inputs.count() == 1:
                    matches.append((inputs.first, False))
                    break
                root = root.locator("xpath=..")
        if len(matches) != 1:
            raise XhsFormListingError(
                "小红书预售发货天数控件不是唯一项：{0}".format(len(matches))
            )
        return matches[0]

    async def apply_full_payment_presale(self) -> Mapping[str, str]:
        full_mode = await self._ensure_radio("全款预售模式")
        await self._wait_for_loading_masks()
        timed_mode = await self._ensure_radio("时段预售")
        control, is_select = await self._presale_day_control()
        if is_select:
            try:
                actual_values = await self._select_values(
                    control, (("15", "15天"),), label="预售发货时间", multi=False
                )
            except TaobaoListingError as exc:
                raise XhsFormListingError(str(exc).replace("淘宝", "小红书")) from exc
            if actual_values is None:
                raise XhsFormListingError("小红书预售发货时间没有“15天”精确候选")
            days = actual_values[0]
        else:
            current = (await control.input_value()).strip()
            if not _numeric_equal(current, "15"):
                await control.fill("15")
                await control.press("Tab")
            days = (await control.input_value()).strip()
            if not _numeric_equal(days, "15"):
                raise XhsFormListingError(
                    "小红书预售发货时间回读失败：期望 15，页面为 {0!r}".format(days)
                )
        return {"发货模式": full_mode, "预售类型": timed_mode, "付款后": days}

    async def _batch_input(self, label: str) -> Any:
        if self.panel is None:
            raise XhsFormListingError("请先调用 open() 打开小红书资料")
        direct = self.panel.locator('[data-xhs-batch-field="{0}"] input:visible'.format(label))
        if await direct.count() == 1:
            return direct.first
        button = self.panel.get_by_role("button", name="批量设置", exact=True)
        if await button.count() != 1:
            raise XhsFormListingError("小红书“批量设置”按钮不是唯一项：{0}".format(await button.count()))
        root = button.first
        for _depth in range(1, 8):
            root = root.locator("xpath=..")
            if await root.locator(".el-table, table").count():
                continue
            inputs = root.locator('input:not([type="hidden"]):not([readonly]):visible')
            count = await inputs.count()
            if len(XHS_BATCH_ORDER) <= count <= 5:
                return inputs.nth(XHS_BATCH_ORDER.index(label))
        raise XhsFormListingError("小红书批量字段“{0}”输入框未找到".format(label))

    async def _fill_batch_number(self, label: str, expected: str) -> str:
        if not _is_nonnegative_decimal(expected):
            raise XhsFormListingError("Excel 小红书{0}不是非负数字：{1!r}".format(label, expected))
        control = await self._batch_input(label)
        current = (await control.input_value()).strip()
        if not _numeric_equal(current, expected):
            await control.fill(expected)
            await control.press("Tab")
        actual = (await control.input_value()).strip()
        if not _numeric_equal(actual, expected):
            raise XhsFormListingError(
                "小红书批量字段“{0}”回读失败：期望 {1!r}，页面为 {2!r}".format(
                    label, expected, actual
                )
            )
        return actual

    async def _sku_table_snapshot(self) -> Mapping[str, Any]:
        if self.panel is None:
            raise XhsFormListingError("请先调用 open() 打开小红书资料")
        tables = self.panel.locator(".el-table:visible")
        if await tables.count() == 0:
            tables = self.panel.locator("table:visible")
        matches = []
        for index in range(await tables.count()):
            table = tables.nth(index)
            snapshot = await table.evaluate(
                """root => {
                  const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
                  const headers = Array.from(root.querySelectorAll(
                    '.el-table__header-wrapper th, thead th'
                  )).map(node => clean(node.innerText));
                  const rows = Array.from(root.querySelectorAll(
                    '.el-table__body-wrapper tbody tr, tbody tr'
                  )).map(row => Array.from(row.querySelectorAll(':scope > td')).map(cell => {
                    const inputs = Array.from(cell.querySelectorAll('input'))
                      .filter(input => input.type !== 'checkbox' && input.type !== 'radio')
                      .map(input => clean(input.value));
                    return inputs.length ? inputs.join('|') : clean(cell.innerText);
                  }));
                  return {headers, rows};
                }"""
            )
            normalized = tuple(normalize_label(value) for value in snapshot.get("headers", ()))
            if all(
                any(header == normalize_label(label) or header.startswith(normalize_label(label)) for header in normalized)
                for label in ("售价", "库存")
            ):
                matches.append(snapshot)
        if len(matches) != 1:
            raise XhsFormListingError("小红书 SKU 表格不是唯一项：{0}".format(len(matches)))
        return matches[0]

    @staticmethod
    def _sku_column_index(headers: Sequence[str], label: str) -> int:
        expected = normalize_label(label)
        indexes = [
            index for index, value in enumerate(headers)
            if normalize_label(value) == expected or normalize_label(value).startswith(expected)
        ]
        if len(indexes) != 1:
            raise XhsFormListingError(
                "小红书 SKU 表格列“{0}”不是唯一项：{1}".format(label, indexes)
            )
        return indexes[0]

    def _validate_sku_snapshot(
        self, snapshot: Mapping[str, Any], expected: Mapping[str, str]
    ) -> Tuple[Mapping[str, str], ...]:
        headers = tuple(str(value) for value in snapshot.get("headers", ()))
        rows = tuple(snapshot.get("rows", ()))
        if not rows:
            raise XhsFormListingError("小红书 SKU 表格没有可校验的明细行")
        indexes = {label: self._sku_column_index(headers, label) for label in expected}
        result = []
        errors = []
        for number, raw_row in enumerate(rows, 1):
            actual = {
                label: str(raw_row[index]) if index < len(raw_row) else ""
                for label, index in indexes.items()
            }
            for label, expected_value in expected.items():
                if not _numeric_equal(actual[label], expected_value):
                    errors.append("第{0}行{1}={2!r}".format(number, label, actual[label]))
            result.append(actual)
        if errors:
            raise XhsFormListingError("小红书批量设置后校验失败：" + "；".join(errors[:12]))
        return tuple(result)

    async def fill_price_inventory_batch(self, fields: Mapping[str, str]) -> Mapping[str, Any]:
        expected = {
            "售价": _required_excel_value(
                fields, ("售价", "售卖价", "价格", "基本售价"), "售价", money=True
            ),
            "库存": _required_excel_value(fields, ("数量", "库存"), "库存"),
        }
        inventory = Decimal(expected["库存"])
        if not inventory.is_finite() or inventory < 0 or inventory % 1:
            raise XhsFormListingError("Excel 小红书库存必须是非负整数：{0!r}".format(expected["库存"]))
        for label, value in expected.items():
            await self._fill_batch_number(label, value)
        if self.panel is None:
            raise XhsFormListingError("请先调用 open() 打开小红书资料")
        button = self.panel.get_by_role("button", name="批量设置", exact=True)
        if await button.count() != 1:
            raise XhsFormListingError("小红书“批量设置”按钮不是唯一项：{0}".format(await button.count()))
        await button.click()
        deadline = asyncio.get_running_loop().time() + 15
        last_error: Optional[Exception] = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                rows = self._validate_sku_snapshot(await self._sku_table_snapshot(), expected)
                return {
                    "batch_clicked": True,
                    "row_count": len(rows),
                    "values": expected,
                    "rows": rows,
                }
            except XhsFormListingError as exc:
                last_error = exc
                await asyncio.sleep(0.15)
        raise XhsFormListingError(str(last_error or "小红书批量设置超时"))

    async def _main_image_item(self) -> Any:
        if self.panel is None:
            raise XhsFormListingError("请先调用 open() 打开小红书资料")
        direct = self.panel.locator('[data-xhs-image-group="main"]:visible')
        if await direct.count() == 1:
            return direct.first
        labels = self.panel.get_by_text(re.compile(r"^\s*\*?\s*主图\s*[：:]?\s*$"))
        matches = []
        for index in range(await labels.count()):
            label = labels.nth(index)
            if not await label.is_visible():
                continue
            root = label
            for _depth in range(8):
                if await root.locator('input[type="file"]').count():
                    matches.append(root)
                    break
                root = root.locator("xpath=..")
        if len(matches) != 1:
            raise XhsFormListingError("小红书主图上传区域不是唯一项：{0}".format(len(matches)))
        # 清空图片后，校验摘要也可能显示“主图”。不能继续用文字匹配的
        # first()/父节点定位，否则悬停后会指向摘要，永远等不到文件控件。
        await matches[0].evaluate(
            "element => element.setAttribute('data-xhs-image-group', 'main')"
        )
        return self.panel.locator('[data-xhs-image-group="main"]:visible')

    async def sync_main_images(
        self,
        portrait_paths: Sequence[Any],
        *,
        timeout_seconds: int,
        uploader: Any,
    ) -> Mapping[str, Any]:
        """Replace XHS main art with the product's 3:4 source images."""
        paths = tuple(portrait_paths)
        if not paths:
            raise XhsFormListingError("小红书没有可上传的 3:4 主图")
        item = await self._main_image_item()
        action = await uploader(
            self.page,
            item,
            paths,
            "小红书3:4主图",
            timeout_seconds,
            force_replace=True,
        )
        return {"source": "3:4主图", "count": len(paths), "action": action}

    async def _wait_for_main_image_validation(self) -> None:
        """Let the form finish validating imageList after the upload callback."""
        if self.panel is None:
            return
        image_group = self.panel.locator('[data-xhs-image-group="main"]:visible').first
        if not await image_group.count():
            return
        deadline = asyncio.get_running_loop().time() + 15
        while True:
            state = await image_group.evaluate("""node => {
              const item = node.closest('.el-form-item');
              const field = item?.__vue__;
              return {
                prop: field?.prop || '',
                state: field?.validateState || '',
                message: field?.validateMessage || '',
                count: Array.isArray(field?.fieldValue) ? field.fieldValue.length : 0,
                visibleError: Boolean(item?.querySelector('.el-form-item__error'))
              };
            }""")
            if state["prop"] != "imageList":
                return
            if state["state"] != "validating" and not (
                state["state"] == "success" and state["visibleError"]
            ):
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise XhsFormListingError(
                    "小红书主图校验未完成：imageList={0}，状态={1}，消息={2}".format(
                        state["count"], state["state"], state["message"]
                    )
                )
            await asyncio.sleep(0.1)

    async def _verify_persisted_identity(
        self, title: str, style_code: str
    ) -> Mapping[str, str]:
        expected = {
            "商品标题": title_without_neigborl(title),
            "货号": str(style_code).strip(),
        }
        actual: Dict[str, str] = {}
        errors: List[str] = []
        for label, expected_value in expected.items():
            control = await self._find_named_input(label)
            current = (await control.input_value()).strip()
            actual[label] = current
            if current != expected_value:
                errors.append(
                    "{0}=期望 {1!r}，页面为 {2!r}".format(
                        label, expected_value, current
                    )
                )
        if errors:
            raise XhsFormListingError(
                "小红书保存后身份字段回读失败：" + "；".join(errors)
            )
        return actual

    async def _verify_saved_api_attributes(
        self, expected_attributes: Mapping[str, Sequence[str]]
    ) -> Mapping[str, Tuple[str, ...]]:
        if not self.base_item_id:
            raise XhsFormListingError("小红书商品属性区域为空，且缺少商品 ID，无法通过详情接口回读")
        endpoint = EndpointSpec(
            "GET", "/xhs/detail.json", ("baseItemId", "api_name"), read_only=True,
        )
        detail, observation = await ApiClient(self.page, (endpoint,)).request(
            endpoint, {"baseItemId": self.base_item_id, "api_name": "xhs_detail"},
        )
        if observation.status != "ok" or not isinstance(detail, Mapping):
            raise XhsFormListingError(
                "小红书保存后详情接口回读失败：HTTP={0}，{1}，{2}".format(
                    observation.http_status, observation.status, observation.sanitized_message,
                )
            )
        if str(detail.get("baseItemId") or "") != self.base_item_id:
            raise XhsFormListingError("小红书保存后详情接口商品身份不一致")
        records = detail.get("attributeList")
        if not isinstance(records, list):
            raise XhsFormListingError("小红书保存后详情接口缺少 attributeList")
        persisted: Dict[str, Tuple[str, ...]] = {}
        errors: List[str] = []
        for label, expected_values in expected_attributes.items():
            matches = [
                record for record in records if isinstance(record, Mapping)
                and normalize_label(str(record.get("name") or "")) == normalize_label(label)
            ]
            if len(matches) != 1:
                errors.append("{0}=接口字段不唯一({1})".format(label, len(matches)))
                continue
            record = matches[0]
            value_list = record.get("valueList")
            if value_list is not None and not isinstance(value_list, list):
                errors.append("{0}=接口多选值格式错误".format(label))
                continue
            if isinstance(value_list, list) and value_list:
                if any(not isinstance(value, Mapping) or value.get("value") in (None, "") for value in value_list):
                    errors.append("{0}=接口多选值格式错误".format(label))
                    continue
                actual = tuple(str(value["value"]) for value in value_list)
            else:
                actual = (str(record["value"]),) if record.get("value") not in (None, "") else ()
            expected_counter = Counter(normalize_option(value) for value in (expected_values or ()) if normalize_option(value))
            actual_counter = Counter(normalize_option(value) for value in actual if normalize_option(value))
            if actual_counter != expected_counter:
                errors.append("{0}=期望 {1}，接口为 {2}".format(label, tuple(expected_values or ()), actual))
            else:
                persisted[label] = actual
        if errors:
            raise XhsFormListingError("小红书保存后类目属性 API 回读失败：" + "；".join(errors[:12]))
        self._xhs_attribute_readback_source = "detail_api"
        if self.logger is not None:
            self.logger.info("小红书商品属性未渲染，已通过详情 API 逐项复核 %s 个保存值", len(persisted))
        return persisted

    async def _verify_persisted_attributes(
        self, expected_attributes: Mapping[str, Sequence[str]]
    ) -> Mapping[str, Tuple[str, ...]]:
        await self._drain_xhs_attribute_tasks()
        page_items = await self._attribute_items(allow_empty=True)
        if not page_items:
            return await self._verify_saved_api_attributes(expected_attributes)
        self._xhs_attribute_readback_source = "dom"
        persisted: Dict[str, Tuple[str, ...]] = {}
        errors: List[str] = []
        for expected_label, expected_values in expected_attributes.items():
            matches = [
                (page_label, item)
                for page_label, item in page_items.values()
                if normalize_label(page_label) == normalize_label(expected_label)
            ]
            if len(matches) != 1:
                errors.append(
                    "{0}=页面字段不唯一({1})".format(expected_label, len(matches))
                )
                continue
            page_label, item = matches[0]
            selects = item.locator(".el-select:visible")
            if await selects.count() != 1:
                errors.append("{0}=下拉控件不唯一".format(page_label))
                continue
            select = selects.first
            multi = await select.locator(".el-select__tags").count() > 0
            actual = await self._read_select_values(select, multi=multi)
            expected_counter = Counter(
                normalize_option(value)
                for value in expected_values
                if normalize_option(value)
            )
            actual_counter = Counter(
                normalize_option(value) for value in actual if normalize_option(value)
            )
            if actual_counter != expected_counter:
                errors.append(
                    "{0}=期望 {1}，页面为 {2}".format(
                        page_label, tuple(expected_values), actual
                    )
                )
                continue
            persisted[page_label] = tuple(actual)
        if errors:
            raise XhsFormListingError(
                "小红书保存后类目属性回读失败：" + "；".join(errors[:12])
            )
        return persisted

    async def _persisted_radio_is_checked(self, option_text: str) -> bool:
        if self.panel is None:
            raise XhsFormListingError("请先调用 open() 打开小红书资料")
        matches = []
        options = self.panel.locator("label.el-radio:visible")
        for index in range(await options.count()):
            option = options.nth(index)
            if normalize_label(await option.inner_text()) == normalize_label(option_text):
                matches.append(option)
        if len(matches) != 1:
            raise XhsFormListingError(
                "小红书保存后单选项“{0}”不唯一：{1}".format(
                    option_text, len(matches)
                )
            )
        option = matches[0]
        control = option.locator('input[type="radio"]').first
        return await control.is_checked() or "is-checked" in set(
            (await option.get_attribute("class") or "").split()
        )

    async def verify_persisted_values(
        self,
        fields: XhsFields,
        *,
        title: str,
        style_code: str,
        expected_attributes: Mapping[str, Sequence[str]],
        expected_freight: Optional[Mapping[str, Any]] = None,
    ) -> Mapping[str, Any]:
        """Read critical Xiaohongshu values after save without changing them."""
        identity = await self._verify_persisted_identity(title, style_code)
        attributes = await self._verify_persisted_attributes(expected_attributes)
        freight = await sync_store_freight(self, fields.fields, read_only=True, expected=expected_freight)

        full_mode = await self._persisted_radio_is_checked("全款预售模式")
        timed_mode = await self._persisted_radio_is_checked("时段预售")
        if not full_mode or not timed_mode:
            raise XhsFormListingError("小红书保存后全款时段预售没有持久化")
        day_control, is_select = await self._presale_day_control()
        if is_select:
            days_values = await self._read_select_values(day_control, multi=False)
            days = days_values[0] if len(days_values) == 1 else ""
        else:
            days = (await day_control.input_value()).strip()
        if normalize_option(days) not in {
            normalize_option("15"),
            normalize_option("15天"),
        }:
            raise XhsFormListingError(
                "小红书保存后预售天数回读失败：{0!r}".format(days)
            )

        expected_sku = {
            "售价": _required_excel_value(
                fields.fields, ("售价", "售卖价", "价格", "基本售价"), "售价", money=True
            ),
            "库存": _required_excel_value(fields.fields, ("数量", "库存"), "库存"),
        }
        rows = self._validate_sku_snapshot(
            await self._sku_table_snapshot(), expected_sku
        )
        errors = await self._visible_validation_errors()
        if errors:
            raise XhsFormListingError(
                "小红书保存后仍有页面校验错误：" + "；".join(errors)
            )
        return {
            "identity": identity,
            "attributes": attributes,
            "attribute_readback_source": self._xhs_attribute_readback_source,
            "presale": {
                "发货模式": "全款预售模式",
                "预售类型": "时段预售",
                "付款后": days,
            },
            "sku_values": expected_sku,
            "row_count": len(rows),
            "freight": freight,
        }

    async def _wait_for_footwear_specification_rows(
        self, *, timeout_seconds: float = 20
    ) -> Any:
        """Wait for shoe specifications to rerender after a category change."""
        if self.panel is None:
            raise XhsFormListingError("请先调用 open() 打开小红书资料")
        rows = self.panel.locator('.block-specification .title-bg')
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        observed: Tuple[str, ...] = ()
        while asyncio.get_running_loop().time() < deadline:
            observed = tuple(
                await rows.locator('.el-select input.el-input__inner').evaluate_all(
                    'inputs => inputs.map(input => input.value.trim())'
                )
            )
            if any(name in {'尺码', '鞋码'} for name in observed):
                return rows
            await asyncio.sleep(0.1)
        raise XhsFormListingError(
            '小红书鞋类规格名未加载完成：当前规格名 {0!r}'.format(observed)
        )

    async def apply_excel_fields(
        self,
        fields: XhsFields,
        *,
        title: str,
        style_code: str,
        portrait_paths: Sequence[Any],
        timeout_seconds: int,
        uploader: Any,
    ) -> Mapping[str, Any]:
        category = await self.apply_category(fields.category_path)
        if is_footwear_category(" > ".join(fields.category_path)):
            titles = await self._wait_for_footwear_specification_rows()
            for index in range(await titles.count()):
                title_row = titles.nth(index)
                select = title_row.locator('.el-select').first
                if not await select.count():
                    continue
                if (await select.locator('input.el-input__inner').first.input_value()).strip() != '尺码':
                    continue
                group = title_row.locator('xpath=..')
                values = group.locator('.specification-value input.el-input__inner')
                before = await values.evaluate_all('xs=>xs.map(x=>x.value)')
                actual = await self._select_values(select, (('鞋码',),), label='鞋类规格名', multi=False)
                if actual != ('鞋码',):
                    raise XhsFormListingError('小红书鞋类规格名未成功匹配“鞋码”')
                after = await values.evaluate_all('xs=>xs.map(x=>x.value)')
                if before != after:
                    raise XhsFormListingError('小红书切换鞋码规格名后原规格值发生变化，停止保存')
        identity = await self.fill_identity(title, style_code)
        attributes = await self.fill_category_attributes(fields)
        presale = await self.apply_full_payment_presale()
        batch = await self.fill_price_inventory_batch(fields.fields)
        freight = await sync_store_freight(self, fields.fields)
        images = await self.sync_main_images(
            portrait_paths, timeout_seconds=timeout_seconds, uploader=uploader
        )
        await self._wait_for_main_image_validation()
        try:
            errors = await self._visible_validation_errors()
        except TaobaoListingError as exc:
            raise XhsFormListingError(str(exc).replace("淘宝", "小红书")) from exc
        if (
            errors
            and not (
                self.attribute_runtime is not None
                and getattr(
                    self.attribute_runtime, "has_deferred_reviews", False
                )
            )
        ):
            raise XhsFormListingError("小红书页面校验错误：" + "；".join(errors))
        return {
            "category": category,
            "identity": identity,
            "attributes": attributes,
            "presale": presale,
            "sku_batch": batch,
            "main_images": images,
            "freight": freight,
            "deferred_validation_errors": (
                errors
                if self.attribute_runtime is not None
                and getattr(
                    self.attribute_runtime, "has_deferred_reviews", False
                )
                else ()
            ),
        }
