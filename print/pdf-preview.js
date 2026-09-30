/* Local PDF rendering only. No document URLs, actions, or attachments are opened. */
'use strict';
(() => {
  const ROOT = '/vendor/pdfjs-6.3.289/';
  const A4 = [595.28, 841.89];
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
      this.sideGeneration = 0;
      this.page = 1;
      this.pages = 0;
      this.zoom = 'fit';
      this.scale = 1;
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
      this.sideGeneration++;
      clearTimeout(this.resizeTimer);
      this.renderTask?.cancel();
      this.sideTask?.cancel();
      this.renderTask = this.sideTask = null;
      const oldTask = this.loadingTask;
      this.loadingTask = this.document = null;
      if (oldTask) oldTask.destroy().catch(() => {});
      this.page = 1; this.pages = 0; this.zoom = 'fit'; this.ready = false;
      this.canvas.width = this.canvas.height = 0;
      this.canvas.removeAttribute('data-page');
      this.update();
    }
    // The stage publishes how much room its chrome needs around a fitted page.
    fitScale() {
      const style = getComputedStyle(this.host);
      const padX = parseFloat(style.getPropertyValue('--fit-x')) || 48;
      const padY = parseFloat(style.getPropertyValue('--fit-y')) || 48;
      return Math.max(.15, Math.min((this.host.clientWidth - padX) / A4[0], (this.host.clientHeight - padY) / A4[1], 1));
    }
    // Size the empty sheet before the first draw so the page can animate into place.
    reserve() {
      if (this.document) return;
      const scale = this.fitScale();
      this.canvas.style.width = Math.round(A4[0] * scale) + 'px';
      this.canvas.style.height = Math.round(A4[1] * scale) + 'px';
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
    // Draw off screen, then swap, so page turns and zoom changes never flash blank.
    async paint(number, slot, current) {
      const page = await this.document.getPage(number);
      if (!current()) return null;
      const original = page.getViewport({ scale: 1 });
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      const pageFit = Math.min(A4[0] / original.width, A4[1] / original.height);
      const viewport = page.getViewport({ scale: pageFit * this.scale * ratio });
      const target = document.createElement('canvas');
      target.width = Math.ceil(A4[0] * this.scale * ratio);
      target.height = Math.ceil(A4[1] * this.scale * ratio);
      const task = page.render({
        canvasContext: target.getContext('2d', { alpha: false }), viewport,
        transform: [1, 0, 0, 1, (target.width - viewport.width) / 2, (target.height - viewport.height) / 2],
        background: '#ffffff', intent: 'print', annotationMode: this.pdfjs.AnnotationMode.ENABLE,
      });
      this[slot] = task;
      await task.promise;
      return current() ? { source: target, ratio } : null;
    }
    blit({ source, ratio }, target) {
      target.width = source.width;
      target.height = source.height;
      target.style.width = (source.width / ratio) + 'px';
      target.style.height = (source.height / ratio) + 'px';
      target.getContext('2d', { alpha: false }).drawImage(source, 0, 0);
      source.width = source.height = 0;
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
      const current = () => generation === this.generation && draw === this.drawGeneration;
      try {
        if (oldRender) await oldRender.promise.catch(() => {});
        if (!current()) return;
        this.scale = this.zoom === 'fit' ? this.fitScale() : Number(this.zoom);
        const result = await this.paint(number, 'renderTask', current);
        if (!result) return;
        this.blit(result, this.canvas);
        this.ready = true;
        this.canvas.dataset.page = String(number);
        this.canvas.setAttribute('aria-label', `文档第 ${number} 页，共 ${this.pages} 页`);
        this.update();
      } catch (error) {
        if (!current() || error.name === 'RenderingCancelledException') return;
        this.ready = false;
        this.update(false, '这一页暂时无法预览，请重新选择文件。');
        throw error;
      }
    }
    // Render the reverse side of a duplex sheet at the current scale; 0 is a blank back.
    async drawInto(target, number) {
      if (!this.document) return;
      const generation = this.generation;
      const side = ++this.sideGeneration;
      this.sideTask?.cancel();
      const current = () => generation === this.generation && side === this.sideGeneration;
      if (!number || number > this.pages) {
        target.width = this.canvas.width || 1;
        target.height = this.canvas.height || 1;
        const context = target.getContext('2d', { alpha: false });
        context.fillStyle = '#ffffff';
        context.fillRect(0, 0, target.width, target.height);
        target.removeAttribute('data-page');
        return;
      }
      try {
        const result = await this.paint(number, 'sideTask', current);
        if (!result) return;
        this.blit(result, target);
        target.dataset.page = String(number);
      } catch (error) {
        if (error?.name !== 'RenderingCancelledException') throw error;
      }
    }
  };
})();
