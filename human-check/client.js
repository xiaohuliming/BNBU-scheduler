/* Shared challenge recovery for the SPA and standalone tools. */
(() => {
  'use strict';
  if (window.MAXCOURSE_HUMAN_CHECK || !window.fetch) return;
  const nativeFetch = window.fetch.bind(window);
  let pending = null;
  let widgetLoading = null;

  async function status() {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await nativeFetch('/api/human/status', {
        credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
      });
      return response.ok && (await response.json()).verified === true;
    } catch { return false; }
    finally { clearTimeout(timeout); }
  }

  function loadWidget() {
    if (customElements.get('cap-widget')) return Promise.resolve();
    if (widgetLoading) return widgetLoading;
    window.CAP_CUSTOM_WASM_URL = '/vendor/cap/0.1.57/cap_wasm_bg.wasm';
    window.CAP_DISABLE_HAPTICS = true;
    window.CAP_DISABLE_WIDGET_REF = true;
    widgetLoading = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      const timeout = setTimeout(() => { script.remove(); reject(new Error('widget_timeout')); }, 15000);
      script.src = '/vendor/cap/0.1.57/cap.min.js';
      script.onload = () => {
        clearTimeout(timeout);
        customElements.get('cap-widget') ? resolve() : reject(new Error('widget_unavailable'));
      };
      script.onerror = () => { clearTimeout(timeout); script.remove(); reject(new Error('widget_load_failed')); };
      document.head.append(script);
    }).catch(error => { widgetLoading = null; throw error; });
    return widgetLoading;
  }

  function addStyle() {
    if (document.getElementById('mc-human-style')) return;
    const style = document.createElement('style');
    style.id = 'mc-human-style';
    style.textContent = `
      .mc-human-dialog{box-sizing:border-box;width:min(420px,calc(100vw - 32px));max-height:calc(100dvh - 32px);overflow:auto;border:2px solid #101820;border-radius:16px;padding:28px;background:#f4efe6;color:#101820;box-shadow:0 20px 60px #0003;font:15px/1.6 system-ui,-apple-system,sans-serif}
      .mc-human-dialog::backdrop{background:#10182099}
      .mc-human-dialog *{box-sizing:border-box}
      .mc-human-dialog h2{font:750 23px/1.3 system-ui;margin:8px 0 12px;color:#101820}
      .mc-human-dialog p{margin:0 0 20px;color:#525b60}
      .mc-human-dialog .mc-human-brand{font-size:11px;font-weight:750;letter-spacing:2px;color:#626b70}
      .mc-human-dialog .mc-human-status{font-size:13px;min-height:22px;margin:14px 0 0;overflow-wrap:anywhere}
      .mc-human-dialog .mc-human-status[data-error]{color:#a12a22}
      .mc-human-dialog .mc-human-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:16px;flex-wrap:wrap}
      .mc-human-dialog button{min-height:44px;border:1px solid #acb0ab;border-radius:8px;padding:9px 14px;background:white;color:#101820;font:600 14px system-ui;cursor:pointer}
      .mc-human-dialog button:focus-visible{outline:3px solid #466350;outline-offset:3px}
      .mc-human-dialog [hidden]{display:none!important}
      .mc-human-dialog cap-widget{display:block;--cap-widget-width:100%;--cap-widget-height:48px;--cap-widget-padding:14px;--cap-background:#fff;--cap-border-radius:10px;--cap-border-color:#acb0ab;--cap-color:#101820;--cap-checkbox-size:26px;--cap-font:system-ui}
      @media(max-width:360px){.mc-human-dialog{padding:22px}.mc-human-dialog h2{font-size:21px}}
    `;
    document.head.append(style);
  }

  async function showChallenge() {
    if (await status()) return true;
    if (!document.body) await new Promise(resolve => document.addEventListener('DOMContentLoaded', resolve, { once: true }));
    addStyle();
    return new Promise(resolve => {
      const previousFocus = document.activeElement;
      const previousOverflow = document.body.style.overflow;
      const dialog = document.createElement('dialog');
      dialog.className = 'mc-human-dialog';
      dialog.setAttribute('aria-labelledby', 'mc-human-title');
      dialog.innerHTML = `<div class="mc-human-brand">MAXCOURSE</div>
        <h2 id="mc-human-title">请验证后继续</h2>
        <p>这次访问触发了自动访问保护。完成验证后，会继续刚才的操作。</p>
        <div class="mc-human-widget"></div>
        <p class="mc-human-status" role="status" aria-live="polite">正在加载验证组件…</p>
        <div class="mc-human-actions"><button type="button" data-retry hidden>重新加载</button><button type="button" data-cancel>暂不验证</button></div>`;
      const message = dialog.querySelector('.mc-human-status');
      const retry = dialog.querySelector('[data-retry]');
      let finished = false;
      const finish = success => {
        if (finished) return;
        finished = true;
        dialog.close(); dialog.remove();
        document.body.style.overflow = previousOverflow;
        if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true });
        resolve(success);
      };
      const error = text => {
        message.textContent = text;
        message.setAttribute('data-error', '');
        retry.hidden = false;
      };
      async function mountWidget() {
        retry.hidden = true;
        message.removeAttribute('data-error');
        message.textContent = '正在加载验证组件…';
        const container = dialog.querySelector('.mc-human-widget');
        container.replaceChildren();
        try {
          await loadWidget();
          if (finished) return;
          const widget = document.createElement('cap-widget');
          widget.setAttribute('data-cap-api-endpoint', '/api/human/');
          widget.setAttribute('data-cap-worker-count', '2');
          const labels = {
            'initial-state': '点击验证', 'verifying-label': '正在验证…',
            'solved-label': '验证成功', 'error-label': '请重新验证',
            'verify-aria-label': '点击完成人机验证', 'verifying-aria-label': '正在验证，请稍候',
            'verified-aria-label': '验证成功', 'error-aria-label': '验证失败，请重试',
            'troubleshooting-label': '验证帮助', 'required-label': '请完成验证',
            'wasm-disabled': '启用 WebAssembly 可以更快完成验证',
          };
          for (const [key, label] of Object.entries(labels)) widget.setAttribute('data-cap-i18n-' + key, label);
          widget.addEventListener('error', () => error('验证未能完成，请重新加载后重试。'));
          widget.addEventListener('solve', async () => {
            message.textContent = '正在恢复访问…';
            if (await status()) finish(true);
            else if (!finished) error('未能保存验证结果，请允许本站 Cookie 后重试。');
          });
          container.append(widget);
          message.textContent = '验证通过后，15 分钟内通常无需重复验证。';
        } catch {
          if (!finished) error('验证组件加载失败，请检查网络后重试。');
        }
      }
      dialog.addEventListener('cancel', event => { event.preventDefault(); finish(false); });
      dialog.querySelector('[data-cancel]').onclick = () => finish(false);
      retry.onclick = mountWidget;
      document.body.append(dialog);
      document.body.style.overflow = 'hidden';
      dialog.showModal();
      mountWidget();
    });
  }

  function ensureVerified() {
    if (!pending) pending = showChallenge().finally(() => { pending = null; });
    return pending;
  }

  // Downloads use hidden same-origin frames to stream without buffering whole
  // videos in JS. Show their challenge in the visible parent, then resume only
  // the exact frame that requested it.
  window.addEventListener('message', async event => {
    if (event.origin !== location.origin || event.data?.type !== 'maxcourse-human-required') return;
    const frame = [...document.querySelectorAll('iframe')].find(el => el.contentWindow === event.source);
    if (!frame) return;
    const original = frame.src;
    const url = new URL(original, location.origin);
    if (url.origin !== location.origin || !url.pathname.startsWith('/api/media-dl/')) return;
    const success = await ensureVerified();
    if (frame.isConnected && frame.src === original) {
      frame.contentWindow.postMessage({ type: 'maxcourse-human-result', success }, location.origin);
    }
  });

  window.fetch = async function (input, options) {
    const request = new Request(input, options);
    const url = new URL(request.url);
    if (url.origin !== location.origin || !url.pathname.startsWith('/api/') || url.pathname.startsWith('/api/human/')) {
      return nativeFetch(request);
    }
    // A copy retains FormData, file uploads and POST bodies for exactly one
    // retry. Only our before-request challenge marker permits a replay.
    const retryRequest = request.clone();
    const response = await nativeFetch(request);
    if (![403, 429].includes(response.status) || response.headers.get('X-Maxcourse-Challenge') !== 'required') return response;
    if (request.signal.aborted) throw new DOMException('Request aborted', 'AbortError');
    if (!await ensureVerified()) return response;
    if (request.signal.aborted) throw new DOMException('Request aborted', 'AbortError');
    return nativeFetch(retryRequest);
  };

  window.MAXCOURSE_HUMAN_CHECK = async () => {
    if (window.parent !== window && typeof window.MAXCOURSE_VERIFY_NEXT === 'string') {
      window.parent.postMessage({ type: 'maxcourse-human-required' }, location.origin);
      return false;
    }
    if (!await ensureVerified()) return false;
    if (typeof window.MAXCOURSE_VERIFY_NEXT === 'string') {
      const target = new URL(window.MAXCOURSE_VERIFY_NEXT, location.origin);
      location.replace(target.origin === location.origin ? target.href : '/');
    }
    return true;
  };
  if (typeof window.MAXCOURSE_VERIFY_NEXT === 'string') {
    let navigated = false;
    window.addEventListener('message', event => {
      if (window.parent === window || event.source !== window.parent || event.origin !== location.origin ||
          event.data?.type !== 'maxcourse-human-result' || navigated) return;
      navigated = true;
      const target = new URL(window.MAXCOURSE_VERIFY_NEXT, location.origin);
      if (event.data.success === true && target.origin === location.origin) location.replace(target.href);
      else window.parent.postMessage({ type: 'media-dl-error', id: target.searchParams.get('feedback'),
        error: '未完成访问验证，请重新点击下载并完成验证。' }, location.origin);
    });
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', window.MAXCOURSE_HUMAN_CHECK, { once: true });
    else window.MAXCOURSE_HUMAN_CHECK();
  }
})();
