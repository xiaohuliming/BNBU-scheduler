'use strict';

const state = {
    status: null,
    account: { authenticated: false },
    services: [],
    countries: [],
    selectedService: null,
    selectedCountry: null,
    orders: [],
    ordersFingerprint: '',
    orderFilter: 'active',
    pollTimer: null,
    busy: false,
    authMode: 'login',
    authProvider: 'local',
};

const SERVICE_RENDER_LIMIT = 100;
const ACTIVE_STATUSES = new Set(['purchasing', 'active', 'code_received']);
const $ = (id) => document.getElementById(id);

const api = async (path, options = {}) => {
    const response = await fetch(path.startsWith('/api/') ? path : '/api/sms-lab' + path, {
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
        ...options,
    });
    let payload = {};
    try { payload = await response.json(); } catch (_) {}
    if (!response.ok) {
        const error = new Error(payload.error || '请求失败，请稍后重试。');
        error.status = response.status;
        error.code = payload.code;
        throw error;
    }
    return payload;
};

const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
})[char]);

const initials = (name) => String(name || '?').trim().slice(0, 2).toUpperCase();
const logoMarkup = (service, className = '') => `
    <span class="service-logo ${className}">
        <img src="${escapeHtml(service.logo_url)}" alt="" loading="lazy"
            onerror="this.hidden=true;this.nextElementSibling.hidden=false">
        <span class="logo-fallback" hidden>${escapeHtml(initials(service.name))}</span>
    </span>`;

const showMessage = (element, message, type = '') => {
    element.textContent = message;
    element.className = 'notice' + (type ? ' ' + type : '');
};
const hideMessage = (element) => element.classList.add('hidden');
const toast = (message) => {
    $('toast').textContent = message;
    $('toast').classList.remove('hidden');
    window.clearTimeout(toast.timer);
    toast.timer = window.setTimeout(() => $('toast').classList.add('hidden'), 3600);
};
const setButtonBusy = (button, busy, label) => {
    if (!button.dataset.label) button.dataset.label = button.textContent;
    button.disabled = busy;
    button.textContent = busy ? label : button.dataset.label;
};
const formatTime = (value) => {
    if (!value) return '';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', { hour12: false });
};

const renderAccount = () => {
    const authenticated = !!state.account.authenticated;
    $('account-guest').classList.toggle('hidden', authenticated);
    $('account-user').classList.toggle('hidden', !authenticated);
    if (authenticated) {
        $('account-name').textContent = state.account.user.display_name;
        $('wallet-balance').textContent = Number(state.account.wallet.balance).toFixed(4);
    }
    updateCheckout();
};

const loadAccount = async () => {
    state.account = await api('/account');
    renderAccount();
    return state.account;
};

const renderServices = () => {
    const query = $('service-search').value.trim().toLowerCase();
    const matches = state.services.filter((service) =>
        !query || service.code.includes(query) || service.name.toLowerCase().includes(query)
    );
    const visible = matches.slice(0, SERVICE_RENDER_LIMIT);
    if (!visible.length) {
        $('service-list').innerHTML = '<div class="step-placeholder">没有匹配的服务，换个关键词试试</div>';
    } else {
        $('service-list').innerHTML = visible.map((service) => `
            <button class="service-option ${state.selectedService?.code === service.code ? 'selected' : ''}" type="button"
                role="option" aria-selected="${state.selectedService?.code === service.code}" data-service="${escapeHtml(service.code)}">
                <span class="service-main">${logoMarkup(service)}<strong>${escapeHtml(service.name)}</strong></span>
                <span class="option-code">${escapeHtml(service.code)}</span>
            </button>
        `).join('');
    }
    $('service-hint').textContent = matches.length > SERVICE_RENDER_LIMIT
        ? `找到 ${matches.length} 项，当前显示前 ${SERVICE_RENDER_LIMIT} 项，请继续输入关键词`
        : `找到 ${matches.length} 项服务`;
};

const loadServices = async () => {
    try {
        const data = await api('/services');
        state.services = data.services || [];
        renderServices();
    } catch (error) {
        $('service-list').innerHTML = '<div class="step-placeholder">服务目录加载失败</div>';
        showMessage($('config-message'), error.message, 'error');
    }
};

