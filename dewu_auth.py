"""Persist Dewu session credentials independently from the ERP browser state."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urlsplit


_RESTORED_MARKER = '__dewu_auth_restored_v1'


def _dewu_host(host):
    host = (host or '').lower()
    return host == 'dewu.com' or host.endswith('.dewu.com')


def _dewu_origin(origin):
    if not isinstance(origin, str):
        return False
    try:
        parsed = urlsplit(origin)
        return (parsed.scheme == 'https' and _dewu_host(parsed.hostname)
                and parsed.port in (None, 443) and not parsed.username
                and not parsed.password and not parsed.path
                and not parsed.query and not parsed.fragment)
    except ValueError:
        return False


def _entries(entries):
    if not isinstance(entries, list):
        return []
    return [{'name': item['name'], 'value': item['value']} for item in entries
            if isinstance(item, dict) and isinstance(item.get('name'), str)
            and isinstance(item.get('value'), str) and item['name'] != _RESTORED_MARKER]


def _cookie(cookie):
    if not isinstance(cookie, dict):
        return None
    domain = cookie.get('domain')
    if not isinstance(domain, str) or not _dewu_host(domain.removeprefix('.')):
        return None
    if not isinstance(cookie.get('name'), str) or not isinstance(cookie.get('value'), str):
        return None
    if not isinstance(cookie.get('path'), str) or not cookie['path'].startswith('/'):
        return None
    expiry = cookie.get('expires', -1)
    if (not isinstance(expiry, (float, int)) or not math.isfinite(expiry)
            or (expiry > 0 and expiry <= time.time())):
        return None
    allowed = ('name', 'value', 'domain', 'path', 'expires', 'httpOnly', 'secure', 'sameSite', 'partitionKey')
    return {key: cookie[key] for key in allowed if key in cookie}


def _cookie_key(cookie):
    return (cookie.get('name'), cookie.get('domain', '').lower().lstrip('.'),
            cookie.get('path'), cookie.get('partitionKey'))


async def save_dewu_auth_state(context, state_path: Path, logger) -> bool:
    """Save session cookies and web storage; credential values never enter logs."""
    temporary = None
    try:
        snapshot = await context.storage_state()
        cookies = [safe for item in snapshot.get('cookies', []) if (safe := _cookie(item))]
        origins = [{'origin': item['origin'], 'localStorage': _entries(item.get('localStorage'))}
                   for item in snapshot.get('origins', [])
                   if isinstance(item, dict) and _dewu_origin(item.get('origin'))]
        sessions = {}
        for page in context.pages:
            try:
                state = await page.evaluate('''() => ({
                    origin: location.origin,
                    entries: Object.entries(sessionStorage).map(([name, value]) => ({name, value}))
                })''')
                if _dewu_origin(state.get('origin')):
                    values = sessions.setdefault(state['origin'], {})
                    values.update({item['name']: item['value'] for item in _entries(state.get('entries'))})
            except Exception:
                # A closing or navigating tab must not prevent saving other tabs.
                continue
        sessions = {origin: [{'name': name, 'value': value} for name, value in entries.items()]
                    for origin, entries in sessions.items()}
        if not cookies and not any(item['localStorage'] for item in origins) and not any(sessions.values()):
            logger.info('得物登录状态为空，保留已有登录状态文件')
            return False
        state = {'cookies': cookies, 'origins': origins, 'sessionStorage': sessions}
        state_path = Path(state_path)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=state_path.name + '.', suffix='.tmp', dir=state_path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(state, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(state_path)
        state_path.chmod(0o600)
        logger.info('已保存得物登录状态：%s 个 Cookie，%s 个站点', len(cookies), len(origins))
        return True
    except Exception:
        logger.warning('得物登录状态保存失败，浏览器配置仍保留在本机')
        return False
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def restore_dewu_auth_state(context, state_path: Path, logger) -> bool:
    """Fill missing session state while preserving newer persistent profile values."""
    if not Path(state_path).is_file():
        return False
    try:
        state = json.loads(Path(state_path).read_text(encoding='utf-8'))
        current = {_cookie_key(cookie) for cookie in await context.cookies()}
        cookies = [safe for item in state.get('cookies', [])
                   if (safe := _cookie(item)) and _cookie_key(safe) not in current]
        if cookies:
            await context.add_cookies(cookies)
        storage = {}
        for item in state.get('origins', []):
            if isinstance(item, dict) and _dewu_origin(item.get('origin')):
                storage.setdefault(item['origin'], {})['localStorage'] = _entries(item.get('localStorage'))
        for origin, entries in state.get('sessionStorage', {}).items():
            if _dewu_origin(origin):
                storage.setdefault(origin, {})['sessionStorage'] = _entries(entries)
        if storage:
            serialized = json.dumps(storage, ensure_ascii=True)
            await context.add_init_script('''(() => {
                const state = %s[location.origin];
                if (!state || sessionStorage.getItem(%s)) return;
                for (const kind of ['localStorage', 'sessionStorage']) {
                    for (const item of state[kind] || []) {
                        if (window[kind].getItem(item.name) === null)
                            window[kind].setItem(item.name, item.value);
                    }
                }
                sessionStorage.setItem(%s, '1');
            })();''' % (serialized, json.dumps(_RESTORED_MARKER), json.dumps(_RESTORED_MARKER)))
        logger.info('已恢复得物登录状态：%s 个 Cookie，%s 个站点', len(cookies), len(storage))
        return True
    except Exception:
        logger.warning('得物登录状态无法恢复，将等待手工登录')
        return False
