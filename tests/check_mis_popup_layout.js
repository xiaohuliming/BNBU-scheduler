// Browser regression for the popup.html extracted from the distributed ZIP.
// Serve the extracted extension locally, navigate there with playwright-cli,
// then run: playwright-cli run-code --filename tests/check_mis_popup_layout.js
// This test never opens MIS or sends a course-selection request.
async (page) => {
  // Routing disables HTTP cache so rebuilding the ZIP cannot test stale CSS.
  await page.route('**/*', route => route.continue());
  await page.addInitScript(() => {
    if (!window.chrome?.storage) {
      window.chrome = {
        storage: { local: { get: async () => ({}) } },
        tabs: { query: async () => [] }
      };
    }
  });
  await page.reload();
  const results = [];
  for (const width of [25, 156, 320, 408, 600, 156]) {
    await page.setViewportSize({ width, height: 600 });
    const measured = await page.evaluate(() => {
      const rect = selector => document.querySelector(selector).getBoundingClientRect();
      const header = rect('.app-header');
      const title = rect('.brand-copy');
      const status = rect('.state-pill');
      const field = rect('#targets');
      const dates = [...document.querySelectorAll('.grid.two input')].map(el => el.getBoundingClientRect());
      return {
        viewport: innerWidth,
        rootWidth: document.documentElement.getBoundingClientRect().width,
        bodyWidth: document.body.getBoundingClientRect().width,
        titleWidth: title.width,
        titleOverlapsStatus: title.right > status.left,
        statusOutsideHeader: status.right > header.right,
        targetWidth: field.width,
        datesSideBySide: dates[0].top === dates[1].top,
        minInterval: document.querySelector('#min-interval').value,
        maxInterval: document.querySelector('#max-interval').value,
        submitTimeout: document.querySelector('#submit-timeout').value
      };
    });
    if (measured.rootWidth !== 408 || measured.bodyWidth !== 408 ||
        measured.titleWidth < 180 || measured.titleOverlapsStatus ||
        measured.statusOutsideHeader || measured.targetWidth < 300 ||
        !measured.datesSideBySide) {
      throw new Error('Popup collapsed or overlaps: ' + JSON.stringify(measured));
    }
    if (measured.minInterval !== '0.5' || measured.maxInterval !== '0.5' ||
        measured.submitTimeout !== '10') {
      throw new Error('Timing defaults changed: ' + JSON.stringify(measured));
    }
    results.push(measured);
  }
  await page.setViewportSize({ width: 408, height: 600 });
  await page.locator('#save').scrollIntoViewIfNeeded();
  if (!await page.locator('#save').isVisible()) throw new Error('Bottom action is unreachable');
  return results;
}
