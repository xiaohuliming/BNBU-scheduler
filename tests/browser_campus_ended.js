async page => {
  const origin = new URL(page.url()).origin;
  if (new URL(origin).hostname !== '127.0.0.1') throw new Error('Local ended-state checks only');
  const assert = (value, message) => { if (!value) throw new Error(message); };
  let heads = 0;
  const own = {claimed:true,active:true,synced:true,expires_at:Math.floor(Date.now()/1000)+86400,
    subscription_url:'https://www.bnbscheduler.top/campus-connect/subscriptions/'+'D'.repeat(43)+'.txt',online_sessions:0};
  await page.route('**/api/campus-connect/status', route => route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({available:true,user:{id:83,display_name:'ended-fixture'},csrf_token:'fixture',capacity:10,used:1,remaining:9,subscription:own,node:{status:'ready'}})}));
  await page.route('**/campus-connect/subscriptions/**', route => { heads++; return route.fulfill({status:200,body:''}); });
  await page.goto(origin+'/campus-connect/');
  await page.locator('.subscription-card').waitFor();
  await page.getByRole('button',{name:'一键导入',exact:true}).click();
  await page.locator('#import-dialog').waitFor();
  own.expires_at=Math.floor(Date.now()/1000)-86400;
  await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));
  await page.locator('#subscription-empty').waitFor();
  assert(!(await page.locator('#subscription-empty').innerText()).includes('登录'), 'Expired owner is incorrectly asked to log in and claim');
  assert(await page.locator('#subscription-empty a').getAttribute('href')==='/privacy/#contact', 'No actionable path after the trial ends');
  assert(await page.locator('#import-dialog').isHidden(), 'Expiry retains private import modal');
  assert(await page.locator('#import-url').inputValue()==='', 'Expiry retains private URL');
  assert(await page.locator('[data-import-client][href]').count()===0, 'Expiry retains native import links');
  assert(await page.locator('#result').isHidden(), 'Expiry leaves a usable configuration guide');
  const before=heads;
  await page.locator('#setup-start').click();
  assert(heads===before, 'Known expired subscription is verified again');
  assert(await page.locator('#subscription-title').evaluate(node=>node===document.activeElement), 'Ended setup entry does not lead to recovery');
  own.active=false; own.expires_at=Math.floor(Date.now()/1000)+86400;
  await page.locator('#refresh-status').click();
  assert(await page.locator('#subscription-empty a').isVisible(), 'Revoked subscription loses contact action');
  assert((await page.locator('#account-expiry').innerText()).includes('原到期'), 'Ended subscription still advertises future validity');
  for(const width of [320,390,1440]){await page.setViewportSize({width,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Ended view overflows '+width);}
  return {accurateEndedState:true,contactAction:true,privateExpiryCleanup:true,noExpiredVerification:true,revocationCopy:true,threeWidths:true};
}
