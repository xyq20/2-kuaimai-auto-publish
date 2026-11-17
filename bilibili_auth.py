"""Keep B 站 login cookies and web storage in the dedicated local profile."""
import json
import math
from pathlib import Path
import time
from urllib.parse import urlsplit


def bili_host(host):
    host = (host or '').lower().lstrip('.')
    return host == 'bilibili.com' or host.endswith('.bilibili.com')


def bili_origin(origin):
    try:
        parsed = urlsplit(origin)
        return (parsed.scheme == 'https' and bili_host(parsed.hostname) and parsed.port in (None, 443)
                and not parsed.username and not parsed.password and not parsed.path and not parsed.query and not parsed.fragment)
    except (TypeError, ValueError):
        return False


def cookies(values):
    result = []
    for cookie in values:
        if not isinstance(cookie, dict) or not bili_host(cookie.get('domain')):
            continue
        expiry = cookie.get('expires', -1)
        if (not isinstance(cookie.get('name'), str) or not isinstance(cookie.get('value'), str)
                or not isinstance(expiry, (float, int)) or not math.isfinite(expiry)
                or (expiry > 0 and expiry <= time.time())):
            continue
        result.append({key: cookie[key] for key in ('name', 'value', 'domain', 'path', 'expires', 'httpOnly', 'secure', 'sameSite', 'partitionKey') if key in cookie})
    return result


def entries(values):
    return [item for item in values if isinstance(item, dict)
            and isinstance(item.get('name'), str) and isinstance(item.get('value'), str)]


async def save_auth(context, path, logger):
    path = Path(path)
    temporary = path.with_suffix('.tmp')
    try:
        state = await context.storage_state()
        state = {'cookies': cookies(state.get('cookies', [])),
                 'origins': [{'origin': item['origin'], 'localStorage': entries(item.get('localStorage', []))}
                             for item in state.get('origins', []) if bili_origin(item.get('origin'))], 'sessions': {}}
        for page in context.pages:
            try:
                data = await page.evaluate('''() => ({origin:location.origin,
                    entries:Object.entries(sessionStorage).map(([name,value])=>({name,value}))})''')
                if bili_origin(data.get('origin')):
                    state['sessions'][data['origin']] = entries(data['entries'])
            except Exception:
                continue
        if not state['cookies']:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        # Create with private permissions before writing any credentials.
        import os
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as stream:
            json.dump(state, stream, ensure_ascii=False)
        temporary.chmod(0o600)
        temporary.replace(path)
    except Exception:
        logger.warning('B 站登录状态保存失败；专用浏览器配置仍保留在本机')
    finally:
        temporary.unlink(missing_ok=True)


async def restore_auth(context, path, logger):
    if not Path(path).is_file():
        return
    try:
        state = json.loads(Path(path).read_text())
        key = lambda c: (c.get('name'), c.get('domain'), c.get('path'), c.get('partitionKey'))
        current = {key(c) for c in await context.cookies()}
        missing = [c for c in cookies(state.get('cookies', [])) if key(c) not in current]
        if missing:
            await context.add_cookies(missing)
        storage = {}
        for item in state.get('origins', []):
            if bili_origin(item.get('origin')):
                storage.setdefault(item['origin'], {})['localStorage'] = entries(item.get('localStorage', []))
        for origin, values in state.get('sessions', {}).items():
            if bili_origin(origin):
                storage.setdefault(origin, {})['sessionStorage'] = entries(values)
        await context.add_init_script('''(() => {
            const state=%s[location.origin]; if(!state)return;
            for(const kind of ['localStorage','sessionStorage'])for(const item of state[kind]||[])
                if(window[kind].getItem(item.name)===null)window[kind].setItem(item.name,item.value);
        })()''' % json.dumps(storage, ensure_ascii=True))
    except Exception:
        logger.warning('B 站登录状态无法恢复，将等待浏览器扫码登录')