const renderCountries = () => {
    if (!state.selectedService) {
        $('country-list').innerHTML = '<div class="step-placeholder">选好服务后，这里会显示实时库存与售价</div>';
        return;
    }
    if (!state.countries.length) {
        $('country-list').innerHTML = '<div class="step-placeholder">这个服务目前没有可用国家，请换一个服务</div>';
        return;
    }
    $('country-list').innerHTML = state.countries.map((country) => `
        <button class="country-option ${state.selectedCountry?.id === country.id ? 'selected' : ''}" type="button"
            role="option" aria-selected="${state.selectedCountry?.id === country.id}" data-country="${country.id}">
            <span class="country-name">${escapeHtml(country.name)}</span>
            <span class="country-meta">${country.stock} 个 · ${Number(country.price).toFixed(4)} USD</span>
        </button>
    `).join('');
};

const loadCountries = async () => {
    state.countries = [];
    state.selectedCountry = null;
    updateCheckout();
    $('country-list').innerHTML = '<div class="step-placeholder"><span class="spinner" aria-hidden="true"></span>正在读取实时库存...</div>';
    try {
        const data = await api('/countries?service=' + encodeURIComponent(state.selectedService.code));
        state.countries = data.countries || [];
        renderCountries();
    } catch (error) {
        $('country-list').innerHTML = '<div class="step-placeholder">国家库存加载失败，请重新选择服务</div>';
        showMessage($('config-message'), error.message, 'error');
    }
};

const updateCheckout = () => {
    const service = state.selectedService;
    const country = state.selectedCountry;
    if (!service || !country) {
        $('selection-name').textContent = service ? service.name + ' · 还需选择国家' : '尚未选完';
        $('selection-price').textContent = '···';
        $('purchase-button').textContent = '购买一个号码';
        $('purchase-button').disabled = true;
        $('wallet-hint').textContent = state.account.authenticated ? '余额不足时请联系管理员充值' : '登录后可使用站内钱包购买';
        return;
    }
    const price = Number(country.price);
    $('selection-name').textContent = `${service.name} · ${country.name}`;
    $('selection-price').textContent = `${price.toFixed(4)} USD`;
    if (!state.account.authenticated) {
        $('purchase-button').textContent = '登录后购买';
        $('purchase-button').disabled = false;
        $('wallet-hint').textContent = '登录或注册后继续';
        return;
    }
    const balance = Number(state.account.wallet.balance);
    const enough = Number.isFinite(balance) && balance >= price;
    $('purchase-button').textContent = enough
        ? `购买 1 个号码 · ${price.toFixed(4)} USD`
        : '钱包余额不足';
    $('purchase-button').disabled = !enough;
    $('wallet-hint').textContent = enough
        ? `购买后预计剩余 ${(balance - price).toFixed(4)} USD`
        : '请联系管理员人工充值';
};

const orderStatusLabel = (status) => ({
    purchasing: '正在下单', active: '等待短信', code_received: '已收到验证码',
    completed: '已完成', cancelled: '已退款', failed: '购买失败', refunded: '已退款',
})[status] || '处理中';

