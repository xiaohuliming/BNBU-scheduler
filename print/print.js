/* The form uses the real MAXCOURSE session and print APIs. Credentials and
   documents stay in page memory; an uncertain dispatch keeps its intent ID. */
'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const ui = Object.fromEntries([
    'submit-form', 'workspace-fields', 'service-status', 'service-dot', 'service-text',
    'service-refresh', 'dropzone', 'pick-btn', 'file-input', 'doc-panel', 'doc-name',
    'doc-sub', 'doc-remove', 'doc-open', 'doc-progress', 'limits-hint', 'file-error',
    'cap-pages', 'school-username', 'school-password', 'password-toggle', 'form-error',
    'progress-text', 'submit-btn', 'submit-label', 'receipt', 'receipt-icon',
    'receipt-title', 'receipt-message', 'receipt-meta', 'receipt-query', 'receipt-retry',
    'new-print', 'history', 'jobs-count', 'jobs-list', 'jobs-empty', 'jobs-refresh',
    'help-open', 'help-close', 'help-dialog', 'announce',
    'go-print', 'go-print-note', 'account-dialog', 'account-back', 'confirm-name', 'account-service',
    'upload-stage', 'preview-canvas', 'page-stage', 'preview-loading', 'page-prev', 'page-next',
    'page-current', 'page-total', 'preview-zoom', 'replace-file', 'range-summary', 'print-total',
    'page-title', 'page-description', 'step-upload', 'step-preview', 'step-submit', 'confirm-specs',
  ].map(id => [id, $(id)]));
  const state = {
    session: null, sessionGeneration: 0, identityGeneration: 0, fileGeneration: 0,
    file: null, fileURL: null, pdf: null, pages: null, busy: false, reading: false,
    intent: null, receipt: null, jobs: [], pollTimer: null, polls: 0,
    checking: false, retry: false, jobQuery: false,
    previewReady: false, previewRendering: false, previewPage: 1,
  };
  const labels = { submitted: '待刷卡取件', processing: '正在提交', unknown: '结果待确认',
    failed: '未提交', rejected: '账号验证失败' };
  const username = () => ui['school-username'].value.trim();
  const schoolUser = () => state.session?.user?.school_username || '';
  const announce = text => { ui.announce.textContent = text; };
  const showError = (target, text = '') => {
    ui[target].textContent = text;
    ui[target].hidden = !text;
  };
  const ready = () => navigator.onLine && state.session?.service?.ready === true &&
    state.session.service.enabled === true && !state.session.service.busy && !state.session.service.demo;

  async function api(path, body, timeout = 60000) {
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

  function progress(message = '') {
    ui['progress-text'].hidden = !message;
    ui['progress-text'].textContent = message;
    ui['submit-label'].textContent = message || '提交打印';
    if (message) announce(message);
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
    const locked = state.busy || !!state.receipt;
    ui['submit-form'].setAttribute('aria-busy', String(state.busy));
    ui['pick-btn'].disabled = locked || !!state.intent;
    ui['file-input'].disabled = locked || !!state.intent;
    ui['doc-remove'].disabled = locked || !!state.intent;
    ui['replace-file'].disabled = locked || !!state.intent;
    ui['school-username'].disabled = locked || !!state.intent;
    ui['school-password'].disabled = locked;
    ui['password-toggle'].disabled = locked;
    ui['go-print'].disabled = locked || state.reading || !state.pdf || !state.previewReady;
    ui['go-print-note'].textContent = state.reading || state.previewRendering ? '正在生成预览…' : state.previewReady ? '下一步填写学校账号。' : '预览完成后继续。';
    ui['account-back'].disabled = state.busy;
    ui['account-service'].hidden = ready();
    ui['account-service'].textContent = service?.busy ? '设备正在处理其他任务，请稍后提交。' : '打印服务未连接，暂时无法提交。';
    ui['confirm-name'].textContent = state.file?.name || '';
    ui['submit-btn'].disabled = state.busy || state.reading || !state.pdf || !state.previewReady || !ready();
    ui['submit-btn'].setAttribute('aria-describedby', 'service-text form-error');
    ui['dropzone'].hidden = !!state.file;
    ui['upload-stage'].hidden = !!state.file;
    ui['page-title'].textContent = state.receipt ? '提交结果' : state.file ? '打印预览' : '校园打印';
    ui['page-description'].textContent = state.receipt ? '查看任务状态，提交成功后到打印点刷卡取件。' : state.file ? '核对文档内容与打印规格，再确认提交。' : '先上传文件，预览确认后再提交。';
    const step = state.receipt || ui['account-dialog'].open ? 'step-submit' : state.file ? 'step-preview' : 'step-upload';
    for (const id of ['step-upload', 'step-preview', 'step-submit']) {
      if (id === step) ui[id].setAttribute('aria-current', 'step');
      else ui[id].removeAttribute('aria-current');
    }
    ui['range-summary'].textContent = state.pages ? `全部 ${state.pages} 页` : '全部页面';
    ui['print-total'].textContent = state.pages ? `${state.pages} 页 / ${state.pages} 张` : '正在检查页数';
    ui['confirm-specs'].textContent = [state.pages ? `${state.pages} 页` : '', 'A4', '黑白', '单面', '1 份'].filter(Boolean).join(' · ');
    ui['page-prev'].disabled = locked || !state.previewReady || state.previewPage <= 1;
    ui['page-next'].disabled = locked || !state.previewReady || state.previewPage >= state.pages;
    ui['page-current'].disabled = locked || !state.previewReady;
    ui['preview-zoom'].disabled = locked || !state.previewReady;
    ui['doc-panel'].hidden = !state.file;
    ui['doc-progress'].hidden = !state.reading && !state.busy;
    ui['cap-pages'].hidden = !state.pages;
    ui['cap-pages'].textContent = state.pages ? state.pages + ' 页' : '';
    if (state.file) {
      ui['doc-name'].textContent = state.file.name;
      ui['doc-sub'].textContent = state.reading ? '正在读取文件…' :
        ((state.file.size / 1048576).toFixed(2) + ' MB' + (state.pages ? ' · ' + state.pages + ' 页' : ' · PDF'));
    }
    ui['workspace-fields'].hidden = !!state.receipt;
    ui.receipt.hidden = !state.receipt;
  }

  function clearPassword() {
    ui['school-password'].value = '';
    ui['school-password'].type = 'password';
    ui['password-toggle'].setAttribute('aria-pressed', 'false');
    ui['password-toggle'].setAttribute('aria-label', '显示密码');
  }

  function openAccount() {
    if (state.busy || state.reading || state.receipt || !state.pdf || !state.previewReady) return;
    clearPassword();
    showError('form-error');
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
    state.reading = false;
    pdfPreview.clear();
    ui['file-input'].value = '';
    ui['doc-open'].removeAttribute('href');
    showError('file-error');
  }

  async function selectFiles(files) {
    if (state.busy || state.receipt || state.intent || !files?.length) return;
    files = Array.from(files); // FileList is live and clearing the input empties it.
    clearFile();
    showError('form-error');
    if (files.length !== 1) { showError('file-error', '每次选择一份 PDF。'); render(); return; }
    const file = files[0];
    const maxBytes = state.session?.limits?.max_bytes || 10485760;
    if (!/\.pdf$/i.test(file.name) || !file.size) {
      showError('file-error', '请选择有效的 PDF 文件。'); render(); return;
    }
    if (file.size > maxBytes) {
      showError('file-error', '文件超过 10 MB，请压缩 PDF 后再试。'); render(); return;
    }
    state.file = file;
    state.reading = true;
    const generation = state.fileGeneration;
    render();
    try {
      const data = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).split(',')[1]);
        reader.onerror = () => reject(new Error('read'));
        reader.readAsDataURL(file);
      });
      if (generation !== state.fileGeneration) return;
      if (atob(data.slice(0, 8)).slice(0, 5) !== '%PDF-') throw new Error('signature');
      const inspected = await pdfPreview.load(file, state.session?.limits?.max_pages || 50);
      if (generation !== state.fileGeneration) return;
      state.pages = inspected.pages;
      state.pdf = data;
      // The browser opens the local blob, never an uploaded public URL.
      state.fileURL = URL.createObjectURL(new Blob([file], { type: 'application/pdf' }));
      ui['doc-open'].href = state.fileURL;
      announce(`已生成预览，共 ${state.pages} 页。`);
      window.scrollTo({ top: 0, behavior: 'instant' });
    } catch (error) {
      if (generation !== state.fileGeneration) return;
      clearFile();
      showError('file-error', error.message && /页|加密|无法读取|预览组件/.test(error.message) ? error.message : '无法生成这份 PDF 的预览，请重新选择或导出文件。');
    } finally {
      if (generation === state.fileGeneration) state.reading = false;
      render();
    }
  }

  async function refreshSession({ flow = false } = {}) {
    if (state.busy && !flow) return false;
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
      const title = document.createElement('span');
      title.className = 'job-status';
      title.textContent = labels[job.state] || '结果待确认';
      const meta = document.createElement('span');
      meta.className = 'job-meta';
      const date = new Date(job.created_at * 1000);
      meta.textContent = (Number.isInteger(job.pages) ? job.pages + ' 页 · ' : '') +
        (Number.isFinite(date.valueOf()) ? date.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '');
      li.append(title, meta);
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
    if (!state.session?.user) return [];
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
    state.receipt = job;
    if (state.intent && job.id) state.intent.id = job.id;
    if (state.intent && ['unknown', 'processing'].includes(job.state)) state.intent.uncertain = true;
    const success = job.state === 'submitted';
    const failed = ['failed', 'rejected'].includes(job.state);
    ui.receipt.className = 'receipt ' + (success ? 'is-success' : failed ? 'is-error' : 'is-pending');
    ui['receipt-icon'].textContent = success ? '✓' : failed ? '!' : '…';
    ui['receipt-title'].textContent = success ? '文件已提交，去刷卡取件吧' :
      job.state === 'processing' ? '正在送往学校队列' : failed ? '这次没有提交成功' : '提交结果待确认';
    ui['receipt-message'].textContent = success ? '到学校打印点刷卡，在待打印列表中选择这份文件。' :
      failed ? (job.message || '请检查账号或文件后重试。') :
      '请先查询任务状态，或在打印机上查看待取任务，避免重复提交。';
    ui['receipt-meta'].textContent = [state.intent?.username, Number.isInteger(job.pages) ? job.pages + ' 页' : '', job.id ? '任务 ' + job.id.slice(0, 8) : ''].filter(Boolean).join(' · ');
    ui['receipt-query'].hidden = success || failed;
    ui['receipt-retry'].hidden = true;
    ui['new-print'].hidden = !success && !failed;
    ui['new-print'].textContent = success ? '打印另一份' : '返回修改';
    render();
    if (focus) { ui.receipt.focus({ preventScroll: true }); announce(ui['receipt-title'].textContent); }
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
      // History inspection cannot reuse a different file for an old intent.
      state.intent = { key: result.data.job.idempotency_key, username: user, owner: state.session.user.id };
      clearFile();
      showReceipt(result.data.job);
      state.polls = 0; schedulePoll();
    } catch (_) { showError('file-error', '暂时无法查询任务，请稍后重试。'); }
  }

  async function submit(event) {
    event.preventDefault();
    if (state.busy || state.reading || state.receipt || !state.previewReady) return;
    showError('file-error'); showError('form-error');
    ui['school-username'].removeAttribute('aria-invalid');
    ui['school-password'].removeAttribute('aria-invalid');
    if (!state.pdf) { showError('file-error', '先选择一份需要打印的 PDF。'); ui['pick-btn'].focus(); return; }
    if (!ui['account-dialog'].open) { openAccount(); return; }
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
    let dispatchStarted = false;
    let acceptedResponse = false;
    try {
      render(); progress('正在连接打印服务…');
      if (!await refreshSession({ flow: true }) || !ready()) throw new Error('打印服务暂未连接，请稍后再试。');
      if (state.intent && state.intent.username !== account) {
        throw new Error('账号已变更，不能重试原任务。请先核对学校队列。');
      }
      if (schoolUser() !== account) {
        progress('正在验证学校账号…');
        const login = await api('/api/login/ispace', { username: account, password });
        if (!login.ok) throw new Error(errorText(login, '账号验证暂时不可用，请稍后重试。'));
        if (!await refreshSession({ flow: true }) || schoolUser() !== account) throw new Error('账号验证状态未确认，请重试。');
      }
      if (state.intent && state.intent.owner !== state.session?.user?.id) {
        throw new Error('登录账号与原任务不同，请先核对学校队列。');
      }
      const identity = state.identityGeneration;
      const owner = state.session.user.id;
      progress('正在检查 PDF…');
      const inspection = await api('/api/print/inspect', { pdf: state.pdf });
      if (!inspection.ok) throw new Error(errorText(inspection, '文件检查失败，请稍后重试。'));
      if (!Number.isInteger(inspection.data?.pages) || !inspection.data?.inspection_token) throw new Error('没有收到有效的文件检查结果，请重试。');
      if (identity !== state.identityGeneration || schoolUser() !== account) throw new Error('登录状态已变化，请重新提交。');
      state.pages = inspection.data.pages;
      if (!state.intent) state.intent = { key: crypto.randomUUID(), username: account, owner };
      progress('正在提交打印…'); render();
      dispatchStarted = true;
      const result = await api('/api/print/jobs', {
        pdf: state.pdf, password, inspection_token: inspection.data.inspection_token,
        idempotency_key: state.intent.key,
      }, 165000);
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
      progress(); render();
      state.polls = 0; schedulePoll();
      loadJobs().catch(() => {});
      if (!state.receipt && !ui['form-error'].hidden) ui['school-password'].focus();
    }
  }

  ui['submit-form'].addEventListener('submit', submit);
  ui['go-print'].addEventListener('click', openAccount);
  ui['account-back'].addEventListener('click', () => closeAccount());
  ui['account-dialog'].addEventListener('cancel', event => {
    if (state.busy) event.preventDefault();
    else clearPassword();
  });
  ui['account-dialog'].addEventListener('close', () => { clearPassword(); render(); });
  ui['account-dialog'].addEventListener('click', event => {
    if (event.target !== ui['account-dialog']) return;
    const bounds = ui['account-dialog'].getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) closeAccount();
  });
  ui['pick-btn'].addEventListener('click', () => ui['file-input'].click());
  ui['replace-file'].addEventListener('click', () => ui['file-input'].click());
  const previewFailure = () => showError('file-error', '这一页暂时无法预览，请重新选择文件。');
  ui['page-prev'].addEventListener('click', () => pdfPreview.setPage(state.previewPage - 1).catch(previewFailure));
  ui['page-next'].addEventListener('click', () => pdfPreview.setPage(state.previewPage + 1).catch(previewFailure));
  ui['page-current'].addEventListener('change', () => pdfPreview.setPage(ui['page-current'].value).catch(previewFailure));
  ui['page-current'].addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); pdfPreview.setPage(ui['page-current'].value).catch(previewFailure); } });
  ui['preview-zoom'].addEventListener('change', () => pdfPreview.setZoom(ui['preview-zoom'].value).catch(previewFailure));
  ui['file-input'].addEventListener('change', () => selectFiles(ui['file-input'].files));
  ui['doc-remove'].addEventListener('click', () => { if (!state.busy && !state.intent) { clearFile(); render(); ui['pick-btn'].focus(); } });
  for (const event of ['dragenter', 'dragover']) ui.dropzone.addEventListener(event, e => {
    e.preventDefault(); if (!state.busy) ui.dropzone.classList.add('dragover');
  });
  for (const event of ['dragleave', 'drop']) ui.dropzone.addEventListener(event, e => {
    e.preventDefault(); ui.dropzone.classList.remove('dragover');
    if (event === 'drop') selectFiles(e.dataTransfer?.files);
  });
  ui['password-toggle'].addEventListener('click', () => {
    const visible = ui['school-password'].type === 'password';
    ui['school-password'].type = visible ? 'text' : 'password';
    ui['password-toggle'].setAttribute('aria-pressed', String(visible));
    ui['password-toggle'].setAttribute('aria-label', visible ? '隐藏密码' : '显示密码');
  });
  ui['school-username'].addEventListener('input', () => {
    clearPassword(); showError('form-error'); renderJobs();
    ui['school-username'].removeAttribute('aria-invalid');
  });
  ui['school-password'].addEventListener('input', () => {
    showError('form-error'); ui['school-password'].removeAttribute('aria-invalid');
  });
  ui['service-refresh'].addEventListener('click', () => refreshSession());
  ui['receipt-query'].addEventListener('click', () => queryReceipt());
  ui['receipt-retry'].addEventListener('click', () => {
    if (state.busy || !state.intent || !state.pdf) return;
    stopPolling(); state.receipt = null; render();
    openAccount();
    showError('form-error', '将沿用原任务编号。请输入密码后继续。');
    ui['school-password'].focus();
  });
  ui['new-print'].addEventListener('click', () => {
    const failed = ['failed', 'rejected'].includes(state.receipt?.state);
    stopPolling(); state.intent = state.receipt = null;
    if (!failed) clearFile();
    clearPassword(); showError('form-error'); render();
    (state.file ? ui['go-print'] : ui['pick-btn']).focus();
  });
  ui['jobs-refresh'].addEventListener('click', async () => {
    ui['jobs-refresh'].disabled = true;
    try { await loadJobs(); } catch (_) { announce('暂时无法刷新任务记录。'); }
    finally { ui['jobs-refresh'].disabled = false; }
  });
  ui['help-open'].addEventListener('click', () => ui['help-dialog'].showModal());
  ui['help-close'].addEventListener('click', () => ui['help-dialog'].close());
  ui['help-dialog'].addEventListener('click', event => { if (event.target === ui['help-dialog']) ui['help-dialog'].close(); });
  window.addEventListener('offline', render);
  window.addEventListener('online', () => refreshSession());
  window.addEventListener('beforeunload', event => {
    if (state.busy || ['unknown', 'processing'].includes(state.receipt?.state)) { event.preventDefault(); event.returnValue = ''; }
  });
  window.addEventListener('pagehide', clearPassword);
  document.addEventListener('visibilitychange', () => { if (!document.hidden && !state.busy) refreshSession(); });
  const pdfPreview = new window.MaxcoursePdfPreview(ui['preview-canvas'], ui['page-stage'], info => {
    state.previewReady = info.ready;
    state.previewRendering = info.rendering;
    state.previewPage = info.page;
    if (state.file && info.pages) state.pages = info.pages;
    ui['page-current'].value = info.page;
    ui['page-current'].max = info.pages || 1;
    ui['page-total'].textContent = '/ ' + info.pages;
    ui['preview-zoom'].value = info.zoom;
    ui['page-stage'].setAttribute('aria-busy', String(info.rendering));
    ui['preview-canvas'].hidden = !info.ready;
    ui['preview-loading'].hidden = info.ready;
    ui['preview-loading'].textContent = info.error || '正在生成预览…';
    render();
  });
  render();
  refreshSession().then(() => loadJobs()).catch(() => {});
  setInterval(() => { if (!document.hidden && !state.busy) refreshSession(); }, 60000);
})();
