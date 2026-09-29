/* Local PDF rendering only. No document URLs, actions, or attachments are opened. */
'use strict';
(() => {
  const ROOT = '/vendor/pdfjs-6.3.289/';
  let library;
  const getLibrary = () => library ||= import(ROOT + 'pdf.mjs').then(pdfjs => {
    pdfjs.GlobalWorkerOptions.workerSrc = ROOT + 'pdf.worker.mjs';
    return pdfjs;
  }).catch(() => { library = null; throw new Error('预览组件加载失败，请检查网络后刷新页面。'); });
  const cancelled = () => new DOMException('Preview replaced', 'AbortError');

  window.MaxcoursePdfPreview = class {
    constructor(canvas, host, onChange) {
      this.canvas = canvas;
      this.host = host;
      this.onChange = onChange;
      this.generation = 0;
      this.drawGeneration = 0;
      this.page = 1;
      this.pages = 0;
      this.zoom = 'fit';
      this.ready = false;
      this.resizer = new ResizeObserver(() => {
        clearTimeout(this.resizeTimer);
        if (this.document && this.zoom === 'fit') {
          this.resizeTimer = setTimeout(() => this.render().catch(() => {}), 120);
        }
      });
      this.resizer.observe(host);
    }
    update(rendering = false, error = '') {
      this.onChange({ page: this.page, pages: this.pages, ready: this.ready, rendering, error, zoom: this.zoom });
    }
    clear() {
      this.generation++;
      this.drawGeneration++;
      clearTimeout(this.resizeTimer);
      this.renderTask?.cancel();
      this.renderTask = null;
      const oldTask = this.loadingTask;
      this.loadingTask = this.document = null;
      if (oldTask) oldTask.destroy().catch(() => {});
      this.page = 1; this.pages = 0; this.zoom = 'fit'; this.ready = false;
      this.canvas.width = this.canvas.height = 0;
      this.canvas.removeAttribute('data-page');
      this.update();
    }
    async load(file, maxPages) {
      this.clear();
      const generation = this.generation;
      const [pdfjs, buffer] = await Promise.all([getLibrary(), file.arrayBuffer()]);
      if (generation !== this.generation) throw cancelled();
      let passwordProtected = false;
      const task = pdfjs.getDocument({
        data: new Uint8Array(buffer),
        cMapUrl: ROOT + 'cmaps/', cMapPacked: true,
        standardFontDataUrl: ROOT + 'standard_fonts/', wasmUrl: ROOT + 'wasm/',
        enableXfa: false, useSystemFonts: false, disableAutoFetch: true,
        stopAtErrors: true, verbosity: 0, canvasMaxAreaInBytes: 16777216,
      });
      this.loadingTask = task;
      task.onPassword = () => { passwordProtected = true; task.destroy().catch(() => {}); };
      try {
        const doc = await task.promise;
        if (generation !== this.generation) throw cancelled();
        if (!doc.numPages || doc.numPages > maxPages) {
          throw new Error(doc.numPages > maxPages ? `这份 PDF 有 ${doc.numPages} 页，最多支持 ${maxPages} 页。` : '这份 PDF 没有可打印的页面。');
        }
        this.document = doc;
        this.pdfjs = pdfjs;
        this.pages = doc.numPages;
        // The application exposes the preview container before this first draw.
        await this.render();
        if (generation !== this.generation) throw cancelled();
        return { pages: doc.numPages };
      } catch (error) {
        if (generation !== this.generation) throw cancelled();
        this.clear();
        if (passwordProtected) throw new Error('暂不支持加密 PDF，请先导出未加密的文件。');
        if (error.name === 'AbortError') throw error;
        if (error.name === 'InvalidPDFException') throw new Error('无法读取这份 PDF，请重新导出后再试。');
        throw error;
      }
    }
    async setPage(value) {
      if (!this.document) return;
      const page = Math.max(1, Math.min(this.pages, Math.trunc(Number(value)) || 1));
      if (page === this.page && this.ready) { this.update(); return; }
      this.page = page;
      await this.render();
      this.host.scrollTo({ top: 0, left: 0, behavior: 'instant' });
    }
    async setZoom(value) {
      if (!['fit', '1', '1.5', '2'].includes(value)) return;
      this.zoom = value;
      await this.render();
    }
    async render() {
      if (!this.document) return;
      const generation = this.generation;
      const draw = ++this.drawGeneration;
      const number = this.page;
      const oldRender = this.renderTask;
      oldRender?.cancel();
      this.ready = false;
      this.update(true);
      try {
        if (oldRender) await oldRender.promise.catch(() => {});
        const page = await this.document.getPage(number);
        if (generation !== this.generation || draw !== this.drawGeneration) return;
        const original = page.getViewport({ scale: 1 });
        const fit = Math.max(.15, Math.min((this.host.clientWidth - 48) / 595.28, (this.host.clientHeight - 48) / 841.89, 1));
        const scale = this.zoom === 'fit' ? fit : Number(this.zoom);
        const ratio = Math.min(window.devicePixelRatio || 1, 2);
        const pageFit = Math.min(595.28 / original.width, 841.89 / original.height);
        const viewport = page.getViewport({ scale: pageFit * scale * ratio });
        const width = Math.ceil(595.28 * scale * ratio);
        const height = Math.ceil(841.89 * scale * ratio);
        this.canvas.width = width; this.canvas.height = height;
        this.canvas.style.width = (width / ratio) + 'px';
        this.canvas.style.height = (height / ratio) + 'px';
        const context = this.canvas.getContext('2d', { alpha: false });
        const render = page.render({
          canvasContext: context, viewport,
          transform: [1, 0, 0, 1, (width - viewport.width) / 2, (height - viewport.height) / 2],
          background: '#ffffff', intent: 'print', annotationMode: this.pdfjs.AnnotationMode.ENABLE,
        });
        this.renderTask = render;
        await render.promise;
        if (generation !== this.generation || draw !== this.drawGeneration) return;
        this.ready = true;
        this.canvas.dataset.page = String(number);
        this.canvas.setAttribute('aria-label', `文档第 ${number} 页，共 ${this.pages} 页，A4 黑白预览`);
        this.update();
      } catch (error) {
        if (generation !== this.generation || draw !== this.drawGeneration || error.name === 'RenderingCancelledException') return;
        this.ready = false;
        this.update(false, '这一页暂时无法预览，请重新选择文件。');
        throw error;
      }
    }
  };
})();
