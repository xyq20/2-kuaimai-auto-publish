"""Local workbench: trusted product catalog and durable, serial CLI jobs."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import unicodedata
import uuid
from zipfile import BadZipFile

from .config import PROJECT_DIR, Settings
from .database import connect, utc_now
from .service import ApiError, require
from platform_registry import PLATFORM_SPECS, canonical_platform_name
from product_materials import MaterialPathError, current_month_directory, resolve_product_materials

PLATFORMS = {s.cli_name: s.display_name.replace("资料", "") for s in PLATFORM_SPECS if s.enabled_in_all}
PLATFORM_LABELS = {**PLATFORMS, 'dewu': '得物', 'bilibili': 'bilibili'}
ACTIVE = {"running", "waiting_review", "stopping"}
IDENTIFIER = re.compile(r"^[0-9a-f]{32}$")


def atomic_json(path: Path, data: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


class ProductCatalog:
    def __init__(self, root: Path, runs_root: Path = None, *, materials_root: Path = None):
        self.root = root.resolve()
        self.runs_root = runs_root
        self.materials_root = materials_root
        self._cache = {}

    @staticmethod
    def _folder_key(path: Path) -> tuple:
        """Return a stable product-folder key across SMB mount locations.

        The same share can be mounted at ``/Volumes/共享文件`` or at
        ``~/Desktop/共享文件``. Run summaries keep the path from the time of
        the run, so comparing absolute paths would hide the old processed
        records after a remount.
        """
        resolved = Path(path).expanduser().resolve()
        parts = resolved.parts
        try:
            share_index = parts.index("共享文件")
        except ValueError:
            return ("local", str(resolved))
        return ("smb-share", *parts[share_index + 1 :])

    def _safe(self, path: Path) -> bool:
        return path.resolve().is_relative_to(self.root)

    def paths(self) -> dict:
        if not self.root.is_dir():
            raise ApiError(503, "products_unavailable")
        try:
            workbooks = {}
            for path in sorted(self.root.glob("*/*")):
                if (
                    re.sub(r"\s+", "", path.name).casefold() == "产品信息.xlsx"
                    and self._safe(path) and path.is_file()
                ):
                    # One product per folder; prefer the canonical workbook
                    # when both a normal name and a whitespace variant exist.
                    if path.parent not in workbooks or path.name == "产品信息.xlsx":
                        workbooks[path.parent] = path
            return {
                hashlib.sha256(str(p.resolve()).encode()).hexdigest()[:32]: p
                for p in workbooks.values()
            }
        except OSError:
            raise ApiError(503, "products_unavailable") from None

    def path(self, product_id: str) -> Path:
        require(isinstance(product_id, str) and IDENTIFIER.fullmatch(product_id), 404, "product_not_found")
        path = self.paths().get(product_id)
        require(path is not None, 404, "product_not_found")
        return path

    def _base_saved_for(self, folder: Path) -> bool:
        if self.runs_root is None:
            return False
        folder_key = self._folder_key(folder)
        for summary_path in self.runs_root.glob("*/input-summary.json"):
            summary = read_json(summary_path, {})
            excel_path = summary.get("excel_path")
            if not excel_path or self._folder_key(Path(excel_path).parent) != folder_key:
                continue
            run = summary_path.parent
            for receipt_path in (
                run / "base" / "save-result.json",
                run / "base" / "base-save-result.json",
                run / "base" / "publish-result.json",
            ):
                receipt = read_json(receipt_path, {})
                if receipt.get("result") == 1:
                    return True
        return False

    def listing(self) -> list:
        paths = self.paths()
        progress = {self._folder_key(p.parent): set() for p in paths.values()}
        base_saved = {folder: False for folder in progress}
        if self.runs_root is not None:
            for summary_path in self.runs_root.glob("*/input-summary.json"):
                summary = read_json(summary_path, {})
                excel_path = summary.get("excel_path")
                if not excel_path:
                    continue
                folder = self._folder_key(Path(excel_path).parent)
                if folder not in progress:
                    continue
                run = summary_path.parent
                base_dir = run / "base"
                if base_dir.is_dir():
                    base_receipts = (
                        base_dir / "save-result.json",
                        base_dir / "base-save-result.json",
                        base_dir / "publish-result.json",
                    )
                    if any(read_json(receipt_path, {}).get("result") == 1
                           for receipt_path in base_receipts):
                        base_saved[folder] = True
                receipts = [*run.glob("save-result.json"), *run.glob("publish-result.json"),
                            *run.glob("*/save-result.json"), *run.glob("*/publish-result.json"),
                            *run.glob("*/base-save-result.json")]
                for receipt_path in receipts:
                    receipt = read_json(receipt_path, {})
                    if receipt.get("result") != 1:
                        continue
                    platform = receipt_path.parent.name
                    label = PLATFORMS.get(platform)
                    if label is None:
                        label = receipt.get("shop_publish", {}).get("platform")
                    if label is None:
                        validations = list(receipt_path.parent.glob("*-after-save-validation.json"))
                        label = next((PLATFORMS.get(p.name.split('-after-save')[0])
                                      for p in validations if p.name.split('-after-save')[0] in PLATFORMS), None)
                    progress[folder].add(label or "商品资料")
        return [{"id": key, "title": p.parent.name,
                 "processed": bool(progress[self._folder_key(p.parent)]),
                 "base_saved": base_saved[self._folder_key(p.parent)],
                 "processed_platforms": sorted(progress[self._folder_key(p.parent)])}
                for key, p in paths.items()]

    def detail(self, product_id: str) -> dict:
        path = self.path(product_id)
        try:
            signature = (path.stat().st_mtime_ns, path.stat().st_size)
            cached = self._cache.get(product_id)
            if cached and cached[0] == signature:
                detail = dict(cached[1])
                detail["base_saved"] = self._base_saved_for(path.parent)
                self._cache[product_id] = (signature, detail)
                return detail
            from openpyxl import load_workbook
            from kuaimai_erp import read_excel_fields, normalize_cell
            from douyin_data import field_lookup
            workbook = load_workbook(path, read_only=True, data_only=True)
            try:
                sheet = workbook[workbook.sheetnames[0]]
                fields = read_excel_fields(list(sheet.iter_rows(values_only=True)))
            finally:
                workbook.close()
            def value(*aliases):
                match = field_lookup(fields, *aliases)
                return normalize_cell(match[1]) if match else ""
            detail = {
                "id": product_id,
                "title": value("商品标题", "商品名称", "宝贝标题") or path.parent.name,
                "style_code": value("货号", "商家外部编码", "款式编码") or path.parent.name,
                "category": value("商品分类", "商品类目") or "以 Excel 类目为准",
                "folder": path.parent.name,
                "image": f"/api/launcher/products/{product_id}/image" if self.image(product_id, fields=fields) else None,
            }
            # The workbook has already been read above. Calling listing() here
            # would rescan every historical run before returning one product,
            # which made selecting a product visibly stall on the SMB share.
            detail["base_saved"] = self._base_saved_for(path.parent)
            self._cache[product_id] = (signature, detail)
            return detail
        except (OSError, ValueError, KeyError, BadZipFile):
            raise ApiError(422, "product_unreadable") from None

    def image(self, product_id: str, *, fields=None):
        workbook_path = self.path(product_id)
        folder = workbook_path.parent
        if self.materials_root is not None:
            from openpyxl import load_workbook
            from kuaimai_erp import normalize_cell, parse_spec_values, read_excel_fields
            from douyin_data import field_lookup
            try:
                if fields is None:
                    workbook = load_workbook(workbook_path, read_only=True, data_only=True)
                    try:
                        fields = read_excel_fields(workbook.worksheets[0].iter_rows(values_only=True))
                    finally:
                        workbook.close()
                def value(*aliases):
                    match = field_lookup(fields, *aliases)
                    return normalize_cell(match[1]) if match else ""
                folder = resolve_product_materials(
                    self.materials_root, workbook_path,
                    title=value("商品标题", "商品名称", "宝贝标题"),
                    style_code=value("货号", "商家外部编码", "款式编码"),
                    colors=parse_spec_values(value("颜色")),
                    nickname=value("商品简称", "产品简称", "款名"),
                )
            except (MaterialPathError, OSError, ValueError, BadZipFile):
                return None
        def safe_image(path):
            return path.resolve().is_relative_to(folder.resolve())
        for name in ("1：1主图", "1:1主图", "1：1 主图", "主图"):
            directory = folder / name
            if not safe_image(directory) or not directory.is_dir():
                continue
            for path in sorted(directory.iterdir()):
                if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"} and safe_image(path) and path.is_file():
                    return path
        return None

    def open_folder(self, product_id: str) -> dict:
        folder = self.path(product_id).parent.resolve()
        require(self._safe(folder) and folder.is_dir(), 404, "product_not_found")
        require(sys.platform == "darwin", 503, "folder_open_unavailable")
        try:
            subprocess.run(
                ["/usr/bin/open", str(folder)], check=True, timeout=10,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            raise ApiError(503, "folder_open_failed") from None
        return {"opened": True}


class DewuProductCatalog(ProductCatalog):
    """Dewu keeps its product discovery and saved drafts separate from ERP."""

    def paths(self) -> dict:
        if not self.root.is_dir():
            raise ApiError(503, 'dewu_products_unavailable')
        from dewu_data import natural_key
        try:
            paths = sorted((path for path in self.root.glob('*/产品信息.xlsx')
                            if path.is_file() and self._safe(path)), key=natural_key)
            return {hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:32]: path for path in paths}
        except OSError:
            raise ApiError(503, 'dewu_products_unavailable') from None

    def _progress(self):
        saved = set()
        if self.runs_root is not None:
            for path in self.runs_root.rglob('dewu-result.json'):
                report = read_json(path, {})
                if not isinstance(report, dict) or report.get('status') not in {'draft_saved', 'already_saved', 'submitted_for_review', 'already_submitted'}:
                    continue
                excel_path = report.get('excel_path')
                if not excel_path:
                    summary = read_json(path.parent / 'input-summary.json', {})
                    excel_path = summary.get('excel_path') if isinstance(summary, dict) else None
                if excel_path:
                    saved.add(self._folder_key(Path(excel_path).parent))
        return saved

    def listing(self) -> list:
        saved = self._progress()
        return [{'id': key, 'title': path.parent.name, 'source': 'dewu',
                 'processed': self._folder_key(path.parent) in saved,
                 'processed_platforms': ['得物'] if self._folder_key(path.parent) in saved else [],
                 'base_saved': False, 'runnable': True}
                for key, path in self.paths().items()]

    def detail(self, product_id: str) -> dict:
        path = self.path(product_id)
        try:
            signature = (path.stat().st_mtime_ns, path.stat().st_size)
            cached = self._cache.get(product_id)
            if cached and cached[0] == signature:
                detail = dict(cached[1])
            else:
                from openpyxl import load_workbook
                from kuaimai_erp import read_excel_fields, normalize_cell
                from douyin_data import field_lookup
                workbook = load_workbook(path, read_only=True, data_only=True)
                try:
                    fields = read_excel_fields(workbook.worksheets[0].iter_rows(values_only=True))
                finally:
                    workbook.close()
                def value(*aliases):
                    match = field_lookup(fields, *aliases)
                    return normalize_cell(match[1]) if match else ''
                detail = {'id': product_id, 'title': value('卖点提炼', '商品标题', '商品名称') or path.parent.name,
                          'style_code': value('货号', '款式编码') or path.parent.name,
                          'category': value('类目', '商品类目', '商品分类') or '以 Excel 类目为准',
                          'folder': path.parent.name, 'source': 'dewu', 'base_saved': False, 'runnable': True}
                self._cache[product_id] = (signature, dict(detail))
            detail['image'] = f'/api/launcher/products/{product_id}/image?source=dewu' if self.image(product_id) else None
            detail['processed'] = self._folder_key(path.parent) in self._progress()
            detail['processed_platforms'] = ['得物'] if detail['processed'] else []
            return detail
        except (OSError, ValueError, KeyError, BadZipFile):
            raise ApiError(422, 'product_unreadable') from None

    def image(self, product_id: str, *, fields=None):
        from dewu_data import natural_key
        folder = self.path(product_id).parent.resolve()
        directory = folder / '白底图'
        if not directory.is_dir() or not directory.resolve().is_relative_to(folder):
            return None
        paths = sorted((path for path in directory.rglob('*')
                        if path.is_file() and path.suffix.lower() in {'.jpg', '.jpeg', '.png'}
                        and path.resolve().is_relative_to(directory.resolve())
                        and not any(part.startswith('.') for part in path.relative_to(directory).parts)), key=natural_key)
        return paths[0] if paths else None


class BilibiliProductCatalog(ProductCatalog):
    """Product workbooks and adjacent assets from the dedicated B 站 share."""

    def _progress(self):
        saved = set()
        if self.runs_root is not None:
            for path in self.runs_root.glob('*/bilibili-result.json'):
                report = read_json(path, {})
                if (isinstance(report, dict) and report.get('status') in {'draft_saved', 'already_saved', 'published', 'already_published'}
                        and report.get('steps', {}).get('persisted_readback', {}).get('verified') and report.get('excel_path')):
                    saved.add(self._folder_key(Path(report['excel_path']).parent))
        return saved

    def listing(self):
        saved = self._progress()
        return [{'id': key, 'title': path.parent.name, 'source': 'bilibili', 'base_saved': False,
                 'processed': self._folder_key(path.parent) in saved,
                 'processed_platforms': ['bilibili'] if self._folder_key(path.parent) in saved else []}
                for key, path in self.paths().items()]

    def detail(self, product_id):
        detail = super().detail(product_id)
        detail.update(source='bilibili', base_saved=False)
        detail['processed'] = self._folder_key(self.path(product_id).parent) in self._progress()
        detail['processed_platforms'] = ['bilibili'] if detail['processed'] else []
        detail['image'] = f'/api/launcher/products/{product_id}/image?source=bilibili' if self.image(product_id) else None
        return detail


class MonthlyProductCatalog(ProductCatalog):
    """Discover current-month artwork folders with a confirmed product workbook."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._field_cache = {}

    def _fields(self, path):
        from openpyxl import load_workbook
        from kuaimai_erp import read_excel_fields
        signature = (path.stat().st_mtime_ns, path.stat().st_size)
        cached = self._field_cache.get(path)
        if cached and cached[0] == signature:
            return cached[1]
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            fields = read_excel_fields(workbook.worksheets[0].iter_rows(values_only=True))
        finally:
            workbook.close()
        self._field_cache[path] = (signature, fields)
        return fields

    def entries(self):
        try:
            month = current_month_directory(self.materials_root).resolve()
            folders = [p for p in sorted(month.iterdir()) if p.is_dir() and not p.name.startswith('.')
                       and p.resolve().is_relative_to(month)]
            external = {}
            # Excel beside the artwork wins. The old library remains a fallback.
            try:
                legacy_paths = super().paths().values() if self.root.is_dir() else ()
            except ApiError:
                legacy_paths = ()
            for path in legacy_paths:
                from douyin_data import field_lookup
                from kuaimai_erp import normalize_cell, parse_spec_values, AutomationError
                try:
                    fields = self._fields(path)
                    def value(*aliases):
                        match = field_lookup(fields, *aliases)
                        return normalize_cell(match[1]) if match else ''
                    folder = resolve_product_materials(
                        self.materials_root, path, title=value('商品标题', '商品名称', '宝贝标题'),
                        style_code=value('货号', '商家外部编码', '款式编码'),
                        colors=parse_spec_values(value('颜色')),
                        nickname=value('商品简称', '产品简称', '款名'),
                    )
                    external.setdefault(folder, []).append(path)
                except (MaterialPathError, OSError, ValueError, BadZipFile, AutomationError):
                    continue
            entries = {}
            for folder in folders:
                local = [p for p in folder.iterdir() if p.is_file()
                         and re.sub(r'\s+', '', p.name).casefold() == '产品信息.xlsx'
                         and p.resolve().is_relative_to(folder.resolve())]
                local.sort(key=lambda p: (p.name != '产品信息.xlsx', p.name))
                candidates = local[:1] or external.get(folder.resolve(), [])
                entries[hashlib.sha256(str(folder.resolve()).encode()).hexdigest()[:32]] = {
                    'folder': folder, 'excel': candidates[0] if len(candidates) == 1 else None,
                    'excel_status': 'ready' if len(candidates) == 1 else 'ambiguous' if candidates else 'missing',
                    'history_folders': [p.parent for p in external.get(folder.resolve(), [])]
                                       if len(external.get(folder.resolve(), [])) == 1 else [],
                }
            return entries
        except (MaterialPathError, OSError):
            raise ApiError(503, 'products_unavailable') from None

    def _entry(self, product_id):
        require(isinstance(product_id, str) and IDENTIFIER.fullmatch(product_id), 404, 'product_not_found')
        entry = self.entries().get(product_id)
        require(entry is not None, 404, 'product_not_found')
        return entry

    def paths(self):
        return {key: entry['excel'] for key, entry in self.entries().items() if entry['excel'] is not None}

    def path(self, product_id):
        entry = self._entry(product_id)
        require(entry['excel_status'] != 'ambiguous', 422, 'product_excel_ambiguous')
        require(entry['excel'] is not None, 422, 'product_excel_missing')
        return entry['excel']

    def _progress(self, entries):
        progress = {key: {'base_saved': False, 'platforms': set()} for key in entries}
        aliases = {}
        for key, entry in entries.items():
            aliases[self._folder_key(entry['folder'])] = key
            if entry['excel']:
                aliases[self._folder_key(entry['excel'].parent)] = key
            for folder in entry['history_folders']:
                aliases[self._folder_key(folder)] = key
        if self.runs_root is None:
            return progress
        for summary_path in self.runs_root.glob('*/input-summary.json'):
            summary = read_json(summary_path, {})
            key = aliases.get(self._folder_key(Path(summary['product_dir']))) if summary.get('product_dir') else None
            if key is None and summary.get('excel_path'):
                key = aliases.get(self._folder_key(Path(summary['excel_path']).parent))
            if key is None:
                continue
            run = summary_path.parent
            receipts = [*run.glob('save-result.json'), *run.glob('publish-result.json'),
                        *run.glob('*/save-result.json'), *run.glob('*/publish-result.json'),
                        *run.glob('*/base-save-result.json')]
            for receipt_path in receipts:
                receipt = read_json(receipt_path, {})
                if receipt.get('result') != 1:
                    continue
                platform = receipt_path.parent.name
                if platform == 'base':
                    progress[key]['base_saved'] = True
                label = PLATFORMS.get(platform) or receipt.get('shop_publish', {}).get('platform')
                if label is None:
                    label = next((PLATFORMS.get(p.name.split('-after-save')[0])
                                  for p in receipt_path.parent.glob('*-after-save-validation.json')
                                  if p.name.split('-after-save')[0] in PLATFORMS), None)
                progress[key]['platforms'].add(label or '商品资料')
        return progress

    def _base_saved_for(self, folder):
        entries = self.entries()
        key = next((key for key, e in entries.items()
                    if folder == e['folder'] or (e['excel'] and folder == e['excel'].parent)), None)
        return self._progress(entries)[key]['base_saved'] if key else False

    def listing(self):
        entries = self.entries()
        progress = self._progress(entries)
        return [{'id': key, 'title': e['folder'].name, 'folder': e['folder'].name,
                 'image': f'/api/launcher/products/{key}/image',
                 'excel_status': e['excel_status'], 'runnable': e['excel'] is not None,
                 'processed': bool(progress[key]['platforms']), 'base_saved': progress[key]['base_saved'],
                 'processed_platforms': sorted(progress[key]['platforms'])}
                for key, e in entries.items() if e['excel'] is not None]

    def detail(self, product_id):
        entry = self._entry(product_id)
        if entry['excel'] is not None:
            detail = super().detail(product_id)
        else:
            detail = {'id': product_id, 'title': entry['folder'].name, 'style_code': '',
                      'category': '找到多个 Excel，请确认产品资料' if entry['excel_status'] == 'ambiguous'
                                  else '待补充产品信息.xlsx',
                      'base_saved': self._base_saved_for(entry['folder']),
                      'image': f'/api/launcher/products/{product_id}/image' if self.image(product_id) else None}
        return {**detail, 'folder': entry['folder'].name,
                'excel_status': entry['excel_status'], 'runnable': entry['excel'] is not None}

    def image(self, product_id, *, fields=None):
        folder = self._entry(product_id)['folder']
        for name in ('1：1主图', '1:1主图', '1：1 主图', '主图'):
            directory = folder / name
            if not directory.is_dir() or not directory.resolve().is_relative_to(folder.resolve()):
                continue
            for path in sorted(directory.iterdir()):
                if (path.suffix.lower() in {'.jpg', '.jpeg', '.png', '.webp'} and path.is_file()
                        and path.resolve().is_relative_to(folder.resolve())):
                    return path
        return None

    def open_folder(self, product_id):
        folder = self._entry(product_id)['folder'].resolve()
        require(sys.platform == 'darwin', 503, 'folder_open_unavailable')
        try:
            subprocess.run(['/usr/bin/open', str(folder)], check=True, timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            raise ApiError(503, 'folder_open_failed') from None
        return {'opened': True}


def build_command(product: Path, config: dict, run_id: str, output_dir: Path, settings: Settings) -> list:
    operation = config.get("operation", "platforms")
    require(operation in {"platforms", "base", "create", "dewu", "bilibili"}, 422, "invalid_operation")
    mode = config.get("mode")
    require(mode in {"preview", "save", "publish"}, 422, "invalid_mode")
    names = config.get("platforms")
    if operation == "platforms":
        require(isinstance(names, list) and 0 < len(names) <= len(PLATFORMS), 422, "invalid_platforms")
        require(all(isinstance(n, str) and n in PLATFORMS for n in names), 422, "invalid_platforms")
        require(len(set(names)) == len(names), 422, "invalid_platforms")
    include_base = config.get("include_base", False)
    require(type(include_base) is bool, 422, "invalid_include_base")
    require(
        not include_base or (operation == "platforms" and mode in {"save", "publish"}),
        422,
        "invalid_include_base",
    )
    require(type(config.get("learning")) is bool, 422, "invalid_learning")
    if operation == 'bilibili':
        require(names == ['bilibili'], 422, 'invalid_platforms')
        require(product.resolve().is_relative_to(settings.bilibili_products_root.expanduser().resolve()),
                422, 'product_not_found')
        require(not include_base, 422, 'invalid_include_base')
        require(mode != 'publish' or config.get('confirm_publish') is True,
                422, 'publish_confirmation_required')
        require(not config['learning'] or settings.device_token, 503, 'review_not_configured')
        command = [sys.executable, '-u', str(PROJECT_DIR / 'kuaimai_erp.py'),
                   '--excel-url', str(product), '--platform', 'bilibili',
                   '--bilibili-root', str(settings.bilibili_products_root),
                   {'preview': '--no-save', 'save': '--save-only', 'publish': '--bilibili-publish'}[mode],
                   '--run-id', run_id, '--output-dir', str(output_dir)]
        if config['learning']:
            command += ['--learning-enabled', '--learning-api-url', f'http://127.0.0.1:{settings.review_port}']
        return command
    if operation == 'dewu':
        require(names == ['dewu'], 422, 'invalid_platforms')
        require(mode in {'save', 'publish'}, 422, 'invalid_mode')
        require(mode != 'publish' or config.get('confirm_publish') is True,
                422, 'review_submission_confirmation_required')
        require(not config['learning'] or settings.device_token, 503, 'review_not_configured')
        require(product.resolve().is_relative_to(settings.dewu_products_root.expanduser().resolve()),
                422, 'product_not_found')
        command = [sys.executable, '-u', str(PROJECT_DIR / 'kuaimai_erp.py'),
                '--excel-url', str(product), '--platform', 'dewu',
                '--dewu-root', str(settings.dewu_products_root),
                '--dewu-submit-review' if mode == 'publish' else '--save-only',
                '--run-id', run_id, '--output-dir', str(output_dir)]
        if config['learning']:
            command += ['--learning-enabled', '--learning-api-url', f'http://127.0.0.1:{settings.review_port}']
        return command
    # Base editing currently saves as part of its workflow. Never expose it as a preview.
    require(operation == "platforms" or mode == "save", 422, "base_requires_save")
    require(mode != "publish" or config.get("confirm_publish") is True, 422, "publish_confirmation_required")
    learning = config["learning"] and operation == "platforms"
    require(not learning or settings.device_token, 503, "review_not_configured")
    command_platforms = (["base", *names] if include_base else names)
    command = [sys.executable, "-u", str(PROJECT_DIR / "kuaimai_erp.py"),
               "--excel-url", str(product), "--platform", ",".join(command_platforms) if operation == "platforms" else "base",
               "--run-id", run_id, "--output-dir", str(output_dir),
               {"preview": "--no-save", "save": "--save-only", "publish": "--save"}[mode]]
    if operation == "create":
        command.append("--create-product")
    if names == ["taobao"] and operation == "platforms" and mode != "preview" and not include_base:
        command.append("--allow-taobao-save-once" if mode == "save" else "--allow-taobao-publish-once")
    if learning:
        command += ["--learning-enabled", "--learning-api-url", f"http://127.0.0.1:{settings.review_port}"]
    return command


def process_command(pid: int) -> str:
    try:
        return subprocess.check_output(["ps", "-ww", "-p", str(pid), "-o", "command="], text=True, errors="replace", timeout=3, stderr=subprocess.DEVNULL).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return ""


def external_runner_active() -> bool:
    """Also respect a runner started from the existing command-line launcher."""
    try:
        lines = subprocess.check_output(["ps", "-ww", "-Ao", "comm=,args="], text=True, errors="replace", timeout=3).splitlines()
        return any(re.match(r"\S*(?:python|Python)\S*\s", line.strip()) and
                   re.search(r"(?:^|[/\s])kuaimai_erp\.py(?:\s|$)", line) for line in lines)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise ApiError(503, "process_check_failed") from None


class Launcher:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.catalog = ProductCatalog(settings.products_root, PROJECT_DIR / "output/kuaimai/runs")
        self.dewu_catalog = DewuProductCatalog(settings.dewu_products_root, PROJECT_DIR / 'output/dewu/runs')
        self.bilibili_catalog = BilibiliProductCatalog(settings.bilibili_products_root, PROJECT_DIR / 'output/bilibili/runs')
        self.root = settings.data_dir.resolve() / "launcher"
        self.root.mkdir(parents=True, exist_ok=True)
        self._mutex = threading.Lock()
        self._children = {}

    def catalog_for(self, source='default'):
        require(source in {'default', 'dewu', 'bilibili'}, 422, 'invalid_product_source')
        return {'default': self.catalog, 'dewu': self.dewu_catalog, 'bilibili': self.bilibili_catalog}[source]

    @contextmanager
    def locked(self):
        with self._mutex, (self.root / ".lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _directory(self, job_id: str) -> Path:
        require(isinstance(job_id, str) and IDENTIFIER.fullmatch(job_id), 404, "job_not_found")
        return self.root / job_id

    def _owned(self, job: dict) -> bool:
        child = self._children.get(job["id"])
        if child is not None and child.poll() is not None:
            self._children.pop(job["id"], None)
            return False
        command = process_command(job.get("pid", 0))
        return "local_review.launcher_worker" in command and str(self.root / job["id"]) in command

    def _refresh(self, job: dict) -> dict:
        child = self._children.get(job["id"])
        if child is not None and child.poll() is not None:
            self._children.pop(job["id"], None)
        if job["status"] in ACTIVE:
            receipt = read_json(self.root / job["id"] / "result.json")
            if receipt is not None:
                job.update(status="stopped" if receipt.get("cancelled") or job.get("stop_requested") else
                           "completed" if receipt["exit_code"] == 0 else "failed",
                           exit_code=receipt["exit_code"], finished_at=receipt["finished_at"])
            elif not self._owned(job):
                job.update(status="stopped" if job.get("stop_requested") else "interrupted", finished_at=utc_now())
            if job["status"] in {"stopped", "interrupted", "failed"}:
                # A dead runner cannot consume a review. Hide its stale queue using
                # the same terminal checkpoint rule as the existing review center.
                with connect(self.settings) as connection:
                    connection.execute(
                        "UPDATE run_checkpoints SET status='failed',version=version+1,updated_at=? "
                        "WHERE run_id=? AND status NOT IN ('completed','failed','cancelled')",
                        (utc_now(), job["id"]),
                    )
            atomic_json(self.root / job["id"] / "job.json", job)
        return job

    def _jobs(self) -> list:
        return sorted([self._refresh(job) for path in self.root.glob("*/job.json")
                       if (job := read_json(path))], key=lambda j: j["created_at"], reverse=True)

    def _public(self, job: dict, *, logs=False) -> dict:
        result = {k: v for k, v in job.items() if k not in {"pid", "output_dir", "request_key"}}
        stages = {name: "pending" for name in job["platforms"]}
        receipts = read_json(Path(job["output_dir"]) / "all-platform-result.json", [])
        for item in receipts:
            if item.get("platform") in stages:
                stages[item["platform"]] = item["status"]
        with connect(self.settings) as connection:
            checkpoint = connection.execute("SELECT current_index,status FROM run_checkpoints WHERE run_id=?", (job["id"],)).fetchone()
            pending = connection.execute("SELECT COUNT(*) FROM review_tasks WHERE run_id=? AND status IN ('pending','claimed')", (job["id"],)).fetchone()[0]
            review_tasks = [dict(row) for row in connection.execute(
                "SELECT id,platform_id,field_id,snapshot_version,status FROM review_tasks WHERE run_id=?",
                (job["id"],),
            )] if logs else []
        current = checkpoint["current_index"] if checkpoint else next((i for i, s in enumerate(stages.values()) if s != "success"), len(stages))
        if checkpoint:
            for name in list(stages)[:current]:
                if stages[name] != "skipped":
                    stages[name] = "success"
        if job["status"] == "completed":
            stages = {name: "skipped" if status == "skipped" else "success" for name, status in stages.items()}
        elif job["status"] in ACTIVE and current < len(stages):
            waiting = pending > 0 and checkpoint and checkpoint["status"] == "waiting_review"
            result["status"] = "stopping" if job["status"] == "stopping" else "waiting_review" if waiting else "running"
            stages[list(stages)[current]] = result["status"]
        elif current < len(stages):
            stages[list(stages)[current]] = job["status"]
        for task in review_tasks:
            task["platform_id"] = canonical_platform_name(task["platform_id"])
        result.update(stages=[{"platform": name, "status": status} for name, status in stages.items()], pending_reviews=pending)
        if job.get('operation') == 'bilibili':
            from bilibili_listing import valid_edit_url
            report = read_json(Path(job['output_dir']) / 'bilibili-result.json', {})
            if isinstance(report, dict):
                result['bilibili_status'] = report.get('status')
                result['error'] = str(report.get('error') or '')
                draft_url = report.get('draft_url')
                if valid_edit_url(draft_url, draft_only=True):
                    result['draft_url'] = draft_url
        if job.get('operation') == 'dewu':
            from dewu_listing import valid_draft_url
            report = read_json(Path(job['output_dir']) / 'dewu-result.json', {})
            if isinstance(report, dict):
                result['dewu_status'] = report.get('status')
                result['dewu_error'] = str(report.get('error') or '')
                draft_url = report.get('draft_url')
                try:
                    valid_url = isinstance(draft_url, str) and valid_draft_url(draft_url)
                except ValueError:
                    valid_url = False
                if valid_url:
                    result['draft_url'] = draft_url
        if logs:
            result["review_tasks"] = review_tasks
            report = read_json(Path(job["output_dir"]) / "model-judgments.json", {})
            result["model_decisions"] = report.get("decisions", [])
            result["model_events"] = report.get("events", [])
            result["model_summary"] = report.get("summary")
            try:
                with (self.root / job["id"] / "run.log").open("rb") as handle:
                    handle.seek(0, 2)
                    handle.seek(max(0, handle.tell() - 48000))
                    result["log"] = handle.read().decode("utf-8", errors="replace")
            except OSError:
                result["log"] = ""
            if result["model_summary"] is None:
                errors = [line.split("ERROR |", 1)[-1].strip() for line in result["log"].splitlines() if "ERROR |" in line]
                result["model_summary"] = {
                    "api_call_count": None,
                    "decision_count": len(result["model_decisions"]),
                    "adopted_count": sum(row.get("status") == "adopted" for row in result["model_decisions"]),
                    "review_count": sum(row.get("status") == "review_required" for row in result["model_decisions"]),
                    "stop_reason": errors[-1] if errors else "",
                    "not_called_reason": "旧版运行记录未记录调用次数，无法确认是否调用模型。" if report else
                        "尚无模型诊断记录。任务执行到需要判断的字段时会显示结果。" if job["status"] in ACTIVE else
                        "本次运行未生成模型诊断记录，请结合运行错误排查。",
                }
            for secret in (self.settings.device_token, self.settings.model_api_key, self.settings.deepseek_api_key):
                if secret:
                    result["log"] = result["log"].replace(secret, "[已隐藏]")
        return result

    def history(self) -> list:
        with self.locked():
            return [self._public(job) for job in self._jobs()]

    def search_history(self, query: str) -> dict:
        terms = [part.casefold() for part in unicodedata.normalize("NFKC", query).split()]
        with self.locked():
            matches = []
            for job in self._jobs():
                haystack = unicodedata.normalize("NFKC", " ".join(
                    str(value) for value in (
                        job.get("style_code", ""), job.get("title", ""),
                        job.get("created_by", ""), job.get("created_at", ""),
                        job.get("status", ""), job.get("mode", ""),
                        *(job.get("platforms") or ()),
                        *(PLATFORM_LABELS.get(name, name) for name in job.get("platforms") or ()),
                    )
                )).casefold()
                if all(term in haystack for term in terms):
                    matches.append(job)
            return {"jobs": [self._public(job) for job in matches], "total_matches": len(matches)}

    def get(self, job_id: str) -> dict:
        with self.locked():
            job = read_json(self._directory(job_id) / "job.json")
            require(job is not None, 404, "job_not_found")
            return self._public(self._refresh(job), logs=True)

    def start(self, payload: dict, username: str) -> dict:
        key = payload.get("request_key")
        require(isinstance(key, str) and IDENTIFIER.fullmatch(key), 422, "invalid_request_key")
        with self.locked():
            jobs = self._jobs()
            existing = next((job for job in jobs if job["request_key"] == key), None)
            if existing:
                return self._public(existing)
            require(not any(job["status"] in ACTIVE for job in jobs), 409, "job_already_running")
            require(not external_runner_active(), 409, "external_job_running")
            operation = payload.get("operation", "platforms")
            require(operation in {'platforms', 'base', 'create', 'dewu', 'bilibili'}, 422, 'invalid_operation')
            if operation in {'dewu', 'bilibili'}:
                require(payload.get('include_base', False) is False, 422, 'invalid_include_base')
            catalog = self.catalog_for(operation if operation in {'dewu', 'bilibili'} else 'default')
            product = catalog.path(payload.get("product_id"))
            detail = catalog.detail(payload["product_id"])
            auto_base = (
                operation == "platforms"
                and payload.get("mode") in {"save", "publish"}
                and not detail.get("base_saved", False)
            )
            command_payload = {**payload, "include_base": auto_base}
            job_id = uuid.uuid4().hex
            output_dir = PROJECT_DIR / (f'output/{operation}/runs' if operation in {'dewu', 'bilibili'} else 'output/kuaimai/runs') / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + job_id[:8])
            command = build_command(product, command_payload, job_id, output_dir, self.settings)
            directory = self.root / job_id
            directory.mkdir()
            atomic_json(directory / "launch.json", {"argv": command})
            job_platforms = ((["base"] if auto_base else []) + payload["platforms"]
                             if operation == "platforms" else [operation] if operation in {'dewu', 'bilibili'} else ["base"])
            job = {"id": job_id, "request_key": key, "product_id": detail["id"], "title": detail["title"],
                   "style_code": detail["style_code"], "platforms": job_platforms,
                   "base_auto_added": auto_base, "mode": payload["mode"], "operation": operation,
                   "learning": "--learning-enabled" in command, "status": "running", "created_at": utc_now(),
                   "created_by": username, "output_dir": str(output_dir)}
            env = os.environ.copy()
            env["KUAIMAI_LEARNING_DEVICE_TOKEN"] = self.settings.device_token
            env["PYTHONUNBUFFERED"] = "1"
            atomic_json(directory / "job.json", job)
            try:
                with (directory / "run.log").open("ab") as log:
                    child = subprocess.Popen([sys.executable, "-u", "-m", "local_review.launcher_worker", str(directory)],
                                             cwd=PROJECT_DIR, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                             start_new_session=True)
                self._children[job_id] = child
                job["pid"] = child.pid
            except OSError:
                job.update(status="failed", finished_at=utc_now(), exit_code=-1)
                atomic_json(directory / "job.json", job)
                raise ApiError(503, "launch_failed") from None
            atomic_json(directory / "job.json", job)
            return self._public(job)

    def stop(self, job_id: str) -> dict:
        with self.locked():
            directory = self._directory(job_id)
            job = read_json(directory / "job.json")
            require(job is not None, 404, "job_not_found")
            self._refresh(job)
            if job["status"] in ACTIVE and self._owned(job):
                job.update(status="stopping", stop_requested=True)
                atomic_json(directory / "job.json", job)
                try:
                    os.killpg(job["pid"], signal.SIGINT)
                except ProcessLookupError:
                    pass
            return self._public(job)
