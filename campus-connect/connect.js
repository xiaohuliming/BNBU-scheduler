'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const origin = 'https://www.bnbscheduler.top';
  let importToken=null, importSource=null, importReturnTarget=null;
  let qrLibraryPromise=null, importRenderGeneration=0, lifecycleEpoch=0;
  const pendingJSONRequests=new Set();
  const emptyMarkup=$('subscription-empty').innerHTML;
  let ownRenderKey=null;
  const clients={shadowrocket:'Shadowrocket',clash:'Clash Verge Rev',flclash:'FlClash',stash:'Stash'};
  const state = { token: null, generation: 0, controller: null, busy: false, mergeBusy: false, feedbackTimer: null, nodeName: "MAXCOURSE Campus" };
  const selected = () => document.querySelector('input[name="client"]:checked').value;
  const formats = () => selected() === 'shadowrocket' ? { subscription: 'txt', complete: 'conf' } : { subscription: 'yaml', complete: 'yaml' };
  const path = suffix => '/campus-connect/subscriptions/' + state.token + '.' + suffix;
  const publicURL = suffix => origin + path(suffix);
  const rules = [
    'DOMAIN-SUFFIX,bnbscheduler.top,DIRECT',
    'DOMAIN,papercut.bnbu.edu.cn,校园资源', 'DOMAIN,ispace.bnbu.edu.cn,校园资源',
    'DOMAIN,lrcs.bnbu.edu.cn,校园资源', 'DOMAIN,ctv24.bnbu.edu.cn,校园资源',
    ...['172.16.244.61','192.168.111.251','172.16.244.66','172.16.242.60','61.143.62.109','10.101.24.90','61.143.62.70','172.16.242.112','172.31.12.111'].map(ip => 'IP-CIDR,' + ip + '/32,校园资源,no-resolve'),
  ];
  function notice(message) {
    $('feedback').textContent = message;
    $('feedback').hidden = false;
    clearTimeout(state.feedbackTimer);
    state.feedbackTimer = setTimeout(() => { $('feedback').hidden = true; }, 3500);
  }
  function error(message = '') {
    $('form-error').textContent = message;
    $('form-error').hidden = !message;
    $('subscription').setAttribute('aria-invalid', String(!!message));
  }
  function reset() {
    closeImport();
    state.generation++;
    state.controller?.abort();
    state.controller = null; state.token = null; state.busy = false; state.mergeBusy = false;
    $('result').hidden = true;
    $('download-full').removeAttribute('href');
    $('validate').disabled = !$('subscription').value.trim();
    $('validate').textContent = '验证订阅并继续 →';
    $('merge').disabled = false;
    error();
  }
  function parse(value) {
    const url = new URL(value.trim());
    const accepted = new Set(['www.bnbscheduler.top', 'bnbscheduler.top']);
    if (url.protocol !== 'https:' || !accepted.has(url.hostname) || url.port || url.username || url.password || url.search || url.hash) throw new Error('请粘贴维护者发放的 MAXCOURSE HTTPS 订阅地址。');
    const match = url.pathname.match(/^\/campus-connect\/subscriptions\/([A-Za-z0-9_-]{43})\.(yaml|txt|conf)$/);
    if (!match) throw new Error('订阅地址格式不正确，请复制完整的私人订阅链接。');
    return match[1];
  }
  function renderClient() {
    const shadowrocket = selected() === 'shadowrocket';
    if($('own-client'))$('own-client').value=selected();
    $('client-guide').textContent=shadowrocket ? '导入节点 → 合并分流规则 → 使用配置模式' : '导入校园配置 → 启用规则模式 → 验证访问';
    $('import-hint').textContent = shadowrocket
      ? '添加订阅后找到 MAXCOURSE Campus 节点。首页的全局路由选择“配置”，原上网节点仍作为默认。'
      : '从 URL 导入校园配置，启用规则模式，选择 MAXCOURSE Campus 节点。';
    $('merge-hint').textContent = shadowrocket
      ? '将片段中的分流规则放到当前规则最前面，并合并 Host 项。校园规则使用 MAXCOURSE Campus 节点，保留原有最终规则。片段用于合并，请勿作为完整配置导入。'
      : '下载含校园节点与规则的合并片段，将其中的节点、分组、Host 和校园规则合并到现有配置。保留原有上网节点与最终规则，请勿把片段当成完整配置导入。';
    if (state.token) {
      $('download-full').href = publicURL(formats().complete);
      $('download-full').download = shadowrocket ? 'Shadowrocket-campus.conf' : 'MAXCOURSE-campus.yaml';
    }
  }
  async function validate(event) {
    event.preventDefault();
    if (state.busy) return;
    reset();
    try { state.token = parse($('subscription').value); }
    catch (failure) { error(failure.message?.startsWith('请') || failure.message?.startsWith('订阅') ? failure.message : '请输入有效的 HTTPS 私人订阅地址。'); $('subscription').focus(); return; }
    const generation = state.generation;
    const controller = new AbortController(); state.controller = controller; state.busy = true;
    $('validate').disabled = true; $('validate').textContent = '正在验证订阅…';
    const timer = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(path(formats().subscription), { method: 'HEAD', credentials: 'omit', cache: 'no-store', redirect: 'error', signal: controller.signal });
      if (generation !== state.generation) return;
      if (!response.ok) throw new Error(response.status === 404 ? '订阅不可用，可能已到期或撤销。请核对地址或联系维护者。' : '订阅服务暂时不可用，请稍后重试。');
      state.nodeName = response.headers.get("X-Campus-Node-Name") || "MAXCOURSE Campus";
      renderClient();
      if(!event.quiet)$('result').hidden = false;
      const expiry = /(?:^|;)\s*expire=(\d+)/.exec(response.headers.get('Subscription-Userinfo') || '');
      $('expiry-note').textContent = expiry ? '试用有效至 ' + new Date(Number(expiry[1]) * 1000).toLocaleString('zh-CN', {timeZone: 'Asia/Singapore', hour12: false}) + '，UTC+8。' : '有效期以发放信息为准，到期或撤销后将无法继续使用。';
      if(!event.quiet)$('result-title').focus();
      return true;
    } catch (failure) {
      if (generation === state.generation) { state.token = null; error(failure.name === 'AbortError' || failure instanceof TypeError ? '验证连接中断，请重试。本次尚未导入或修改客户端配置。' : failure.message); }
    } finally {
      clearTimeout(timer);
      if (generation === state.generation) { state.busy = false; state.controller = null; $('validate').disabled = !$('subscription').value.trim(); $('validate').textContent = '验证订阅并继续 →'; }
    }
  }
  async function copy(text,source='manual') {
    try {await navigator.clipboard.writeText(text);notice('订阅地址已复制');return true;}
    catch(_) {
      try {showImport(parse(text),'copy',source);importReturnTarget=source==='own'?'[data-subscription-action=copy]':'#copy-subscription';$('import-url').value=text;$('import-url').focus();$('import-url').select();$('import-feedback').textContent='复制受限，请长按链接或使用复制快捷键。';}
      catch(_) {notice('复制未完成，请重新验证订阅。');}
      return false;
    }
  }
  function download(text, filename) {
    const url = URL.createObjectURL(new Blob([text], {type: 'text/plain;charset=utf-8'}));
    const link = document.createElement('a'); link.href = url; link.download = filename;
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  }
  async function merge() {
    if (!state.token || state.mergeBusy) return;
    state.mergeBusy = true;
    const generation = state.generation;
    $('merge').disabled = true;
    try {
      if (selected() === 'shadowrocket') {
        const lines = rules.map(rule => rule.replace('校园资源', state.nodeName));
        download('# Merge into your existing Shadowrocket profile; keep its final rule.\n[Rule]\n' + lines.join('\n') + '\n\n[Host]\npapercut.bnbu.edu.cn = 172.16.244.61\nlrcs.bnbu.edu.cn = 10.101.24.90\nctv24.bnbu.edu.cn = 172.16.242.112\n', 'Shadowrocket-campus-merge.conf');
      } else {
        const controller = new AbortController(); state.controller = controller;
        const timer = setTimeout(() => controller.abort(), 12000);
        let payload;
        try {
          const response = await fetch(path('yaml'), {credentials: 'omit', cache: 'no-store', redirect: 'error', signal: controller.signal});
          if (!response.ok) throw new Error('订阅不可用，请重新验证或联系维护者。');
          const text = await response.text();
          if (text.length > 65536) throw new Error('配置格式不正确，请联系维护者。');
          payload = JSON.parse(text);
        } finally { clearTimeout(timer); }
        if (generation !== state.generation) return;
        if (!Array.isArray(payload.proxies) || !Array.isArray(payload['proxy-groups']) || !Array.isArray(payload.rules)) throw new Error('配置格式不正确，请联系维护者。');
        const fragment = { hosts: payload.hosts || {}, proxies: payload.proxies, 'proxy-groups': payload['proxy-groups'], rules: payload.rules.filter(rule => typeof rule === 'string' && !/^(MATCH|FINAL),/.test(rule)) };
        download(jsonSafe(fragment), 'MAXCOURSE-campus-merge.yaml');
      }
      notice('合并片段已下载，请保留原有最终规则');
    } catch (_) { if (generation === state.generation) notice('暂时无法下载片段，请重新验证订阅后重试。'); }
    finally { if (generation === state.generation) { state.mergeBusy = false; state.controller = null; $('merge').disabled = false; } }
  }
  function closeImport() {
    if($('import-dialog').open)$('import-dialog').close();
    importToken=null;importSource=null;importRenderGeneration++;$('subscription-qr').removeAttribute('aria-busy');
    const target=importReturnTarget;importReturnTarget=null;
    if(target)requestAnimationFrame(()=>{const element=document.querySelector(target);if(element&&!element.disabled&&element.getClientRects().length)element.focus({preventScroll:true});});
    $('import-url').value='';$('subscription-qr').replaceChildren();
    for(const link of document.querySelectorAll('[data-import-client]'))link.removeAttribute('href');
  }
  function subscriptionFor(token,suffix) {return origin+'/campus-connect/subscriptions/'+token+'.'+suffix;}
  function nativeURL(client,token) {
    const url=subscriptionFor(token,client==='shadowrocket'?'txt':'yaml');
    if(client==='shadowrocket')return 'shadowrocket://add/sub://'+btoa(url)+'?remarks='+encodeURIComponent('MAXCOURSE Campus');
    return ({clash:'clash-verge',flclash:'flclash',stash:'stash'}[client])+'://install-config?name='+encodeURIComponent('MAXCOURSE Campus')+'&url='+encodeURIComponent(url);
  }
  function loadQRLibrary() {
    if(typeof window.qrcode==='function')return Promise.resolve(window.qrcode);
    if(qrLibraryPromise)return qrLibraryPromise;
    qrLibraryPromise=new Promise((resolve,reject)=>{
      const script=document.createElement('script');script.src='/vendor/qrcode-generator.js?v=1.4.4';script.async=true;
      const timer=setTimeout(()=>{script.remove();qrLibraryPromise=null;reject(new Error('QR library timeout'));},8000);
      script.onload=()=>{clearTimeout(timer);if(typeof window.qrcode==='function')resolve(window.qrcode);else{qrLibraryPromise=null;reject(new Error('QR library unavailable'));}};
      script.onerror=()=>{clearTimeout(timer);script.remove();qrLibraryPromise=null;reject(new Error('QR library unavailable'));};
      document.head.append(script);
    });
    return qrLibraryPromise;
  }
  async function renderImport(mode) {
    if(!importToken)return;
    const qr=mode==='qr', copying=mode==='copy';
    const generation=++importRenderGeneration, token=importToken;
    document.querySelector('.import-tabs').hidden=copying;
    $('show-native').setAttribute('aria-pressed',String(!qr));$('show-qr').setAttribute('aria-pressed',String(qr));
    $('native-pane').hidden=qr||copying;$('qr-pane').hidden=!qr;
    const shadowrocket=(qr?$('qr-format').value:formats().subscription)==='txt';
    $('import-warning').textContent=shadowrocket
      ? '导入节点后合并分流规则，全局路由选“配置”，保留原默认上网节点。'
      : '导入后启用规则模式。完整校园配置会让其他流量直连，请先保留原配置。';
    $('qr-next-step').textContent=shadowrocket ? '扫码添加节点后合并分流规则，全局路由选“配置”。' : '扫码导入配置后，启用规则模式。';
    $('import-guide-link').textContent=shadowrocket ? '下一步：配置校园规则 →' : '查看连接与验证步骤 →';
    $('import-title').textContent=copying?'复制订阅':qr?'扫码订阅':'导入订阅';
    $('import-url').value=subscriptionFor(importToken,qr?$('qr-format').value:formats().subscription);
    for(const link of document.querySelectorAll('[data-import-client]'))link.href=nativeURL(link.dataset.importClient,importToken);
    $('subscription-qr').replaceChildren();$('subscription-qr').removeAttribute('aria-busy');
    if(qr) {
      $('subscription-qr').setAttribute('aria-busy','true');$('subscription-qr').textContent='二维码准备中…';
      try {
        const factory=await loadQRLibrary();
        if(generation!==importRenderGeneration||token!==importToken)return;
        const code=factory(0,'M');code.addData($('import-url').value,'Byte');code.make();
        $('subscription-qr').innerHTML=code.createSvgTag({cellSize:4,margin:16,scalable:true});
        $('subscription-qr').querySelector('svg')?.setAttribute('aria-hidden','true');
      } catch(_) {if(generation===importRenderGeneration&&token===importToken)$('subscription-qr').textContent='二维码暂不可用，请复制下方地址。';}
      finally {if(generation===importRenderGeneration)$('subscription-qr').removeAttribute('aria-busy');}
    }
  }
  function showImport(token,mode,source) {
    importToken=token;importSource=source;$('qr-format').value=formats().subscription;
    $('import-feedback').textContent='未打开客户端？复制链接手动添加。';renderImport(mode);
    if(!$('import-dialog').open)$('import-dialog').showModal();
  }
  async function prepareOwn(mode) {
    const own=account.data?.subscription,user=account.data?.user;
    if(account.busy || !own?.active || !own?.synced)return;
    account.busy=true;renderAccount();
    try {
      $('subscription').value=own.subscription_url;
      const ok=await validate({quiet:true,preventDefault(){}});
      if(ok && state.token && account.data?.user?.id===user.id){showImport(state.token,mode,'own');importReturnTarget=mode==='qr'?'[data-subscription-action=qr]':'[data-subscription-action=import]';}
      else accountError('订阅暂时无法验证，请刷新后再试。');
    } finally {account.busy=false;renderAccount();}
  }
  $('manual-import').addEventListener('click',()=>{if(state.token){showImport(state.token,'native','manual');importReturnTarget='#manual-import';}});
  $('import-close').addEventListener('click',closeImport);
  $('import-dialog').addEventListener('close',closeImport);
  $('show-native').addEventListener('click',()=>renderImport('native'));
  $('show-qr').addEventListener('click',()=>renderImport('qr'));
  $('qr-format').addEventListener('change',()=>renderImport('qr'));
  $('copy-import').addEventListener('click',async()=>{
    try {await navigator.clipboard.writeText($('import-url').value);$('import-feedback').textContent='订阅地址已复制，可粘贴到客户端。';}
    catch(_) {$('import-url').focus();$('import-url').select();$('import-feedback').textContent='浏览器未允许复制，请长按或使用复制快捷键。';}
  });
  $('import-guide-link').addEventListener('click',event=>{
    event.preventDefault();
    if(!$('qr-pane').hidden && $('qr-format').value==='yaml' && selected()==='shadowrocket')document.querySelector('input[name="client"][value="clash"]').checked=true;
    if(!$('qr-pane').hidden && $('qr-format').value==='txt')document.querySelector('input[name="client"][value="shadowrocket"]').checked=true;
    renderClient();importReturnTarget=null;closeImport();
    if(location.hash!=='#setup')history.pushState(null,'','#setup');updateNavigation();
    if(state.token){
      const generation=state.generation;$('result').hidden=false;document.querySelector('.guide-detail').open=true;
      requestAnimationFrame(()=>{if(generation===state.generation&&!document.hidden&&!$('result').hidden)$('result-title').focus({preventScroll:true});});
    }
    $('setup').scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth'});
  });
  for(const link of document.querySelectorAll('[data-import-client]'))link.addEventListener('click',()=>{
    document.querySelector('input[name="client"][value="'+link.dataset.importClient+'"]').checked=true;renderClient();renderImport('native');
    $('import-feedback').textContent='已请求打开 '+clients[link.dataset.importClient]+'，请在客户端确认导入。';
  });
  const jsonSafe = data => JSON.stringify(data, null, 2);
  $('connect-form').addEventListener('submit', validate);
  $('subscription').addEventListener('input', reset);
  $('clear-key').addEventListener('click', () => { $('subscription').value = ''; reset(); $('subscription').focus(); });
  function chooseClient(client) {
    if(!clients[client])return;
    document.querySelector('input[name="client"][value="'+client+'"]').checked=true;
    if(state.busy || state.mergeBusy)reset();
    renderClient();
  }
  for (const input of document.querySelectorAll('input[name="client"]')) input.addEventListener('change', () => chooseClient(input.value));
  $('copy-subscription').addEventListener('click', () => { if (state.token) copy(publicURL(formats().subscription)); });
  $('merge').addEventListener('click', merge);
  window.addEventListener('pagehide', () => { $('subscription').value = ''; reset(); });
  const account = { data: null, busy: false, loginBusy: false, authMode: 'login', generation: 0, timer: null, resetTarget: null };
  function accountError(message = '') { $('account-error').textContent = message; $('account-error').hidden = !message; }
  async function jsonRequest(url,options,message) {
    const controller=new AbortController();const epoch=lifecycleEpoch;pendingJSONRequests.add(controller);
    const timer=setTimeout(()=>controller.abort(),12000);
    try {
      const response=await fetch(url,{...options,signal:controller.signal});
      const data=await response.json();
      if(epoch!==lifecycleEpoch)throw new Error('页面状态已变化，请重试。');
      return {response,data};
    } catch(error) {
      if(error.name==='AbortError')throw new Error(message);
      if(error instanceof SyntaxError)throw new Error('服务返回异常，请稍后重试。');
      if(error instanceof TypeError)throw new Error('网络连接中断，请稍后重试。');
      throw error;
    } finally {clearTimeout(timer);pendingJSONRequests.delete(controller);}
  }
  async function accountAPI(path, body) {
    const {response,data}=await jsonRequest('/api/campus-connect/'+path,{method:body?'POST':'GET',credentials:'same-origin',cache:'no-store',headers:body?{'Content-Type':'application/json','X-Campus-CSRF':account.data?.csrf_token||''}:{},...(body?{body:JSON.stringify(body)}:{})},body?'请求超时，请刷新确认结果。':'状态读取超时，请重试。');
    if(!response.ok){const failure=new Error(typeof data.error==='string'?data.error:'申请服务暂时不可用。');failure.responseData=data;throw failure;}
    return data;
  }
  function renderAccount() {
    const data=account.data, user=data?.user, own=data?.subscription;
    const active=Boolean(own?.claimed && own.active && own.subscription_url && own.expires_at*1000>Date.now());
    $('quota-label').textContent=data?.available ? '测试名额 '+data.used+' / '+data.capacity : '名额暂时不可读取';
    $('identity').textContent=user ? '已登录 · '+user.display_name : data?.identityUnknown ? '登录状态暂不可读取' : '尚未登录';
    $('login-open').textContent=user ? '切换账号' : '登录';
    $('login-open').disabled=account.busy;
    $('claim').textContent=own?.claimed ? (own.active ? '查看我的订阅 →' : '测试订阅已结束') : account.busy?'正在领取…':user?'申请领取 60 天订阅 →':'登录后领取 →';
    const canClaim=data?.available && (!own?.claimed || own.active) && (own?.claimed || !user || $('consent').checked) && (own?.claimed || own?.active || data.remaining>0);
    $('claim').disabled=account.busy || !canClaim;
    $('consent').disabled=account.busy;
    $('refresh-status').disabled=account.busy;
    $('application').hidden=Boolean(own?.claimed);
    $('subscription-badge').textContent=active ? (own.synced ? '订阅有效' : '配置准备中') : own?.claimed ? '测试已结束' : '60 天测试';
    const renderKey=JSON.stringify([user?.id,own,account.busy,active?Math.ceil((own.expires_at*1000-Date.now())/86400000):null]);
    const restoreClientFocus=document.activeElement?.id==='own-client';
    const changed=renderKey!==ownRenderKey;ownRenderKey=renderKey;
    if(changed)$('own-subscription').replaceChildren();
    $('own-subscription').hidden=!active;
    $('subscription-empty').hidden=active || !own?.claimed;
    if(own?.claimed && !own.active) {
      $('subscription-empty').replaceChildren();
      const note=document.createElement('p');note.textContent='测试已结束，请联系维护者。';$('subscription-empty').append(note);
    }
    if(!own?.claimed && $('subscription-empty').innerHTML!==emptyMarkup)$('subscription-empty').innerHTML=emptyMarkup;
    $('account-expiry').textContent=own?.claimed ? '有效至 '+new Date(own.expires_at*1000).toLocaleString('zh-CN',{timeZone:'Asia/Singapore',hour12:false})+' · UTC+8' : '每个 MAXCOURSE 账号一份。';
    $('account-expiry').hidden=active || !own?.claimed;
    if(active && changed) {
      const card=document.createElement('article');card.className='subscription-card';
      const title=document.createElement('h3');title.textContent='个人订阅';
      const status=document.createElement('p');status.className='subscription-state';status.textContent=own.synced ? '' : '配置准备中';status.hidden=own.synced;
      const metrics=document.createElement('dl');metrics.className='subscription-metrics';
      const days=Math.max(0,Math.ceil((own.expires_at*1000-Date.now())/86400000));
      for(const [label,value,unit,extra] of [['剩余有效期',String(days),'天',''],['在线出口',Number.isInteger(own.online_sessions)?String(own.online_sessions):'未知','/ 2',''],['到期日期',new Date(own.expires_at*1000).toLocaleDateString('en-CA',{timeZone:'Asia/Singapore'}),'','expiry-day']]) {
        const cell=document.createElement('div'),dt=document.createElement('dt'),dd=document.createElement('dd'),small=document.createElement('small');dt.textContent=label;dd.textContent=value;dd.className=extra;small.textContent=unit?' '+unit:'';if(unit)dd.append(small);cell.append(dt,dd);metrics.append(cell);
      }
      const clientControl=document.createElement('div');clientControl.className='subscription-client';
      const clientLabel=document.createElement('label');clientLabel.htmlFor='own-client';clientLabel.textContent='客户端';
      const clientSelect=document.createElement('select');clientSelect.id='own-client';clientSelect.disabled=account.busy || !own.synced || !data?.available;
      for(const [value,label] of Object.entries(clients)){const option=document.createElement('option');option.value=value;option.textContent=label;clientSelect.append(option);}
      clientSelect.value=selected();clientSelect.addEventListener('change',()=>chooseClient(clientSelect.value));clientControl.append(clientLabel,clientSelect);
      const actions=document.createElement('div');actions.className='subscription-actions';
      for(const [label,className,handler] of [['一键导入','primary',()=>prepareOwn('native')],['复制订阅','secondary',()=>copy(own.subscription_url.replace(/\.(txt|yaml|conf)$/,'.'+formats().subscription),'own')],['二维码','secondary',()=>prepareOwn('qr')]]) {
        const button=document.createElement('button');button.dataset.subscriptionAction=label==='一键导入'?'import':label==='复制订阅'?'copy':'qr';button.className='button '+className;button.type='button';button.textContent=label;button.disabled=account.busy || !own.synced || !data?.available;button.addEventListener('click',handler);actions.append(button);
      }
      const footer=document.createElement('div');footer.className='subscription-footer';
      const note=document.createElement('p');note.textContent='同一公网出口合并计数。';
      const resetButton=document.createElement('button');resetButton.className='subscription-reset';resetButton.type='button';resetButton.textContent='重置订阅';resetButton.disabled=account.busy || !data?.available;resetButton.addEventListener('click',()=>{account.resetTarget={user:user.id};$('reset-error').hidden=true;$('reset-dialog').showModal();});
      footer.append(note,resetButton);card.append(title,status,metrics,clientControl,actions,footer);$('own-subscription').append(card);
      if(restoreClientFocus && !clientSelect.disabled)clientSelect.focus({preventScroll:true});
      $('claim-message').textContent=own.synced ? '已领取，同一订阅最多两个公网出口同时在线。' : '申请已保存，配置生效后即可导入。';
    } else if(!active && data?.available && data.remaining===0) $('claim-message').textContent='测试名额已满，已有订阅可继续使用。';
    else if(!active) $('claim-message').textContent='每个账号领取一次。';
    const node=data?.node;
    $('node-status').dataset.state=node?.status || 'unknown';
    $('node-status').textContent=node?.status==='ready' ? '服务端就绪' : node?.status==='unavailable' ? '暂不可用' : '状态未知';
    $('node-note').textContent=node?.status==='ready' ? '客户端连接后，用下方入口验证。' : node?.status==='unavailable' ? '转发通道未就绪，请稍后再试。' : '请刷新后重试。';
  }
  async function refreshAccount() {
    const generation=++account.generation;
    try {const data=await accountAPI('status');if(generation!==account.generation)return;if((account.data?.user?.id && account.data.user.id!==data.user?.id) || (account.data?.subscription?.subscription_url && (account.data.subscription.subscription_url!==data.subscription?.subscription_url || !data.subscription?.active))){$('subscription').value='';reset();}account.data=data;accountError();}
    catch(error){
      if(generation!==account.generation)return;
      if(importSource==='own'){$('subscription').value='';reset();}
      const confirmed=error.responseData;
      account.data={available:false,user:confirmed?.user || null,subscription:null,identityUnknown:!confirmed || !('user' in confirmed)};
      accountError(error.message);
    }
    renderAccount();
    clearTimeout(account.timer);
    account.timer=setTimeout(()=>{if(!document.hidden&&!account.busy)refreshAccount();},account.data?.subscription?.claimed && !account.data.subscription.synced ? 3000 : 15000);
  }
  function renderLogin() {
    const register=account.authMode==='register';
    $('login-title').textContent=register?'注册 MAXCOURSE':'登录 MAXCOURSE';
    $('login-submit').textContent=account.loginBusy ? '请稍候…' : register?'注册并登录':'登录';
    $('auth-toggle').textContent=register?'已有账号？登录':'没有账号？注册';
    $('login-password').autocomplete=register?'new-password':'current-password';
    for(const id of ['login-submit','login-close','auth-toggle','login-username','login-password'])$(id).disabled=account.loginBusy;
  }
  function openLogin() {
    account.authMode='login';$('login-password').value='';$('login-error').hidden=true;renderLogin();
    if(!$('login-dialog').open)$('login-dialog').showModal();$('login-username').focus();
  }
  const loginErrors={'Invalid credentials':'用户名或密码不正确。','Username already exists':'用户名已存在，请登录或换一个用户名。','Username and password required':'请填写用户名和密码。','Username or password is too long':'用户名或密码过长。'};
  async function siteAuth(endpoint,username,password) {
    const {response,data}=await jsonRequest('/api/'+endpoint,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,password})},endpoint==='register'?'注册结果暂未确认，请先尝试登录。':'登录超时，请稍后重试。');
    if(!response.ok)throw new Error(loginErrors[data.error]||data.error||'暂时无法登录，请稍后重试。');
  }
  $('login-open').addEventListener('click',openLogin);
  $('auth-toggle').addEventListener('click',()=>{if(account.loginBusy)return;account.authMode=account.authMode==='login'?'register':'login';$('login-password').value='';$('login-error').hidden=true;renderLogin();});
  $('login-close').addEventListener('click',()=>{if(!account.loginBusy)$('login-dialog').close();});
  $('login-dialog').addEventListener('cancel',event=>{if(account.loginBusy)event.preventDefault();});
  $('login-dialog').addEventListener('close',()=>{$('login-password').value='';});
  $('login-form').addEventListener('submit',async event=>{
    event.preventDefault();if(account.loginBusy)return;
    const epoch=lifecycleEpoch;const username=$('login-username').value.trim();let password=$('login-password').value;
    if(!username||!password){$('login-error').textContent='请填写用户名和密码。';$('login-error').hidden=false;return;}
    account.loginBusy=true;renderLogin();$('login-error').hidden=true;
    try {
      if(account.authMode==='register')await siteAuth('register',username,password);
      if(epoch!==lifecycleEpoch)return;
      await siteAuth('login',username,password);
      if(epoch!==lifecycleEpoch)return;
      $('login-dialog').close();$('subscription').value='';reset();await refreshAccount();notice('已登录');
    } catch(error){$('login-error').textContent=error.message;$('login-error').hidden=false;}
    finally{password=null;$('login-password').value='';account.loginBusy=false;renderLogin();}
  });
  $('consent').addEventListener('change',renderAccount);
  $('refresh-status').addEventListener('click',refreshAccount);
  $('claim').addEventListener('click',async()=>{
    if(account.busy)return;
    if(!account.data?.user){openLogin();return;}
    if(account.data.subscription?.claimed){$('subscription-panel').scrollIntoView({behavior:'smooth'});return;}
    const epoch=lifecycleEpoch;account.busy=true;accountError();renderAccount();
    try {const data=await accountAPI('claim',{consent:$('consent').checked});if(epoch!==lifecycleEpoch)return;account.data={...account.data,...data};notice('测试订阅已领取');}
    catch(error){accountError(error.message);}
    finally{account.busy=false;if(epoch===lifecycleEpoch){renderAccount();refreshAccount();}}
  });
  $('reset-cancel').addEventListener('click',()=>{if(!account.busy)$('reset-dialog').close();});
  $('reset-dialog').addEventListener('close',()=>requestAnimationFrame(()=>{const button=document.querySelector('.subscription-reset');if(button&&!button.disabled&&button.getClientRects().length)button.focus({preventScroll:true});}));
  $('reset-dialog').addEventListener('cancel',event=>{if(account.busy)event.preventDefault();});
  $('reset-confirm').addEventListener('click',async()=>{
    if(account.busy||!account.resetTarget||account.resetTarget.user!==account.data?.user?.id)return;
    const epoch=lifecycleEpoch;account.busy=true;$('reset-confirm').disabled=true;$('reset-cancel').disabled=true;$('reset-error').hidden=true;
    try{const data=await accountAPI('reset-subscription',{});if(epoch!==lifecycleEpoch)return;account.data={...account.data,...data};$('subscription').value='';reset();$('reset-dialog').close();notice('旧链接已失效，请重新导入新链接');}
    catch(error){$('reset-error').textContent=error.message;$('reset-error').hidden=false;}
    finally{account.busy=false;$('reset-confirm').disabled=false;$('reset-cancel').disabled=false;if(epoch===lifecycleEpoch){renderAccount();refreshAccount();}}
  });
  window.addEventListener('pagehide',()=>{lifecycleEpoch++;account.generation++;for(const request of pendingJSONRequests)request.abort();pendingJSONRequests.clear();clearTimeout(account.timer);$('login-password').value='';account.data=null;account.resetTarget=null;ownRenderKey=null;$('own-subscription').replaceChildren();});
  document.addEventListener('visibilitychange',()=>{if(!document.hidden&&!account.busy)refreshAccount();});
  window.addEventListener('pageshow',event=>{if(event.persisted)refreshAccount();});
  const navLinks=[...document.querySelectorAll('.sidebar nav a[href^="#"]')];
  function updateNavigation() {
    const hash=navLinks.some(link=>link.hash===location.hash)?location.hash:'#subscription-panel';
    for(const link of navLinks){const active=link.hash===hash;link.classList.toggle('active',active);if(active)link.setAttribute('aria-current','location');else link.removeAttribute('aria-current');}
  }
  window.addEventListener('hashchange',updateNavigation);
  for(const link of navLinks)link.addEventListener('click',()=>{for(const item of navLinks){item.classList.toggle('active',item===link);if(item===link)item.setAttribute('aria-current','location');else item.removeAttribute('aria-current');}});
  updateNavigation();renderClient();refreshAccount();
})();
