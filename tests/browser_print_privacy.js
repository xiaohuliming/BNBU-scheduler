async page => {
  const origin = new URL(page.url()).origin;
  if (new URL(origin).hostname !== '127.0.0.1') throw new Error('Use an isolated local application.');
  const assert = (ok, message) => { if (!ok) throw new Error(message); };
  const key = 'maxcourse.print.privacy', version = '2026-10-03.1';
  const requests = [], errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/**', route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    requests.push({path, method: request.method()});
    const data = path === '/api/print/session' ? {
      csrf_token: 'privacy-test-only', user: null,
      service: {enabled: true, ready: true, online: true, busy: false, demo: false, features: ['color', 'duplex', 'copies', 'convert', 'balance']},
      limits: {max_bytes: 52428800, max_pages: 300, max_impressions: 30000},
      capabilities: {copies: {max: 100}},
    } : {error: 'No mutations permitted in privacy acceptance checks'};
    return route.fulfill({status: path === '/api/print/session' ? 200 : 403, contentType: 'application/json', body: JSON.stringify(data)});
  });
  await page.evaluate(() => {localStorage.clear(); sessionStorage.clear();});
  await page.goto(origin + '/print/');
  await page.locator('#privacy-title').waitFor();
  assert(await page.locator('#print-workspace').isHidden(), 'Workspace flashed before acknowledgement');
  assert(await page.locator('#print-workspace').evaluate(node => node.inert), 'Workspace was not inert');
  assert(!await page.locator('#privacy-check').isChecked(), 'Acknowledgement was preselected');
  assert(await page.locator('#privacy-accept').isDisabled(), 'Unchecked acknowledgement could continue');
  assert(requests.length === 0, 'Print APIs started before acknowledgement');
  await page.evaluate(() => {
    window.dispatchEvent(new Event('online'));
    document.dispatchEvent(new Event('visibilitychange'));
    const input = document.getElementById('file-input');
    const transfer = new DataTransfer();
    transfer.items.add(new File(['synthetic-only'], 'private.docx'));
    input.files = transfer.files;
    input.dispatchEvent(new Event('change'));
    document.getElementById('submit-form').dispatchEvent(new Event('submit', {cancelable: true}));
    window.dispatchEvent(new DragEvent('drop', {dataTransfer: transfer, cancelable: true}));
    document.getElementById('balance-query').click();
  });
  await page.waitForTimeout(200);
  assert(requests.length === 0, 'A hidden control, drop or lifecycle event bypassed acknowledgement');

  await page.setViewportSize({width: 1440, height: 1000});
  await page.screenshot({path: 'output/print-privacy-desktop.png', fullPage: true});
  for (const width of [390, 320]) {
    await page.setViewportSize({width, height: 844});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Privacy page has mobile horizontal overflow');
    await page.locator('#privacy-choice').scrollIntoViewIfNeeded();
    assert(await page.locator('#privacy-accept').isVisible(), 'Mobile continue action inaccessible');
  }
  await page.setViewportSize({width: 390, height: 844});
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({path: 'output/print-privacy-mobile.png', fullPage: true});
  await page.locator('#privacy-check').focus();
  await page.keyboard.press('Space');
  assert(await page.locator('#privacy-accept').isEnabled(), 'Keyboard acknowledgement failed');
  await page.locator('#privacy-accept').focus();
  await page.keyboard.press('Enter');
  await page.getByText('打印服务已连接', {exact: true}).waitFor();
  assert(await page.locator('#privacy-notice').isHidden(), 'Acknowledgement did not enter workspace');
  assert(await page.evaluate(([key, version]) => localStorage.length === 1 && localStorage.getItem(key) === version && !sessionStorage.length, [key, version]), 'Acknowledgement stored extra personal data');
  assert(await page.locator('#pick-btn').evaluate(node => node === document.activeElement), 'Keyboard focus lost after entering');

  await page.goto(origin + '/print/');
  await page.getByText('打印服务已连接', {exact: true}).waitFor();
  assert(await page.locator('#privacy-notice').isHidden(), 'Current version did not remember acknowledgement');
  await page.locator('#file-input').setInputFiles('campus_print/tests/fixtures/print-portal.pdf');
  await page.locator('#go-print').click();
  await page.locator('#school-username').fill('t_privacy_synthetic');
  await page.locator('#school-password').fill('synthetic-never-sent');
  await page.keyboard.press('Escape');
  await page.locator('#privacy-open').click();
  assert(await page.locator('#privacy-review').isVisible(), 'Cannot review notice after entry');
  assert(await page.locator('#privacy-choice').isHidden(), 'Review incorrectly asked for repeat consent');
  await page.locator('#privacy-return').click();
  assert(await page.locator('#doc-name').textContent() === 'print-portal.pdf', 'Review lost selected document');
  await page.locator('#privacy-open').click();
  await page.locator('#privacy-revoke').click();
  assert(await page.locator('#privacy-choice').isVisible(), 'Withdrawal did not restore gate');
  assert(await page.locator('#school-password').inputValue() === '' && await page.locator('#school-username').inputValue() === '', 'Withdrawal retained credentials');
  assert(await page.locator('#file-input').inputValue() === '' && await page.locator('#doc-open').getAttribute('href') === null, 'Withdrawal retained local file');
  assert(await page.locator('#doc-name').textContent() === '' && await page.locator('#confirm-name').textContent() === '', 'Withdrawal retained document names');
  assert(await page.locator('#print-workspace canvas').evaluateAll(nodes => nodes.every(canvas => canvas.width === 0 && canvas.height === 0)), 'Withdrawal retained preview pixels');
  assert(await page.evaluate(key => !localStorage.getItem(key), key), 'Withdrawal retained saved consent');
  const afterRevoke = requests.length;
  await page.evaluate(() => {window.dispatchEvent(new Event('online')); document.dispatchEvent(new Event('visibilitychange'));});
  await page.waitForTimeout(150);
  assert(requests.length === afterRevoke, 'Print APIs continued after withdrawal');
  await page.goto(origin + '/print/');
  assert(await page.locator('#privacy-notice').isVisible(), 'Reload bypassed withdrawal');

  await page.evaluate(key => localStorage.setItem(key, 'old-notice-version'), key);
  const beforeVersion = requests.length;
  await page.goto(origin + '/print/');
  assert(await page.locator('#privacy-notice').isVisible() && await page.locator('#privacy-accept').isDisabled(), 'Old version bypassed updated notice');
  assert(requests.length === beforeVersion, 'Old version triggered printing APIs');
  await page.getByRole('link', {name: '暂不使用', exact: true}).click();
  assert(new URL(page.url()).pathname === '/', 'Declining did not leave printing');

  await page.addInitScript(() => {
    for (const method of ['getItem', 'setItem', 'removeItem']) Storage.prototype[method] = () => {throw new DOMException('Storage blocked', 'SecurityError');};
  });
  await page.goto(origin + '/print/');
  assert(await page.locator('#privacy-notice').isVisible(), 'Storage failure bypassed gate');
  assert(await page.locator('#privacy-storage-note').isVisible(), 'Storage failure was not explained');
  await page.locator('#privacy-check').check();
  await page.locator('#privacy-accept').click();
  await page.getByText('打印服务已连接', {exact: true}).waitFor();
  await page.goto(origin + '/print/');
  assert(await page.locator('#privacy-notice').isVisible(), 'Storage unavailable consent should last only this page');
  assert(!requests.some(request => request.method !== 'GET'), 'Privacy checks transmitted credentials or a print job');
  assert(!errors.length, errors.join('\n'));
  return {passed: 10, firstVisit: true, keyboard: true, mobile: [390, 320], acknowledgementOnlyPersistence: true,
    withdrawal: true, versionChange: true, storageUnavailable: true, printMutations: 0};
}
