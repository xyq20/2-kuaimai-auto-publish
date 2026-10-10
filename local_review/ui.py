from __future__ import annotations


STYLES = r''':root{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#1d1d1f;background:#f5f5f7;font-synthesis:none}*{box-sizing:border-box}body{margin:0}main{max-width:1000px;margin:52px auto;padding:0 24px}header{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px}header p{color:#6e6e73;font-size:14px}h1{font-size:26px;letter-spacing:-.6px;font-weight:600}.card{background:white;border-radius:24px;padding:32px;box-shadow:0 8px 40px #00000006}.layout{display:grid;grid-template-columns:1fr 1fr;gap:36px}.photo{width:100%;aspect-ratio:4/5;object-fit:contain;background:#f8f8fa;border-radius:16px}h2{font-size:25px;margin:8px 0 20px}.label{font-size:13px;color:#6e6e73;margin:20px 0 8px}.suggestion{font-size:20px;font-weight:600}.option-search{display:grid;grid-template-columns:1fr auto;align-items:center;gap:8px;margin-top:18px}.option-search input{grid-column:1/-1;margin:0}.option-search span{color:#6e6e73;font-size:12px}.options{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:12px 0 20px;max-height:360px;overflow:auto;padding:2px}.custom-value{display:block;margin:4px 0 20px}.custom-value input{margin-bottom:0}button,input,select{font:inherit}button{cursor:pointer;border:1px solid #d2d2d7;border-radius:12px;padding:13px 18px;background:#fff;color:#1d1d1f}button[aria-pressed=true]{border:2px solid #0071e3;background:#f0f7ff;padding:12px 17px}button:disabled{opacity:.45;cursor:default}.primary{background:#0071e3;color:white;border-color:#0071e3}.actions .status{flex:1;min-width:0;margin:0;align-self:center}.actions{display:flex;gap:12px;justify-content:flex-end;margin-top:28px;border-top:1px solid #eee;padding-top:24px}summary{cursor:pointer;color:#0066cc;padding:14px 0}details p{font-size:14px;line-height:1.7;color:#515154}input,select{display:block;width:100%;padding:12px;border:1px solid #ccc;border-radius:9px;margin:8px 0 16px}label{font-size:14px}:focus-visible{outline:3px solid #0071e3;outline-offset:4px}.status{min-height:24px;color:#a53a22;font-size:14px;margin-top:16px}#login{max-width:420px;margin:60px auto}.embedded main{max-width:1100px;margin:20px auto}.embedded header{display:none}.embedded .card{border-radius:12px;box-shadow:none}#empty{text-align:center;padding:60px 10px;color:#6e6e73}[hidden]{display:none!important}@media(max-width:700px){main{margin:20px auto;padding:0 16px}.card{padding:20px}.layout{grid-template-columns:1fr;gap:16px}.photo{aspect-ratio:1;max-height:360px}.actions{justify-content:stretch}.actions button{flex:1}h1{font-size:22px}}.embedded{height:100vh;background:#f5f5f7}.embedded main{height:100vh;max-width:none;margin:0;padding:14px}.embedded header{display:none}.embedded #review.card{height:calc(100vh - 28px);display:flex;flex-direction:column;overflow:hidden;padding:18px}.embedded #review .layout{flex:1;min-height:0;grid-template-columns:minmax(0,1fr) minmax(360px,1fr);gap:24px;overflow:hidden}.embedded #review .layout>div{min-height:0}.embedded #review .photo{width:100%;height:100%;aspect-ratio:auto;object-fit:contain}.embedded #review .layout>div:last-child{overflow-y:auto;padding-right:8px}.embedded #review .actions{flex-shrink:0;margin-top:14px}.embedded #review h2{font-size:22px}.embedded #review .options{max-height:none;overflow:visible}body.embedded,body.embedded main{height:100%;overflow:hidden}.embedded #review.card{width:100%;max-width:none;margin:0;border-radius:0;box-shadow:none}.embedded #review .evidence{max-height:120px;overflow:auto}@media(max-width:700px){.embedded main{padding:8px}.embedded #review.card{height:calc(100vh - 16px);padding:12px}.embedded #review .layout{grid-template-columns:1fr;overflow-y:auto}.embedded #review .photo{height:38vh;max-height:none}.embedded #review .layout>div:last-child{overflow:visible}.embedded #review .actions{margin-top:8px}}body.embedded{background:var(--paper,#f5f5f7)!important;overflow:hidden}.embedded main{height:100vh;max-width:none;margin:0;padding:0}.embedded #review.card{height:100vh;min-height:0;width:100%;max-width:none;margin:0;padding:20px;border-radius:0;background:var(--white,#fff)}@media(max-width:700px){.embedded main{padding:0}.embedded #review.card{height:100vh;padding:12px}}'''

