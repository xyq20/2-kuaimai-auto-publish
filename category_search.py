"""Capture category responses belonging to the current keyword search."""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import parse_qs, urlsplit


CATEGORY_QUERY_PATH = "/category/base/queryCategoryList"


def category_response_paths(payload: Any) -> tuple[str, ...]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), Mapping):
        return ()
    paths = []
    for record in payload["data"].get("records") or ():
        if (isinstance(record, Mapping) and record.get("leaf") not in (0, "0", False)
                and isinstance(record.get("categoryNameList"), (tuple, list))):
            path = " > ".join(str(part).strip() for part in record["categoryNameList"] if str(part).strip())
            if path and path not in paths:
                paths.append(path)
    return tuple(paths)


class CategorySearchCapture:
    def __init__(self, page: Any, parse_paths: Callable[[Any], Sequence[str]]) -> None:
        self.page = page
        self.parse_paths = parse_paths
        self.generation = 0
        self.term = ""
        self.paths: list[str] = []
        self.payloads: list[tuple[str, Any]] = []
        self.received = False
        self.error = ""
        self._requests: dict[Any, int] = {}
        self._tasks: list[asyncio.Task[Any]] = []

    async def __aenter__(self):
        self.page.on("request", self._on_request)
        self.page.on("response", self._on_response)
        return self

    async def __aexit__(self, *_exc):
        self.page.remove_listener("request", self._on_request)
        self.page.remove_listener("response", self._on_response)
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def start(self, term: str) -> None:
        self.generation += 1
        self.term = term
        self.paths.clear()
        self.payloads.clear()
        self.received = False
        self.error = ""

    @staticmethod
    def _keyword(request: Any) -> str:
        try:
            payload = request.post_data_json
        except Exception:
            payload = None
        if isinstance(payload, Mapping) and "queryTxt" in payload:
            return str(payload["queryTxt"]).strip()
        query = parse_qs(urlsplit(request.url).query)
        return str(query.get("queryTxt", [""])[0]).strip()

    def _on_request(self, request: Any) -> None:
        if CATEGORY_QUERY_PATH in urlsplit(request.url).path and self.term:
            keyword = self._keyword(request)
            if not keyword or keyword == self.term:
                self._requests[request] = self.generation

    def _on_response(self, response: Any) -> None:
        generation = self._requests.pop(response.request, None)
        if generation == self.generation:
            self._tasks.append(asyncio.create_task(self._capture(response, generation)))

    async def _capture(self, response: Any, generation: int) -> None:
        try:
            if response.status >= 400:
                raise ValueError("HTTP={0}".format(response.status))
            payload = await response.json()
            if isinstance(payload, Mapping) and payload.get("result", 1) not in (1, "1"):
                raise ValueError("result={0}：{1}".format(
                    payload.get("result"), payload.get("message") or payload.get("msg") or "请求失败",
                ))
            paths = tuple(self.parse_paths(payload))
            if generation != self.generation:
                return
            self.paths.extend(path for path in paths if path not in self.paths)
            self.payloads.append((response.url, payload))
            self.received = True
        except Exception as exc:
            if generation == self.generation:
                self.error = str(exc)
                self.received = True
