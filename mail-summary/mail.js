'use strict';
(() => {
  const $ = id => document.getElementById(id);
  let user = null, csrf = '', inbox = null, selected = new Set(), busy = false, aiReady = false;
  const text = (tag, content, cls) => { const e = document.createElement(tag); e.textContent = content || ''; if (cls) e.className = cls; return e; };
  function notice(message) { $('notice').textContent = message || ''; $('notice').hidden = !message; }
  async function api(path, body) {
    const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: body === undefined ? {} : {'Content-Type': 'application/json', 'X-Mail-CSRF': csrf},
      body: body === undefined ? undefined : JSON.stringify(body)});
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) { const e = new Error(payload.error || '请求失败，请稍后重试。'); e.code = payload.code; throw e; }
    if (payload.csrf) csrf = payload.csrf;
    return payload;
  }
  function setBusy(value) {
    busy = value;
    ['refresh','disconnect','previous','next','select-first','connect-button'].forEach(id => { $(id).disabled = value; });
    document.querySelectorAll('.message-row input').forEach(e => { e.disabled = value; });
    updateSelection();
    if (inbox && !value) { $('previous').disabled = inbox.page === 0; $('next').disabled = !inbox.has_next; }
  }
  function updateSelection() {
    $('selected-count').textContent = `已选 ${selected.size} / 10 封`;
    $('summarize').disabled = busy || !aiReady || !selected.size;
    $('summarize').textContent = busy ? '正在处理…' : `总结所选 ${selected.size} 封邮件`;
    if (inbox) $('select-first').checked = inbox.messages.length > 0 && inbox.messages.slice(0,10).every(m => selected.has(m.id));
  }
  function date(value) { const d = new Date(value); return Number.isNaN(d.getTime()) ? '时间未提供' : d.toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',timeZone:'Asia/Shanghai',hour12:false}); }
  function renderInbox(data) {
    inbox = data; selected = new Set(data.messages.slice(0,10).map(m => m.id));
    $('connect-panel').hidden = true; $('workspace').hidden = false;
    $('inbox-heading').textContent = `${data.total_unread.toLocaleString()} 封未读邮件`;
    $('scope').textContent = `${user.ispace_username}@mail.bnbu.edu.cn · 仅收件箱 · 北京时间 · 本页 ${data.messages.length} 封`;
    $('page-label').textContent = `第 ${data.page + 1} 页`;
    $('mail-list').replaceChildren();
    if (!data.messages.length) $('mail-list').append(text('p','收件箱没有未读邮件。','empty'));
    for (const m of data.messages) {
      const row = text('label','', 'message-row'), check = document.createElement('input');
      check.type = 'checkbox'; check.checked = selected.has(m.id); check.setAttribute('aria-label', `选择邮件：${m.subject}`);
      check.addEventListener('change', () => {
        if (check.checked && selected.size >= 10) { check.checked = false; notice('每次最多总结 10 封，可取消部分选择后再试。'); return; }
        check.checked ? selected.add(m.id) : selected.delete(m.id); updateSelection();
      });
      const copy = text('div','', 'message-copy'), meta = text('div','', 'mail-meta');
      meta.append(text('span',m.sender || m.sender_address),text('time', date(m.received_at)));
      copy.append(meta,text('div',m.subject,'mail-subject'),text('div',m.snippet,'mail-snippet'));
      row.append(check,copy); $('mail-list').append(row);
    }
    $('digest-output').replaceChildren(text('p',data.messages.length ? '选择邮件后生成本批简报。' : '没有需要总结的未读邮件。','empty'));
    updateSelection();
  }
  async function checkAI() {
    aiReady = false;
    try { const config = await api('/api/mail-digest/ai-status'); aiReady = true; $('ai-status').textContent = `OmniChat · ${config.model} · 使用共享账号积分`; }
    catch (e) { $('ai-status').textContent = e.message; }
    updateSelection();
  }
  async function loadStatus() {
    const status = await api('/api/mail-digest/status');
    $('username').value = status.username; $('username').readOnly = true;
    $('saved-row').hidden = !status.credential_saved;
    $('account-hint').textContent = `已登录 ${user.display_name || user.username}，连接已绑定的学校邮箱。`;
    return status;
  }
  $('use-saved').addEventListener('change', () => {
    const saved = $('use-saved').checked; $('password-row').hidden = saved; $('password').required = !saved;
    if (saved) $('password').value = '';
  });
  $('connect-form').addEventListener('submit', async event => {
    event.preventDefault(); if (busy) return; notice(''); setBusy(true); $('connect-button').textContent = '正在连接 MIS 和邮箱…';
    let password = $('password').value;
    try {
      if (!user) { const login = await api('/api/login/ispace', {username:$('username').value.trim(),password}); user = login.user; }
      await loadStatus();
      const data = await api('/api/mail-digest/connect', {password, use_saved_password:$('use-saved').checked});
      renderInbox(data); await checkAI();
    } catch (e) { notice(e.message); }
    finally { password = ''; $('password').value = ''; setBusy(false); $('connect-button').textContent = '连接并查看未读邮件 →'; }
  });
  $('select-first').addEventListener('change', () => {
    selected = new Set($('select-first').checked ? inbox.messages.slice(0,10).map(m=>m.id) : []);
    document.querySelectorAll('.message-row input').forEach((e,i) => {e.checked = selected.has(inbox.messages[i].id);}); updateSelection();
  });
  async function changePage(page) {
    if (busy) return; setBusy(true); notice('');
    try {renderInbox(await api('/api/mail-digest/inbox',{page})); await checkAI();} catch(e) {notice(e.message);} finally {setBusy(false);}
  }
  $('refresh').addEventListener('click',()=>changePage(0));
  $('previous').addEventListener('click',()=>changePage(inbox.page-1));
  $('next').addEventListener('click',()=>changePage(inbox.page+1));
  $('disconnect').addEventListener('click',async()=> {
    if (busy) return; setBusy(true);
    try { await api('/api/mail-digest/disconnect',{}); inbox=null; selected.clear(); $('mail-list').replaceChildren(); $('digest-output').replaceChildren(); $('workspace').hidden=true; $('connect-panel').hidden=false; await loadStatus(); notice(''); }
    catch(e) {notice(e.message);} finally {setBusy(false);}
  });
  $('summarize').addEventListener('click',async()=> {
    if (busy || !selected.size) return;
    setBusy(true); notice(''); $('progress').hidden=false; $('progress').textContent='正在提取邮件文字并生成简报，可能需要一两分钟。';
    try {
      const result=await api('/api/mail-digest/summarize',{ids:[...selected]}), out=$('digest-output');
      out.replaceChildren(text('div',result.digest.overview,'overview'));
      out.append(text('p',`本次 ${result.summarized_count} / ${result.total_unread} 封未读邮件 · ${result.cached ? '已生成结果，未重复计费' : '刚刚生成'}${Number.isFinite(result.digest.credits) ? ` · ${result.digest.credits} 积分` : ''}`,'digest-meta'));
      const mails=new Map(result.messages.map(m=>[m.id,m]));
      for(const item of [...result.digest.items].sort((a,b)=>(a.priority==='action'?0:1)-(b.priority==='action'?0:1))) {
        const m=mails.get(item.id), card=text('article','','digest-card');
        card.append(text('span',item.priority==='action'?'需关注 / 可能需行动':'知悉即可',`badge ${item.priority}`),text('h3',m.subject),text('p',item.summary));
        if(item.action) card.append(text('p',`待办：${item.action}`));
        if(item.deadline) card.append(text('p',`日期：${item.deadline}`,'deadline'));
        if(m.body_truncated || m.has_images) card.append(text('p',`${m.body_truncated?'正文较长，仅总结前 8,000 字。 ':''}${m.has_images?'邮件包含图片，图片文字与二维码未读取。':''}`,'body-note'));
        const details=document.createElement('details'); details.append(text('summary','查看本次使用的邮件文字 ▾'),text('div',`${m.sender} <${m.sender_address}>\n${date(m.received_at)}\n\n${m.body || '未提取到文字，请打开学校邮箱查看图片。'}`,'original'));
        card.append(details);out.append(card);
      }
    }catch(e){notice(e.message);}finally{$('progress').hidden=true;setBusy(false);}
  });
  async function init() {
    setBusy(true);
    try {
      const auth=await api('/api/user');user=auth.user;
      if(user) {
        const status=await loadStatus();
        if(status.connected) {renderInbox(await api('/api/mail-digest/inbox',{page:0}));await checkAI();}
      }
    }catch(e){notice(e.message);}finally{setBusy(false);}
  }
  // A restored browser history entry must not expose the previous user's mail.
  window.addEventListener('pagehide',()=>{ $('password').value='';$('mail-list').replaceChildren();$('digest-output').replaceChildren();inbox=null;selected.clear(); });
  window.addEventListener('pageshow',event=>{if(event.persisted)location.reload();});
  init();
})();