# Keep the action row inside the iframe even when messages wrap or the user zooms.
STYLES += r"""
.embedded #review .actions{display:grid;grid-template-columns:minmax(0,1fr) auto auto auto;gap:12px;align-items:center;padding:16px 4px 8px;margin-top:12px}
.embedded #review .actions button{white-space:nowrap;min-width:96px;flex:none}
.embedded #review .actions .status{overflow-wrap:anywhere}
.embedded #review .actions .status:empty{display:none}
.embedded #review .actions #skip{grid-column:2}
@media(max-width:700px){.embedded #review .actions{grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.embedded #review .actions .status{grid-column:1/-1}.embedded #review .actions #skip{grid-column:1}.embedded #review .actions button{min-width:0;padding:12px 6px}}
"""

STYLES += r"""
.review-media{display:flex;flex-direction:column;min-width:0;min-height:0}
.photo-frame{position:relative;min-height:0;flex:1}
.ocr-highlight{position:absolute;border:2px solid #d97706;background:#fbbf2422;border-radius:4px;pointer-events:none}
#image-note{font-size:13px;color:#6e6e73;line-height:1.5;margin:10px 0 0;flex-shrink:0}
#image-note:empty{display:none}
.review-context{padding:12px 16px;border-left:3px solid #0071e3;background:#0071e30a;border-radius:8px;line-height:1.6}
#review-question{font-size:16px;font-weight:600;margin:0 0 6px}
#review-instruction{font-size:14px;color:#515154;margin:0}
.ocr-review #field-name{margin-bottom:14px}
.ocr-review .label{margin-top:14px}
.ocr-review .suggestion{font-size:16px;line-height:1.5;margin:6px 0 12px}
.ocr-review .options button{padding:10px 16px}
.ocr-review .options button[aria-pressed=true]{padding:9px 15px}
"""

