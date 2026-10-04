async page => {
  const origin = new URL(page.url()).origin;
  if (new URL(origin).hostname !== '127.0.0.1') throw new Error('Use an isolated local preview.');
  const assert = (ok, message) => {if (!ok) throw new Error(message);};
  const token = 'A'.repeat(43);
  const valid = 'https://www.bnbscheduler.top/campus-connect/subscriptions/' + token + '.txt';
  let status = 200, held = null, copied = null;
  const calls = [], errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', {value: {writeText: async text => {window.__copiedForTest = text;}}, configurable: true});
  });
  await page.route('**/campus-connect/subscriptions/**', async route => {
    calls.push({method: route.request().method(), path: new URL(route.request().url()).pathname});
    if (held) await held;
    const payload = {hosts: {'papercut.bnbu.edu.cn': '172.16.244.61'},
      proxies: [{name: 'MAXCOURSE Campus', type: 'trojan', server: 'www.bnbscheduler.top', port: 27443, password: 'synthetic-only'}],
      'proxy-groups': [{name: '校园资源', type: 'select', proxies: ['MAXCOURSE Campus']}],
      rules: ['DOMAIN,ispace.bnbu.edu.cn,校园资源', 'MATCH,DIRECT']};
    try {await route.fulfill({status, contentType: 'text/plain', headers: {'Subscription-Userinfo': 'expire=' + Math.floor(Date.now()/1000+86400)}, body: route.request().method() === 'HEAD' ? '' : JSON.stringify(payload)});} catch (_) { /* Expected when an obsolete request is cancelled. */ }
  });
  await page.route('**/api/campus-connect/status',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({available:true,capacity:10,used:1,remaining:9,test_days:60,device_limit:2,user:null,csrf_token:'synthetic-csrf',subscription:null})}));
  await page.goto(origin + '/campus-connect/');
  await page.locator('.existing').evaluate(node=>{node.open=true;});
  assert(calls.length === 0, 'Page requested a private subscription before user action');
  assert(await page.locator('#result').isHidden(), 'Connection result fabricated before validation');
  assert(await page.locator('#validate').isDisabled(), 'Empty subscription accepted');
  assert(await page.locator('#login-dialog').isHidden(), 'School password requested before applying');
  for (const value of [valid.replace('www.bnbscheduler.top','evil.example'), valid.replace('https:','http:'), valid+'?share=1', valid.replace('https://','https://user:password@')]) {
    await page.locator('#subscription').fill(value);
    await page.locator('#validate').click();
    assert(await page.locator('#form-error').isVisible(), 'Malformed or foreign subscription accepted');
  }
  assert(calls.length === 0, 'Invalid URL sent credentials to a server');
  await page.locator('#subscription').fill(valid);
  await page.locator('#validate').click();
  await page.locator('#result').waitFor();
  await page.locator('.guide-detail').evaluate(node=>{node.open=true;});
  assert(calls.at(-1).method === 'HEAD', 'Validation unnecessarily fetched node passwords');
  assert((await page.locator('#expiry-note').innerText()).includes('试用有效至'), 'Expiry header not displayed');
  await page.locator('#copy-subscription').click();
  copied = await page.evaluate(() => window.__copiedForTest);
  assert(copied === valid, 'Shadowrocket did not receive its node subscription');
  const shadowDownload = page.waitForEvent('download');
  await page.locator('#merge').click();
  await (await shadowDownload).saveAs('output/campus-shadowrocket-merge.conf');
  await page.locator('input[value=clash]').check();
  await page.locator('#copy-subscription').click();
  assert((await page.evaluate(() => window.__copiedForTest)).endsWith('.yaml'), 'Clash subscription format incorrect');
  const clashDownload = page.waitForEvent('download');
  await page.locator('#merge').click();
  await (await clashDownload).saveAs('output/campus-clash-merge.yaml');
  await page.locator('input[value=stash]').check();
  assert((await page.locator('#download-full').getAttribute('href')).endsWith('.yaml'), 'Stash full configuration incorrect');
  assert(await page.evaluate(token => !JSON.stringify(Object.entries(localStorage)).includes(token) && !JSON.stringify(Object.entries(sessionStorage)).includes(token), token), 'Credential persisted in browser storage');
  await page.locator('#clear-key').click();
  assert(await page.locator('#subscription').inputValue() === '' && await page.locator('#result').isHidden(), 'Clear retained credential or actions');
  status = 404;
  await page.locator('#subscription').fill(valid);
  await page.locator('#validate').click();
  await page.locator('#form-error').filter({hasText:'到期或撤销'}).waitFor();
  assert(await page.locator('#result').isHidden(), 'Revoked subscription still offered import');
  status = 200;
  let release;
  held = new Promise(resolve => {release = resolve;});
  await page.locator('#validate').click();
  await page.locator('#validate').filter({hasText:'正在验证'}).waitFor();
  await page.locator('#clear-key').click();
  release(); held = null;
  await page.waitForTimeout(200);
  assert(await page.locator('#result').isHidden() && await page.locator('#subscription').inputValue() === '', 'Stale validation restored cleared credentials');
  for (const width of [320,390,768,1440]) {
    await page.setViewportSize({width,height:900});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Horizontal overflow at '+width);
  }
  await page.emulateMedia({reducedMotion:'reduce'});
  await page.setViewportSize({width:1440,height:1000});
  await page.evaluate(() => {document.activeElement?.blur(); window.scrollTo(0,0);});
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(250);
  await page.screenshot({path:'output/campus-connect-desktop.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  await page.evaluate(() => window.scrollTo(0,0));
  await page.waitForTimeout(250);
  await page.screenshot({path:'output/campus-connect-mobile.png',fullPage:true});
  await page.locator('#subscription').fill(valid);
  await page.evaluate(() => window.dispatchEvent(new Event('pagehide')));
  assert(await page.locator('#subscription').inputValue() === '', 'Page exit retained credential');
  assert(calls.every(call => ['GET','HEAD'].includes(call.method)), 'Portal sent an unexpected mutation');
  assert(!errors.length, errors.join('\n'));
  return {passed: 12, invalidURLsBlocked:true, clientFormats:true, mergeDownloads:true, noCredentialPersistence:true, staleRequestsDiscarded:true, mobile:true, mutations:0};
}
