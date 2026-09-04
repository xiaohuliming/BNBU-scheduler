// Load the unpacked release ZIP in an isolated persistent Chromium profile.
// Run on about:blank with playwright-cli run-code --filename <this file>.
// All HTTP(S) is intercepted: no request reaches MIS and no selection is made.
async (page) => {
  if (page.url() !== 'about:blank') throw new Error('Start in an isolated about:blank test window');
  const context = page.context();
  const worker = context.serviceWorkers().find(item => item.url().endsWith('/background.js'));
  if (!worker) throw new Error('Unpacked test extension not found');
  await context.route(/^https?:\/\//, async route => {
    if (route.request().method() !== 'GET') throw new Error('Unexpected mutation in read-only domain test');
    await route.fulfill({
      contentType: 'text/html',
      body: '<!doctype html><title>Offline MIS fixture</title><h1>Offline fixture</h1><table><tr><td>TEST1013</td><td>Fixture course (1001)</td><td><button disabled>Full</button></td></tr></table>'
    });
  });
  const results = [];
  for (const host of ['mis.bnbu.edu.cn', 'mis.uic.edu.cn']) {
    const url = `https://${host}/mis/student/es/index.do`;
    await page.goto(url);
    await page.locator('#bnbu-course-helper').waitFor({ state: 'visible' });
    const state = await worker.evaluate(async url => {
      const tabs = await chrome.tabs.query({ url });
      if (tabs.length !== 1) throw new Error('Fixture tab not found');
      return chrome.tabs.sendMessage(tabs[0].id, { type: 'get-state' });
    }, url);
    if (!state.ok || state.config.enabled || state.url !== url) throw new Error('Connection or stopped state is incorrect');
    if (state.config.minInterval !== 0.5 || state.config.maxInterval !== 0.5 || state.config.submitTimeout !== 10) throw new Error('Timing defaults changed');
    results.push({ host, connected: state.ok, stopped: !state.config.enabled });
  }
  for (const url of [
    'https://mis.bnbu.edu.cn/mis/student/as/dropSubject.do',
    'https://mis.bnbu.edu.cn.evil.example/mis/student/es/index.do'
  ]) {
    await page.goto(url);
    if (await page.locator('#bnbu-course-helper').count()) throw new Error('Injection escaped the selection scope');
    results.push({ url, injected: false });
  }
  await page.goto('about:blank');
  return results;
}