SCRIPT = r''''use strict';
if(document.body.classList.contains('embedded')) document.documentElement.style.overflow='hidden';
const $=id=>document.getElementById(id);let current=null,selectedParts=[],busy=false,leaseAt=0;const skipped=new Set();
const reviewScope=new URLSearchParams(location.search);
async function reviewTasks(){const run=reviewScope.get('run_id');const result=await api('/api/reviews'+(run?'?run_id='+encodeURIComponent(run):''));const preferred=reviewScope.get('review_id');if(preferred)result.tasks.sort((a,b)=>Number(b.id===preferred)-Number(a.id===preferred));return result;}
function message(value){const status=$('status');status.hidden=!value;const parent=$('review').hidden?document.querySelector('main'):document.querySelector('.actions');parent.prepend(status);status.textContent=value;}
function normalizeSearch(value){return String(value||'').normalize('NFKC').toLocaleLowerCase('zh-CN').replace(/\s+/g,'');}
function splitValues(value){return String(value||'').split(/[,，、;；]/).map(part=>part.trim()).filter(Boolean);}
function syncPressed(){document.querySelectorAll('[data-value-id]').forEach(el=>el.setAttribute('aria-pressed',String(selectedParts.includes(el.dataset.valueId))));}
function suggestedParts(task){return task.options.some(o=>o.value_id===task.suggested_value_id)?[task.suggested_value_id]:splitValues(task.suggested_value_id);}
function selectionDiffers(){const suggested=current?suggestedParts(current):[];return !current||selectedParts.length!==suggested.length||selectedParts.some(value=>!suggested.includes(value));}
function filterOptions(){const query=normalizeSearch($('option-search').value);let visible=0,total=0;document.querySelectorAll('[data-value-id]').forEach(button=>{total+=1;const matches=!query||normalizeSearch(button.textContent).includes(query)||selectedParts.includes(button.dataset.valueId);button.hidden=!matches;if(matches)visible+=1;});$('option-count').textContent=query?'显示 '+visible+' / '+total:'共 '+total+' 项';}
function lock(value){busy=value;$('confirm').disabled=value||!selectedParts.length;if($('no-fill'))$('no-fill').disabled=value;$('skip').disabled=value;document.querySelectorAll('[data-value-id],#reason,#option-search,#custom-value').forEach(el=>el.disabled=value);}
async function api(path,data){const response=await fetch(path,{method:data===undefined?'GET':'POST',credentials:'same-origin',headers:{'content-type':'application/json'},body:data===undefined?undefined:JSON.stringify(data)});if(!response.ok){const detail=await response.json().catch(()=>({}));const error=Error(response.status===409?'任务已更新，正在重新获取审核状态。':response.status===401?'登录已过期，请刷新页面重新登录。':response.status===422?(detail.error==='candidate_not_unique'?'候选校验失败，请刷新审核后重新选择。':detail.error==='correction_reason_required'?'请选择改选原因后再确认。':'请检查候选值与改选原因。'):'暂时无法完成，请稍后重试。');error.status=response.status;error.code=detail.error;throw error;}return response.json();}
function allowsMulti(task){return !isOCRReview(task)&&['multi_select','multi-select','multiselect'].includes(task?.control_type);}
function isOCRReview(task){return String(task?.field_id||'').startsWith('ocr:')||typeof task?.evidence?.ocr_text==='string';}
function highlightOCR(task){
  const image=$('product-image'),mark=$('ocr-highlight'),box=task?.evidence?.ocr_bbox;
  mark.hidden=true;
  if(!isOCRReview(task)||!box||image.hidden||!image.naturalWidth||![box.x,box.y,box.width,box.height].every(Number.isFinite)||box.x<0||box.y<0||box.width<=0||box.height<=0||box.x+box.width>1.001||box.y+box.height>1.001)return;
  const scale=Math.min(image.clientWidth/image.naturalWidth,image.clientHeight/image.naturalHeight);
  const width=image.naturalWidth*scale,height=image.naturalHeight*scale;
  Object.assign(mark.style,{left:((image.clientWidth-width)/2+box.x*width)+'px',top:((image.clientHeight-height)/2+box.y*height)+'px',width:(box.width*width)+'px',height:(box.height*height)+'px'});
  mark.hidden=false;
}
function render(task){
  document.querySelector('.actions').prepend($('status'));$('review').hidden=false;$('empty').hidden=true;
  const ocr=isOCRReview(task),evidence=task.evidence||{};
  $('review').classList.toggle('ocr-review',ocr);
  $('product-title').textContent=task.title;
  const platforms={douyin:'抖音',taobao:'淘宝',tmall:'天猫',pdd:'拼多多',wxsph:'微信小店',jd:'京东',base:'基础资料'};
  $('platform').textContent=ocr?(platforms[task.platform_id]||task.platform_id)+' · 尺码图文字确认':task.platform_id;
  $('field-label').textContent=ocr?'待确认的原图文字':'字段名称';
  $('field-name').textContent=ocr?(evidence.ocr_text||task.field_label):task.field_label;
  $('review-context').hidden=!ocr;
  $('review-question').textContent=ocr?'这段文字对应哪个测量项？请选择一项。':'';
  $('review-instruction').textContent=ocr?'核对左侧尺码原图中标出的文字。只确认文字含义，尺寸数字保持原图数据；若是标题或备注，点击“不是测量项”。':'';
  selectedParts=suggestedParts(task).filter(value=>task.options.some(o=>o.value_id===value));
  if(!allowsMulti(task))selectedParts=selectedParts.slice(0,1);
  $('suggestion-label').textContent=(ocr&&!selectedParts.length)||evidence.selection_only?'审核提示':'AI 建议';
  $('suggestion').textContent=ocr?(selectedParts.map(value=>task.options.find(o=>o.value_id===value)?.label).filter(Boolean).join('、')||'暂无通过校验的建议，请核对原图后选择。'):
    evidence.selection_only?(evidence.summary||'无法手填，请从平台候选中选择'):selectedParts.map(value=>task.options.find(o=>o.value_id===value)?.label).filter(Boolean).join('、')||'需要人工判断';
  $('option-search-label').textContent=ocr?'选择对应测量项（单选）':('搜索候选值（'+(allowsMulti(task)?'可多选':'单选')+' · '+(task.custom_allowed?'可手动输入':'仅限候选，不可手填')+'）');
  const judgment=evidence.model_judgment;
  $('model-judgment').hidden=!judgment;
  if(judgment){
    const confidence=typeof judgment.confidence==='number'?` · 置信度 ${Math.round(judgment.confidence*100)}%`:'';
    $('model-heading').textContent=(judgment.api_called===true?'AI 已判断':judgment.api_called===false?'AI 未调用':'AI 调用未确认')+confidence;
    const labels=judgment.selected_labels||[];
    $('model-choice').textContent=(labels.length?'模型建议：'+labels.join('、'):'暂无有效模型建议')+' · '+(judgment.status==='review_required'?'待人工确认，尚未通过':judgment.status==='unavailable'?'判断不可用':'请核对后确认');
    $('model-reason').textContent=judgment.reason||'暂无判断说明';
  }
  $('option-search').hidden=ocr&&task.options.length<=8;
  $('options').setAttribute('aria-label',allowsMulti(task)?'可选属性值，可多选':'可选属性值，单选');
  $('no-fill').textContent=ocr?'不是测量项':'不填写';
  $('option-search').value='';$('custom-value').value='';$('custom-value-label').hidden=ocr||!task.custom_allowed;
  $('options').replaceChildren();
  for(const option of task.options){
    const button=document.createElement('button');button.type='button';button.dataset.valueId=option.value_id;button.textContent=option.label;
    button.setAttribute('aria-pressed',String(selectedParts.includes(option.value_id)));
    button.onclick=()=>{
      const index=selectedParts.indexOf(option.value_id);
      if(!allowsMulti(task))selectedParts=index>=0?[]:[option.value_id];
      else if(index>=0)selectedParts.splice(index,1);else selectedParts.push(option.value_id);
      $('custom-value').value='';syncPressed();$('reason-label').hidden=!task.suggested_value_id||!selectionDiffers();filterOptions();lock(false);
    };
    $('options').append(button);
  }
  filterOptions();$('reason').value='';$('reason-label').hidden=true;
  const image=$('product-image');$('ocr-highlight').hidden=true;image.alt=ocr?'待审核尺码原图':'待审核商品图';
  image.onload=()=>highlightOCR(current);
  if(task.asset_ids.length){
    image.src='/api/assets/'+encodeURIComponent(task.asset_ids[0]);image.hidden=false;
    $('image-note').textContent=ocr?'尺码原图：'+(evidence.source_filename||'原始尺码表')+' · 标框为待确认文字':'';
    image.onerror=()=>{image.hidden=true;$('ocr-highlight').hidden=true;$('image-note').textContent=ocr?'尺码原图暂不可用，请先核对原始尺码表再确认。':'商品图暂不可用，请核对判断依据。';};
  }else{
    image.removeAttribute('src');image.hidden=true;
    $('image-note').textContent=ocr?'此条审核未关联尺码原图，请先核对原始尺码表；商品照片无法确认测量项。':'尚未上传商品图。';
  }
  $('evidence').textContent=typeof evidence.summary==='string'?evidence.summary:'本项需要人工确认，请结合商品图片与当前候选值判断。';
  lock(false);$('field-name').tabIndex=-1;$('field-name').focus();
}
new ResizeObserver(()=>highlightOCR(current)).observe($('photo-frame'));
async function next(){message('');lock(true);try{const {tasks}=await reviewTasks();const task=tasks.find(t=>!skipped.has(t.id));if(!task){current=null;$('review').hidden=true;$('empty').hidden=false;$('progress').textContent='待审任务已处理或暂时跳过';return;}const claimed=await api('/api/reviews/'+encodeURIComponent(task.id)+'/claim',{version:task.version});current={...task,...claimed};leaseAt=Date.now();$('progress').textContent='还有 '+tasks.length+' 项待判断';render(current);if(current.run_status==='failed'){$('progress').textContent+=' · 运行已中断，审核后需重新运行';$('confirm').textContent='确认并保存审核';}else{$('confirm').textContent='确认并继续';}}catch(error){message(error.message);$('empty').hidden=false;}finally{lock(false);}}
async function submit(action){if(busy||!current)return;if(action==='confirm'&&current.suggested_value_id&&selectionDiffers()&&!$('reason').value){message('改选候选后，请选择纠正原因。');$('reason').focus();return;}lock(true);try{const path='/api/reviews/'+encodeURIComponent(current.id)+'/'+action;const payload=()=>({version:current.version,...(action==='confirm'?{final_value_id:selectedParts.join(','),...($('reason').value?{correction_reason:$('reason').value}:{})}:{} )});let result;try{result=await api(path,payload());}catch(error){if(error.status!==409||error.code!=='task_locked_or_updated')throw error;const {tasks}=await reviewTasks();const fresh=tasks.find(task=>task.id===current.id);if(!fresh){current=null;await next();message('这条任务已更新，请核对当前待审字段。');return;}const claimed=await api('/api/reviews/'+encodeURIComponent(fresh.id)+'/claim',{version:fresh.version});current={...fresh,...claimed};leaseAt=Date.now();result=await api(path,payload());}if(action==='skip')skipped.add(current.id);const ended=result?.resume_expected===false||['failed','cancelled','completed'].includes(result?.run_status??current.run_status);current=null;await next();if(action!=='skip')message(ended?'审核已保存。原运行已结束，请重新运行；已确认结果会复用。':'审核已保存，已发送续跑通知；本平台全部审核完成后自动继续。');}catch(error){message(error.message);}finally{lock(false);}}
const noFillButton=document.createElement('button');noFillButton.id='no-fill';noFillButton.type='button';noFillButton.textContent='不填写';$('confirm').parentElement.insertBefore(noFillButton,$('confirm'));noFillButton.onclick=()=>submit('no_fill');$('confirm').onclick=()=>submit('confirm');$('skip').onclick=()=>submit('skip');$('refresh').onclick=()=>{skipped.clear();next();};$('option-search').oninput=filterOptions;$('custom-value').oninput=event=>{if(!current?.custom_allowed)return;const parts=allowsMulti(current)?splitValues(event.target.value):[event.target.value.trim()].filter(Boolean);if(parts.length){selectedParts=parts.map(raw=>{const exact=current.options.find(option=>normalizeSearch(option.label)===normalizeSearch(raw));return exact?exact.value_id:raw;});}syncPressed();$('reason-label').hidden=!current.suggested_value_id||!selectionDiffers();lock(false);};
$('login-form').onsubmit=async event=>{event.preventDefault();const button=event.target.querySelector('button');button.disabled=true;try{const data=new FormData(event.target);await api('/api/login',{username:data.get('username'),password:data.get('password')});location.reload();}catch{message('登录失败，请检查用户名和密码。');}finally{button.disabled=false;}};
setInterval(async()=>{if(!current||busy||document.hidden||Date.now()-leaseAt<300000)return;lock(true);try{const renewed=await api('/api/reviews/'+encodeURIComponent(current.id)+'/renew',{version:current.version});current={...current,...renewed};leaseAt=Date.now();}catch(error){message(error.message);}finally{lock(false);}},60000);
window.addEventListener('message',event=>{if(event.origin!==location.origin||event.source!==window.parent||event.data?.type!=='refresh-review'||busy)return;if(!current){skipped.clear();next();}});
if(document.body.dataset.authenticated==='true')next();'''


