'use strict';

const icons = {
  grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  history: '<path d="M3 11a9 9 0 1 1 2 7M3 4v7h7"/><path d="M12 7v5l3 2"/>',
  help: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 1 1 3.5 2.3c-1 .5-1 1.2-1 2.2M12 17h.01"/>',
  spark: '<path d="m12 3 2.6 6.4L21 12l-6.4 2.6L12 21l-2.6-6.4L3 12l6.4-2.6L12 3ZM20 2v4M18 4h4"/>',
  eye: '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
  more: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
  sheet: '<path d="M14 3H5v18h14V8l-5-5Z"/><path d="M14 3v5h5M8 12h8M8 16h8M12 12v7"/>',
  image: '<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8" cy="8" r="1.4"/><path d="m3 17 5-5 4 4 4-6 5 6"/>',
  chevron: '<path d="m9 5 7 7-7 7"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/>',
  edit: '<path d="m15 4 5 5M4 20l4-1 13-13a2 2 0 0 0-5-5L3 14l1 6Z"/>',
  save: '<path d="M4 3h13l4 4v14H3V3h1ZM7 3v6h9V3M7 21v-8h10v8"/>',
  upload: '<path d="M12 16V3m-5 5 5-5 5 5M4 14v7h16v-7"/>',
  lock: '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3M12 14v3"/>',
  route: '<circle cx="6" cy="5" r="2"/><circle cx="18" cy="19" r="2"/><path d="M8 5h7a4 4 0 0 1 0 8H9a3 3 0 0 0 0 6h7"/>',
  shield: '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/>',
  play: '<path d="m7 4 14 8-14 8V4Z"/>',
  arrow: '<path d="M4 12h16m-6-6 6 6-6 6"/>',
  up: '<path d="m6 14 6-6 6 6"/>',
  down: '<path d="m6 10 6 6 6-6"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  search: '<circle cx="10" cy="10" r="6.5"/><path d="m15 15 6 6"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  box: '<path d="m12 3 9 5v9l-9 5-9-5V8l9-5Zm0 10 9-5M3 8l9 5v9M7 6l10 5"/>',
  plus: '<path d="M12 4v16M4 12h16"/>',
  shirt: '<path d="m8 3-6 4 3 6 3-1v9h8v-9l3 1 3-6-6-4c0 4-8 4-8 0Z"/>',
};
const icon = name => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${icons[name] || icons.box}</svg>`;
document.querySelectorAll('[data-icon]').forEach(el => { el.innerHTML = icon(el.dataset.icon); });
const $ = id => document.getElementById(id);
const platforms = [
  {id:'douyin', name:'抖音', mark:'<span>♪</span>'},
  {id:'taobao', name:'淘宝', mark:'淘'},
  {id:'tmall', name:'天猫', mark:'天'},
  {id:'pdd', name:'拼多多', mark:'拼'},
  {id:'wxsph', name:'微信小店', mark:'微'},
  {id:'xhs', name:'小红书', mark:'小红书'},
  {id:'youzan', name:'有赞', mark:'赞'},
  {id:'jd', name:'京东', mark:'JD'},
];
const platform = id => platforms.find(p => p.id === id);
const logo = p => `<span class="platform-logo ${p.id}" aria-hidden="true">${p.mark}</span>`;
const modes = [
  {id:'preview', name:'仅填写', description:'检查字段，不保存', icon:'edit', note:'填写与校验后结束，保留检查结果。'},
  {id:'save', name:'只保存', description:'保存资料，不提交店铺', icon:'save', note:'资料保存后结束，不提交到店铺。'},
  {id:'publish', name:'保存并铺货', description:'保存后提交指定店铺', icon:'upload', note:'会保存资料并提交到已配置的店铺。'},
];
const state = {selected:['douyin','jd','xhs'], product:null, products:[], productFilter:'unprocessed', mode:'save', learning:true,
  operation:'platforms', history:[], active:null, opened:null, draft:null, admin:false, learningAvailable:false, reviewCount:0};
const labels = {pending:'等待执行', running:'运行中', waiting_review:'等待审核', stopping:'正在停止',
  stopped:'已停止', interrupted:'运行中断', failed:'失败', completed:'已完成', success:'已完成'};
const errors = {
  products_unavailable:'产品目录暂不可用，请先连接共享文件目录，再刷新商品列表。',
  product_not_found:'商品已移动或删除，请刷新后重新选择。', product_unreadable:'无法读取这个商品的 Excel，请检查文件。',
  job_already_running:'已有任务正在运行，请先查看当前任务。', external_job_running:'命令行有任务正在运行，结束后再从网页启动。',
  publish_confirmation_required:'请确认本次要保存并铺货。', review_not_configured:'审核服务尚未配置，请通过 run-review-center.command 启动。',
  admin_required:'当前账号可审核和查看记录；启动或停止任务需要管理员账号。', unauthorized:'登录已过期，请重新登录。',
  invalid_platforms:'请至少选择一个平台，且不要重复选择。', base_requires_save:'基础资料和新建商品使用“只保存”模式。',
  launch_failed:'任务进程启动失败，请检查本机运行环境。', process_check_failed:'无法确认本机任务状态，请稍后重试。',
};
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const mode = () => modes.find(m => m.id === state.mode);
const platformName = id => platform(id)?.name || (id === 'base' ? '基础资料' : id);
const operationName = id => ({platforms:'平台资料',base:'基础资料',create:'新建商品'}[id] || id);
const isActive = job => job && ['running','waiting_review','stopping'].includes(job.status);
const uuid = () => crypto.randomUUID().replaceAll('-','');
let toastTimer, pollTimer, productRequest = 0, actionBusy = false;

async function api(path, data) {
  const response = await fetch(path, {method:data === undefined ? 'GET' : 'POST', credentials:'same-origin',
    headers:{'content-type':'application/json'}, body:data === undefined ? undefined : JSON.stringify(data)});
  let body;
  try { body = await response.json(); }
  catch (_) { throw Error('服务暂时无法响应，请稍后重试。'); }
  if (!response.ok) {
    if (response.status === 401) { window.top.location.assign('/'); }
    throw Error(errors[body.error] || `操作未完成（${body.error || response.status}），请检查后重试。`);
  }
  return body;
}
function toast(message) {
  clearTimeout(toastTimer); $('toast').textContent = message; $('toast').hidden = false;
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, 4000);
}
function notice(message) { $('notice').textContent = message; $('notice').hidden = !message; }
function remember() {
  try { localStorage.setItem('kuaimai.workbench', JSON.stringify({selected:state.selected, mode:state.mode,
    learning:state.learning, productId:state.product?.id})); } catch (_) { /* Browsing still works without storage. */ }
}
function renderProduct() {
  const p = state.product;
  $('product-image').replaceChildren();
  if (p?.image) { const img = document.createElement('img'); img.src = p.image; img.alt = p.title; $('product-image').append(img); }
  else $('product-image').innerHTML = `<div class="demo-art">${icon('shirt')}</div>`;
  $('product-code').textContent = p?.style_code || '待选择';
  $('product-title').textContent = p?.title || '请选择本次处理的商品';
  $('product-category').textContent = p?.category || '';
  $('plan-product').textContent = p?.style_code || '待选择';
  renderPlan();
}
function renderPlatformSelection() {
  document.querySelectorAll('[data-platform]').forEach(button => button.setAttribute('aria-pressed', String(state.selected.includes(button.dataset.platform))));
  $('selected-count').textContent = `已选 ${state.selected.length} 个`;
  renderPlan();
}
function renderPlan() {
  const normal = state.operation === 'platforms';
  const ids = normal ? state.selected : ['base'];
  $('platform-section').hidden = !normal;
  $('operation-banner').hidden = normal;
  $('operation-label').textContent = `${operationName(state.operation)} · 保存快麦商品，不铺货`;
  $('plan-count').textContent = normal ? `${ids.length} 个平台` : operationName(state.operation);
  $('plan-list').innerHTML = ids.map((id,index) => {
    const p = platform(id);
    return `<li><span class="queue-number">${index+1}</span><span class="queue-logo">${p ? logo(p) : icon('box')}</span><span class="queue-name">${normal ? platformName(id) : operationName(state.operation)}</span>${normal ? `<div class="reorder"><button data-move="${id}" data-direction="-1" aria-label="上移${p.name}" ${index === 0 ? 'disabled' : ''}>${icon('up')}</button><button data-move="${id}" data-direction="1" aria-label="下移${p.name}" ${index === ids.length-1 ? 'disabled' : ''}>${icon('down')}</button></div>` : ''}</li>`;
  }).join('');
  $('empty-plan').hidden = ids.length > 0;
  $('preview-run').disabled = !state.admin || !state.product || !ids.length || !!state.active;
  $('preview-run').innerHTML = icon('play') + (state.active ? '已有任务运行中' : '开始运行') + icon('arrow');
  $('plan-mode').textContent = mode().name;
  $('plan-learning').innerHTML = state.learning && normal ? '<i></i>已开启' : '已关闭';
  $('mode-note').classList.toggle('publish', state.mode === 'publish');
  $('mode-note').innerHTML = icon(state.mode === 'publish' ? 'info' : 'shield') + `<p>${mode().note}</p>`;
  $('learning-toggle').disabled = !normal || !state.learningAvailable;
  $('learning-toggle').checked = state.learning && normal;
  document.querySelectorAll('[data-mode]').forEach(button => {
    button.disabled = !normal && button.dataset.mode !== 'save';
    button.setAttribute('aria-checked', String(button.dataset.mode === state.mode));
    button.tabIndex = button.dataset.mode === state.mode ? 0 : -1;
  });
}
function setMode(id) { state.mode = id; renderPlan(); remember(); }
function showView(view) {
  ['home','history','review'].forEach(name => {
    $(name+'-view').hidden = name !== view;
    $('nav-'+name).classList.toggle('active', name === view);
  });
  document.querySelector('.breadcrumb strong').textContent = {home:'商品运行',history:'运行记录',review:'商品属性审核'}[view];
  if (view === 'review') {
    if (!$('review-frame').getAttribute('src')) $('review-frame').src = '/review?embedded=1';
    else $('review-frame').contentWindow.postMessage({type:'refresh-review'}, location.origin);
  }
  if (location.hash !== '#'+view) history.replaceState(null,'','#'+view);
}
function openDialog(id) { if (!$(id).open) $(id).showModal(); }
document.querySelectorAll('[data-close]').forEach(button => button.onclick = () => $(button.dataset.close).close());
$('nav-home').onclick = () => showView('home');
$('nav-history').onclick = () => { renderHistory(); showView('history'); };
$('nav-review').onclick = () => showView('review');
$('nav-help').onclick = () => openDialog('help-dialog');
$('more-button').onclick = () => openDialog('more-dialog');
$('refresh-review').onclick = () => { $('review-frame').contentWindow.postMessage({type:'refresh-review'}, location.origin); };
$('logout').onclick = async () => { try { await api('/api/logout', {}); location.assign('/'); } catch (e) { toast(e.message); } };
$('platform-grid').innerHTML = platforms.map(p => `<button class="platform-card" data-platform="${p.id}" aria-label="${p.name}" aria-pressed="false">${logo(p)}<span class="platform-name">${p.name}</span><span class="select-check">${icon('check')}</span></button>`).join('');
$('platform-grid').onclick = event => {
  const button = event.target.closest('[data-platform]'); if (!button) return;
  const id = button.dataset.platform;
  state.selected = state.selected.includes(id) ? state.selected.filter(item => item !== id) : [...state.selected,id];
  renderPlatformSelection(); remember();
};
$('select-all').onclick = () => { state.selected = [...state.selected, ...platforms.map(p => p.id).filter(id => !state.selected.includes(id))]; renderPlatformSelection(); remember(); };
$('clear-all').onclick = () => { state.selected = []; renderPlatformSelection(); remember(); };
$('plan-list').onclick = event => {
  const button = event.target.closest('[data-move]'); if (!button || button.disabled) return;
  const index = state.selected.indexOf(button.dataset.move), target = index + Number(button.dataset.direction);
  if (index < 0 || target < 0 || target >= state.selected.length) return;
  [state.selected[index],state.selected[target]] = [state.selected[target],state.selected[index]];
  renderPlan(); remember();
};
$('mode-grid').innerHTML = modes.map(m => `<button class="mode-card" role="radio" data-mode="${m.id}" aria-checked="false"><div class="mode-top">${icon(m.icon)}<span>${m.name}</span><span class="radio-mark"></span></div><p>${m.description}</p></button>`).join('');
$('mode-grid').onclick = event => { const button = event.target.closest('[data-mode]'); if (button && !button.disabled) setMode(button.dataset.mode); };
$('mode-grid').onkeydown = event => {
  if (!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key) || state.operation !== 'platforms') return;
  event.preventDefault(); const index = modes.findIndex(m => m.id === state.mode);
  setMode(modes[(index+(['ArrowLeft','ArrowUp'].includes(event.key) ? 2 : 1))%3].id);
  document.querySelector(`[data-mode="${state.mode}"]`).focus();
};
$('learning-toggle').onchange = event => { state.learning = event.target.checked; renderPlan(); remember(); };
document.querySelectorAll('[data-operation]').forEach(button => button.onclick = () => {
  state.operation = button.dataset.operation; state.mode = 'save'; $('more-dialog').close(); renderPlan();
});
$('back-platforms').onclick = () => { state.operation = 'platforms'; renderPlan(); };

async function chooseProduct(id) {
  const request = ++productRequest;
  state.product = null; renderProduct();
  try {
    const p = await api('/api/launcher/products/'+id);
    if (request !== productRequest) return;
    state.product = p; renderProduct(); remember(); $('product-dialog').close();
  } catch (e) { if (request === productRequest) notice(e.message); }
}
function productIdentity(p) {
  const folder = p.folder || p.title || '';
  const parts = folder.split(/[+＋]/).map(part => part.trim()).filter(Boolean);
  const code = p.style_code || (parts.length > 1 ? parts.pop() : (folder.match(/NGBL-[A-Z0-9-]+/i)?.[0] || ''));
  const name = parts.length && parts.join(' · ') !== code ? parts.join(' · ') : folder;
  return {name, code, folder};
}
function renderProducts() {
  const terms = $('product-search').value.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  const processedCount = state.products.filter(p => p.processed).length;
  $('unprocessed-count').textContent = state.products.length - processedCount;
  $('processed-count').textContent = processedCount;
  $('all-products-count').textContent = state.products.length;
  document.querySelectorAll('[data-product-filter]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.productFilter === state.productFilter)));
  const group = state.products.filter(p => state.productFilter === 'all' || Boolean(p.processed) === (state.productFilter === 'processed'));
  const products = group.filter(p => {
    const identity = productIdentity(p);
    const text = `${identity.folder} ${identity.name} ${identity.code}`.toLocaleLowerCase();
    return terms.every(term => text.includes(term));
  });
  $('product-results').textContent = terms.length ? `找到 ${products.length} / ${group.length} 件商品` : `共 ${products.length} 件商品`;
  $('product-list').innerHTML = products.map(p => {
    const {name, code, folder} = productIdentity(p);
    const selected = p.id === state.product?.id;
    return `<button class="product-choice ${selected ? 'selected' : ''}" data-product="${escapeHTML(p.id)}" aria-pressed="${selected}">
      <span class="choice-image">${icon('shirt')}<img src="${escapeHTML(p.image || '/api/launcher/products/'+encodeURIComponent(p.id)+'/image')}" alt="" loading="lazy" decoding="async"></span>
      <span class="choice-info"><strong title="${escapeHTML(folder)}">${escapeHTML(name)}</strong><span class="choice-code">${escapeHTML(code || '查看商品详情')}</span>${p.processed ? `<span class="product-status-note" title="${escapeHTML((p.processed_platforms || []).join('、'))}">已保存 · ${escapeHTML((p.processed_platforms || []).join('、') || '商品资料')}</span>` : ''}</span>
      <span class="choice-state">${selected ? `<span>当前商品</span>${icon('check')}` : icon('chevron')}</span>
    </button>`;
  }).join('');
  $('product-list').querySelectorAll('img').forEach(img => { img.onerror = () => img.remove(); });
  $('product-empty').hidden = products.length > 0;
  $('product-empty').textContent = terms.length ? '没有匹配的商品，试试其他关键词或切换到“全部”。' : state.productFilter === 'unprocessed' ? '当前商品都有保存记录了，可到“已处理”查看或继续处理其他平台。' : '这个分组暂时没有商品。';
}
async function loadProducts(preferred, refreshOnly = false) {
  $('refresh-products').disabled = true;
  try {
    const data = await api('/api/launcher/products'); state.products = data.products; renderProducts();
    notice(state.products.length ? (!state.admin ? errors.admin_required : '') : '目录内没有找到产品信息.xlsx，请检查共享目录。');
    const selected = state.products.find(p => p.id === (preferred || state.product?.id)) || state.products[0];
    if (!refreshOnly) {
      if (selected) await chooseProduct(selected.id); else { state.product = null; renderProduct(); }
    }
  } catch (e) { state.product = null; renderProduct(); notice(e.message); }
  finally { $('refresh-products').disabled = false; }
}
$('change-product').onclick = () => { $('product-search').value=''; renderProducts(); openDialog('product-dialog'); $('product-search').focus(); loadProducts(undefined, true); };
$('product-search').oninput = renderProducts;
document.querySelectorAll('[data-product-filter]').forEach(button => button.onclick = () => { state.productFilter = button.dataset.productFilter; renderProducts(); });
$('refresh-products').onclick = () => loadProducts(undefined, true);
$('product-list').onclick = event => { const b = event.target.closest('[data-product]'); if (b) chooseProduct(b.dataset.product); };

function renderHistory() {
  $('history-count').textContent = state.history.length;
  $('history-list').innerHTML = state.history.length ? state.history.map(job => `<button class="history-row" data-job="${job.id}"><div><strong>${escapeHTML(job.style_code)} · ${escapeHTML(job.title)}</strong><p>${escapeHTML(job.platforms.map(platformName).join(' → '))} · ${modes.find(m=>m.id===job.mode)?.name || ''}<br>${escapeHTML(new Date(job.created_at).toLocaleString('zh-CN'))} · ${escapeHTML(job.created_by)}</p></div><span class="history-status ${job.status}">${labels[job.status] || job.status} ${icon('chevron')}</span></button>`).join('') : '<p class="empty-message">还没有网页运行记录。开始一次任务后会记录在这里。</p>';
}
$('history-list').onclick = event => { const b = event.target.closest('[data-job]'); if (b) openJob(b.dataset.job); };
$('active-job').onclick = () => { if (state.active) openJob(state.active.id); };
function renderJob(job) {
  $('run-title').textContent = `${job.style_code} · ${labels[job.status] || job.status}`;
  $('run-subtitle').textContent = isActive(job) ? '关闭此窗口不影响任务。需要人工确认时可进入商品属性审核。' : '以下为实际运行结果；已保存或已铺货的操作不会因关闭窗口而撤回。';
  $('run-summary').innerHTML = `<span>商品</span><strong>${escapeHTML(job.style_code)}</strong><span>执行模式</span><strong>${modes.find(m=>m.id===job.mode)?.name}</strong><span>任务编号</span><strong>${job.id.slice(0,8)}</strong>`;
  $('run-stages').innerHTML = job.stages.map(s => `<div class="run-stage ${s.status === 'success' ? 'done' : s.status}">${platform(s.platform) ? logo(platform(s.platform)) : icon('box')}<span>${platformName(s.platform)}</span><span class="run-stage-status">${labels[s.status] || s.status}</span></div>`).join('');
  $('log-details').hidden = false;
  if (job.log !== undefined) { const log = $('run-log'); const bottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40; log.textContent = job.log || '任务已创建，等待程序输出…'; if (bottom) log.scrollTop = log.scrollHeight; }
  $('run-confirm').hidden = true;
  $('run-review').hidden = !(job.pending_reviews && isActive(job));
  $('run-secondary').textContent = job.status === 'stopping' ? '正在停止…' : isActive(job) && state.admin ? '停止任务' : '关闭';
  $('run-secondary').classList.toggle('stop', !!isActive(job));
  $('run-secondary').disabled = job.status === 'stopping' || actionBusy;
}
async function openJob(id) {
  state.opened = id; state.draft = null; $('run-error').hidden = true;
  $('run-title').textContent='读取任务…'; $('run-log').textContent='';
  try { const job = await api('/api/launcher/jobs/'+id); if (state.opened !== id) return; renderJob(job); openDialog('run-dialog'); }
  catch(e) { toast(e.message); }
}
$('preview-run').onclick = () => {
  if (!state.product || state.active || !state.admin) return;
  state.opened = null;
  state.draft = {request_key:uuid(), product_id:state.product.id, platforms:[...state.selected], mode:state.mode,
    operation:state.operation, learning:state.operation === 'platforms' && state.learning};
  $('run-title').textContent = '确认这次运行';
  $('run-subtitle').textContent = state.mode === 'publish' ? '本次会真实保存并铺货到已配置的店铺，请核对商品与平台。' : state.mode === 'save' ? '本次会保存快麦商品资料，不提交到店铺。' : '本次只填写并校验，不保存、不铺货。';
  $('run-summary').innerHTML = `<span>商品</span><strong>${escapeHTML(state.product.style_code)}</strong><span>执行模式</span><strong>${mode().name}</strong><span>处理范围</span><strong>${state.operation === 'platforms' ? state.selected.map(platformName).join(' → ') : operationName(state.operation)}</strong>`;
  $('run-stages').replaceChildren(); $('log-details').hidden = true; $('run-error').hidden = true; $('run-review').hidden = true;
  $('run-confirm').hidden = false; $('run-confirm').disabled = false;
  $('run-confirm').textContent = state.mode === 'publish' ? '确认保存并铺货' : '确认开始';
  $('run-secondary').textContent = '返回调整'; $('run-secondary').disabled = false; $('run-secondary').classList.remove('stop');
  openDialog('run-dialog');
};
$('run-confirm').onclick = async () => {
  if (actionBusy || !state.draft) return;
  actionBusy = true; $('run-confirm').disabled = true; $('run-error').hidden = true;
  try {
    const job = await api('/api/launcher/jobs', {...state.draft, confirm_publish:state.draft.mode === 'publish'});
    state.opened = job.id; state.active = job; state.draft = null; renderJob(job); renderPlan();
  } catch(e) { $('run-error').textContent=e.message; $('run-error').hidden=false; }
  finally { actionBusy = false; $('run-confirm').disabled = false; $('run-secondary').disabled = false; }
};
$('run-secondary').onclick = async () => {
  if (!state.opened) { $('run-dialog').close(); return; }
  const job = state.history.find(j=>j.id === state.opened) || state.active;
  if (!isActive(job) || !state.admin) { $('run-dialog').close(); return; }
  if (actionBusy || !window.confirm('停止当前任务？已经保存或铺货的部分会保留，后续步骤将停止。')) return;
  actionBusy = true; $('run-secondary').disabled = true;
  try { renderJob(await api('/api/launcher/jobs/'+state.opened+'/stop', {})); }
  catch(e) { $('run-error').textContent=e.message; $('run-error').hidden=false; }
  finally { actionBusy = false; }
};
$('run-review').onclick = () => { $('run-dialog').close(); showView('review'); };
async function poll() {
  try {
    const [historyData, reviewData] = await Promise.all([api('/api/launcher/jobs'),api('/api/reviews')]);
    state.history = historyData.jobs; state.active = state.history.find(isActive) || null;
    renderHistory(); renderPlan();
    $('active-job').hidden = !state.active;
    if (state.active) $('active-job').innerHTML = icon(state.active.status === 'waiting_review' ? 'edit' : 'play') + `<strong>${escapeHTML(state.active.style_code)} · ${labels[state.active.status]}</strong><span>查看任务 ${icon('chevron')}</span>`;
    const count = reviewData.tasks.length; $('review-count').textContent = count;
    if (count !== state.reviewCount && !$('review-view').hidden) $('review-frame').contentWindow.postMessage({type:'refresh-review'}, location.origin);
    state.reviewCount = count;
    if (state.opened && $('run-dialog').open && !actionBusy) {
      const id = state.opened, job = await api('/api/launcher/jobs/'+id);
      if (state.opened === id && !actionBusy) renderJob(job);
    }
    document.querySelector('.preview-badge').innerHTML = icon('lock')+'本机运行';
  } catch (e) { document.querySelector('.preview-badge').textContent = '连接中断，正在重试'; }
  finally { pollTimer = setTimeout(poll, state.active ? 2500 : 5000); }
}
async function boot() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem('kuaimai.workbench') || '{}'); } catch (_) {}
  if (Array.isArray(saved.selected)) state.selected = [...new Set(saved.selected.filter(id=>platform(id)))];
  // Every new session defaults to saving only; publishing needs a deliberate choice.
  state.mode = 'save';
  renderProduct(); renderPlatformSelection();
  try {
    const config = await api('/api/launcher/config');
    state.admin = config.user.role === 'admin'; state.learningAvailable = config.learning_available;
    state.learning = config.learning_available && saved.learning !== false;
    $('account').textContent = config.user.username + (state.admin ? ' · 管理员' : ' · 审核员');
    document.querySelector('.avatar').textContent = config.user.username.slice(0,1).toUpperCase();
    renderPlan();
    showView(['home','history','review'].includes(location.hash.slice(1)) ? location.hash.slice(1) : state.admin ? 'home' : 'review');
    await Promise.all([loadProducts(saved.productId), poll()]);
  } catch(e) { notice(e.message); }
}
boot();
