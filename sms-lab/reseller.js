'use strict';

function createModalFocusManager({ modal, backgrounds, document: documentApi }) {
    let active = false;
    let returnFocus = null;
    const focusableElements = () => Array.from(modal.querySelectorAll(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )).filter((element) => !element.disabled);
    return {
        open(trigger) {
            if (!active) returnFocus = trigger || documentApi.activeElement;
            active = true;
            backgrounds.forEach((element) => { if (element) element.inert = true; });
            const first = focusableElements()[0];
            if (first) first.focus();
        },
        close() {
            if (!active) return;
            active = false;
            backgrounds.forEach((element) => { if (element) element.inert = false; });
            if (returnFocus && typeof returnFocus.focus === 'function') returnFocus.focus();
            returnFocus = null;
        },
        handleKeydown(event) {
            if (!active || event.key !== 'Tab') return false;
            const focusable = focusableElements();
            if (!focusable.length) return false;
            const first = focusable[0];
            const last = focusable[focusable.length - 1];
            if (event.shiftKey && documentApi.activeElement === first) {
                event.preventDefault();
                last.focus();
                return true;
            }
            if (!event.shiftKey && (documentApi.activeElement === last
                    || !modal.contains(documentApi.activeElement))) {
                event.preventDefault();
                first.focus();
                return true;
            }
            return false;
        },
    };
}