const renderOrders = () => {
    const activeCount = state.orders.filter((order) => ACTIVE_STATUSES.has(order.status)).length;
    $('activation-count').textContent = activeCount;
    document.querySelectorAll('[data-order-filter]').forEach((button) => {
        const selected = button.dataset.orderFilter === state.orderFilter;
        button.classList.toggle('selected', selected);
        button.setAttribute('aria-pressed', String(selected));
    });
    const visible = state.orderFilter === 'active'
        ? state.orders.filter((order) => ACTIVE_STATUSES.has(order.status))
        : state.orders;
    if (!state.account.authenticated) {
        $('activation-list').innerHTML = '<div class="empty"><div><div class="empty-doodle" aria-hidden="true"></div><strong>登录后查看订单</strong><span>每位用户拥有独立钱包、号码和验证码记录。</span><button class="btn btn-primary empty-action" type="button" data-open-auth>登录或注册</button></div></div>';
        return;
    }
    if (!visible.length) {
        const title = state.orderFilter === 'active' ? '没有进行中的订单' : '还没有购买记录';
        $('activation-list').innerHTML = `<div class="empty"><div><div class="empty-doodle" aria-hidden="true"></div><strong>${title}</strong><span>从左侧选择服务和国家，购买后的号码与验证码会出现在这里。</span></div></div>`;
        return;
    }
    $('activation-list').innerHTML = visible.map((order) => {
        const otp = order.otpList?.[order.otpList.length - 1];
        const createdAt = new Date(order.createdAt || 0).getTime();
        const cancelWait = Number.isFinite(createdAt) && createdAt > 0
            ? Math.max(0, Math.ceil((createdAt + 120000 - Date.now()) / 1000))
            : 0;
        return `<article class="activation-card" data-id="${order.id}">
            <div class="card-head">
                <div class="order-service">${logoMarkup(order.service, 'order-logo')}<div><strong>${escapeHtml(order.service.name)}</strong><span>${escapeHtml(order.country.name)} · ${Number(order.sale_price).toFixed(4)} USD</span></div></div>
                <span class="status-badge">${escapeHtml(orderStatusLabel(order.status))}</span>
            </div>
            ${order.phone ? `<div class="phone">+${escapeHtml(order.phone.replace(/^\+/, ''))}</div>` : ''}
            <div class="card-meta">订单 #${order.id} · ${escapeHtml(formatTime(order.createdAt))}</div>
            ${otp ? `<div class="otp"><div class="otp-code">${escapeHtml(otp.smsCode || '已收到')}</div><div class="otp-text">${escapeHtml(otp.smsText)}${otp.receivedAt ? '<br>' + escapeHtml(formatTime(otp.receivedAt)) : ''}</div></div>` : (ACTIVE_STATUSES.has(order.status) ? '<div class="waiting"><span class="spinner" aria-hidden="true"></span>每 5 秒自动检查一次新短信</div>' : '')}
            <div class="actions">
                ${order.phone ? `<button class="btn btn-small" type="button" data-action="copy-phone" data-value="+${escapeHtml(order.phone.replace(/^\+/, ''))}">复制号码</button>` : ''}
                ${otp?.smsCode ? `<button class="btn btn-small" type="button" data-action="copy-code" data-value="${escapeHtml(otp.smsCode)}">复制验证码</button>` : ''}
                ${order.can_finish ? '<button class="btn btn-small" type="button" data-action="finish">完成</button>' : ''}
                ${order.can_replace ? '<button class="btn btn-small" type="button" data-action="replace">换号</button>' : ''}
                ${order.can_cancel ? `<button class="btn btn-small btn-danger" type="button" data-action="cancel" ${cancelWait ? 'disabled' : ''}>${cancelWait ? cancelWait + ' 秒后可取消' : '取消并退款'}</button>` : ''}
            </div>
        </article>`;
    }).join('');
};

const loadOrders = async (announce = false) => {
    if (!state.account.authenticated || state.busy) {
        renderOrders();
        return;
    }
    state.busy = true;
    setButtonBusy($('refresh-button'), true, '刷新中...');
    try {
        const data = await api('/orders');
        const nextOrders = data.orders || [];
        const fingerprint = JSON.stringify(nextOrders);
        if (fingerprint !== state.ordersFingerprint) {
            state.orders = nextOrders;
            state.ordersFingerprint = fingerprint;
            renderOrders();
        }
        hideMessage($('activation-status'));
        if (announce) toast('订单状态已刷新');
    } catch (error) {
        showMessage($('activation-status'), error.message, 'error');
    } finally {
        state.busy = false;
        setButtonBusy($('refresh-button'), false, '刷新中...');
    }
};

const startPolling = () => {
    if (state.pollTimer) window.clearInterval(state.pollTimer);
    state.pollTimer = window.setInterval(() => {
        if (document.hidden || !state.orders.some((order) => ACTIVE_STATUSES.has(order.status))) return;
        loadOrders(false);
    }, 5000);
};

