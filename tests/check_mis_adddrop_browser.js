// Use an isolated persistent Chromium profile with the release ZIP unpacked.
// Run on about:blank via playwright-cli run-code --filename <this file>.
// All MIS traffic is fulfilled locally. Synthetic records only, never real enrollment.
async (page) => {
  if (page.url() !== 'about:blank') throw new Error('Use an isolated about:blank browser');
  const context = page.context();
  const worker = context.serviceWorkers().find(w => w.url().endsWith('/background.js'));
  if (!worker) throw new Error('Load the test extension first');
  const host = 'https://mis.bnbu.edu.cn';
  const home = '/mis/student/as/home.do';
  const add = '/mis/student/as/addSubject.do';
  const pendingKey = 'bnbu-adddrop-pending-v1';
  const escape = value => String(value).replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
  const item = (code = 'TEST1013', section = '1001', label = 'Add', disabled = false) => ({ code, section, label, disabled, id: `${code}-${section}`, type: 'ME' });
  let s;
  const render = (results = []) => {
    const registered = s.registered.map(record => `<tr><td>ME</td><td>${escape(record.code)}</td><td id="reg-${escape(record.id)}">${escape(record.title || 'Fixture')} (${escape(record.section)})</td><td>Fixture</td><td>3</td><td>Mon</td><td></td><td><input type="button" value="Drop" onclick="dropSubject('never')"></td></tr>`).join('');
    const rows = results.map(record => `<tr><td>${escape(record.type)}</td><td>${escape(record.code)}</td><td id="${escape(record.id)}">${escape(record.title || 'Fixture')} (${escape(record.section)})<input type="hidden" id="${escape(record.id)}_type" value="${escape(record.type)}"></td><td>Fixture</td><td>3</td><td>Mon</td><td></td><td><input type="button" value="${escape(record.label)}" onclick="${escape(record.handler || `addSubject('${record.id}')`)}" ${record.disabled ? 'disabled' : ''}></td></tr>`).join('');
    return `<!doctype html><meta charset="utf-8"><title>Offline Add Course Fixture</title><body class="as">
      <input type="hidden" id="FEGE" value="${s.creditLimit || 0}"><input type="hidden" id="FEOW" value="0"><input type="hidden" id="FEOT" value="0">
      <table class="tablestyle-2"><thead><tr><th colspan="8">List of Courses Registered</th></tr></thead>${registered}</table>
      <form id="seachForm" name="seachForm" action="${home}" method="post"><input name="keyWord" id="keyWord" value="unrelated old search"><select name="keyWordType"><option value="1">Search By Course</option><option value="7">Search By Code</option></select><select name="allowType"><option value="1">All</option><option value="2">Allow Add</option></select><button type="submit">Search</button><input type="hidden" name="pageIndex" value="1"><input type="hidden" name="pageSize" value="20"><input type="hidden" name="csrf" value="fixture-only"></form>
      <table class="tablestyle-10"><caption>List of Courses found</caption><thead><tr><th>Type</th><th>Course Code</th><th>Course</th><th>Teacher</th><th>Units</th><th>Time</th><th>Remark</th><th>Action</th></tr></thead>${rows}</table>
      <form id="frm" name="frm" method="post" action="${s.badAction || home}"><input type="hidden" name="id"><input type="hidden" name="timeClash"><input type="hidden" name="csrf" value="fixture-only"></form>
      <script>/* new Pagination({total: ${s.total || results.length}}) */</script></body>`;
  };
  const assert = (ok, message) => { if (!ok) throw new Error(message + ' [' + s.name + ']'); };
  await context.route(/^https?:\/\//, async route => {
    const req = route.request();
    const path = req.url().split('?')[0].slice(host.length);
    assert(req.url().startsWith(host + '/') && [home, add].includes(path), 'Unexpected request or forbidden Drop/Replace endpoint');
    if (req.method() === 'GET') {
      s.reads += 1;
      await route.fulfill({ contentType: 'text/html', body: render() });
      return;
    }
    assert(req.method() === 'POST', 'Unexpected HTTP method');
    const fields = req.postDataJSON();
    const body = { get: name => fields[name] };
    assert(body.get('csrf') === 'fixture-only', 'School hidden fields were not preserved');
    if (path === home) {
      s.searches.push({ code: body.get('keyWord'), page: Number(body.get('pageIndex')), type: body.get('keyWordType') });
      assert(['1', '7'].includes(body.get('keyWordType')) && body.get('allowType') === '1' && body.get('pageSize') === '50', 'Search mode / paging fields are incorrect');
      if (s.slowSearch) await page.waitForTimeout(1000);
      if (s.busyOnce && s.searches.length === 1) { await route.fulfill({ status: 503, body: 'Fixture busy' }); return; }
      if (s.login) { await route.fulfill({ contentType: 'text/html', body: '<input type="password">' }); return; }
      if (s.malformed) { await route.fulfill({ contentType: 'text/html', body: '<p>Unknown page</p>' }); return; }
      const rows = s.results(body.get('keyWord'), Number(body.get('pageIndex')), body.get('keyWordType'));
      s.offered = rows;
      await route.fulfill({ contentType: 'text/html', body: render(rows) });
      return;
    }
    const chosen = s.offered.find(row => row.id === body.get('id'));
    assert(chosen && chosen.label === 'Add' && !chosen.disabled, 'Submitted a non-Add or disabled row');
    assert(!body.get('timeClash'), 'Clash override was sent');
    assert(!s.adds.includes(chosen.id), 'Duplicate Add submission');
    s.adds.push(chosen.id);
    if (!s.unknown) s.registered.push(chosen);
    if (s.lostResponse) { await route.abort('failed'); return; }
    await route.fulfill({ contentType: 'text/html', body: render() });
  });

  const send = async (targetPage, message) => worker.evaluate(async ({ url, message }) => {
    const tabs = await chrome.tabs.query({});
    const tab = tabs.find(item => item.url === url);
    if (!tab) throw new Error('Test tab not found');
    return chrome.tabs.sendMessage(tab.id, message);
  }, { url: targetPage.url(), message });
  const start = async (config = {}, targetPage = page) => {
    const response = await send(targetPage, { type: 'start-monitoring', config: {
      targets: 'TEST1013 | 1001', minInterval: 0.5, maxInterval: 0.5, submitTimeout: 10,
      autoConfirm: true, directSubmit: true, startAt: '', endAt: '', notify: false, ...config
    } });
    assert(response.ok, 'Start rejected');
  };
  const stoppedWith = async text => {
    await page.waitForFunction(text => document.querySelector('.bch-status')?.textContent.includes(text), text, { timeout: 20000 });
    const state = await send(page, { type: 'get-state' });
    assert(!state.config.enabled, 'Runner did not stop');
  };
  const prepare = async (name, extra = {}) => {
    if (page.url() !== 'about:blank') await send(page, { type: 'stop-monitoring' });
    await page.goto('about:blank');
    await worker.evaluate(async () => { await chrome.storage.local.clear(); await chrome.storage.session.clear(); });
    s = { name, registered: [], searches: [], offered: [], adds: [], reads: 0, results: () => [item()], ...extra };
    await page.goto(host + home);
    await page.locator('#bnbu-course-helper').waitFor({ state: 'visible' });
  };
  const results = [];
  const saveResult = () => results.push({ test: s.name, searches: s.searches.length, adds: s.adds.slice() });

  await prepare('automatic code search and single add');
  await start(); await stoppedWith('所有目标课程');
  assert(s.adds.length === 1 && s.searches[0].code === 'TEST1013', 'Search or add failed'); saveResult();

  await prepare('code without section accepts any eligible class', { results: () => [item('TEST1013', '1001', 'Clash', true), item('TEST1013', '1002')] });
  await start({ targets: 'TEST1013' }); await stoppedWith('所有目标课程');
  assert(s.adds.join() === 'TEST1013-1002' && s.searches.every(x => x.type === '7'), 'Any-section code failed'); saveResult();

  await prepare('explicit unlimited section syntax', { results: () => [item('TEST1013', '1002')] });
  await start({ targets: 'TEST1013 | 不限' }); await stoppedWith('所有目标课程');
  assert(s.adds.join() === 'TEST1013-1002', 'Unlimited section syntax failed'); saveResult();

  const named = (title = 'Computer Vision', section = '1001', code = 'TEST1013') => ({ ...item(code, section), title });
  for (const [name, target, rows, expected] of [
    ['English course name any section', 'Computer Vision', [{ ...named('Computer Vision'), label: 'Full', disabled: true }, named('Computer Vision', '1002')], 'TEST1013-1002'],
    ['Chinese course name', '计算机视觉', [named('计算机视觉', '1002')], 'TEST1013-1002'],
    ['name with fixed section', 'Computer Vision | 1001', [named('Computer Vision', '1002'), named()], 'TEST1013-1001'],
    ['name case and whitespace normalized', '  computer    vision  ', [named()], 'TEST1013-1001'],
    ['course name containing comma', 'Art, Science and Society', [named('Art, Science and Society')], 'TEST1013-1001'],
    ['name explicit any section', 'Computer Vision | *', [named('Computer Vision', '1002')], 'TEST1013-1002'],
    ['exact name does not select similar course', 'Computer Vision', [named('Advanced Computer Vision', '1001', 'TEST1023'), named()], 'TEST1013-1001']
  ]) {
    await prepare(name, { results: () => rows });
    await start({ targets: target }); await stoppedWith('所有目标课程');
    assert(s.adds.join() === expected, 'Wrong name or section selected');
    assert(s.searches[0].type === '1' && s.searches.at(-1).type === '7', 'Name was not resolved before exact-code add'); saveResult();
  }

  await prepare('ambiguous same-name courses pause', { results: () => [named(), named('Computer Vision', '1002', 'TEST1023')] });
  await start({ targets: 'Computer Vision' }); await stoppedWith('同名课程');
  assert(s.adds.length === 0, 'Ambiguous course was added'); saveResult();

  await prepare('name ambiguity on later page is checked before add', { total: 51, results: (_query, p) => [p === 1 ? named() : named('Computer Vision', '1002', 'TEST1023')] });
  await start({ targets: 'Computer Vision' }); await stoppedWith('同名课程');
  assert(s.adds.length === 0 && s.searches.length === 2, 'Later-page ambiguity missed'); saveResult();

  await prepare('partial name never guessed', { results: () => [named()] });
  await start({ targets: 'Computer' }); await stoppedWith('完整匹配');
  assert(s.adds.length === 0, 'Partial name was guessed'); saveResult();

  await prepare('name not offered yet does not starve code target', { results: (query, _p, type) => type === '1'
    ? s.searches.filter(x => x.type === '1').length === 1 ? [] : [named()]
    : [query === 'TEST1023' ? named('Other Course', '1001', 'TEST1023') : named()] });
  await start({ targets: 'Computer Vision\nTEST1023' }); await stoppedWith('所有目标课程');
  assert(s.adds.join() === 'TEST1023-1001,TEST1013-1001', 'Missing name blocked other targets'); saveResult();

  await prepare('already registered name skipped', { registered: [named()], results: () => [] });
  await start({ targets: 'Computer Vision' }); await stoppedWith('所有目标课程');
  assert(s.adds.length === 0 && s.searches.length === 1, 'Registered name not skipped'); saveResult();

  await prepare('name and code alias combine section alternatives', { results: () => [{ ...named('Computer Vision', '1002'), label: 'Full', disabled: true }, named()] });
  await start({ targets: 'Computer Vision | 1002\nTEST1013 | 1001' }); await stoppedWith('所有目标课程');
  assert(s.adds.join() === 'TEST1013-1001', 'Alias caused duplicate or conflicting add'); saveResult();

  await prepare('multiple courses and already registered skip', { registered: [item('TEST1003')], results: code => [item(code)] });
  await start({ targets: 'TEST1003 | 1001\nTEST1013 | 1001\nTEST1023 | 1001' }); await stoppedWith('所有目标课程');
  assert(s.adds.length === 2 && !s.searches.some(x => x.code === 'TEST1003'), 'Registered skip or multi-course failed'); saveResult();

  await prepare('full seat retried then added', { results: () => [s.searches.length < 2 ? item('TEST1013', '1001', 'Full', true) : item()] });
  await start(); await stoppedWith('所有目标课程'); assert(s.searches.length >= 2 && s.adds.length === 1, 'Full retry failed'); saveResult();

  await prepare('temporary busy search retries without adding twice', { busyOnce: true });
  await start(); await stoppedWith('所有目标课程'); assert(s.searches.length === 2 && s.adds.length === 1, 'Busy retry failed'); saveResult();

  await prepare('section alternatives add only one', { results: () => [item('TEST1013', '1001', 'Full', true), item('TEST1013', '1002')] });
  await start({ targets: 'TEST1013 | 1001\nTEST1013 | 1002' }); await stoppedWith('所有目标课程');
  assert(s.adds.join() === 'TEST1013-1002', 'Alternatives were not respected'); saveResult();

  await prepare('pagination finds requested section', { total: 51, results: (_code, p) => [p === 1 ? item('TEST1013', '1002', 'Full', true) : item()] });
  await start(); await stoppedWith('所有目标课程'); assert(s.searches.some(x => x.page === 2), 'Pagination failed'); saveResult();

  await prepare('section priority respected across pages', { total: 51, results: (_code, p) => [p === 1 ? item('TEST1013', '1002') : item()] });
  await start({ targets: 'TEST1013 | 1001\nTEST1013 | 1002' }); await stoppedWith('所有目标课程');
  assert(s.adds.join() === 'TEST1013-1001', 'Earlier-page fallback beat first-choice section'); saveResult();

  await prepare('earlier-page fallback revalidated before add', { total: 51, results: (_code, p) => [p === 1 ? item('TEST1013', '1002') : item('TEST1013', '1001', 'Full', true)] });
  await start({ targets: 'TEST1013 | 1001\nTEST1013 | 1002' }); await stoppedWith('所有目标课程');
  assert(s.adds.join() === 'TEST1013-1002' && s.searches.map(x => x.page).join() === '1,2,1', 'Fallback not revalidated'); saveResult();

  for (const [name, rows] of [
    ['disabled Add never submitted', [item('TEST1013', '1001', 'Add', true)]],
    ['Full and Clash never submitted', [item('TEST1013', '1001', 'Clash', true)]],
    ['exact course code and section', [item('TEST10130'), item('TEST1013', '1002')]],
    ['Drop disguised as Add rejected', [{ ...item(), handler: "dropSubject('TEST1013-1001')" }]]
  ]) {
    await prepare(name, { results: () => rows });
    const second = page.waitForResponse(r => r.request().method() === 'POST' && s.searches.length >= 2);
    await start(); await second;
    await send(page, { type: 'stop-monitoring' });
    assert(s.adds.length === 0, 'Unsafe row submitted'); saveResult();
  }

  await prepare('already registered different section never dropped', { registered: [item('TEST1013', '1002')] });
  await start(); await stoppedWith('其他班号'); assert(s.adds.length === 0 && s.searches.length === 0, 'Automatic switch attempted'); saveResult();

  await prepare('manual confirmation option respected');
  await start({ autoConfirm: false }); await stoppedWith('未提交'); assert(s.adds.length === 0, 'Manual mode submitted'); saveResult();

  await prepare('credit warning requires manual handling', { creditLimit: 6, results: () => [{ ...item(), type: 'FE/GE' }] });
  await start(); await stoppedWith('类别学分提醒'); assert(s.adds.length === 0, 'Credit warning bypassed'); saveResult();

  await prepare('unexpected form action blocked', { badAction: '/mis/student/as/dropSubject.do' });
  await start(); await stoppedWith('表单目标'); assert(s.adds.length === 0, 'Unsafe form submitted'); saveResult();

  await prepare('login loss stops safely', { login: true });
  await start(); await stoppedWith('登录已失效'); assert(s.adds.length === 0, 'Login page submitted'); saveResult();

  await prepare('unknown HTML stops safely', { malformed: true });
  await start(); await stoppedWith('未能唯一识别'); assert(s.adds.length === 0, 'Malformed page submitted'); saveResult();

  await prepare('stop during search prevents late add', { slowSearch: true });
  const requestStarted = page.waitForRequest(r => r.method() === 'POST');
  await start(); await requestStarted; await send(page, { type: 'stop-monitoring' });
  await page.waitForTimeout(1200); assert(s.adds.length === 0, 'Late add after stop'); saveResult();

  await prepare('end time during search prevents late add', { slowSearch: true });
  await start({ endAt: new Date(Date.now() + 600).toISOString() }); await stoppedWith('暂停');
  await page.waitForTimeout(600); assert(s.adds.length === 0, 'Late add after deadline'); saveResult();

  await prepare('lost POST response verified without duplicate', { lostResponse: true });
  await start(); await stoppedWith('所有目标课程'); assert(s.adds.length === 1, 'Duplicate on lost response'); saveResult();

  await prepare('unknown POST result paused and pending preserved', { unknown: true });
  await start(); await stoppedWith('未能确认上次');
  let pending = await worker.evaluate(key => chrome.storage.local.get(key), pendingKey);
  assert(pending[pendingKey] && s.adds.length === 1, 'Pending result was not preserved');
  await send(page, { type: 'stop-monitoring' });
  pending = await worker.evaluate(key => chrome.storage.local.get(key), pendingKey);
  assert(pending[pendingKey], 'Ordinary Stop erased pending result');
  await send(page, { type: 'ack-adddrop-result' });
  pending = await worker.evaluate(key => chrome.storage.local.get(key), pendingKey);
  assert(!pending[pendingKey], 'Explicit acknowledgement did not clear pending'); saveResult();

  await prepare('resume only verifies previous pending submission', { registered: [item()] });
  await worker.evaluate(({ key, origin }) => chrome.storage.local.set({ [key]: { origin, code: 'TEST1013', section: '1001', time: Date.now() } }), { key: pendingKey, origin: host });
  await start(); await stoppedWith('所有目标课程'); assert(s.adds.length === 0, 'Resume submitted twice'); saveResult();

  await prepare('old selection run does not silently start add mode');
  await worker.evaluate(() => chrome.storage.local.set({ 'bnbu-course-helper-config-v1': {
    enabled: true, targets: 'TEST1013 | 1001', runMode: 'selection'
  } }));
  await page.reload(); await page.locator('#bnbu-course-helper').waitFor({ state: 'visible' });
  const notStarted = await send(page, { type: 'get-state' });
  assert(!notStarted.config.enabled && s.searches.length === 0 && s.adds.length === 0, 'Cross-mode auto-start occurred'); saveResult();

  await prepare('single-tab lease blocks a second runner', { results: () => [item('TEST1013', '1001', 'Full', true)] });
  const firstSearch = page.waitForResponse(r => r.request().method() === 'POST');
  await start(); await firstSearch;
  const secondPage = await context.newPage();
  await secondPage.goto(host + home + '?test=second');
  await secondPage.waitForFunction(() => document.querySelector('.bch-status')?.textContent.includes('另一个页面'));
  assert(s.adds.length === 0, 'Second runner submitted');
  await secondPage.close(); await send(page, { type: 'stop-monitoring' }); saveResult();

  await page.goto('about:blank');
  return results;
}
