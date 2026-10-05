async page => {
  const origin = new URL(page.url()).origin;
  if (new URL(origin).hostname !== '127.0.0.1') throw new Error('Local import-feedback checks only');
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
    try { await route.fulfill({status:responseStatus,body:'',headers:{'X-Campus-Node-Name':'MAXCOURSE Campus'}}); } catch (_) { /* Expected after cancellation. */ }
  });
  await page.goto(origin+'/campus-connect/');
  await page.locator('.subscription-card').waitFor();
  await page.locator('[data-subscription-action=import]').click();
  await started;
  try {
    assert((await page.locator('[data-subscription-action=import]').innerText()).includes('验证'), 'Import has no visible progress');
    assert(await page.locator('[data-subscription-action=import]').isDisabled(), 'Duplicate import is still possible');
    assert(await page.locator('#node-status').innerText()==='服务端就绪', 'Verification is mistaken for node connectivity');
  } finally { release?.(); }
  await page.locator('#account-error').waitFor();
  assert(await page.locator('#import-dialog').isHidden(), 'Failed verification exposed a private import');
  assert(await page.evaluate(()=>location.hash)==='', 'Failure unexpectedly navigated away');
  assert(!await page.locator('[data-subscription-action=import]').isDisabled(), 'Failed verification prevents retry');
  responseStatus=200;started=nextHead();
  await page.locator('[data-subscription-action=import]').click();await started;release();
  await page.locator('#import-dialog').waitFor();
  assert(heads===2, 'Retry did not verify again');
  assert(await page.locator('#account-error').isHidden(), 'Successful retry kept an old error');
  await page.locator('#import-close').click();
  await page.goto(origin+'/campus-connect/');await page.locator('.subscription-card').waitFor();
  started=nextHead();
  await page.locator('[data-subscription-action=import]').click();await started;
  await page.locator('#setup > summary').click();
  await page.locator('input[name=client][value=clash]').check();release();
  await page.locator('[data-subscription-action=import]').filter({hasText:'一键导入'}).waitFor();
  assert(await page.locator('#import-dialog').isHidden(), 'Cancelled verification opened an obsolete import');
  assert(await page.locator('#account-error').isHidden(), 'Client cancellation displayed a stale error');
  return {visibleImportProgress:true,duplicateImportPrevented:true,accurateServerStatus:true,retryVerified:true,clientSwitchCancelsImport:true,nativeClientLaunched:false};
}
