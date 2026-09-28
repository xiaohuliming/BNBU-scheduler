/* 校园打印 · MAXCOURSE
   Vanilla JS frontend for the print portal.
   Constraints honored here:
   - all requests same-origin with credentials: 'same-origin'
   - no tokens, passwords or PDF bodies in storage, URLs, console or DOM attributes
   - API text is only ever inserted via textContent
   - the chosen file lives in page memory only
*/
'use strict';

(() => {
  const $ = (id) => document.getElementById(id);

  const els = {
    identity: $('identity'),
    serviceDot: $('service-dot'),
    serviceText: $('service-text'),
    demoBadge: $('demo-badge'),
    limitsNote: $('limits-note'),
    limitsHint: $('limits-hint'),
    dropzone: $('dropzone'),
    pickBtn: $('pick-btn'),
    fileInput: $('file-input'),
    docPanel: $('doc-panel'),
    docName: $('doc-name'),
    docSub: $('doc-sub'),
    docRemove: $('doc-remove'),
    docProgress: $('doc-progress'),
    docErrorRow: $('doc-error-row'),
    docError: $('doc-error'),
    docRetry: $('doc-retry'),
    capPaper: $('cap-paper'),
    capColor: $('cap-color'),
    capSides: $('cap-sides'),
    capCopies: $('cap-copies'),
    capPages: $('cap-pages'),
    gate: $('prepare-gate'),
    gateText: $('gate-text'),
    gateAction: $('gate-action'),
    submitForm: $('submit-form'),
    schoolUsername: $('school-username'),
    schoolPassword: $('school-password'),
    submitBtn: $('submit-btn'),
    attempt: $('attempt'),
    jobsList: $('jobs-list'),
    jobsEmpty: $('jobs-empty'),
    jobsRefresh: $('jobs-refresh'),
    loginDialog: $('login-dialog'),
    loginForm: $('login-form'),
    loginOpen: $('login-open'),
    loginClose: $('login-close'),
    loginUsername: $('login-username'),
    loginPassword: $('login-password'),
    loginError: $('login-error'),
    loginSubmit: $('login-submit'),
  };

  const SESSION_POLL_MS = 60000;
  const JOB_POLL_MS = 3000;
  const JOB_POLL_MAX = 15;
  const JOBS_RENDER_MAX = 10;

  const state = {
    csrf: null,
    user: null,
    limits: { max_bytes: 10485760, max_pages: 50 },
    capabilities: null,
    service: null,
    sessionLoaded: false,
    lastSessionAt: 0,
    file: null,          // { name, size, base64 }
    inspection: null,    // { pages, bytes, sha256, token }
    inspecting: false,
    inspectGen: 0,
    inspectAbort: null,
    uploadError: null,
    submitting: false,
    pendingKey: null,    // idempotency key of an ambiguous in-flight result
    retryKey: null,      // retain the same intent across an explicit transport retry
    currentJobId: null,
    attempt: null,       // { tone, title, text, actions: [{id, label}] }
    jobs: [],
    pollTimers: new Map(),
    loginBusy: false,
    userId: null,
    identityGen: 0,
    sessionFetchGen: 0,
  };

  /* ---------------- helpers ---------------- */

  function fmtBytes(n) {
    if (typeof n !== 'number' || !isFinite(n) || n < 0) return '';
    if (n >= 1048576) {
      const v = n / 1048576;
      return (v >= 10 ? Math.round(v) : v.toFixed(1)) + ' MiB';
    }
    if (n >= 1024) return Math.round(n / 1024) + ' KiB';
    return n + ' B';
  }

  function pad2(n) { return String(n).padStart(2, '0'); }

  function fmtTime(ts) {
    if (typeof ts !== 'number' || !isFinite(ts) || ts <= 0) return '';
    const d = new Date(ts * 1000);
    return (d.getMonth() + 1) + '月' + d.getDate() + '日 ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  }

  function fmtTimeFull(ts) {
    if (typeof ts !== 'number' || !isFinite(ts) || ts <= 0) return '';
    const d = new Date(ts * 1000);
    return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) +
      ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  }

  function newIdempotencyKey() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
      return window.crypto.randomUUID();
    }
    const buf = new Uint8Array(16);
    window.crypto.getRandomValues(buf);
    buf[6] = (buf[6] & 0x0f) | 0x40;
    buf[8] = (buf[8] & 0x3f) | 0x80;
    const hex = Array.from(buf, (b) => b.toString(16).padStart(2, '0')).join('');
    return hex.slice(0, 8) + '-' + hex.slice(8, 12) + '-' + hex.slice(12, 16) + '-' +
      hex.slice(16, 20) + '-' + hex.slice(20);
  }

  async function api(path, opts) {
    const options = opts || {};
    const headers = {};
    const init = {
      method: options.method || (options.body !== undefined ? 'POST' : 'GET'),
      headers,
      credentials: 'same-origin',
    };
    if (options.body !== undefined) {
      headers['Content-Type'] = 'application/json';
      if (state.csrf) headers['X-Print-CSRF'] = state.csrf;
      if (path.startsWith('/api/print/') && state.user) headers['X-Print-User'] = String(state.user.id);
      init.body = JSON.stringify(options.body);
    }
    if (options.signal) init.signal = options.signal;
    const res = await fetch(path, init);
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    return { ok: res.ok, status: res.status, data };
  }

  function serverMessage(data) {
    if (data && typeof data.error === 'string' && data.error) return data.error;
    return null;
  }

  function resolveApiError(result, fallback) {
    const msg = serverMessage(result.data);
    switch (result.status) {
      case 401: return { message: msg || '请先登录后再继续。', action: 'login' };
      case 403: return { message: msg || '登录状态已失效，请重新登录。', action: 'session' };
      case 413: return { message: msg || '文件超过大小限制。' };
      case 422: return { message: msg || 'PDF 无法识别、已加密或页数超出限制。' };
      case 429: return { message: msg || '操作太频繁，请稍后再试。' };
      case 503: return { message: msg || '打印服务暂时不可用或正忙，请稍后再试。', action: 'session' };
      default: return { message: msg || fallback };
    }
  }

  function serviceUsable() {
    const s = state.service;
    return !!(s && s.enabled && s.online && s.ready && !s.busy);
  }

  function serviceMessage() {
    const s = state.service;
    if (s && typeof s.message === 'string' && s.message) return s.message;
    return null;
  }

  function canInspect() {
    return !!(state.sessionLoaded && state.user && state.user.school_username && serviceUsable() && !state.submitting);
  }

  function canSubmit() {
    return !!(
      state.sessionLoaded &&
      state.user &&
      state.user.school_username &&
      serviceUsable() &&
      state.inspection &&
      !state.inspecting &&
      !state.submitting &&
      !state.pendingKey &&
      els.schoolPassword.value.length > 0
    );
  }

  /* ---------------- session ---------------- */

  function applySession(s) {
    if (!s || typeof s !== 'object') return;
    if (typeof s.csrf_token === 'string' && s.csrf_token) state.csrf = s.csrf_token;
    const nextUser = s.user && typeof s.user === 'object' ? s.user : null;
    const nextUserId = nextUser && typeof nextUser.id !== 'undefined' ? nextUser.id : null;
    const hadIdentity = state.userId !== null;
    if (nextUserId !== state.userId || (nextUser && nextUser.school_username) !== (state.user && state.user.school_username)) {
      state.identityGen += 1;
      // identity changed: drop everything tied to the previous session
      state.pollTimers.forEach((t) => window.clearTimeout(t));
      state.pollTimers.clear();
      state.jobs = [];
      els.jobsList.replaceChildren();
      state.currentJobId = null;
      state.pendingKey = null;
      state.attempt = null;
      state.submitting = false;
      if (hadIdentity || nextUserId === null) clearFile();
      else els.schoolPassword.value = '';
    }
    state.userId = nextUserId;
    state.user = nextUser;
    if (s.limits && typeof s.limits === 'object') {
      if (typeof s.limits.max_bytes === 'number' && s.limits.max_bytes > 0) {
        state.limits.max_bytes = s.limits.max_bytes;
      }
      if (typeof s.limits.max_pages === 'number' && s.limits.max_pages > 0) {
        state.limits.max_pages = s.limits.max_pages;
      }
    }
    if (s.capabilities && typeof s.capabilities === 'object') {
      state.capabilities = s.capabilities;
    }
    state.service = s.service && typeof s.service === 'object' ? s.service : null;
    state.sessionLoaded = true;
    state.lastSessionAt = Date.now();
  }

  async function refreshSession(options) {
    if (state.loginBusy) return;
    const generation = ++state.sessionFetchGen;
    const quiet = !!(options && options.quiet);
    try {
      const r = await api('/api/print/session');
      if (generation !== state.sessionFetchGen) return;
      if (!r.ok || !r.data) throw new Error('session');
      applySession(r.data);
    } catch (e) {
      if (generation !== state.sessionFetchGen) return;
      state.service = null;
      state.sessionLoaded = true;
      if (!quiet) state.lastSessionAt = Date.now();
    }
    renderSession();
    renderUpload();
    renderSubmitArea();
    renderAttempt();
    renderJobsEmpty();
    maybeAutoInspect();
  }

  function maybeAutoInspect() {
    if (state.file && !state.inspection && !state.inspecting && !state.uploadError && canInspect()) {
      inspectCurrentFile();
    }
  }

  /* ---------------- login ---------------- */

  function openLogin() {
    if (state.submitting) return;
    els.loginError.hidden = true;
    els.loginError.textContent = '';
    if (typeof els.loginDialog.showModal === 'function') {
      if (!els.loginDialog.open) els.loginDialog.showModal();
    } else {
      els.loginDialog.setAttribute('open', '');
    }
    window.setTimeout(() => els.loginUsername.focus(), 30);
  }

  function closeLogin() {
    if (els.loginDialog.open) els.loginDialog.close();
    els.loginForm.reset();
    els.loginError.hidden = true;
    els.loginError.textContent = '';
  }

  async function submitLogin(event) {
    event.preventDefault();
    if (state.loginBusy) return;
    const username = els.loginUsername.value.trim();
    const password = els.loginPassword.value;
    if (!username || !password) {
      els.loginError.textContent = '请输入学校账号和密码。';
      els.loginError.hidden = false;
      return;
    }
    state.loginBusy = true;
    state.sessionFetchGen += 1;
    els.loginSubmit.disabled = true;
    els.loginError.hidden = true;
    let result = null;
    let networkFailed = false;
    try {
      result = await api('/api/login/ispace', { body: { username, password } });
    } catch (e) {
      networkFailed = true;
    }
    els.loginPassword.value = '';
    state.loginBusy = false;
    els.loginSubmit.disabled = false;

    if (networkFailed) {
      els.loginError.textContent = '网络异常，请检查连接后重试。';
      els.loginError.hidden = false;
      return;
    }
    const failed = !result.ok || (result.data && (result.data.error || result.data.ok === false));
    if (failed) {
      els.loginError.textContent = serverMessage(result.data) || '登录失败，请检查账号和密码。';
      els.loginError.hidden = false;
      return;
    }
    closeLogin();
    await refreshSession({ quiet: true });
    maybeAutoInspect();
  }

  /* ---------------- file selection & inspection ---------------- */

  function setUploadError(message) {
    state.uploadError = message || null;
    renderUpload();
    renderSubmitArea();
  }

  function clearFile() {
    state.inspectGen += 1;
    state.retryKey = null;
    if (state.inspectAbort) { state.inspectAbort.abort(); state.inspectAbort = null; }
    state.file = null;
    state.inspection = null;
    state.inspecting = false;
    state.uploadError = null;
    els.fileInput.value = '';
    els.schoolPassword.value = '';
  }

  function handleFiles(fileList) {
    if (state.submitting || state.pendingKey) return;
    const f = fileList && fileList[0];
    if (!f) return;
    clearFile();
    state.attempt = null;
    renderAttempt();
    const name = typeof f.name === 'string' ? f.name : '';
    if (!/\.pdf$/i.test(name)) {
      state.file = null;
      setUploadError('仅支持 PDF 文件，请选择以 .pdf 结尾的文档。');
      return;
    }
    if (f.size === 0) {
      setUploadError('文件是空的，请选择有效的 PDF。');
      return;
    }
    if (f.size > state.limits.max_bytes) {
      setUploadError('文件超过 ' + fmtBytes(state.limits.max_bytes) + ' 限制，请压缩或拆分后再试。');
      return;
    }
    const readGen = state.inspectGen;
    const reader = new FileReader();
    reader.onload = () => {
      if (readGen !== state.inspectGen || state.submitting || state.pendingKey) return;
      const result = typeof reader.result === 'string' ? reader.result : '';
      const comma = result.indexOf(',');
      const base64 = comma >= 0 ? result.slice(comma + 1) : result;
      if (!base64) {
        setUploadError('读取文件失败，请重新选择。');
        return;
      }
      state.file = { name, size: f.size, base64 };
      state.uploadError = null;
      startInspect();
    };
    reader.onerror = () => {
      if (readGen === state.inspectGen) setUploadError('读取文件失败，请重新选择。');
    };
    reader.readAsDataURL(f);
  }

  function startInspect() {
    if (!state.file || state.inspecting || state.submitting || state.pendingKey) return;
    if (canInspect()) {
      inspectCurrentFile();
    } else {
      // gated by login or service state; the gate message explains what to do
      renderUpload();
      renderSubmitArea();
    }
  }

  async function inspectCurrentFile() {
    const file = state.file;
    if (!file) return;
    const gen = ++state.inspectGen;
    if (state.inspectAbort) state.inspectAbort.abort();
    const ctrl = new AbortController();
    state.inspectAbort = ctrl;
    state.inspecting = true;
    state.inspection = null;
    state.uploadError = null;
    renderUpload();
    renderSubmitArea();

    try {
      const r = await api('/api/print/inspect', { body: { pdf: file.base64 }, signal: ctrl.signal });
      if (gen !== state.inspectGen || state.file !== file) return;
      if (r.ok && r.data && typeof r.data.pages === 'number' && r.data.inspection_token) {
        state.inspection = {
          pages: r.data.pages,
          bytes: typeof r.data.bytes === 'number' ? r.data.bytes : file.size,
          sha256: typeof r.data.sha256 === 'string' ? r.data.sha256 : '',
          token: r.data.inspection_token,
        };
        state.uploadError = null;
      } else {
        const err = resolveApiError(r, '文档检查失败，请稍后重试。');
        state.inspection = null;
        state.uploadError = err.message;
        if (err.action) refreshSession({ quiet: true });
      }
    } catch (e) {
      if (e && e.name === 'AbortError') return;
      if (gen !== state.inspectGen || state.file !== file) return;
      state.inspection = null;
      state.uploadError = '网络异常，未能完成文档检查。文件仍在，请重试。';
    } finally {
      if (gen === state.inspectGen) {
        state.inspecting = false;
        state.inspectAbort = null;
        renderUpload();
        renderSubmitArea();
      }
    }
  }

  /* ---------------- submission ---------------- */

  async function submitJob(event) {
    event.preventDefault();
    if (!canSubmit()) return;
    const identity = state.identityGen;

    const key = state.retryKey || newIdempotencyKey();
    state.retryKey = key;
    const body = {
      pdf: state.file.base64,
      password: els.schoolPassword.value,
      inspection_token: state.inspection.token,
      idempotency_key: key,
    };

    state.submitting = true;
    els.schoolPassword.value = '';
    setAttempt({ tone: 'info', title: '正在提交', text: '正在把文档交给学校队列，请不要关闭页面。', actions: [] });
    renderUpload();
    renderSubmitArea();
    renderSession();

    let result = null;
    let networkFailed = false;
    try {
      result = await api('/api/print/jobs', { body });
      // A gateway error can occur after the agent received the job.
      if (result.status >= 500 && !(result.data && result.data.job) &&
          !(result.status === 503 && result.data && ['offline', 'busy'].includes(result.data.code))) {
        networkFailed = true;
      }
    } catch (e) {
      networkFailed = true;
    }
    body.pdf = '';
    body.password = '';
    if (identity !== state.identityGen) return;
    state.submitting = false;

    if (networkFailed) {
      // Ambiguous: the request may or may not have arrived. Keep the key,
      // do not resend, let the user reconcile explicitly.
      state.pendingKey = key;
      setAttempt({
        tone: 'warn',
        title: '提交结果待确认',
        text: '网络异常，无法确认本次提交是否送达。本站不会自动重复提交。请查询任务状态，或到打印机取件界面确认队列。',
        actions: [{ id: 'query', label: '查询任务状态' }],
      });
      renderUpload();
      renderSubmitArea();
      renderSession();
      return;
    }

    if (result.data && result.data.job && typeof result.data.job === 'object') {
      state.pendingKey = null;
      upsertJob(result.data.job);
      state.currentJobId = result.data.job.id;
      if (['submitted', 'processing', 'unknown'].includes(result.data.job.state)) {
        clearFile();
      } else {
        state.retryKey = null;
      }
      setAttempt(attemptFromJob(result.data.job));
      maybePoll(result.data.job);
      renderJobs();
    } else {
      const err = resolveApiError(result, '提交失败，请稍后重试。');
      if (err.action === 'session') refreshSession({ quiet: true });
      if (result.data && result.data.code === 'inspection_expired') {
        state.inspection = null;
        startInspect();
      }
      setAttempt({
        tone: 'error',
        title: '提交未成功',
        text: err.message + ' 文档仍在，可以重新输入密码再次提交。',
        actions: [],
      });
    }
    renderUpload();
    renderSubmitArea();
    renderSession();
  }

  function attemptFromJob(job) {
    const server = job && typeof job.message === 'string' && job.message ? job.message : null;
    switch (job.state) {
      case 'submitted':
        return {
          tone: 'ok',
          title: '已交给学校队列',
          text: (server || '已交给学校队列。') + ' 请到学校打印点刷卡取件，出纸与计费以打印机为准。',
          actions: [],
        };
      case 'processing':
        return {
          tone: 'info',
          title: '正在处理',
          text: server || '正在交给学校队列，请稍候。',
          actions: [{ id: 'refresh-job', label: '刷新状态' }],
        };
      case 'unknown':
        return {
          tone: 'warn',
          title: '提交结果待确认',
          text: (server || '提交结果待确认。') + ' 请到打印机取件界面确认任务是否已进入队列。',
          actions: [{ id: 'refresh-job', label: '刷新状态' }],
        };
      case 'rejected':
        return {
          tone: 'error',
          title: '提交未通过校验',
          text: server || '提交未通过校验。',
          actions: [],
        };
      case 'failed':
      default:
        return {
          tone: 'error',
          title: '提交失败',
          text: server || '提交失败，请稍后重试。',
          actions: [],
        };
    }
  }

  async function queryPendingSubmission() {
    if (!state.pendingKey) return;
    const identity = state.identityGen;
    setAttempt({ tone: 'info', title: '正在查询任务状态', text: '请稍候。', actions: [] });
    try {
      const r = await api('/api/print/jobs');
      if (identity !== state.identityGen) return;
      if (!r.ok || !r.data || !Array.isArray(r.data.jobs)) throw new Error('jobs');
      mergeJobs(r.data.jobs);
      renderJobs();
      const found = state.jobs.find((j) => j && j.idempotency_key === state.pendingKey);
      if (found) {
        state.pendingKey = null;
        state.currentJobId = found.id;
        clearFile();
        setAttempt(attemptFromJob(found));
        maybePoll(found);
      } else {
        setAttempt({
          tone: 'warn',
          title: '暂未查到本次提交',
          text: '任务记录中还没有本次提交。请先到打印机取件界面确认是否已进入队列；如确认没有提交，可以重新输入密码再次尝试。',
          actions: [
            { id: 'query', label: '再查一次' },
            { id: 'retry-new', label: '确认未提交，再次尝试' },
          ],
        });
      }
    } catch (e) {
      if (identity !== state.identityGen) return;
      setAttempt({
        tone: 'warn',
        title: '查询失败',
        text: '网络异常，未能查询任务状态。请稍后重试，或到打印机取件界面确认。本站不会自动重复提交。',
        actions: [{ id: 'query', label: '重新查询' }],
      });
    }
    renderUpload();
    renderSubmitArea();
  }

  /* ---------------- jobs ---------------- */

  function upsertJob(job) {
    if (!job || typeof job.id !== 'string' || !job.id) return;
    const i = state.jobs.findIndex((j) => j && j.id === job.id);
    if (i >= 0) {
      if (state.jobs[i].state === 'submitted' && job.state !== 'submitted') return;
      if (state.jobs[i].state === 'unknown' && job.state === 'processing') return;
      state.jobs[i] = job;
    }
    else state.jobs.push(job);
    state.jobs.sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
  }

  function mergeJobs(list) {
    list.forEach(upsertJob);
    state.jobs = state.jobs.filter(job => job.created_at >= Date.now() / 1000 - 86400);
  }

  function jobIsActive(job) {
    return job && (job.state === 'processing' || job.state === 'unknown');
  }

  async function loadJobs(options) {
    const identity = state.identityGen;
    const quiet = !!(options && options.quiet);
    if (!state.user) {
      state.jobs = [];
      renderJobs();
      return;
    }
    try {
      const r = await api('/api/print/jobs');
      if (identity !== state.identityGen) return;
      if (!r.ok || !r.data || !Array.isArray(r.data.jobs)) throw new Error('jobs');
      mergeJobs(r.data.jobs);
      renderJobs();
      state.jobs.forEach((job) => { if (jobIsActive(job)) maybePoll(job); });
    } catch (e) {
      if (identity !== state.identityGen) return;
      if (!quiet) {
        els.jobsEmpty.textContent = '暂时无法获取任务记录，请稍后刷新。';
        els.jobsEmpty.hidden = state.jobs.length > 0;
      }
    }
  }

  async function refreshJob(id) {
    const identity = state.identityGen;
    try {
      const r = await api('/api/print/jobs/' + encodeURIComponent(id));
      if (identity !== state.identityGen) return;
      if (!r.ok || !r.data || !r.data.job) return;
      upsertJob(r.data.job);
      if (state.currentJobId === id) setAttempt(attemptFromJob(r.data.job));
      renderJobs();
      if (jobIsActive(r.data.job)) maybePoll(r.data.job);
      renderSubmitArea();
    } catch (e) {
      /* manual refresh stays available */
    }
  }

  function maybePoll(job) {
    if (!jobIsActive(job)) return;
    if (state.pollTimers.has(job.id)) return;
    const identity = state.identityGen;
    let count = 0;
    const tick = async () => {
      count += 1;
      try {
        const r = await api('/api/print/jobs/' + encodeURIComponent(job.id));
        if (identity !== state.identityGen) return;
        if (r.ok && r.data && r.data.job) {
          upsertJob(r.data.job);
          if (state.currentJobId === job.id) setAttempt(attemptFromJob(r.data.job));
          renderJobs();
          renderSubmitArea();
          if (jobIsActive(r.data.job) && count < JOB_POLL_MAX) {
            state.pollTimers.set(job.id, window.setTimeout(tick, JOB_POLL_MS));
            return;
          }
        }
      } catch (e) {
        /* stop polling on network failure; manual refresh remains */
      }
      if (identity !== state.identityGen) return;
      state.pollTimers.delete(job.id);
      renderJobs();
    };
    state.pollTimers.set(job.id, window.setTimeout(tick, JOB_POLL_MS));
  }

  /* ---------------- attempt panel ---------------- */

  function setAttempt(cfg) {
    state.attempt = cfg;
    renderAttempt();
  }

  function renderAttempt() {
    const a = state.attempt;
    els.attempt.replaceChildren();
    if (!a) {
      els.attempt.hidden = true;
      return;
    }
    els.attempt.hidden = false;
    els.attempt.dataset.tone = a.tone || 'info';

    const title = document.createElement('p');
    title.className = 'attempt-title';
    const chip = document.createElement('span');
    chip.className = 'attempt-chip';
    chip.setAttribute('aria-hidden', 'true');
    title.appendChild(chip);
    title.appendChild(document.createTextNode(a.title || ''));
    els.attempt.appendChild(title);

    if (a.text) {
      const text = document.createElement('p');
      text.className = 'attempt-text';
      text.textContent = (state.service && state.service.demo ? '本地演示，不会真实打印。' : '') + a.text;
      els.attempt.appendChild(text);
    }

    if (a.actions && a.actions.length) {
      const row = document.createElement('div');
      row.className = 'attempt-actions';
      a.actions.forEach((act, i) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = i === 0 ? 'btn btn-outline btn-sm' : 'btn btn-ghost btn-sm';
        btn.dataset.action = act.id;
        btn.textContent = act.label;
        row.appendChild(btn);
      });
      els.attempt.appendChild(row);
    }
  }

  /* ---------------- rendering ---------------- */

  function renderSession() {
    // identity
    els.identity.replaceChildren();
    if (state.user) {
      const box = document.createElement('span');
      box.className = 'identity';
      const name = document.createElement('span');
      name.className = 'identity-name';
      name.textContent = state.user.display_name || '同学';
      box.appendChild(name);
      const sub = document.createElement('span');
      sub.className = 'identity-sub';
      sub.textContent = state.user.school_username || '未完成校园认证';
      box.appendChild(sub);
      els.identity.appendChild(box);
      if (!state.user.school_username) {
        const verify = document.createElement('button');
        verify.type = 'button';
        verify.className = 'btn btn-outline btn-sm';
        verify.id = 'verify-open';
        verify.textContent = '完成认证';
        verify.disabled = state.submitting;
        els.identity.appendChild(verify);
      }
    } else {
      const login = document.createElement('button');
      login.type = 'button';
      login.className = 'btn btn-ghost btn-sm';
      login.id = 'login-open';
      login.textContent = '登录';
      login.disabled = state.submitting;
      els.identity.appendChild(login);
    }

    // service status line
    els.serviceDot.className = 'status-dot';
    if (!state.sessionLoaded) {
      els.serviceText.textContent = '正在连接打印服务…';
    } else if (!state.service) {
      els.serviceDot.classList.add('down');
      els.serviceText.textContent = '暂时无法连接打印服务，将自动重试。';
    } else if (serviceUsable()) {
      els.serviceDot.classList.add('ok');
      els.serviceText.textContent = serviceMessage() || '可以提交打印';
    } else {
      els.serviceDot.classList.add('down');
      els.serviceText.textContent = serviceMessage() || '打印服务暂不可用，请稍后再试。';
    }
    const demo = !!(state.service && state.service.demo);
    els.demoBadge.hidden = !demo;

    // limits copy
    const lim = '仅 PDF · 不超过 ' + fmtBytes(state.limits.max_bytes) + ' · ' + state.limits.max_pages + ' 页以内';
    els.limitsNote.textContent = lim;
    els.limitsHint.textContent =
      '仅支持 PDF，单个文件不超过 ' + fmtBytes(state.limits.max_bytes) +
      '，页数不超过 ' + state.limits.max_pages + ' 页。';

    // capabilities
    const cap = state.capabilities || {};
    els.capPaper.textContent = typeof cap.paper === 'string' && cap.paper ? cap.paper : 'A4';
    els.capColor.textContent = cap.color === 'grayscale' ? '黑白' : (cap.color || '黑白');
    els.capSides.textContent = cap.sides === 'one-sided' ? '单面' : (cap.sides || '单面');
    els.capCopies.textContent = (typeof cap.copies === 'number' ? cap.copies : 1) + ' 份';
  }

  function renderUpload() {
    const locked = state.submitting || !!state.pendingKey;
    els.dropzone.classList.toggle('locked', locked);
    els.pickBtn.disabled = locked;
    els.fileInput.disabled = locked;

    els.dropzone.hidden = !!state.file;
    if (!state.file && !state.uploadError) {
      els.docPanel.hidden = true;
      return;
    }
    els.docPanel.hidden = false;
    els.docPanel.classList.toggle('doc-error-only', !state.file);

    if (!state.file) {
      els.docError.textContent = state.uploadError || '';
      els.docErrorRow.hidden = false;
      els.docRetry.hidden = true;
      els.docProgress.hidden = true;
      return;
    }
    els.docRetry.hidden = false;
    els.docName.textContent = state.file.name;
    els.docName.title = state.file.name;

    if (state.inspecting) {
      els.docSub.textContent = fmtBytes(state.file.size) + ' · 正在检查文档';
      els.docProgress.hidden = false;
      els.docErrorRow.hidden = true;
      els.docRemove.disabled = true;
    } else if (state.inspection) {
      els.docSub.textContent = state.inspection.pages + ' 页 · ' + fmtBytes(state.inspection.bytes) + ' · 检查通过';
      els.docProgress.hidden = true;
      els.docErrorRow.hidden = true;
      els.docRemove.disabled = locked;
    } else {
      els.docSub.textContent = fmtBytes(state.file.size);
      els.docProgress.hidden = true;
      els.docRemove.disabled = locked;
      if (state.uploadError) {
        els.docError.textContent = state.uploadError;
        els.docErrorRow.hidden = false;
        els.docRetry.disabled = locked;
      } else {
        els.docErrorRow.hidden = true;
      }
    }
  }

  function renderSubmitArea() {
    els.capPages.textContent = state.inspection ? state.inspection.pages + ' 页' : '待检查';

    const showForm = !!(
      state.inspection &&
      state.user &&
      state.user.school_username &&
      serviceUsable() &&
      !state.pendingKey
    );
    els.submitForm.hidden = !showForm;
    if (showForm) {
      els.schoolUsername.value = state.user.school_username || '';
      els.schoolPassword.disabled = state.submitting;
      els.submitBtn.disabled = !canSubmit();
      els.submitBtn.textContent = state.submitting ? '正在提交…' : '提交打印';
    }

    // gate message when the form cannot be shown yet
    let gateText = '';
    let gateAction = null;
    if (state.pendingKey || state.inspecting) {
      gateText = '';
    } else if (!state.file) {
      gateText = '先在上面选择一份 PDF，检查通过后在这里输入密码提交。';
    } else if (!state.sessionLoaded || !state.service) {
      gateText = '正在确认打印服务状态…';
    } else if (!serviceUsable()) {
      gateText = serviceMessage() || '打印服务暂不可用，请稍后再试。';
    } else if (!state.user) {
      gateText = '登录后才能提交打印。';
      gateAction = { id: 'open-login', label: '登录' };
    } else if (!state.user.school_username) {
      gateText = '当前账号还没有通过校园统一认证，请使用学校账号登录。';
      gateAction = { id: 'open-login', label: '使用学校账号登录' };
    }
    els.gate.hidden = !gateText;
    els.gateText.textContent = gateText;
    if (gateAction) {
      els.gateAction.hidden = false;
      els.gateAction.textContent = gateAction.label;
      els.gateAction.dataset.action = gateAction.id;
      els.gateAction.disabled = state.submitting;
    } else {
      els.gateAction.hidden = true;
      els.gateAction.dataset.action = '';
    }
  }

  const STATE_LABEL = {
    processing: '处理中',
    submitted: '已交给学校队列',
    unknown: '待确认',
    failed: '提交失败',
    rejected: '未通过校验',
  };

  function renderJobs() {
    els.jobsList.replaceChildren();
    const jobs = state.jobs.slice(0, JOBS_RENDER_MAX);
    jobs.forEach((job) => {
      const li = document.createElement('li');
      li.className = 'job-row';

      const stateSpan = document.createElement('span');
      stateSpan.className = 'job-state';
      const dot = document.createElement('span');
      const knownState = Object.prototype.hasOwnProperty.call(STATE_LABEL, job.state) ? job.state : '';
      dot.className = 'job-dot' + (knownState ? ' ' + knownState : '');
      dot.setAttribute('aria-hidden', 'true');
      stateSpan.appendChild(dot);
      stateSpan.appendChild(document.createTextNode(STATE_LABEL[job.state] || job.state || '未知'));
      li.appendChild(stateSpan);

      const idSpan = document.createElement('span');
      idSpan.className = 'job-id';
      idSpan.textContent = '#' + String(job.id).slice(0, 8);
      li.appendChild(idSpan);

      if (typeof job.pages === 'number') {
        const pages = document.createElement('span');
        pages.className = 'job-pages';
        pages.textContent = job.pages + ' 页';
        li.appendChild(pages);
      }

      const time = document.createElement('time');
      time.className = 'job-time';
      if (typeof job.created_at === 'number') {
        time.dateTime = new Date(job.created_at * 1000).toISOString();
        time.title = fmtTimeFull(job.created_at);
        time.textContent = fmtTime(job.created_at);
      }
      li.appendChild(time);

      if (jobIsActive(job)) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn btn-ghost btn-sm job-act';
        btn.dataset.jobId = job.id;
        btn.textContent = '查询';
        li.appendChild(btn);
      }

      if (job.state !== 'submitted' && typeof job.message === 'string' && job.message) {
        const msg = document.createElement('p');
        msg.className = 'job-message';
        msg.textContent = job.message;
        li.appendChild(msg);
      }

      els.jobsList.appendChild(li);
    });
    renderJobsEmpty();
  }

  function renderJobsEmpty() {
    if (state.jobs.length > 0) {
      els.jobsEmpty.hidden = true;
      return;
    }
    els.jobsEmpty.hidden = false;
    els.jobsEmpty.textContent = state.user ? '暂无任务记录。' : '登录后显示最近任务。';
  }

  function renderAll() {
    renderSession();
    renderUpload();
    renderSubmitArea();
    renderAttempt();
    renderJobs();
  }

  /* ---------------- events ---------------- */

  els.pickBtn.addEventListener('click', () => {
    if (!els.pickBtn.disabled) els.fileInput.click();
  });

  els.dropzone.addEventListener('click', (e) => {
    if (state.submitting || state.pendingKey) return;
    if (e.target === els.pickBtn) return;
    els.fileInput.click();
  });

  els.fileInput.addEventListener('change', () => {
    handleFiles(els.fileInput.files);
  });

  ['dragenter', 'dragover'].forEach((type) => {
    els.dropzone.addEventListener(type, (e) => {
      e.preventDefault();
      if (state.submitting || state.pendingKey) return;
      els.dropzone.classList.add('dragover');
    });
  });

  ['dragleave', 'drop'].forEach((type) => {
    els.dropzone.addEventListener(type, (e) => {
      e.preventDefault();
      els.dropzone.classList.remove('dragover');
    });
  });

  els.dropzone.addEventListener('drop', (e) => {
    if (state.submitting || state.pendingKey) return;
    if (e.dataTransfer) handleFiles(e.dataTransfer.files);
  });

  els.docRemove.addEventListener('click', () => {
    if (state.submitting || state.pendingKey) return;
    clearFile();
    state.attempt = null;
    renderUpload();
    renderSubmitArea();
    renderAttempt();
  });

  els.docRetry.addEventListener('click', () => {
    if (state.submitting || state.pendingKey) return;
    startInspect();
  });

  els.schoolPassword.addEventListener('input', () => {
    els.submitBtn.disabled = !canSubmit();
  });

  els.submitForm.addEventListener('submit', submitJob);

  els.attempt.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-action]');
    if (!btn) return;
    const action = btn.dataset.action;
    if (action === 'query') {
      queryPendingSubmission();
    } else if (action === 'retry-new') {
      state.retryKey = state.pendingKey;
      state.pendingKey = null;
      setAttempt(null);
      renderUpload();
      renderSubmitArea();
    } else if (action === 'refresh-job') {
      if (state.currentJobId) refreshJob(state.currentJobId);
    }
  });

  els.gateAction.addEventListener('click', () => {
    if (els.gateAction.dataset.action === 'open-login') openLogin();
  });

  els.identity.addEventListener('click', (e) => {
    const btn = e.target.closest('button');
    if (!btn || btn.disabled) return;
    if (btn.id === 'login-open' || btn.id === 'verify-open') openLogin();
  });

  els.jobsRefresh.addEventListener('click', () => loadJobs());

  els.jobsList.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-job-id]');
    if (!btn) return;
    refreshJob(btn.dataset.jobId);
  });

  els.loginForm.addEventListener('submit', submitLogin);
  els.loginClose.addEventListener('click', closeLogin);
  els.loginDialog.addEventListener('cancel', () => { els.loginForm.reset(); els.loginError.hidden = true; });
  els.loginDialog.addEventListener('close', () => { els.loginForm.reset(); els.loginError.hidden = true; });
  els.loginDialog.addEventListener('click', (e) => {
    if (e.target === els.loginDialog && !state.loginBusy) closeLogin();
  });

  window.addEventListener('online', () => refreshSession({ quiet: true }));
  window.addEventListener('offline', () => {
    state.service = null;
    renderSession();
    renderSubmitArea();
  });

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && Date.now() - state.lastSessionAt > 30000) {
      refreshSession({ quiet: true });
    }
  });

  window.addEventListener('beforeunload', (e) => {
    if (state.submitting) {
      e.preventDefault();
      e.returnValue = '';
    }
  });

  /* ---------------- boot ---------------- */

  renderAll();
  refreshSession({ quiet: true }).then(() => {
    loadJobs({ quiet: true });
  });
  window.setInterval(() => {
    refreshSession({ quiet: true });
    if (state.user) loadJobs({ quiet: true });
  }, SESSION_POLL_MS);
})();