function createRechargeController(dependencies) {
    const terminalStatuses = new Set(['credited', 'rejected', 'failed', 'cancelled', 'expired']);
    const retryDelays = [1000, 2500, 5000];
    const request = dependencies.request;
    const storage = dependencies.storage;
    const makeRequestId = dependencies.makeRequestId;
    const setTimer = dependencies.setTimer || setTimeout;
    const clearTimer = dependencies.clearTimer || clearTimeout;
    const onCheckout = dependencies.onCheckout || (() => {});
    const onWallet = dependencies.onWallet || (() => {});
    const onSuccess = dependencies.onSuccess || (() => {});
    const onView = dependencies.onView || (() => {});
    const onOpen = dependencies.onOpen || (() => {});
    const onClose = dependencies.onClose || (() => {});
    const view = {
        visible: false,
        busy: false,
        selectedPackage: 5,
        status: null,
        message: '',
        order: null,
        orders: [],
    };
    let username = null;
    let accountEpoch = 0;
    let modalEpoch = 0;
    let createPromise = null;
    let pollTimer = null;
    let activeOrderId = null;
    let pendingRecovery = null;
    let detailRevision = 0;
    const successfulOrders = new Set();

    const snapshot = () => ({ ...view, orders: view.orders.slice() });
    const emit = () => onView(snapshot());
    const pendingKey = (packageUsd) => `sms-market-recharge:${encodeURIComponent(username)}:${packageUsd}`;
    const validPackage = (value) => [1, 5, 10].includes(value);
    const isCurrent = (account, modal) => account === accountEpoch && modal === modalEpoch && view.visible;
    const stopPolling = () => {
        if (pollTimer !== null) clearTimer(pollTimer);
        pollTimer = null;
    };
    const trustedCheckout = (value) => {
        try {
            const parsed = new URL(value);
            return parsed.protocol === 'https:' && parsed.hostname === 'chat.bnbscheduler.top'
                && (parsed.port === '' || parsed.port === '443')
                && !parsed.username && !parsed.password;
        } catch (_) {
            return false;
        }
    };
    const clearRequestId = (order) => {
        if (!order || !terminalStatuses.has(order.status)) return;
        const packageUsd = Number(order.wallet_units) / 10000;
        if (!validPackage(packageUsd)) return;
        const key = pendingKey(packageUsd);
        if (storage.getItem(key) === order.request_id) storage.removeItem(key);
    };
    const applyDetail = (payload) => {
        const order = payload.order;
        detailRevision += 1;
        view.order = order;
        const existingIndex = view.orders.findIndex((item) => item.id === order.id);
        if (existingIndex === -1) view.orders.unshift(order);
        else view.orders[existingIndex] = order;
        view.status = order.status;
        view.message = '';
        activeOrderId = terminalStatuses.has(order.status) ? null : order.id;
        if (!activeOrderId) stopPolling();
        if (payload.wallet_balance !== undefined) onWallet(payload.wallet_balance);
        clearRequestId(order);
        if (order.status === 'credited' && !successfulOrders.has(order.id)) {
            successfulOrders.add(order.id);
            onSuccess(order);
        }
        emit();
        return order;
    };

    const controller = {
        getState: snapshot,
        getUsername: () => username,
        setAccount(nextUsername) {
            const normalized = typeof nextUsername === 'string' && nextUsername.trim()
                ? nextUsername.trim() : null;
            if (normalized === username) return;
            const wasVisible = view.visible;
            username = normalized;
            accountEpoch += 1;
            modalEpoch += 1;
            stopPolling();
            createPromise = null;
            activeOrderId = null;
            view.visible = false;
            view.busy = false;
            view.status = null;
            view.message = '';
            view.order = null;
            view.orders = [];
            if (wasVisible) onClose();
            emit();
            if (username && pendingRecovery) {
                const recovery = pendingRecovery;
                pendingRecovery = null;
                controller.recoverFromUrl(recovery.urlValue, recovery.historyApi).catch(() => {});
            }
        },
        open(trigger, startPolling = true) {
            if (!username) return false;
            const becomingVisible = !view.visible;
            if (becomingVisible) view.visible = true;
            view.message = '';
            emit();
            if (becomingVisible) onOpen(trigger);
            if (startPolling && activeOrderId) return controller.poll(activeOrderId);
            return true;
        },
        openWithRecent(trigger) {
            modalEpoch += 1;
            stopPolling();
            const opening = controller.open(trigger, false);
            if (!opening) return Promise.resolve(null);
            const account = accountEpoch;
            const modal = modalEpoch;
            return controller.loadRecent().catch(() => null).then(() => {
                if (isCurrent(account, modal) && activeOrderId) return controller.poll(activeOrderId);
                return null;
            });
        },
        close() {
            const wasVisible = view.visible;
            modalEpoch += 1;
            stopPolling();
            view.visible = false;
            if (wasVisible) onClose();
            emit();
        },
        select(packageUsd) {
            if (!validPackage(packageUsd) || view.busy) return false;
            view.selectedPackage = packageUsd;
            emit();
            return true;
        },
        create(packageUsd = view.selectedPackage) {
            if (createPromise) return createPromise;
            if (!username || !view.visible || !validPackage(packageUsd)) {
                return Promise.reject(new Error('请选择有效的充值套餐。'));
            }
            view.selectedPackage = packageUsd;
            const key = pendingKey(packageUsd);
            let requestId = storage.getItem(key);
            if (!requestId) {
                requestId = makeRequestId();
                storage.setItem(key, requestId);
            }
            const account = accountEpoch;
            const modal = modalEpoch;
            view.busy = true;
            view.status = 'creating';
            view.message = '';
            emit();
            let operation;
            try {
                operation = request('/recharge/orders', {
                    method: 'POST',
                    body: JSON.stringify({ package_usd: packageUsd, request_id: requestId }),
                });
            } catch (error) {
                operation = Promise.reject(error);
            }
            const promise = Promise.resolve(operation).then((payload) => {
                if (!isCurrent(account, modal)) return payload;
                view.order = payload.order;
                view.status = payload.order.status === 'credited' ? 'paid' : payload.order.status;
                activeOrderId = terminalStatuses.has(payload.order.status) ? null : payload.order.id;
                clearRequestId(payload.order);
                if (terminalStatuses.has(payload.order.status)) {
                    stopPolling();
                    emit();
                    if (payload.order.status === 'credited') {
                        return controller.poll(payload.order.id).then(() => payload);
                    }
                } else if (trustedCheckout(payload.checkout_url)) {
                    onCheckout(payload.checkout_url);
                } else {
                    view.status = 'invalid_checkout';
                    view.message = '支付链接无效，请稍后重试。';
                    emit();
                }
                return payload;
            }).catch((error) => {
                if (isCurrent(account, modal)) {
                    view.status = 'error';
                    view.message = error.message || '充值请求失败，请稍后重试。';
                    emit();
                }
                throw error;
            }).finally(() => {
                if (createPromise === promise) createPromise = null;
                if (account === accountEpoch) {
                    view.busy = false;
                    emit();
                }
            });
            createPromise = promise;
            return promise;
        },
        poll(orderId, retryAttempt = 0) {
            if (!username || !view.visible || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$/.test(orderId)) {
                return Promise.resolve(null);
            }
            stopPolling();
            activeOrderId = orderId;
            const account = accountEpoch;
            const modal = modalEpoch;
            return Promise.resolve(request(`/recharge/orders/${encodeURIComponent(orderId)}`)).then((payload) => {
                if (!isCurrent(account, modal)) return payload;
                const order = applyDetail(payload);
                if (!terminalStatuses.has(order.status)) {
                    pollTimer = setTimer(() => {
                        pollTimer = null;
                        controller.poll(orderId, 0).catch(() => {});
                    }, 2500);
                }
                return payload;
            }).catch((error) => {
                if (isCurrent(account, modal)) {
                    view.status = 'error';
                    view.message = error.message || '充值状态读取失败，请稍后重试。';
                    emit();
                    const recoverable = error.status === undefined || Number(error.status) >= 500;
                    if (recoverable && retryAttempt < retryDelays.length) {
                        pollTimer = setTimer(() => {
                            pollTimer = null;
                            controller.poll(orderId, retryAttempt + 1).catch(() => {});
                        }, retryDelays[retryAttempt]);
                    }
                }
                throw error;
            });
        },
        loadRecent() {
            if (!username || !view.visible) return Promise.resolve(null);
            const account = accountEpoch;
            const modal = modalEpoch;
            const revision = detailRevision;
            return Promise.resolve(request('/recharge/orders')).then((payload) => {
                if (!isCurrent(account, modal) || revision !== detailRevision) return payload;
                view.orders = Array.isArray(payload.orders) ? payload.orders : [];
                if (payload.wallet_balance !== undefined) onWallet(payload.wallet_balance);
                view.orders.forEach(clearRequestId);
                emit();
                return payload;
            }).catch((error) => {
                if (isCurrent(account, modal) && revision === detailRevision) {
                    view.status = 'error';
                    view.message = error.message || '充值记录读取失败，请稍后重试。';
                    emit();
                }
                throw error;
            });
        },
        recoverFromUrl(urlValue, historyApi) {
            let parsed;
            try { parsed = new URL(urlValue); } catch (_) { return Promise.resolve(null); }
            const orderId = parsed.searchParams.get('recharge_order');
            if (!orderId || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$/.test(orderId)) {
                return Promise.resolve(null);
            }
            if (!username) {
                pendingRecovery = { urlValue, historyApi };
                return Promise.resolve(null);
            }
            pendingRecovery = null;
            activeOrderId = orderId;
            parsed.searchParams.delete('recharge_order');
            historyApi.replaceState(null, '', parsed.pathname + parsed.search + parsed.hash);
            return controller.openWithRecent().catch(() => null);
        },
    };
    emit();
    return controller;
}

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
    cancelCountdownTimer: null,
    busy: false,
    authMode: 'login',
    purchaseUsesTrial: false,
    authProvider: 'local',
};
let recharge = null;

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
    button.dataset.busy = String(busy);
    button.disabled = busy;
    button.textContent = busy ? label : button.dataset.label;
};
const formatTime = (value) => {
    if (!value) return '';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', { hour12: false });
};

