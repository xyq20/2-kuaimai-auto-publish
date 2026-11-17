"""Credential isolation and real-browser session restoration regressions."""
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock

from playwright.async_api import async_playwright

from dewu_auth import restore_dewu_auth_state, save_dewu_auth_state


def cookie(domain='.dewu.com', value='saved-secret', name='session', expires=-1):
    return dict(name=name, value=value, domain=domain, path='/', expires=expires,
                httpOnly=True, secure=True, sameSite='Lax')


class DewuAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'auth-state.json'
        self.logger = Mock()

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def test_save_excludes_other_platforms_and_domain_suffix_spoofs(self):
        context = Mock()
        context.storage_state = AsyncMock(return_value={
            'cookies': [cookie(), cookie('.superboss.cc'), cookie('.dewu.com.evil.test'), cookie('evildewu.com')],
            'origins': [
                {'origin': 'https://stark.dewu.com', 'localStorage': [{'name': 'token', 'value': 'local-secret'}]},
                {'origin': 'https://dewu.com.evil.test', 'localStorage': [{'name': 'token', 'value': 'bad'}]},
            ]})
        page = Mock()
        page.evaluate = AsyncMock(return_value={
            'origin': 'https://stark.dewu.com',
            'entries': [{'name': 'temporary-token', 'value': 'session-secret'}]})
        context.pages = [page]
        self.assertTrue(await save_dewu_auth_state(context, self.path, self.logger))
        saved = json.loads(self.path.read_text())
        self.assertEqual([item['domain'] for item in saved['cookies']], ['.dewu.com'])
        self.assertEqual(saved['cookies'][0]['expires'], -1)
        self.assertEqual(len(saved['origins']), 1)
        self.assertEqual(saved['sessionStorage']['https://stark.dewu.com'][0]['value'], 'session-secret')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('secret', str(self.logger.mock_calls))

    async def test_restore_keeps_newer_cookies_and_drops_expired_and_foreign_ones(self):
        self.path.write_text(json.dumps({'cookies': [cookie(), cookie(name='extra'),
                                                   cookie(expires=1, name='expired'), cookie('.superboss.cc')]}))
        context = Mock()
        context.cookies = AsyncMock(return_value=[cookie(value='newer')])
        context.add_cookies = AsyncMock()
        context.add_init_script = AsyncMock()
        self.assertTrue(await restore_dewu_auth_state(context, self.path, self.logger))
        added = context.add_cookies.call_args.args[0]
        self.assertEqual([item['name'] for item in added], ['extra'])
        self.assertNotIn('secret', str(self.logger.mock_calls))

    async def test_missing_or_invalid_state_falls_back_without_exposing_content(self):
        context = Mock()
        self.assertFalse(await restore_dewu_auth_state(context, self.path, self.logger))
        self.path.write_text('secret-token-not-json')
        self.assertFalse(await restore_dewu_auth_state(context, self.path, self.logger))
        self.assertNotIn('secret-token', str(self.logger.mock_calls))

    async def test_empty_browser_does_not_replace_saved_login(self):
        self.path.write_text('original')
        context = Mock()
        context.pages = []
        context.storage_state = AsyncMock(return_value={'cookies': [], 'origins': []})
        self.assertFalse(await save_dewu_auth_state(context, self.path, self.logger))
        self.assertEqual(self.path.read_text(), 'original')

    async def test_real_browser_reopens_with_session_cookie_and_web_storage(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel='chrome', headless=True)
            try:
                first = await browser.new_context()
                await first.route('https://stark.dewu.com/**', lambda route: route.fulfill(body='<html>fixture</html>'))
                page = await first.new_page()
                await page.goto('https://stark.dewu.com/test')
                await first.add_cookies([cookie()])
                await page.evaluate("localStorage.setItem('token', 'local-secret'); sessionStorage.setItem('sessionToken', 'tab-secret')")
                self.assertTrue(await save_dewu_auth_state(first, self.path, logging.getLogger(__name__)))
                await first.close()
                second = await browser.new_context()
                await second.route('https://stark.dewu.com/**', lambda route: route.fulfill(body='<html>fixture</html>'))
                # Current profile values take priority over older snapshots.
                await second.add_init_script("localStorage.setItem('token', 'current-local')")
                self.assertTrue(await restore_dewu_auth_state(second, self.path, logging.getLogger(__name__)))
                reopened = await second.new_page()
                await reopened.goto('https://stark.dewu.com/test')
                self.assertEqual((await second.cookies())[0]['value'], 'saved-secret')
                self.assertEqual(await reopened.evaluate("localStorage.getItem('token')"), 'current-local')
                self.assertEqual(await reopened.evaluate("sessionStorage.getItem('sessionToken')"), 'tab-secret')
                await reopened.evaluate("sessionStorage.removeItem('sessionToken')")
                await reopened.reload()
                self.assertIsNone(await reopened.evaluate("sessionStorage.getItem('sessionToken')"))
                await second.close()
            finally:
                await browser.close()


if __name__ == '__main__':
    unittest.main()