const setAuthProvider = (provider) => {
    state.authProvider = provider;
    if (provider === 'ispace') state.authMode = 'login';
    const isISpace = provider === 'ispace';
    $('auth-local-tab').classList.toggle('selected', !isISpace);
    $('auth-ispace-tab').classList.toggle('selected', isISpace);
    $('auth-local-tab').setAttribute('aria-selected', String(!isISpace));
    $('auth-ispace-tab').setAttribute('aria-selected', String(isISpace));
    $('auth-username-label').textContent = isISpace ? 'iSpace 学号' : '用户名';
    $('auth-password-label').textContent = isISpace ? 'iSpace 密码' : '密码';
    $('auth-username').placeholder = isISpace ? '输入 BNBU 学号' : '输入用户名';
    $('auth-password').minLength = 1;
    $('auth-provider-hint').textContent = isISpace
        ? '验证通过后会同步 iSpace DDL。密码只用于本次登录，不会保存在 SMS Market。'
        : '使用 MAXCOURSE、SlideCraft 或 OmniChat 的同一账号登录。';
    $('auth-switch').classList.toggle('hidden', isISpace);
    $('auth-title').textContent = isISpace
        ? '使用 iSpace 登录'
        : (state.authMode === 'register' ? '创建 MAXCOURSE 账号' : '登录 MAXCOURSE');
    $('auth-submit').textContent = isISpace
        ? '连接 iSpace'
        : (state.authMode === 'register' ? '注册并登录' : '登录');
};

const openAuth = (mode = 'login', provider = 'local') => {
    state.authMode = mode;
    $('auth-modal').classList.remove('hidden');
    $('auth-switch').textContent = mode === 'register' ? '已有账号，去登录' : '没有账号，去注册';
    setAuthProvider(provider);
    hideMessage($('auth-message'));
    $('auth-username').focus();
};
const closeAuth = () => $('auth-modal').classList.add('hidden');

const openPurchaseConfirm = () => {
    $('confirm-service').textContent = state.selectedService.name;
    $('confirm-country').textContent = state.selectedCountry.name;
    $('confirm-price').textContent = `${Number(state.selectedCountry.price).toFixed(4)} USD`;
    $('purchase-modal').classList.remove('hidden');
    $('purchase-confirm').focus();
};
const closePurchaseConfirm = () => $('purchase-modal').classList.add('hidden');

