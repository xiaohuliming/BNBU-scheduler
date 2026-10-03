'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const origin = 'https://www.bnbscheduler.top';
  const state = { token: null, generation: 0, controller: null, busy: false, mergeBusy: false, feedbackTimer: null };
  const selected = () => document.querySelector('input[name="client"]:checked').value;
  const formats = () => selected() === 'shadowrocket' ? { subscription: 'txt', complete: 'conf' } : { subscription: 'yaml', complete: 'yaml' };
  const path = suffix => '/campus-connect/subscriptions/' + state.token + '.' + suffix;
  const publicURL = suffix => origin + path(suffix);
  const rules = [
    'DOMAIN,papercut.bnbu.edu.cn,校园资源', 'DOMAIN,ispace.bnbu.edu.cn,校园资源',
    ...['172.16.244.61','192.168.111.251','172.16.244.66','172.16.242.60','61.143.62.109'].map(ip => 'IP-CIDR,' + ip + '/32,校园资源,no-resolve'),
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
    $('import-hint').textContent = shadowrocket
      ? '在 Shadowrocket 中添加订阅，粘贴下方地址，更新后找到 MAXCOURSE Campus 节点。添加订阅会保留现有节点。'
      : '在客户端中选择添加订阅或从 URL 导入，粘贴下方地址。FlClash、Clash Verge Rev 与 Stash 使用 YAML 格式。';
    $('merge-hint').textContent = shadowrocket
      ? '将片段中的校园规则放到当前规则最前面，并合并 Host 项。使用刚添加的 MAXCOURSE Campus 节点，不修改原有最终规则。片段用于合并，请勿作为完整配置导入。'
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
      renderClient();
      $('result').hidden = false;
      const expiry = /(?:^|;)\s*expire=(\d+)/.exec(response.headers.get('Subscription-Userinfo') || '');
      $('expiry-note').textContent = expiry ? '试用有效至 ' + new Date(Number(expiry[1]) * 1000).toLocaleString('zh-CN', {timeZone: 'Asia/Singapore', hour12: false}) + '，UTC+8。' : '有效期以发放信息为准，到期或撤销后将无法继续使用。';
      $('result-title').focus();
    } catch (failure) {
      if (generation === state.generation) { state.token = null; error(failure.name === 'AbortError' || failure instanceof TypeError ? '验证连接中断，请重试。本次尚未导入或修改客户端配置。' : failure.message); }
    } finally {
      clearTimeout(timer);
      if (generation === state.generation) { state.busy = false; state.controller = null; $('validate').disabled = !$('subscription').value.trim(); $('validate').textContent = '验证订阅并继续 →'; }
    }
  }
  async function copy(text) {
    try { await navigator.clipboard.writeText(text); notice('订阅地址已复制'); }
    catch (_) { notice('浏览器未允许复制。请从私人连接说明中复制订阅地址。'); }
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
        const lines = rules.map(rule => rule.replace('校园资源', 'MAXCOURSE Campus'));
        download('# Merge into your existing Shadowrocket profile; keep its final rule.\n[Rule]\n' + lines.join('\n') + '\n\n[Host]\npapercut.bnbu.edu.cn = 172.16.244.61\n', 'Shadowrocket-campus-merge.conf');
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
  const jsonSafe = data => JSON.stringify(data, null, 2);
  $('connect-form').addEventListener('submit', validate);
  $('subscription').addEventListener('input', reset);
  $('clear-key').addEventListener('click', () => { $('subscription').value = ''; reset(); $('subscription').focus(); });
  for (const input of document.querySelectorAll('input[name="client"]')) input.addEventListener('change', () => { if (state.busy || state.mergeBusy) reset(); renderClient(); });
  $('copy-subscription').addEventListener('click', () => { if (state.token) copy(publicURL(formats().subscription)); });
  $('merge').addEventListener('click', merge);
  window.addEventListener('pagehide', () => { $('subscription').value = ''; reset(); });
  renderClient();
})();
