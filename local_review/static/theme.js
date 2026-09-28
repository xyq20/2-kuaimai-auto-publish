/* Shared by the workbench, login page, and embedded review page. */
(() => {
  'use strict';
  const key = 'kuaimai.theme';
  const valid = value => value === 'dark' || value === 'light';
  function savedTheme() {
    try { const value = localStorage.getItem(key); return valid(value) ? value : 'light'; }
    catch (_) { return 'light'; }
  }
  function syncFrame(frame) {
    frame.contentWindow?.postMessage({type:'kuaimai-theme', theme:document.documentElement.dataset.theme}, location.origin);
  }
  function apply(theme) {
    document.documentElement.dataset.theme = theme;
    document.querySelectorAll('[data-theme-toggle]').forEach(button => {
      const label = theme === 'dark' ? '切换到浅色模式' : '切换到深色模式';
      button.title = label;
      button.setAttribute('aria-label', label);
    });
    document.querySelectorAll('iframe').forEach(syncFrame);
  }
  // Run before the stylesheets so a saved dark theme does not flash white.
  apply(savedTheme());
  document.addEventListener('DOMContentLoaded', () => {
    apply(document.documentElement.dataset.theme);
    document.querySelectorAll('[data-theme-toggle]').forEach(button => {
      button.addEventListener('click', () => {
        const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
        try { localStorage.setItem(key, theme); } catch (_) { /* Still switch for this page. */ }
        apply(theme);
      });
    });
  });
  document.addEventListener('load', event => {
    if (event.target instanceof HTMLIFrameElement) syncFrame(event.target);
  }, true);
  window.addEventListener('storage', event => {
    if (event.key === key || event.key === null) apply(savedTheme());
  });
  window.addEventListener('message', event => {
    if (window.parent !== window && event.source === window.parent && event.origin === location.origin &&
        event.data?.type === 'kuaimai-theme' && valid(event.data.theme)) apply(event.data.theme);
  });
})();