const submitPurchase = async () => {
    const key = (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}_${Math.random()}`).replace(/[^A-Za-z0-9_-]/g, '_');
    setButtonBusy($('purchase-confirm'), true, '正在向号池下单...');
    try {
        const data = await api('/orders', {
            method: 'POST',
            body: JSON.stringify({
                service: state.selectedService.code,
                country: state.selectedCountry.id,
                idempotency_key: key,
            }),
        });
        state.account.wallet.balance = data.wallet_balance;
        closePurchaseConfirm();
        renderAccount();
        await loadOrders(false);
        toast('号码已购买，正在等待短信');
    } catch (error) {
        closePurchaseConfirm();
        showMessage($('activation-status'), error.message, 'error');
    } finally {
        setButtonBusy($('purchase-confirm'), false, '');
        updateCheckout();
    }
};

const copyText = async (value, label) => {
    try {
        await navigator.clipboard.writeText(value);
        toast(label + '已复制');
    } catch (_) {
        toast('复制失败，请手动选择文本。');
    }
};

$('login-button').addEventListener('click', () => openAuth('login'));
$('logout-button').addEventListener('click', async () => {
    await api('/api/logout', { method: 'POST', body: '{}' });
    state.account = { authenticated: false };
    state.orders = [];
    state.ordersFingerprint = '';
    renderAccount();
    renderOrders();
    toast('已退出账号');
});
$('auth-close').addEventListener('click', closeAuth);
$('auth-switch').addEventListener('click', () => openAuth(state.authMode === 'login' ? 'register' : 'login'));
$('auth-local-tab').addEventListener('click', () => setAuthProvider('local'));
$('auth-ispace-tab').addEventListener('click', () => setAuthProvider('ispace'));
$('auth-modal').addEventListener('click', (event) => {
    if (event.target === $('auth-modal')) closeAuth();
});
$('purchase-cancel').addEventListener('click', closePurchaseConfirm);
$('purchase-confirm').addEventListener('click', submitPurchase);
$('purchase-modal').addEventListener('click', (event) => {
    if (event.target === $('purchase-modal')) closePurchaseConfirm();
});
$('auth-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const username = $('auth-username').value.trim();
    const password = $('auth-password').value;
    const isISpace = state.authProvider === 'ispace';
    if (!username || !password) {
        showMessage($('auth-message'), isISpace ? '请输入 iSpace 学号和密码。' : '请输入用户名和密码。', 'error');
        return;
    }
    const busyLabel = isISpace ? '正在连接 iSpace...' : (state.authMode === 'register' ? '注册中...' : '登录中...');
    setButtonBusy($('auth-submit'), true, busyLabel);
    try {
        if (!isISpace && state.authMode === 'register') {
            await api('/api/register', { method: 'POST', body: JSON.stringify({ username, password }) });
        }
        await api(isISpace ? '/api/login/ispace' : '/api/login', {
            method: 'POST',
            body: JSON.stringify({ username, password }),
        });
        closeAuth();
        $('auth-username').value = '';
        $('auth-password').value = '';
        await loadAccount();
        await loadOrders(false);
        toast(isISpace ? 'iSpace 登录成功，DDL 已同步' : (state.authMode === 'register' ? '注册成功' : '登录成功'));
    } catch (error) {
        showMessage($('auth-message'), error.message, 'error');
    } finally {
        setButtonBusy($('auth-submit'), false, '');
        setAuthProvider(state.authProvider);
    }
});

$('service-search').addEventListener('input', renderServices);
$('service-list').addEventListener('click', (event) => {
    const button = event.target.closest('[data-service]');
    if (!button) return;
    state.selectedService = state.services.find((service) => service.code === button.dataset.service) || null;
    state.selectedCountry = null;
    renderServices();
    updateCheckout();
    if (state.selectedService) loadCountries();
});
$('country-list').addEventListener('click', (event) => {
    const button = event.target.closest('[data-country]');
    if (!button) return;
    state.selectedCountry = state.countries.find((country) => country.id === Number(button.dataset.country)) || null;
    renderCountries();
    updateCheckout();
});
$('purchase-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!state.selectedService || !state.selectedCountry) return;
    if (!state.account.authenticated) {
        openAuth('login');
        return;
    }
    openPurchaseConfirm();
});

document.addEventListener('click', (event) => {
    if (event.target.closest('[data-open-auth]')) openAuth('login');
});
document.querySelectorAll('[data-order-filter]').forEach((button) => {
    button.addEventListener('click', () => {
        state.orderFilter = button.dataset.orderFilter;
        renderOrders();
    });
});
$('refresh-button').addEventListener('click', () => loadOrders(true));
$('activation-list').addEventListener('click', async (event) => {
    const button = event.target.closest('button[data-action]');
    const card = event.target.closest('[data-id]');
    if (!button || !card) return;
    const action = button.dataset.action;
    const orderId = Number(card.dataset.id);
    if (action === 'copy-phone') return copyText(button.dataset.value, '号码');
    if (action === 'copy-code') return copyText(button.dataset.value, '验证码');
    setButtonBusy(button, true, '处理中...');
    try {
        const result = await api(`/orders/${orderId}/${action}`, { method: 'POST', body: '{}' });
        if (action === 'cancel' && result.authenticated) state.account = result;
        await loadAccount();
        await loadOrders(false);
        toast(action === 'replace' ? '号码已更换' : action === 'finish' ? '订单已完成' : '订单已取消并退款');
    } catch (error) {
        showMessage($('activation-status'), error.message, 'error');
    } finally {
        setButtonBusy(button, false, '');
    }
});

if (window.location.protocol !== 'file:') {
    fetch('/api/analytics/track', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ view: 'sms-market', path: location.pathname, referrer: document.referrer }),
    }).catch(() => {});
}

Promise.all([api('/status'), loadAccount(), loadServices()]).then(async ([status]) => {
    state.status = status;
    if (!status.configured) showMessage($('config-message'), '号码供应服务暂未配置。', 'error');
    renderAccount();
    renderCountries();
    await loadOrders(false);
    startPolling();
}).catch((error) => {
    showMessage($('config-message'), error.message, 'error');
    renderOrders();
});
