async page => {
  const origin = new URL(page.url()).origin;
  if (new URL(origin).hostname !== '127.0.0.1') throw new Error('Local feedback checks only');
  const assert = (value, message) => { if (!value) throw new Error(message); };
  let responseStatus = 503, release = null, heads = 0, markStarted;
  const nextHead = () => new Promise(resolve => { markStarted = resolve; });
  let started = nextHead();
  const own = {claimed:true,active:true,synced:true,expires_at:Math.floor(Date.now()/1000)+50*86400,
    subscription_url:'https://www.bnbscheduler.top/campus-connect/subscriptions/'+'V'.repeat(43)+'.txt',online_sessions:0};
  await page.route('**/api/campus-connect/status', route => route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({available:true,user:{id:82,display_name:'feedback-fixture'},csrf_token:'fixture',capacity:10,used:1,remaining:9,subscription:own,node:{status:'ready'}})}));
  await page.route('**/campus-connect/subscriptions/**', async route => {
    heads++;
    await new Promise(resolve => { release = resolve; markStarted(); });
    try { await route.fulfill({status:responseStatus,body:'',headers:{'X-Campus-Node-Name':'MAXCOURSE Campus'}}); } catch (_) { /* Expected after client cancellation. */ }
  });
  await page.goto(origin+'/campus-connect/');
  await page.locator('.subscription-card').waitFor();
  await page.locator('#routing-mode').selectOption('full');
  await page.locator('.route-guide').click();
  await started;
  try {
    await page.waitForFunction(() => document.querySelector('.route-guide').getAttribute('aria-busy')==='true',null,{timeout:1500});
    assert((await page.locator('#route-guide-status').innerText()).includes('验证'), 'No visible verification progress');
    assert(await page.locator('#node-status').innerText()==='服务端就绪', 'Subscription verification changed server connectivity status');
  } finally { release?.(); }
  await page.locator('#route-guide-status').filter({hasText:'未完成'}).waitFor();
  assert(await page.evaluate(()=>location.hash)==='', 'Failed verification unexpectedly navigated away');
  assert(await page.locator('#result').isHidden(), 'Failed verification exposed a usable configuration');
  assert(await page.locator('.route-guide').getAttribute('aria-busy')===null, 'Failed request kept the entry busy');
  responseStatus = 200; release = null; started = nextHead();
  await page.locator('.route-guide').click();
  await started;
  await page.waitForFunction(() => document.querySelector('.route-guide').getAttribute('aria-busy')==='true');
  release();
  await page.locator('#result').waitFor();
  assert(heads===2, 'Retry did not verify again');
  assert(await page.locator('.full-config').evaluate(node=>node.open), 'Successful retry opened the wrong configuration guide');
  assert(await page.locator('#import-dialog').isHidden(), 'Reading a guide unexpectedly opened a native import');
  assert(await page.locator('#route-guide-status').innerText()==='查看配置方式', 'Success did not clear the failure message');
  assert(await page.locator('#account-error').isHidden(), 'Successful retry kept an earlier account error');
  await page.goto(origin+'/campus-connect/');
  await page.locator('.subscription-card').waitFor();
  release=null; started=nextHead();
  await page.locator('.route-guide').click(); await started;
  await page.locator('input[name=client][value=clash]').check();
  await page.waitForFunction(()=>document.querySelector('.route-guide').getAttribute('aria-busy')===null);
  release();
  assert(await page.evaluate(()=>location.hash)==='', 'Cancelled verification navigated after switching clients');
  assert(await page.locator('#route-guide-status').innerText()==='查看配置方式', 'Cancelled request left an unrelated failure message');
  assert(await page.locator('#result').isHidden(), 'Cancelled request exposed an obsolete guide');
  assert(await page.locator('#account-error').isHidden(), 'Client cancellation displayed a stale verification error');
  return {visibleProgress:true,accurateServerStatus:true,failureStaysPut:true,retryVerified:true,clientSwitchCancelsNavigation:true,noNativeImport:true};
}