const freeTrialApplies = (price) => !!state.account.authenticated
    && !!state.account.free_trial?.available && Number.isFinite(price) && price > 0
    && price <= Number(state.account.free_trial.max_price);

const renderTrialNotice = () => {
    const trial = state.account.free_trial;
    const notice = $('trial-notice');
    if (!state.account.authenticated || !trial || trial.status === 'used') {
        hideMessage(notice);
        return;
    }
    showMessage(notice, trial.available
        ? `新用户免费接码 1 次 · 限售价 ${Number(trial.max_price).toFixed(2)} USD 以内。未收到验证码并成功取消后可重试。`
        : '接码订单正在进行中，结束后会更新免费体验资格。');
};

const renderAccount = () => {
    const authenticated = !!state.account.authenticated;
    $('account-guest').classList.toggle('hidden', authenticated);
    $('account-user').classList.toggle('hidden', !authenticated);
    if (authenticated) {
        $('account-name').textContent = state.account.user.display_name;
        $('wallet-balance').textContent = Number(state.account.wallet.balance).toFixed(4);
    }
    if (recharge) recharge.setAccount(authenticated ? state.account.user.username : null);
    renderTrialNotice();
    updateCheckout();
};

const loadAccount = async () => {
    state.account = await api('/account');
    renderAccount();
    return state.account;
};

