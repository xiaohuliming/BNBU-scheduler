/* The form uses the real MAXCOURSE session and print APIs. Credentials and
   documents stay in page memory; an uncertain dispatch keeps its intent ID. */
'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const ui = Object.fromEntries([
    'privacy-notice', 'privacy-title', 'print-workspace', 'privacy-open', 'privacy-check',
    'privacy-accept', 'privacy-choice', 'privacy-review', 'privacy-return', 'privacy-revoke',
    'privacy-storage-note', 'help-privacy',
    'topbar', 'submit-form', 'workspace-fields', 'service-status', 'service-dot', 'service-text',
    'service-refresh', 'dropzone', 'drop-visual', 'pick-btn', 'file-input', 'doc-panel', 'doc-name',
    'doc-sub', 'doc-remove', 'doc-open', 'doc-progress', 'limits-hint', 'file-error', 'capability-row', 'drop-sub', 'file-kind',
    'school-username', 'school-password', 'password-toggle', 'form-error',
    'progress-text', 'submit-btn', 'submit-label', 'submit-steps', 'account-fields', 'receipt', 'receipt-icon',
    'receipt-title', 'receipt-message', 'receipt-meta', 'receipt-details', 'receipt-query', 'receipt-retry',
    'new-print', 'history', 'jobs-count', 'jobs-list', 'jobs-empty', 'jobs-refresh',
    'help-open', 'help-close', 'help-dialog', 'announce',
    'go-print', 'go-print-note', 'account-dialog', 'account-back', 'confirm-name', 'account-service',
    'confirm-thumb', 'thumb-frame', 'upload-stage', 'preview-canvas', 'preview-tint', 'back-canvas', 'back-tint',
    'blank-note', 'sheet', 'sheet-wrap', 'copies-badge', 'page-stage', 'preview-loading', 'preview-caption',
    'page-prev', 'page-next', 'page-current', 'page-total', 'flip-group', 'flip-sheet', 'sheet-caption',
    'zoom-out', 'zoom-fit', 'zoom-in', 'replace-file', 'range-summary', 'print-total', 'print-total-sheets',
    'cost-note', 'estimated-cost', 'confirm-cost', 'receipt-cost', 'copies', 'copies-dec', 'copies-inc', 'color-note', 'sides-note', 'copies-note', 'edge-setting',
    'page-title', 'page-description', 'stepper', 'step-upload', 'step-preview', 'step-submit', 'confirm-specs', 'bulk-confirm', 'bulk-check', 'bulk-label', 'balance-panel', 'balance-query', 'balance-value', 'balance-status', 'receipt-balance',
  ].map(id => [id, $(id)]));
  const radios = name => Array.from(document.querySelectorAll(`input[name="${name}"]`));
  const DEFAULT_OPTIONS = Object.freeze({ color: 'grayscale', sides: 'one-sided', copies: 1 });
  const PRIVACY_KEY = 'maxcourse.print.privacy';
  const PRIVACY_VERSION = '2026-10-03.2';
  const COPIES_LIMIT = 100;
  const ZOOMS = ['fit', '1', '1.5', '2'];
  const STEPS = ['auth', 'inspect', 'send'];
  const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)');
  // Office documents and images are converted to PDF by the print service before preview.
  const CONVERTIBLE = /\.(docx?|odt|rtf|pptx?|odp|xlsx?|ods|jpe?g|png)$/i;
  const CONVERT_ACCEPT = '.pdf,.doc,.docx,.odt,.rtf,.ppt,.pptx,.odp,.xls,.xlsx,.ods,.jpg,.jpeg,.png';
  const state = {
    privacyAccepted: false,
    session: null, sessionGeneration: 0, identityGeneration: 0, fileGeneration: 0,
    file: null, fileURL: null, pdf: null, pages: null, busy: false, reading: false,
    intent: null, receipt: null, receiptTone: '', receiptDoc: null, jobs: [], pollTimer: null, polls: 0,
    checking: false, retry: false, jobQuery: false,
    balance: null, balanceQuerying: false, balanceGeneration: 0, balanceMessage: '',
    previewReady: false, previewRendering: false, previewPage: 1, previewZoom: 'fit', drawn: false,
    faceKey: '', facePromise: null, options: { ...DEFAULT_OPTIONS }, edge: 'long', flipped: false,
    step: '', copiesTyping: false, sweepTimer: null, demoTimer: null, dragDepth: 0, converting: false, converted: false,
  };
  const labels = { submitted: '待刷卡取件', processing: '正在提交', unknown: '结果待确认',
    failed: '未提交', rejected: '账号验证失败' };
  const ICONS = {
    success: '<svg viewBox="0 0 48 48"><path class="draw" d="M14 25l7 7 13-15"/></svg>',
    error: '<svg viewBox="0 0 48 48"><path class="draw" d="M24 13.5v14"/><path d="M24 34.5v.2"/></svg>',
    pending: '<span class="dots"><i></i><i></i><i></i></span>',
  };
  const username = () => ui['school-username'].value.trim();
  const schoolUser = () => state.session?.user?.school_username || '';
  const announce = text => { ui.announce.textContent = text; };
  const showError = (target, text = '') => {
    ui[target].textContent = text;
    ui[target].hidden = !text;
  };
  const ready = () => state.privacyAccepted && navigator.onLine && state.session?.service?.ready === true &&
    state.session.service.enabled === true && !state.session.service.busy && !state.session.service.demo;
  const calm = () => reduceMotion.matches;
  const balanceAvailable = () => state.session?.service?.enabled && state.session.service.online &&
    (state.session.service.features || []).includes('balance');

  // Output options. Until the service reports its features, every option stays previewable.
  const duplex = (options = state.options) => options.sides !== 'one-sided';
  const sidesFor = edge => edge === 'short' ? 'two-sided-short-edge' : 'two-sided-long-edge';
  const activeOptions = () => state.intent?.options || state.options;
  const featuresKnown = () => state.session?.service?.online === true;
  const supports = feature => !featuresKnown() || (state.session.service.features || []).includes(feature);
  const copiesCap = () => {
    const cap = !featuresKnown() ? COPIES_LIMIT :
      supports('copies') ? (state.session.capabilities?.copies?.max || COPIES_LIMIT) : 1;
    const impressions = state.session?.limits?.max_impressions || 30000;
    return state.pages ? Math.max(1, Math.min(cap, Math.floor(impressions / state.pages))) : cap;
  };
  const unsupported = (options = state.options) => [
    options.color !== 'grayscale' && !supports('color') && '彩色',
    duplex(options) && !supports('duplex') && '双面',
    options.copies !== 1 && !supports('copies') && '多份',
  ].filter(Boolean);
  const describe = (options = DEFAULT_OPTIONS) => [
    options.color === 'color' ? '彩色' : '黑白',
    !duplex(options) ? '单面' : options.sides === 'two-sided-short-edge' ? '双面 · 短边' : '双面 · 长边',
    options.copies + ' 份',
  ];
  const sheetsFor = (pages, options) => (duplex(options) ? Math.ceil(pages / 2) : pages) * options.copies;
  const needsBulk = (pages, options) => pages * options.copies > (state.session?.limits?.bulk_confirmation_threshold || 200);
  const previewed = () => !!state.pdf && state.drawn;
  const canConvert = () => ready() && (state.session?.service?.features || []).includes('convert');
  const fileSize = bytes => bytes < 1048576 ? Math.max(1, Math.round(bytes / 1024)) + ' KB' : (bytes / 1048576).toFixed(1) + ' MB';

  async function api(path, body, timeout = 60000) {
    if (!state.privacyAccepted) throw new Error('请先阅读并确认打印隐私告知。');
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    const headers = {};
    if (body) {
      headers['Content-Type'] = 'application/json';
      if (path.startsWith('/api/print/')) {
        headers['X-Print-CSRF'] = state.session?.csrf_token || '';
        headers['X-Print-User'] = String(state.session?.user?.id || '');
      }
    }
    try {
      const response = await fetch(path, { method: body ? 'POST' : 'GET', headers,
        credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
        ...(body ? { body: JSON.stringify(body) } : {}),
      });
      let data = null;
      try { data = await response.json(); } catch (_) { /* HTML errors are not receipts. */ }
      return { ok: response.ok, status: response.status, data };
    } finally { clearTimeout(timer); }
  }

  function errorText(result, fallback) {
    if (result.status === 401) return '学号或密码验证失败，请核对后重试。';
    if (result.status === 429) return '操作较频繁，请稍后重试。';
    return typeof result.data?.error === 'string' ? result.data.error : fallback;
  }

  function progress(message = '', step = '') {
    ui['progress-text'].hidden = !message;
    ui['progress-text'].textContent = message;
    ui['submit-label'].textContent = message || '提交打印';
    state.step = message ? step || state.step : '';
    const current = STEPS.indexOf(state.step);
    for (const item of ui['submit-steps'].children) {
      const index = STEPS.indexOf(item.dataset.step);
      item.dataset.state = current < 0 ? '' : index < current ? 'done' : index === current ? 'active' : '';
    }
    if (message) announce(message);
  }

  // Motion helpers. Every one of them is a no-op under reduced motion.
  async function morph(update) {
    if (calm() || !document.startViewTransition) { update(); return; }
    const transition = document.startViewTransition(update);
    // A newer transition may skip this one; that is expected, not an error.
    transition.ready.catch(() => {});
    transition.finished.catch(() => {});
    await transition.updateCallbackDone.catch(() => {});
  }
  function setText(element, text) {
    if (element.textContent === text) return;
    element.textContent = text;
    if (!calm()) element.animate([{ opacity: 0, transform: 'translateY(6px)', filter: 'blur(4px)' }, { opacity: 1, transform: 'none', filter: 'blur(0)' }],
      { duration: 520, easing: 'cubic-bezier(.16,1,.3,1)' });
  }
  function tweenNumber(element, value) {
    const from = Number(element.dataset.value || 0);
    if (from === value) return;
    element.dataset.value = String(value);
    cancelAnimationFrame(element.tween);
    if (calm()) { element.textContent = String(value); return; }
    const start = performance.now();
    const step = now => {
      const t = Math.min(1, (now - start) / 560);
      element.textContent = String(Math.round(from + (value - from) * (1 - Math.pow(1 - t, 4))));
      if (t < 1) element.tween = requestAnimationFrame(step);
    };
    element.tween = requestAnimationFrame(step);
  }
  function roll(element, direction) {
    if (calm() || !direction) return;
    element.animate([{ opacity: 0, transform: `translateY(${direction * 60}%)` }, { opacity: 1, transform: 'none' }],
      { duration: 380, easing: 'cubic-bezier(.16,1,.3,1)' });
  }
  async function turn(direction, work) {
    const wrap = ui['sheet-wrap'];
    if (!direction || calm()) return work();
    const away = wrap.animate([{ opacity: 1, transform: 'none' }, { opacity: 0, transform: `translateX(${-direction * 36}px) rotate(${-direction * .8}deg)` }],
      { duration: 150, easing: 'cubic-bezier(.4,0,1,1)', fill: 'forwards' });
    await away.finished.catch(() => {});
    try { await work(); } finally {
      wrap.animate([{ opacity: 0, transform: `translateX(${direction * 36}px) rotate(${direction * .8}deg)` }, { opacity: 1, transform: 'none' }],
        { duration: 420, easing: 'cubic-bezier(.16,1,.3,1)' });
      away.cancel();
    }
  }

  function render() {
    const service = state.session?.service;
    let status = '正在连接打印服务';
    let tone = 'connecting';
    if (!navigator.onLine) { status = '网络已断开'; tone = 'offline'; }
    else if (service?.demo) { status = '未连接真实打印设备'; tone = 'offline'; }
    else if (service?.busy) { status = '正在处理其他任务'; tone = 'busy'; }
    else if (ready()) { status = '打印服务已连接'; tone = 'online'; }
    else if (!state.checking) { status = '打印服务未连接'; tone = 'offline'; }
    ui['service-text'].textContent = status;
    ui['service-status'].dataset.state = tone;
    ui['service-dot'].className = 'service-dot ' + tone;
    ui['service-refresh'].hidden = ready() || state.busy;
    ui['service-refresh'].disabled = state.checking;
    ui['privacy-open'].disabled = state.busy || state.reading || state.balanceQuerying || state.jobQuery;
    const locked = !state.privacyAccepted || state.busy || state.balanceQuerying || !!state.receipt;
    const options = activeOptions();
    ui['submit-form'].setAttribute('aria-busy', String(state.busy));
    ui['pick-btn'].disabled = locked || !!state.intent;
    ui['file-input'].disabled = locked || !!state.intent;
    ui['doc-remove'].disabled = locked || !!state.intent;
    ui['replace-file'].disabled = locked || !!state.intent;
    ui['school-username'].disabled = locked || !!state.intent;
    ui['school-password'].disabled = locked;
    ui['password-toggle'].disabled = locked;
    ui['go-print'].disabled = locked || state.reading || !previewed();
    const missing = unsupported(options);
    ui['go-print-note'].textContent = state.converting ? '正在转换为 PDF…' : state.reading || (state.file && !state.drawn) ? '正在生成预览…' :
      !previewed() ? '预览完成后继续。' : missing.length ? `当前设备暂不支持${missing.join('、')}` : '下一步填写学校账号。';
    ui['account-back'].disabled = state.busy;
    ui['account-dialog'].classList.toggle('is-busy', state.busy);
    ui['account-fields'].inert = state.busy;
    ui['account-service'].hidden = ready();
    ui['account-service'].textContent = service?.busy ? '设备正在处理其他任务，请稍后提交。' : '打印服务未连接，暂时无法提交。';
    ui['confirm-name'].textContent = state.file?.name || '';
    ui['submit-btn'].disabled = state.busy || state.balanceQuerying || state.reading || !previewed() || !ready();
    ui['submit-btn'].setAttribute('aria-describedby', 'service-text form-error');
    ui['dropzone'].hidden = !!state.file;
    ui['upload-stage'].hidden = !!state.file;
    setText(ui['page-title'], state.receipt ? '提交结果' : state.file ? '打印预览' : '校园打印');
    const description = state.receipt ? '到打印点刷卡取件。' : '';
    ui['page-description'].hidden = !description;
    setText(ui['page-description'], description);
    const step = state.receipt || ui['account-dialog'].open ? 3 : state.file ? 2 : 1;
    ui.stepper.dataset.step = String(step);
    ['step-upload', 'step-preview', 'step-submit'].forEach((id, index) => {
      if (index + 1 === step) ui[id].setAttribute('aria-current', 'step');
      else ui[id].removeAttribute('aria-current');
    });
    renderSettings(locked, options);
    renderPreview(locked, options);
    renderCapabilities();
    renderBalance();
    ui['doc-panel'].hidden = !state.file;
    ui['doc-progress'].hidden = !state.reading && !state.busy && !state.previewRendering;
    if (state.file) {
      ui['doc-name'].textContent = state.file.name;
      ui['file-kind'].textContent = (state.file.name.split('.').pop() || 'PDF').slice(0, 4).toUpperCase();
      ui['doc-sub'].textContent = state.converting ? '正在转换为 PDF…' : state.reading ? '正在读取文件…' :
        [fileSize(state.file.size), state.pages && state.pages + ' 页', state.converted && '已转为 PDF'].filter(Boolean).join(' · ');
    }
    ui['workspace-fields'].hidden = !!state.receipt;
    ui.receipt.hidden = !state.receipt;
  }

  function renderBalance() {
    const current = state.balance && state.balance.username === username() && state.balance.owner === state.session?.user?.id;
    ui['balance-panel'].hidden = !balanceAvailable() && !current && !state.balanceMessage;
    ui['balance-query'].disabled = state.busy || state.balanceQuerying || !balanceAvailable();
    ui['balance-query'].textContent = state.balanceQuerying ? '正在查询…' : current ? '刷新余额' : '查询余额';
    ui['balance-value'].textContent = current ? state.balance.display : '待查询';
    ui['balance-status'].textContent = state.balanceQuerying ? '正在登录 PaperCut 读取余额，不会提交打印任务。' :
      state.balanceMessage || (current ? `更新于 ${new Date(state.balance.checked_at * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}` : '填写学校账号和密码后查询。');
    ui['receipt-balance'].hidden = !state.receipt || (!current && !state.balanceMessage);
    ui['receipt-balance'].textContent = current ? `查询时 PaperCut 余额 ${state.balance.display}，实际扣费以刷卡取件时为准。` :
      state.balanceMessage ? `PaperCut 余额未能读取：${state.balanceMessage}` : '';
  }

  async function verifySchoolAccount(account, password) {
    if (state.intent && state.intent.username !== account) throw new Error('账号已变更，不能重试原任务。请先核对学校队列。');
    if (schoolUser() !== account) {
      const login = await api('/api/login/ispace', { username: account, password, purpose: 'print' });
      if (!login.ok) throw new Error(errorText(login, '账号验证暂时不可用，请稍后重试。'));
      if (!await refreshSession({ flow: true }) || schoolUser() !== account) throw new Error('账号验证状态未确认，请重试。');
    }
    if (state.intent && state.intent.owner !== state.session?.user?.id) throw new Error('登录账号与原任务不同，请先核对学校队列。');
  }

  async function lookupBalance(account, password, generation = state.balanceGeneration) {
    const identity = state.identityGeneration;
    const owner = state.session?.user?.id;
    const result = await api('/api/print/balance', { password }, 40000);
    if (generation !== state.balanceGeneration || identity !== state.identityGeneration || username() !== account) return;
    const balance = result.data?.balance;
    if (!result.ok) throw new Error(errorText(result, '暂时无法读取 PaperCut 余额。'));
    if (!balance || balance.username !== account || typeof balance.display !== 'string' || !Number.isInteger(balance.checked_at)) {
      throw new Error('没有收到有效的余额，请稍后再查。');
    }
    state.balance = { ...balance, owner };
    state.balanceMessage = '';
    renderBalance();
  }

  async function queryBalance() {
    if (!state.privacyAccepted || state.busy || state.balanceQuerying || !balanceAvailable()) return;
    const account = username();
    let password = ui['school-password'].value;
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(account) || !password || password.length > 512 || /[\r\n\0]/.test(password)) {
      state.balanceMessage = '请先填写本人学校账号和密码。'; renderBalance(); return;
    }
    const generation = ++state.balanceGeneration;
    state.balance = null;
    state.balanceMessage = '';
    state.balanceQuerying = true;
    state.sessionGeneration++;
    render();
    try {
      if (!await refreshSession({ flow: true }) || !balanceAvailable()) throw new Error('余额查询服务暂未连接。');
      await verifySchoolAccount(account, password);
      if (generation !== state.balanceGeneration || username() !== account) return;
      await lookupBalance(account, password, generation);
    } catch (error) {
      if (generation === state.balanceGeneration) state.balanceMessage = error.name === 'AbortError' || error instanceof TypeError ?
        '余额查询连接中断，请稍后再查。' : error.message;
    } finally {
      password = null;
      if (generation === state.balanceGeneration) { state.balanceQuerying = false; render(); }
    }
  }

  function note(id, text, warn = false) {
    ui[id].textContent = text;
    ui[id].classList.toggle('is-warn', warn);
  }

  function renderSettings(locked, options) {
    const frozen = locked || !!state.intent;
    const two = duplex(options);
    for (const input of radios('color')) {
      input.checked = input.value === options.color;
      input.disabled = frozen || (input.value === 'color' && !supports('color'));
    }
    for (const input of radios('sides-mode')) {
      input.checked = (input.value === 'two') === two;
      input.disabled = frozen || (input.value === 'two' && !supports('duplex'));
    }
    const edge = two ? (options.sides === 'two-sided-short-edge' ? 'short' : 'long') : state.edge;
    for (const input of radios('edge')) {
      input.checked = input.value === edge;
      input.disabled = frozen;
    }
    ui['edge-setting'].dataset.open = String(two);
    ui['edge-setting'].inert = !two;
    const cap = copiesCap();
    if (!state.copiesTyping) ui.copies.value = String(options.copies);
    ui.copies.max = String(cap);
    ui.copies.disabled = frozen || cap <= 1;
    ui['copies-dec'].disabled = frozen || options.copies <= 1;
    ui['copies-inc'].disabled = frozen || options.copies >= cap;
    const pages = state.pages || 0;
    const bulkKey = JSON.stringify([state.fileGeneration, pages, options]);
    if (ui['bulk-check'].dataset.key !== bulkKey) ui['bulk-check'].checked = false;
    ui['bulk-check'].dataset.key = bulkKey;
    ui['bulk-check'].disabled = locked;
    ui['bulk-confirm'].hidden = !needsBulk(pages, options);
    ui['bulk-label'].textContent = `我确认打印 ${pages} 页 × ${options.copies} 份，共 ${pages * options.copies} 面，预计用纸 ${sheetsFor(pages, options)} 张。`;
    ui['limits-hint'].textContent = `最大 ${Math.round((state.session?.limits?.max_bytes || 52428800) / 1048576)} MB · 最多 ${state.session?.limits?.max_pages || 300} 页`;
    if (!supports('color')) note('color-note', '设备暂不支持彩色', true);
    else note('color-note', options.color === 'color' ? '按彩色标准计费' : '');
    if (!supports('duplex')) note('sides-note', '设备暂不支持双面', true);
    else note('sides-note', two && pages ? `每份 ${Math.ceil(pages / 2)} 张纸` : '');
    if (!supports('copies')) note('copies-note', '设备暂不支持多份', true);
    else note('copies-note', pages && cap < COPIES_LIMIT ? `最多 ${cap} 份` : '');
    tweenNumber(ui['print-total-sheets'], pages ? sheetsFor(pages, options) : 0);
    ui['print-total'].textContent = pages ? `${pages} 页 × ${options.copies} 份 · 共 ${pages * options.copies} 面` : '正在检查页数';
    const cost = window.MaxcoursePrintPricing?.estimate(pages, options);
    ui['estimated-cost'].textContent = cost?.display || (pages ? '暂无法估算' : '待选文档');
    ui['cost-note'].textContent = cost?.note || '依据学校公开价目表估算，实际以刷卡结算为准。';
    ui['confirm-cost'].textContent = cost ? `估算费用 ${cost.display} · 实际以刷卡结算为准` : '';
    const receiptCost = state.receiptDoc && window.MaxcoursePrintPricing?.estimate(state.receiptDoc.pages, state.receiptDoc.options);
    ui['receipt-cost'].hidden = !state.receipt || !receiptCost;
    ui['receipt-cost'].textContent = receiptCost ? `本次估算费用 ${receiptCost.display}，实际扣费以学校系统为准。` : '';
    ui['range-summary'].textContent = pages ? `全部 ${pages} 页` : '全部页面';
    ui['confirm-specs'].textContent = [pages ? pages + ' 页' : '', 'A4', ...describe(options)].filter(Boolean).join(' · ');
  }

  function renderPreview(locked, options) {
    const two = duplex(options);
    const edge = options.sides === 'two-sided-short-edge' ? 'short' : 'long';
    const pages = state.pages || 0;
    const blankBack = two && state.previewPage + 1 > pages;
    ui.sheet.dataset.flip = two ? edge : 'none';
    ui.sheet.classList.toggle('is-color', options.color === 'color');
    ui.sheet.classList.toggle('is-flipped', two && state.flipped);
    ui.sheet.classList.toggle('is-loading', !state.drawn);
    ui['sheet-wrap'].dataset.copies = String(Math.min(options.copies, 5));
    ui['copies-badge'].textContent = '× ' + options.copies;
    ui['blank-note'].hidden = !blankBack;
    ui['flip-group'].hidden = !two;
    ui['flip-sheet'].setAttribute('aria-pressed', String(two && state.flipped));
    ui['sheet-caption'].textContent = state.flipped ? (blankBack ? '背面空白' : '背面') : '正面';
    ui['page-current'].value = String(two && state.flipped && !blankBack ? state.previewPage + 1 : state.previewPage);
    ui['page-current'].max = String(pages || 1);
    ui['page-total'].textContent = '/ ' + pages;
    const stride = two ? 2 : 1;
    const idle = locked || !state.drawn;
    ui['page-prev'].disabled = idle || state.previewPage <= 1;
    ui['page-next'].disabled = idle || state.previewPage + stride > pages;
    ui['page-current'].disabled = idle;
    ui['flip-sheet'].disabled = idle;
    const zoom = ZOOMS.indexOf(state.previewZoom);
    ui['zoom-out'].disabled = idle || zoom <= 0;
    ui['zoom-in'].disabled = idle || zoom >= ZOOMS.length - 1;
    ui['zoom-fit'].disabled = idle;
    ui['zoom-fit'].textContent = state.previewZoom === 'fit' ? '适合' : Math.round(Number(state.previewZoom) * 100) + '%';
    ui['preview-caption'].textContent = [options.color === 'color' ? '彩色预览' : '黑白预览', 'A4',
      ...(two ? [edge === 'short' ? '短边翻页' : '长边翻页'] : [])].join(' · ');
  }

  function renderCapabilities() {
    const convert = canConvert();
    ui['drop-sub'].textContent = convert ? 'PDF、Word、PPT、Excel 或图片' : 'PDF 文件';
    ui['file-input'].accept = convert ? CONVERT_ACCEPT : '.pdf,application/pdf';
    const cap = state.session?.capabilities?.copies?.max || COPIES_LIMIT;
    const items = featuresKnown()
      ? ['A4 纸张', supports('color') ? '黑白 / 彩色' : '黑白', supports('duplex') ? '单面 / 双面' : '单面', supports('copies') ? `最多 ${cap} 份` : '单份']
      : ['A4 纸张', 'PDF 格式', '浏览器本地预览'];
    const key = items.join('|');
    if (ui['capability-row'].dataset.items === key) return;
    ui['capability-row'].dataset.items = key;
    ui['capability-row'].replaceChildren(...items.map((text, index) => {
      const item = document.createElement('li');
      item.textContent = text;
      item.style.animationDelay = (index * 60) + 'ms';
      return item;
    }));
  }

  // Options change the preview itself: colour sweeps in, duplex flips, copies stack up.
  function applyOptions(next) {
    const before = state.options;
    next.copies = Math.max(1, Math.min(copiesCap(), Math.trunc(Number(next.copies)) || 1));
    state.options = next;
    if (before.color !== next.color) sweep();
    if (before.sides !== next.sides) sidesChanged(before, next);
    if (before.copies !== next.copies) roll(ui.copies, Math.sign(next.copies - before.copies));
  }

  function setOptions(patch) {
    if (state.busy || state.receipt || state.intent) { render(); return; }
    applyOptions({ ...state.options, ...patch });
    render();
  }

  function reconcileOptions() {
    if (state.intent || state.busy) return;
    const next = { ...state.options };
    const dropped = [];
    if (next.color !== 'grayscale' && !supports('color')) { next.color = 'grayscale'; dropped.push('彩色'); }
    if (duplex(next) && !supports('duplex')) { next.sides = 'one-sided'; dropped.push('双面'); }
    if (next.copies !== 1 && !supports('copies')) dropped.push('多份');
    applyOptions(next);
    if (dropped.length) announce(`当前打印设备暂不支持${dropped.join('、')}，已恢复默认设置。`);
  }

  function sweep() {
    clearTimeout(state.sweepTimer);
    if (calm() || !state.drawn) return;
    ui.sheet.classList.add('scanning');
    state.sweepTimer = setTimeout(() => ui.sheet.classList.remove('scanning'), 1150);
  }

  function setFlipped(value, animate = true) {
    value = !!value && duplex(activeOptions());
    if (value === state.flipped) return;
    if (!animate) ui.sheet.classList.add('no-anim');
    state.flipped = value;
    ui.sheet.classList.toggle('is-flipped', value);
    if (!animate) { void ui.sheet.offsetWidth; ui.sheet.classList.remove('no-anim'); }
  }

  function sidesChanged(before, next) {
    clearTimeout(state.demoTimer);
    const back = state.flipped && state.previewPage + 1 <= (state.pages || 0) ? state.previewPage + 1 : 0;
    state.flipped = false;
    ui.sheet.classList.add('no-anim');
    ui.sheet.classList.remove('is-flipped');
    void ui.sheet.offsetWidth;
    ui.sheet.classList.remove('no-anim');
    if (!duplex(next)) {
      // Keep looking at the same page when leaving duplex.
      if (back) queueMicrotask(() => goToPage(back));
      return;
    }
    const front = state.previewPage % 2 ? state.previewPage : state.previewPage - 1;
    if (front !== state.previewPage) queueMicrotask(() => pdfPreview.setPage(front).catch(previewFailure));
    else syncFaces();
    demoFlip();
  }

  // Show the reverse side once, so the chosen binding edge is obvious.
  function demoFlip() {
    clearTimeout(state.demoTimer);
    if (calm() || !state.drawn || !state.pages) return;
    state.demoTimer = setTimeout(async () => {
      await (state.facePromise || Promise.resolve());
      if (!duplex(activeOptions()) || state.flipped || state.busy) return;
      setFlipped(true); render();
      state.demoTimer = setTimeout(() => {
        if (duplex(activeOptions()) && state.flipped) { setFlipped(false); render(); }
      }, 1400);
    }, 320);
  }

  function copyInto(source, target) {
    target.width = source.width;
    target.height = source.height;
    if (source.width) target.getContext('2d', { alpha: false }).drawImage(source, 0, 0);
  }

  function syncFaces() {
    const canvas = ui['preview-canvas'];
    if (!state.drawn || !canvas.width) return Promise.resolve();
    const two = duplex(activeOptions());
    const key = [canvas.dataset.page, canvas.width, canvas.height, two].join(':');
    if (key === state.faceKey) return state.facePromise || Promise.resolve();
    state.faceKey = key;
    copyInto(canvas, ui['preview-tint']);
    if (!two) return (state.facePromise = Promise.resolve());
    const back = Number(canvas.dataset.page) + 1;
    state.facePromise = pdfPreview.drawInto(ui['back-canvas'], back <= state.pages ? back : 0)
      .then(() => copyInto(ui['back-canvas'], ui['back-tint'])).catch(() => {});
    return state.facePromise;
  }

  async function goToPage(value) {
    if (!state.drawn) return;
    clearTimeout(state.demoTimer);
    const pages = state.pages || 1;
    let target = Math.max(1, Math.min(pages, Math.trunc(Number(value)) || 1));
    let flipped = false;
    if (duplex(activeOptions())) {
      flipped = target % 2 === 0;
      if (flipped) target -= 1;
    }
    if (target === state.previewPage) { setFlipped(flipped); render(); return; }
    try {
      await turn(Math.sign(target - state.previewPage), async () => {
        setFlipped(flipped, false);
        await pdfPreview.setPage(target);
      });
    } catch (_) { previewFailure(); }
    render();
  }

  function zoomTo(value) {
    if (!state.drawn || !ZOOMS.includes(value)) return;
    state.previewZoom = value;
    pdfPreview.setZoom(value).catch(previewFailure);
    render();
  }

  function clearPassword() {
    ui['school-password'].value = '';
    ui['school-password'].type = 'password';
    ui['password-toggle'].setAttribute('aria-pressed', 'false');
    ui['password-toggle'].setAttribute('aria-label', '显示密码');
  }

  function drawThumb() {
    const source = ui['preview-canvas'];
    const thumb = ui['confirm-thumb'];
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    thumb.width = Math.round(54 * ratio);
    thumb.height = Math.round(76 * ratio);
    if (source.width) thumb.getContext('2d').drawImage(source, 0, 0, thumb.width, thumb.height);
    ui['thumb-frame'].classList.toggle('is-gray', activeOptions().color !== 'color');
  }

  function openAccount() {
    if (!state.privacyAccepted || state.busy || state.reading || state.receipt || !previewed()) return;
    clearPassword();
    ui['bulk-check'].checked = false;
    showError('form-error');
    drawThumb();
    render();
    if (!ui['account-dialog'].open) ui['account-dialog'].showModal();
    render();
    (username() ? ui['school-password'] : ui['school-username']).focus();
  }

  function closeAccount(force = false) {
    if (state.busy && !force) return;
    clearPassword();
    if (ui['account-dialog'].open) ui['account-dialog'].close();
  }

  function clearFile() {
    state.fileGeneration++;
    if (state.fileURL) URL.revokeObjectURL(state.fileURL);
    state.file = state.fileURL = state.pdf = state.pages = null;
    state.reading = state.drawn = state.flipped = state.converting = state.converted = false;
    state.faceKey = '';
    state.facePromise = null;
    clearTimeout(state.demoTimer);
    pdfPreview.clear();
    ui['file-input'].value = '';
    ui['doc-open'].removeAttribute('href');
    ui['preview-loading'].hidden = false;
    ui['preview-loading'].textContent = '正在生成预览…';
    showError('file-error');
  }

  async function selectFiles(files) {
    if (!state.privacyAccepted || state.busy || state.receipt || state.intent || !files?.length) return;
    files = Array.from(files); // FileList is live and clearing the input empties it.
    const fromUpload = !state.file;
    clearFile();
    showError('form-error');
    if (files.length !== 1) { showError('file-error', '每次选择一个文件。'); render(); return; }
    const file = files[0];
    const maxBytes = state.session?.limits?.max_bytes || 52428800;
    const convert = CONVERTIBLE.test(file.name);
    if ((!convert && !/\.pdf$/i.test(file.name)) || !file.size) {
      showError('file-error', '暂不支持这种文件，请选择 PDF、Word、PPT、Excel 或图片。'); render(); return;
    }
    if (file.size > maxBytes) {
      showError('file-error', `文件超过 ${Math.round(maxBytes / 1048576)} MB，请压缩后再试。`); render(); return;
    }
    if (convert && !canConvert()) {
      showError('file-error', ready() ? '当前设备暂不支持转换，请先导出为 PDF。' : '打印服务未连接，暂时无法转换，请先导出为 PDF。');
      render(); return;
    }
    state.file = file;
    state.reading = true;
    state.converting = convert;
    const generation = state.fileGeneration;
    // The empty upload sheet grows into the preview page while the PDF parses.
    const reveal = () => { render(); pdfPreview.reserve(); };
    if (fromUpload) await morph(reveal); else reveal();
    if (generation !== state.fileGeneration) return;
    try {
      let data = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).split(',')[1]);
        reader.onerror = () => reject(new Error('read'));
        reader.readAsDataURL(file);
      });
      if (generation !== state.fileGeneration) return;
      let pdfFile = file;
      if (convert) {
        ui['preview-loading'].textContent = '正在转换为 PDF…';
        announce('正在转换为 PDF…');
        const result = await api('/api/print/convert', { document: data, name: file.name }, 630000);
        if (generation !== state.fileGeneration) return;
        if (!result.ok || typeof result.data?.pdf !== 'string') {
          throw Object.assign(new Error(errorText(result, '这份文件无法转换，请导出为 PDF 后再试。')), { shown: true });
        }
        data = result.data.pdf;
        pdfFile = new File([Uint8Array.from(atob(data), c => c.charCodeAt(0))], file.name.replace(/\.[^.]+$/, '') + '.pdf', { type: 'application/pdf' });
        state.converting = false;
        state.converted = true;
        render();
      }
      if (atob(data.slice(0, 8)).slice(0, 5) !== '%PDF-') throw new Error('signature');
      const inspected = await pdfPreview.load(pdfFile, state.session?.limits?.max_pages || 300);
      if (generation !== state.fileGeneration) return;
      state.pages = inspected.pages;
      state.pdf = data;
      applyOptions({ ...state.options }); // Re-check the copy cap for this page count.
      // The browser opens the local blob, never an uploaded public URL.
      state.fileURL = URL.createObjectURL(new Blob([pdfFile], { type: 'application/pdf' }));
      ui['doc-open'].href = state.fileURL;
      announce(`已生成预览，共 ${state.pages} 页。`);
      window.scrollTo({ top: 0, behavior: 'instant' });
    } catch (error) {
      if (generation !== state.fileGeneration) return;
      clearFile();
      showError('file-error', error.shown || /页|加密|无法读取|预览组件/.test(error.message || '') ? error.message :
        error instanceof TypeError || error.name === 'AbortError' ? '连接中断，请重新选择文件。' : '无法生成这份文件的预览，请重新选择或导出为 PDF。');
    } finally {
      if (generation === state.fileGeneration) state.reading = state.converting = false;
      render();
    }
  }

  async function refreshSession({ flow = false } = {}) {
    if (!state.privacyAccepted) return false;
    if ((state.busy || state.balanceQuerying) && !flow) return false;
    const generation = ++state.sessionGeneration;
    state.checking = true;
    render();
    try {
      const result = await api('/api/print/session', undefined, 12000);
      if (generation !== state.sessionGeneration) return false;
      if (!result.ok || !result.data?.service) throw new Error('session');
      const previous = state.session?.user?.id ?? null;
      const next = result.data.user?.id ?? null;
      if (previous !== next) {
        state.balance = null;
        state.balanceMessage = '';
        state.identityGeneration++;
        state.jobs = [];
        renderJobs();
        if (!flow && previous !== null) {
          closeAccount(true); clearPassword(); clearFile(); stopPolling();
          state.intent = state.receipt = null;
          showError('file-error', '登录账号已变更，请重新选择文件并确认学号。');
        }
      }
      state.session = result.data;
      if (!username() && schoolUser()) ui['school-username'].value = schoolUser();
      reconcileOptions();
      return true;
    } catch (_) {
      if (generation === state.sessionGeneration) state.session = null;
      return false;
    } finally {
      if (generation === state.sessionGeneration) { state.checking = false; render(); renderJobs(); }
    }
  }

  function renderJobs() {
    ui['jobs-list'].replaceChildren();
    // Never show the previous account's history alongside a newly typed ID.
    const jobs = schoolUser() && username() === schoolUser() ? state.jobs : [];
    ui.history.hidden = jobs.length === 0;
    ui['jobs-count'].textContent = jobs.length ? String(jobs.length) : '';
    ui['jobs-empty'].hidden = jobs.length > 0;
    for (const job of jobs.slice(0, 10)) {
      const li = document.createElement('li');
      li.className = 'job-row';
      li.dataset.state = job.state;
      const title = document.createElement('span');
      title.className = 'job-status';
      title.textContent = labels[job.state] || '结果待确认';
      const spec = document.createElement('span');
      spec.className = 'job-spec';
      spec.textContent = describe(job.options || DEFAULT_OPTIONS).join(' · ');
      const meta = document.createElement('span');
      meta.className = 'job-meta';
      const date = new Date(job.created_at * 1000);
      meta.textContent = (Number.isInteger(job.pages) ? job.pages + ' 页 · ' : '') +
        (Number.isFinite(date.valueOf()) ? date.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '');
      li.append(title, spec, meta);
      if (['unknown', 'processing'].includes(job.state)) {
        const button = document.createElement('button');
        button.type = 'button'; button.className = 'text-btn'; button.textContent = '查询';
        button.addEventListener('click', () => queryHistoryJob(job.id));
        li.append(button);
      }
      ui['jobs-list'].append(li);
    }
  }

  async function loadJobs() {
    if (!state.privacyAccepted || !state.session?.user) return [];
    const generation = state.identityGeneration;
    const result = await api('/api/print/jobs', undefined, 15000);
    if (generation !== state.identityGeneration) return [];
    if (!result.ok || !Array.isArray(result.data?.jobs)) throw new Error('history');
    state.jobs = result.data.jobs;
    renderJobs();
    return state.jobs;
  }

  function stopPolling() { clearTimeout(state.pollTimer); state.pollTimer = null; }
  function schedulePoll() {
    stopPolling();
    if (!state.intent || !['processing', 'unknown'].includes(state.receipt?.state) || state.polls >= 12) return;
    state.pollTimer = setTimeout(async () => {
      state.polls++;
      if (!document.hidden) await queryReceipt(false);
      schedulePoll();
    }, 5000);
  }

  function showReceipt(job, focus = true) {
    closeAccount(true);
    const previousTone = state.receipt ? state.receiptTone : '';
    state.receipt = job;
    if (state.intent && job.id) state.intent.id = job.id;
    if (state.intent && ['unknown', 'processing'].includes(job.state)) state.intent.uncertain = true;
    const success = job.state === 'submitted';
    const failed = ['failed', 'rejected'].includes(job.state);
    const tone = success ? 'success' : failed ? 'error' : 'pending';
    state.receiptTone = tone;
    if (tone !== previousTone) {
      // A new outcome prints a fresh ticket.
      ui.receipt.className = 'receipt is-' + tone;
      ui['receipt-icon'].innerHTML = ICONS[tone];
      ui.receipt.hidden = false;
      void ui.receipt.offsetWidth;
      ui.receipt.classList.add('is-printing');
    }
    ui['receipt-title'].textContent = success ? '文件已提交，去刷卡取件吧' :
      job.state === 'processing' ? '正在送往学校队列' : failed ? '这次没有提交成功' : '提交结果待确认';
    ui['receipt-message'].textContent = success ? '到学校打印点刷卡，在待打印列表中选择这份文件。' :
      failed ? (job.message || '请检查账号或文件后重试。') :
      '请先查询任务状态，或在打印机上查看待取任务，避免重复提交。';
    const doc = state.receiptDoc;
    const options = job.options || doc?.options || DEFAULT_OPTIONS;
    const pages = Number.isInteger(job.pages) ? job.pages : doc?.pages;
    const rows = [
      doc?.name && ['文档', doc.name],
      pages && ['纸张', `${pages} 页 · ${sheetsFor(pages, options)} 张 A4`],
      ['设置', describe(options).join(' · ')],
      job.id && ['任务', job.id.slice(0, 8)],
    ].filter(Boolean);
    ui['receipt-details'].replaceChildren(...rows.map(([term, value], index) => {
      const row = document.createElement('div');
      row.style.setProperty('--i', String(index));
      const dt = document.createElement('dt');
      dt.textContent = term;
      const dd = document.createElement('dd');
      dd.textContent = value;
      row.append(dt, dd);
      return row;
    }));
    ui['receipt-meta'].textContent = [state.intent?.username,
      new Date((job.created_at || Date.now() / 1000) * 1000).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })].filter(Boolean).join(' · ');
    ui['receipt-query'].hidden = success || failed;
    ui['receipt-retry'].hidden = true;
    ui['new-print'].hidden = !success && !failed;
    ui['new-print'].textContent = success ? '打印另一份' : '返回修改';
    render();
    if (focus) {
      window.scrollTo({ top: 0, behavior: calm() ? 'instant' : 'smooth' });
      ui.receipt.focus({ preventScroll: true });
      announce(ui['receipt-title'].textContent);
    }
    if (success || failed) stopPolling();
  }

  async function queryReceipt(manual = true) {
    if (!state.intent || state.jobQuery) return;
    const intent = state.intent;
    const generation = state.identityGeneration;
    state.jobQuery = true;
    ui['receipt-query'].disabled = true;
    try {
      const jobs = await loadJobs();
      if (generation !== state.identityGeneration || intent !== state.intent) return;
      const job = jobs.find(item => item.idempotency_key === intent.key);
      if (job) showReceipt(job, false);
      else if (manual) {
        ui['receipt-message'].textContent = '暂未查到任务回执。请先在打印机确认；继续同一任务会沿用原编号。';
        ui['receipt-retry'].hidden = !state.pdf;
      }
    } catch (_) {
      if (manual) ui['receipt-message'].textContent = '暂时无法查询，请稍后重试。不要重复新建任务。';
    } finally {
      state.jobQuery = false;
      ui['receipt-query'].disabled = false;
    }
  }

  async function queryHistoryJob(id) {
    if (state.busy || state.receipt) return;
    const generation = state.identityGeneration;
    const user = schoolUser();
    try {
      const result = await api('/api/print/jobs/' + encodeURIComponent(id), undefined, 15000);
      if (generation !== state.identityGeneration || user !== username()) return;
      if (!result.ok || !result.data?.job) throw new Error('query');
      const job = result.data.job;
      // History inspection cannot reuse a different file for an old intent.
      state.intent = { key: job.idempotency_key, username: user, owner: state.session.user.id, options: job.options || { ...DEFAULT_OPTIONS } };
      state.receiptDoc = { name: '', pages: job.pages, options: state.intent.options };
      clearFile();
      showReceipt(job);
      state.polls = 0; schedulePoll();
    } catch (_) { showError('file-error', '暂时无法查询任务，请稍后重试。'); }
  }

  async function submit(event) {
    event.preventDefault();
    if (!state.privacyAccepted || state.busy || state.balanceQuerying || state.reading || state.receipt || !previewed()) return;
    showError('file-error'); showError('form-error');
    ui['school-username'].removeAttribute('aria-invalid');
    ui['school-password'].removeAttribute('aria-invalid');
    if (!state.pdf) { showError('file-error', '先选择一份需要打印的 PDF。'); ui['pick-btn'].focus(); return; }
    if (!ui['account-dialog'].open) { openAccount(); return; }
    const confirmedPages = state.pages;
    const confirmedOptions = JSON.stringify(activeOptions());
    const bulkApproved = ui['bulk-check'].checked;
    if (needsBulk(state.pages, activeOptions()) && !bulkApproved) {
      showError('form-error', '请先确认本次打印总量。'); ui['bulk-check'].focus(); return;
    }
    const account = username();
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(account)) {
      showError('form-error', '请输入正确的学校账号。');
      ui['school-username'].setAttribute('aria-invalid', 'true'); ui['school-username'].focus(); return;
    }
    let password = ui['school-password'].value;
    if (!password || password.length > 512 || /[\r\n\0]/.test(password)) {
      showError('form-error', '请输入学校密码。');
      ui['school-password'].setAttribute('aria-invalid', 'true'); ui['school-password'].focus(); return;
    }
    if (!ready()) { showError('form-error', '打印服务暂未连接，请稍后再试。'); return; }
    state.busy = true;
    state.sessionGeneration++; // Invalidate earlier background requests before authenticating.
    stopPolling();
    clearTimeout(state.demoTimer);
    let dispatchStarted = false;
    let acceptedResponse = false;
    try {
      render(); progress('正在连接打印服务…', 'auth');
      if (!await refreshSession({ flow: true }) || !ready()) throw new Error('打印服务暂未连接，请稍后再试。');
      const options = state.intent?.options || { ...state.options };
      const missing = unsupported(options);
      if (missing.length) throw new Error(`当前打印设备暂不支持${missing.join('、')}，请返回调整打印设置。`);
      progress('正在验证学校账号…', 'auth');
      await verifySchoolAccount(account, password);
      if (balanceAvailable()) {
        progress('正在读取 PaperCut 余额…', 'auth');
        try { await lookupBalance(account, password); }
        catch (error) {
          state.balance = null;
          state.balanceMessage = typeof error.message === 'string' && error.name !== 'AbortError' && !(error instanceof TypeError)
            ? error.message : '余额暂时无法查询，本次打印继续提交。';
        }
      }
      const identity = state.identityGeneration;
      const owner = state.session.user.id;
      progress('正在检查 PDF…', 'inspect');
      const inspection = await api('/api/print/inspect', { pdf: state.pdf }, 300000);
      if (!inspection.ok) throw new Error(errorText(inspection, '文件检查失败，请稍后重试。'));
      if (!Number.isInteger(inspection.data?.pages) || !inspection.data?.inspection_token) throw new Error('没有收到有效的文件检查结果，请重试。');
      if (identity !== state.identityGeneration || schoolUser() !== account) throw new Error('登录状态已变化，请重新提交。');
      state.pages = inspection.data.pages;
      if (needsBulk(state.pages, options) && (!bulkApproved || confirmedPages !== state.pages || confirmedOptions !== JSON.stringify(options))) {
        throw new Error('打印总量已变化，请重新确认后提交。');
      }
      if (!state.intent) state.intent = { key: crypto.randomUUID(), username: account, owner, options };
      state.receiptDoc = { name: state.file?.name || '', pages: state.pages, options: state.intent.options };
      progress('正在提交打印…', 'send'); render();
      dispatchStarted = true;
      const result = await api('/api/print/jobs', {
        pdf: state.pdf, password, inspection_token: inspection.data.inspection_token,
        idempotency_key: state.intent.key, options: state.intent.options,
        ...(bulkApproved ? { bulk_confirmation: { sha256: inspection.data.sha256, pages: state.pages, options: state.intent.options } } : {}),
      }, 630000);
      if (result.ok && result.data?.job && result.data.job.idempotency_key === state.intent.key &&
          ['submitted', 'processing', 'unknown', 'failed', 'rejected'].includes(result.data.job.state)) {
        acceptedResponse = true;
        showReceipt(result.data.job);
        if (result.data.job.state === 'submitted') {
          // Release the in-memory document as soon as a definite receipt exists.
          clearFile();
        }
      } else if (result.status < 500 && result.data?.code && !result.data?.job) {
        acceptedResponse = true;
        // A previous uncertain attempt must keep its identity even after rejection.
        if (!state.intent.uncertain) state.intent = null;
        throw new Error(errorText(result, '提交未被接受，请稍后重试。'));
      } else {
        showReceipt({ state: 'unknown', pages: state.pages });
      }
    } catch (error) {
      if (dispatchStarted && !acceptedResponse) showReceipt({ state: 'unknown', pages: state.pages });
      else showError('form-error', error.name === 'AbortError' || error instanceof TypeError ?
        '连接中断，本次尚未发送打印任务，请重试。' : error.message);
    } finally {
      password = null;
      clearPassword();
      state.busy = false;
      progress();
      if (!state.receipt) reconcileOptions();
      render();
      state.polls = 0; schedulePoll();
      loadJobs().catch(() => {});
      if (!state.receipt && !ui['form-error'].hidden) ui['school-password'].focus();
    }
  }

  const previewFailure = () => showError('file-error', '这一页暂时无法预览，请重新选择文件。');
  const pdfPreview = new window.MaxcoursePdfPreview(ui['preview-canvas'], ui['page-stage'], info => {
    state.previewReady = info.ready;
    state.previewRendering = info.rendering;
    state.previewPage = info.page;
    state.previewZoom = info.zoom;
    if (state.file && info.pages) state.pages = info.pages;
    if (state.file && info.ready) { state.drawn = true; syncFaces(); }
    ui['page-stage'].setAttribute('aria-busy', String(info.rendering));
    ui['preview-loading'].hidden = state.drawn && !info.error;
    ui['preview-loading'].textContent = info.error || (state.converting ? '正在转换为 PDF…' : '正在生成预览…');
    render();
  });

  // A versioned acknowledgement stores no credentials, filenames or document data.
  function privacyScreen(show, review = false, focus = true) {
    ui['privacy-notice'].hidden = !show;
    ui['print-workspace'].hidden = show;
    ui['print-workspace'].inert = show;
    ui['service-status'].hidden = show;
    ui['help-open'].hidden = show;
    ui['privacy-open'].hidden = show;
    ui['privacy-choice'].hidden = review;
    ui['privacy-review'].hidden = !review;
    if (focus) {
      window.scrollTo({ top: 0, behavior: 'instant' });
      (show ? ui['privacy-title'] : review ? ui['privacy-open'] : ui['pick-btn']).focus();
    }
  }

  function storageNote(text = '') {
    ui['privacy-storage-note'].textContent = text;
    ui['privacy-storage-note'].hidden = !text;
  }

  function enterPrinting(remember = false, focus = true) {
    if (state.privacyAccepted) return;
    state.privacyAccepted = true;
    if (remember) {
      try { localStorage.setItem(PRIVACY_KEY, PRIVACY_VERSION); storageNote(); }
      catch (_) { storageNote('浏览器无法记住确认，下次进入时会再次显示告知。本次仍可继续打印。'); }
    }
    render();
    privacyScreen(false, false, focus);
    refreshSession().then(() => loadJobs()).catch(() => {});
  }

  function reviewPrivacy() {
    if (!state.privacyAccepted || state.busy || state.reading || state.balanceQuerying || state.jobQuery) return;
    closeAccount(true);
    ui['help-dialog'].close();
    privacyScreen(true, true);
  }

  ui['privacy-check'].addEventListener('change', () => { ui['privacy-accept'].disabled = !ui['privacy-check'].checked; });
  ui['privacy-accept'].addEventListener('click', () => { if (ui['privacy-check'].checked) enterPrinting(true); });
  ui['privacy-open'].addEventListener('click', reviewPrivacy);
  ui['help-privacy'].addEventListener('click', reviewPrivacy);
  ui['privacy-return'].addEventListener('click', () => { if (state.privacyAccepted) privacyScreen(false, true); });
  ui['privacy-revoke'].addEventListener('click', () => {
    if (state.busy || state.reading || state.balanceQuerying || state.jobQuery) return;
    state.privacyAccepted = false;
    state.sessionGeneration++; state.identityGeneration++; state.balanceGeneration++;
    stopPolling(); clearPassword(); clearFile();
    state.session = state.balance = state.intent = state.receipt = state.receiptDoc = null;
    state.jobs = []; state.balanceMessage = ''; state.checking = false;
    ui['school-username'].value = '';
    for (const id of ['doc-name', 'doc-sub', 'confirm-name', 'receipt-title', 'receipt-message', 'receipt-meta', 'receipt-details']) ui[id].textContent = '';
    for (const canvas of ui['print-workspace'].querySelectorAll('canvas')) { canvas.width = 0; canvas.height = 0; }
    ui['preview-canvas'].setAttribute('aria-label', '文档预览');
    ui['privacy-check'].checked = false;
    ui['privacy-accept'].disabled = true;
    try { localStorage.removeItem(PRIVACY_KEY); storageNote('已撤回本浏览器的确认。已提交的学校任务仍需自行核对，账号解绑或注销请前往首页设置。'); }
    catch (_) { storageNote('本页面已停止打印处理。浏览器无法删除确认记录，请清除此站点的本地存储，确保下次进入也显示告知。'); }
    render(); renderJobs(); privacyScreen(true);
  });

  ui['submit-form'].addEventListener('submit', submit);
  ui['go-print'].addEventListener('click', openAccount);
  ui['balance-query'].addEventListener('click', queryBalance);
  ui['account-back'].addEventListener('click', () => closeAccount());
  ui['account-dialog'].addEventListener('cancel', event => {
    if (state.busy) event.preventDefault();
    else clearPassword();
  });
  ui['account-dialog'].addEventListener('close', () => {
    clearPassword(); state.balanceGeneration++; state.balanceQuerying = false; render();
  });
  ui['account-dialog'].addEventListener('click', event => {
    if (event.target !== ui['account-dialog']) return;
    const bounds = ui['account-dialog'].getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) closeAccount();
  });
  ui['pick-btn'].addEventListener('click', () => ui['file-input'].click());
  ui['replace-file'].addEventListener('click', () => ui['file-input'].click());
  ui.dropzone.addEventListener('click', event => {
    if (!event.target.closest('button, input') && !ui['pick-btn'].disabled) ui['file-input'].click();
  });
  ui['page-prev'].addEventListener('click', () => goToPage(state.previewPage - (duplex(activeOptions()) ? 2 : 1)));
  ui['page-next'].addEventListener('click', () => goToPage(state.previewPage + (duplex(activeOptions()) ? 2 : 1)));
  ui['page-current'].addEventListener('change', () => goToPage(ui['page-current'].value));
  ui['page-current'].addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); goToPage(ui['page-current'].value); } });
  ui['flip-sheet'].addEventListener('click', () => { clearTimeout(state.demoTimer); setFlipped(!state.flipped); render(); });
  ui['zoom-in'].addEventListener('click', () => zoomTo(ZOOMS[ZOOMS.indexOf(state.previewZoom) + 1]));
  ui['zoom-out'].addEventListener('click', () => zoomTo(ZOOMS[ZOOMS.indexOf(state.previewZoom) - 1]));
  ui['zoom-fit'].addEventListener('click', () => zoomTo('fit'));
  for (const input of radios('color')) input.addEventListener('change', () => { if (input.checked) setOptions({ color: input.value }); });
  for (const input of radios('sides-mode')) input.addEventListener('change', () => {
    if (input.checked) setOptions({ sides: input.value === 'two' ? sidesFor(state.edge) : 'one-sided' });
  });
  for (const input of radios('edge')) input.addEventListener('change', () => {
    if (!input.checked) return;
    state.edge = input.value;
    if (duplex()) setOptions({ sides: sidesFor(state.edge) }); else render();
  });
  ui['copies-dec'].addEventListener('click', () => setOptions({ copies: state.options.copies - 1 }));
  ui['copies-inc'].addEventListener('click', () => setOptions({ copies: state.options.copies + 1 }));
  ui.copies.addEventListener('input', () => { state.copiesTyping = true; });
  const commitCopies = () => { state.copiesTyping = false; setOptions({ copies: ui.copies.value }); };
  ui.copies.addEventListener('change', commitCopies);
  ui.copies.addEventListener('blur', () => { if (state.copiesTyping) commitCopies(); });
  ui.copies.addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); commitCopies(); } });
  ui['file-input'].addEventListener('change', () => selectFiles(ui['file-input'].files));
  ui['doc-remove'].addEventListener('click', () => {
    if (state.busy || state.intent) return;
    clearFile();
    morph(render).then(() => ui['pick-btn'].focus());
  });

  // Dropping anywhere on the page selects the file instead of navigating away from it.
  const carriesFiles = event => Array.from(event.dataTransfer?.types || []).includes('Files');
  window.addEventListener('dragover', event => { if (carriesFiles(event)) event.preventDefault(); });
  window.addEventListener('drop', event => {
    if (!carriesFiles(event)) return;
    event.preventDefault();
    state.dragDepth = 0;
    ui.dropzone.classList.remove('dragover');
    if (!document.querySelector('dialog[open]')) selectFiles(event.dataTransfer.files);
  });
  ui.dropzone.addEventListener('dragenter', event => {
    event.preventDefault();
    state.dragDepth++;
    if (!state.busy) ui.dropzone.classList.add('dragover');
  });
  ui.dropzone.addEventListener('dragleave', () => {
    state.dragDepth = Math.max(0, state.dragDepth - 1);
    if (!state.dragDepth) ui.dropzone.classList.remove('dragover');
  });
  if (matchMedia('(hover: hover) and (pointer: fine)').matches) {
    ui.dropzone.addEventListener('pointermove', event => {
      if (calm()) return;
      const box = ui.dropzone.getBoundingClientRect();
      const x = (event.clientX - box.left) / box.width - .5;
      const y = (event.clientY - box.top) / box.height - .5;
      ui['drop-visual'].style.setProperty('--ry', (x * 16).toFixed(2) + 'deg');
      ui['drop-visual'].style.setProperty('--rx', (-y * 12).toFixed(2) + 'deg');
    });
    ui.dropzone.addEventListener('pointerleave', () => {
      ui['drop-visual'].style.setProperty('--ry', '0deg');
      ui['drop-visual'].style.setProperty('--rx', '0deg');
    });
  }
  document.addEventListener('keydown', event => {
    if (!state.drawn || state.receipt || document.querySelector('dialog[open]') || event.altKey || event.metaKey || event.ctrlKey) return;
    if (event.target.closest('input, select, textarea, button, a, [contenteditable]')) return;
    if (event.key === 'ArrowLeft' && !ui['page-prev'].disabled) { event.preventDefault(); ui['page-prev'].click(); }
    if (event.key === 'ArrowRight' && !ui['page-next'].disabled) { event.preventDefault(); ui['page-next'].click(); }
  });

  ui['password-toggle'].addEventListener('click', () => {
    const visible = ui['school-password'].type === 'password';
    ui['school-password'].type = visible ? 'text' : 'password';
    ui['password-toggle'].setAttribute('aria-pressed', String(visible));
    ui['password-toggle'].setAttribute('aria-label', visible ? '隐藏密码' : '显示密码');
  });
  ui['school-username'].addEventListener('input', () => {
    state.balance = null; state.balanceMessage = ''; state.balanceGeneration++;
    clearPassword(); showError('form-error'); render(); renderJobs();
    ui['school-username'].removeAttribute('aria-invalid');
  });
  ui['school-password'].addEventListener('input', () => {
    showError('form-error'); ui['school-password'].removeAttribute('aria-invalid');
  });
  ui['service-refresh'].addEventListener('click', () => refreshSession());
  ui['receipt-query'].addEventListener('click', () => queryReceipt());
  ui['receipt-retry'].addEventListener('click', () => {
    if (state.busy || !state.intent || !state.pdf) return;
    stopPolling(); state.receipt = null; state.receiptTone = ''; render();
    openAccount();
    showError('form-error', '将沿用原任务编号。请输入密码后继续。');
    ui['school-password'].focus();
  });
  ui['new-print'].addEventListener('click', () => {
    const failed = ['failed', 'rejected'].includes(state.receipt?.state);
    stopPolling(); state.intent = state.receipt = state.receiptDoc = null; state.receiptTone = '';
    if (!failed) clearFile();
    clearPassword(); showError('form-error'); reconcileOptions();
    morph(render).then(() => (state.file ? ui['go-print'] : ui['pick-btn']).focus());
  });
  ui['jobs-refresh'].addEventListener('click', async () => {
    ui['jobs-refresh'].disabled = true;
    try { await loadJobs(); } catch (_) { announce('暂时无法刷新任务记录。'); }
    finally { ui['jobs-refresh'].disabled = false; }
  });
  ui['help-open'].addEventListener('click', () => ui['help-dialog'].showModal());
  ui['help-close'].addEventListener('click', () => ui['help-dialog'].close());
  ui['help-dialog'].addEventListener('click', event => { if (event.target === ui['help-dialog']) ui['help-dialog'].close(); });
  window.addEventListener('scroll', () => ui.topbar.classList.toggle('is-scrolled', window.scrollY > 4), { passive: true });
  window.addEventListener('offline', render);
  window.addEventListener('online', () => refreshSession());
  window.addEventListener('beforeunload', event => {
    if (state.busy || ['unknown', 'processing'].includes(state.receipt?.state)) { event.preventDefault(); event.returnValue = ''; }
  });
  window.addEventListener('pagehide', clearPassword);
  document.addEventListener('visibilitychange', () => { if (!document.hidden && !state.busy) refreshSession(); });
  render();
  try {
    if (localStorage.getItem(PRIVACY_KEY) === PRIVACY_VERSION) enterPrinting(false, false);
  } catch (_) { storageNote('浏览器无法记住确认，下次进入时会再次显示告知。本次仍可继续打印。'); }
  setInterval(() => { if (!document.hidden && !state.busy) refreshSession(); }, 60000);
})();