def page(authenticated: bool, embedded: bool = False) -> str:
    hidden = "hidden" if authenticated else ""
    body_class = "embedded" if embedded else ""
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>商品属性审核</title><link rel="icon" href="/workbench/favicon.svg" type="image/svg+xml"><script src="/workbench/theme.js"></script><link rel="stylesheet" href="/app.css"><link rel="stylesheet" href="/workbench/theme.css"><script src="/app.js" defer></script></head><body class="{body_class}" data-authenticated="{str(authenticated).lower()}"><main><header><h1>商品属性审核</h1><div class="review-header-actions"><p id="progress">本机数据 · 一次专注一个判断</p><button class="theme-toggle" type="button" aria-label="切换到深色模式" title="切换到深色模式" data-theme-toggle><svg class="theme-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20.5 13.4A8.7 8.7 0 0 1 10.6 3.5 9 9 0 1 0 20.5 13.4Z"/></svg></button></div></header><section id="login" class="card" {hidden}><h2>登录商品工作台</h2><form id="login-form"><label>用户名<input name="username" autocomplete="username" required maxlength="128"></label><label>4 位审核密码<input name="password" type="password" inputmode="numeric" pattern="[0-9]{{4}}" autocomplete="current-password" required minlength="4" maxlength="4"></label><button class="primary" type="submit">登录</button></form></section><section id="review" class="card" hidden><div class="layout"><div class="review-media"><div id="photo-frame" class="photo-frame"><img class="photo" id="product-image" alt="待审核商品图"><span id="ocr-highlight" class="ocr-highlight" hidden aria-hidden="true"></span></div><p id="image-note"></p></div><div><p class="label" id="platform"></p><h2 id="product-title"></h2><p class="label" id="field-label">字段名称</p><h2 id="field-name"></h2><div id="review-context" class="review-context" hidden><p id="review-question"></p><p id="review-instruction"></p></div><p class="label" id="suggestion-label">AI 建议</p><p class="suggestion" id="suggestion"></p><section id="model-judgment" class="review-context" hidden><strong id="model-heading"></strong><p id="model-choice"></p><details><summary>查看模型判断依据</summary><p id="model-reason" style="white-space:pre-wrap;overflow-wrap:anywhere"></p></details></section><label class="option-search" for="option-search"><span id="option-search-label">搜索候选值（可多选）</span><span id="option-count"></span><input id="option-search" type="search" placeholder="输入属性值快速筛选" autocomplete="off"></label><div id="options" class="options" role="group" aria-label="可选属性值，可多选"></div><label id="custom-value-label" class="custom-value" hidden>候选中没有？直接输入（可用逗号分隔多个）<input id="custom-value" type="text" placeholder="输入页面允许的新属性值，逗号分隔" autocomplete="off" maxlength="256"></label><label id="reason-label" hidden>改选原因<select id="reason"><option value="">请选择原因</option><option>图片证据与建议不符</option><option>文字资料与建议不符</option><option>候选映射不准确</option><option>需要人工纠正</option></select></label><details><summary>查看判断依据与相似商品</summary><p id="evidence"></p><p id="similar">暂无经保存回读验证的相似商品。</p></details></div></div><div class="actions"><p id="status" class="status" role="status" aria-live="polite"></p><button id="skip" type="button">暂时跳过</button><button id="confirm" class="primary" type="button" disabled>确认并继续</button></div></section><section id="empty" class="card" hidden>当前没有可处理的审核任务。<p><button id="refresh" type="button">刷新待审任务</button></p></section></main></body></html>'''