const renderServices = () => {
    const query = $('service-search').value;
    const matches = state.services.filter((service) => SMSServiceSearch.matches(service, query));
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
            <span class="country-meta">${country.stock} 个 · ${Number(country.price).toFixed(4)} USD${freeTrialApplies(Number(country.price)) ? ' · 可免费体验' : ''}</span>
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
        $('wallet-hint').textContent = state.account.free_trial?.available
            ? '选择售价不超过 0.50 USD 的号码，即可免费体验一次'
            : state.account.authenticated ? '可先在线充值，再选择服务和国家购买号码' : '登录后可使用站内钱包购买';
        $('wallet-helper-recharge').classList.toggle('hidden', !state.account.authenticated || !!state.account.free_trial?.available);
        return;
    }
    const price = Number(country.price);
    $('selection-name').textContent = `${service.name} · ${country.name}`;
    $('selection-price').textContent = `${price.toFixed(4)} USD`;
    if (!state.account.authenticated) {
        $('purchase-button').textContent = '登录后购买';
        $('purchase-button').disabled = false;
        $('wallet-hint').textContent = '登录或注册后继续';
        $('wallet-helper-recharge').classList.add('hidden');
        return;
    }
    if (freeTrialApplies(price)) {
        $('selection-price').textContent = '0.0000 USD · 免费体验';
        $('purchase-button').textContent = '免费接码 · 1 次';
        $('purchase-button').disabled = false;
        $('wallet-hint').textContent = '本单使用新用户免费体验，钱包不扣款';
        $('wallet-helper-recharge').classList.add('hidden');
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
        : state.account.free_trial?.available ? '本单超过 0.50 USD 体验上限，请选择更低价格或充值购买'
            : '余额不足，可在线充值后继续购买';
    $('wallet-helper-recharge').classList.toggle('hidden', enough);
};

const orderStatusLabel = (status) => ({
    purchasing: '正在下单', active: '等待短信', code_received: '已收到验证码',
    completed: '已完成', cancelled: '已退款', failed: '购买失败', refunded: '已退款',
})[status] || '处理中';

const updateCancelCountdowns = () => {
    const now = Date.now();
    $('activation-list').querySelectorAll('button[data-cancel-at]').forEach((button) => {
        if (button.dataset.busy === 'true') return;
        const deadline = Number(button.dataset.cancelAt);
        const remaining = Number.isFinite(deadline)
            ? Math.max(0, Math.ceil((deadline - now) / 1000)) : 0;
        const label = remaining > 0 ? `${remaining} 秒后可取消` : (button.dataset.cancelLabel || '取消并退款');
        if (button.textContent !== label) button.textContent = label;
        button.dataset.label = label;
        button.disabled = remaining > 0;
    });
};

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
        const cancelAt = Number.isFinite(createdAt) && createdAt > 0 ? createdAt + 120000 : 0;
        const cancelWait = Math.max(0, Math.ceil((cancelAt - Date.now()) / 1000));
        return `<article class="activation-card" data-id="${order.id}">
            <div class="card-head">
                <div class="order-service">${logoMarkup(order.service, 'order-logo')}<div><strong>${escapeHtml(order.service.name)}</strong><span>${escapeHtml(order.country.name)} · ${order.is_free_trial ? '免费体验 · 实付 0 USD' : Number(order.sale_price).toFixed(4) + ' USD'}</span></div></div>
                <span class="status-badge">${escapeHtml(order.is_free_trial && order.status === 'cancelled' ? '已取消' : orderStatusLabel(order.status))}</span>
            </div>
            ${order.phone ? `<div class="phone">+${escapeHtml(order.phone.replace(/^\+/, ''))}</div>` : ''}
            <div class="card-meta">订单 #${order.id} · ${escapeHtml(formatTime(order.createdAt))}</div>
            ${otp ? `<div class="otp"><div class="otp-code">${escapeHtml(otp.smsCode || '已收到')}</div><div class="otp-text">${escapeHtml(otp.smsText)}${otp.receivedAt ? '<br>' + escapeHtml(formatTime(otp.receivedAt)) : ''}</div></div>` : (ACTIVE_STATUSES.has(order.status) ? '<div class="waiting"><span class="spinner" aria-hidden="true"></span>每 5 秒自动检查一次新短信</div>' : '')}
            <div class="actions">
                ${order.phone ? `<button class="btn btn-small" type="button" data-action="copy-phone" data-value="+${escapeHtml(order.phone.replace(/^\+/, ''))}">复制号码</button>` : ''}
                ${otp?.smsCode ? `<button class="btn btn-small" type="button" data-action="copy-code" data-value="${escapeHtml(otp.smsCode)}">复制验证码</button>` : ''}
                ${order.can_finish ? '<button class="btn btn-small" type="button" data-action="finish">完成</button>' : ''}
                ${order.can_replace ? '<button class="btn btn-small" type="button" data-action="replace">换号</button>' : ''}
                ${order.can_cancel ? `<button class="btn btn-small btn-danger" type="button" data-action="cancel" data-cancel-at="${cancelAt}" data-cancel-label="${order.is_free_trial ? '取消体验订单' : '取消并退款'}" ${cancelWait ? 'disabled' : ''}>${cancelWait ? cancelWait + ' 秒后可取消' : order.is_free_trial ? '取消体验订单' : '取消并退款'}</button>` : ''}
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
        if (data.free_trial) {
            state.account.free_trial = data.free_trial;
            renderTrialNotice();
            renderCountries();
            updateCheckout();
        }
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
    if (state.cancelCountdownTimer) window.clearInterval(state.cancelCountdownTimer);
    updateCancelCountdowns();
    state.cancelCountdownTimer = window.setInterval(() => {
        if (!document.hidden) updateCancelCountdowns();
    }, 1000);
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
    state.purchaseUsesTrial = freeTrialApplies(Number(state.selectedCountry.price));
    $('confirm-price').textContent = state.purchaseUsesTrial ? '0.0000 USD · 免费体验' : `${Number(state.selectedCountry.price).toFixed(4)} USD`;
    $('purchase-explainer').textContent = state.purchaseUsesTrial
        ? '本单免费，钱包不扣款。收到验证码后消耗体验机会；未收到验证码并成功取消后可重试。'
        : '下单后将从站内钱包扣款。未收到验证码且供应商接受取消时，款项会退回钱包。';
    $('purchase-confirm').textContent = state.purchaseUsesTrial ? '确认免费接码' : '确认付款';
    $('purchase-confirm').dataset.label = $('purchase-confirm').textContent;
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
                use_free_trial: state.purchaseUsesTrial,
            }),
        });
        state.account.wallet.balance = data.wallet_balance;
        if (data.free_trial) state.account.free_trial = data.free_trial;
        closePurchaseConfirm();
        renderAccount();
        await loadOrders(false);
        toast('号码已购买，正在等待短信');
    } catch (error) {
        closePurchaseConfirm();
        showMessage($('activation-status'), error.message, 'error');
        if (error.code === 'trial_unavailable') await loadAccount().catch(() => {});
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

const rechargeStatusText = (view) => ({
    creating: '正在创建充值订单，请不要重复提交。',
    pending: '订单待支付。完成支付后返回本页，我们会自动确认到账。',
    paid: '支付结果已收到，正在确认钱包到账。',
    credited: '充值成功，钱包余额已更新。',
    failed: '本次充值失败，钱包未增加。请重新选择套餐。',
    rejected: '本次充值失败，钱包未增加。请重新选择套餐并创建订单。',
    cancelled: '充值订单已取消。',
    expired: '充值订单已过期，请重新创建。',
    invalid_checkout: view.message,
    error: view.message,
})[view.status] || view.message;

const rechargeStatusName = (status) => ({
    pending: '待支付', paid: '确认中', credited: '已到账', failed: '失败', rejected: '充值失败',
    cancelled: '已取消', expired: '已过期',
})[status] || '处理中';

const renderRecharge = (view) => {
    $('recharge-modal').classList.toggle('hidden', !view.visible);
    document.querySelectorAll('[data-recharge-package]').forEach((button) => {
        const selected = Number(button.dataset.rechargePackage) === view.selectedPackage;
        button.classList.toggle('selected', selected);
        button.setAttribute('aria-pressed', String(selected));
        button.disabled = view.busy;
    });
    $('recharge-confirm').disabled = view.busy;
    $('recharge-confirm').textContent = view.busy
        ? '正在创建订单...'
        : `前往支付 · $${view.selectedPackage}`;
    const statusText = rechargeStatusText(view);
    $('recharge-status').textContent = statusText || '';
    $('recharge-status').className = 'recharge-status' + (statusText ? '' : ' hidden')
        + (view.status === 'credited' ? ' success' : '')
        + (['rejected', 'failed', 'error', 'invalid_checkout'].includes(view.status) ? ' error' : '');
    $('recharge-recent').innerHTML = view.orders.length ? view.orders.map((order) => `
        <li class="recharge-order-row">
            <span><strong>$${escapeHtml(order.wallet_amount)}</strong><small>${escapeHtml(formatTime(order.created_at))}</small></span>
            <span class="recharge-order-status">${escapeHtml(rechargeStatusName(order.status))}</span>
        </li>
    `).join('') : '<li class="recharge-empty">暂无充值记录</li>';
};

const rechargeFocus = createModalFocusManager({
    modal: $('recharge-modal'),
    backgrounds: [document.querySelector('.app-header'), document.querySelector('main'), document.querySelector('.footer')],
    document,
});

recharge = createRechargeController({
    request: (path, options) => api(path, options),
    storage: window.localStorage,
    makeRequestId: () => {
        const random = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}_${Math.random()}`;
        return `sms_${random}`.replace(/[^A-Za-z0-9_-]/g, '_');
    },
    setTimer: (callback, delay) => window.setTimeout(callback, delay),
    clearTimer: (timer) => window.clearTimeout(timer),
    onCheckout: (url) => window.location.assign(url),
    onWallet: (balance) => {
        if (!state.account.authenticated) return;
        state.account.wallet.balance = balance;
        renderAccount();
    },
    onSuccess: () => toast('充值成功，钱包余额已更新'),
    onView: renderRecharge,
    onOpen: (trigger) => rechargeFocus.open(trigger || $('recharge-button')),
    onClose: () => rechargeFocus.close(),
});

const openRecharge = () => {
    if (!state.account.authenticated) {
        openAuth('login');
        return;
    }
    recharge.openWithRecent(document.activeElement).catch(() => {});
};
const closeRecharge = () => recharge.close();

$('login-button').addEventListener('click', () => openAuth('login'));
$('recharge-button').addEventListener('click', openRecharge);
$('wallet-helper-recharge').addEventListener('click', openRecharge);
$('recharge-close').addEventListener('click', closeRecharge);
$('recharge-modal').addEventListener('click', (event) => {
    if (event.target === $('recharge-modal')) closeRecharge();
});
$('recharge-packages').addEventListener('click', (event) => {
    const button = event.target.closest('[data-recharge-package]');
    if (button) recharge.select(Number(button.dataset.rechargePackage));
});
$('recharge-confirm').addEventListener('click', () => {
    recharge.create().catch(() => {});
});
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
document.addEventListener('keydown', (event) => {
    if (!recharge.getState().visible) return;
    if (event.key === 'Escape') closeRecharge();
    else rechargeFocus.handleKeydown(event);
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
document.addEventListener('visibilitychange', () => {
    if (!document.hidden) updateCancelCountdowns();
});
$('activation-list').addEventListener('click', async (event) => {
    const button = event.target.closest('button[data-action]');
    const card = event.target.closest('[data-id]');
    if (!button || !card) return;
    const action = button.dataset.action;
    const orderId = Number(card.dataset.id);
    const isTrialOrder = state.orders.find((order) => order.id === orderId)?.is_free_trial;
    if (action === 'copy-phone') return copyText(button.dataset.value, '号码');
    if (action === 'copy-code') return copyText(button.dataset.value, '验证码');
    setButtonBusy(button, true, '处理中...');
    try {
        const result = await api(`/orders/${orderId}/${action}`, { method: 'POST', body: '{}' });
        if (action === 'cancel' && result.authenticated) state.account = result;
        await loadAccount();
        await loadOrders(false);
        toast(action === 'replace' ? '号码已更换' : action === 'finish' ? '订单已完成'
            : isTrialOrder ? '体验订单已取消，资格已更新' : '订单已取消并退款');
    } catch (error) {
        showMessage($('activation-status'), error.message, 'error');
    } finally {
        setButtonBusy(button, false, '');
        updateCancelCountdowns();
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
    await recharge.recoverFromUrl(window.location.href, window.history);
    startPolling();
}).catch((error) => {
    showMessage($('config-message'), error.message, 'error');
    renderOrders();
});
