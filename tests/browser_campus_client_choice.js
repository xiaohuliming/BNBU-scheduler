async page => {
  const origin = new URL(page.url()).origin;
  if (new URL(origin).hostname !== '127.0.0.1') throw new Error('Local client-selection checks only');
  const assert = (value, message) => { if (!value) throw new Error(message); };
  let online = 0;
  const token = 'S'.repeat(43);
  const own = () => ({claimed:true, active:true, synced:true, expires_at:Math.floor(Date.now()/1000)+50*86400,
    subscription_url:'https://www.bnbscheduler.top/campus-connect/subscriptions/'+token+'.txt', online_sessions:online});
  await page.addInitScript(() => Object.defineProperty(navigator, 'clipboard', {value:{writeText:async value => {window.__clientChoiceCopied = value;}}, configurable:true}));
  await page.route('**/api/campus-connect/status', route => route.fulfill({status:200, contentType:'application/json', body:JSON.stringify({available:true, user:{id:81,display_name:'client-choice-fixture'}, csrf_token:'fixture', capacity:10,used:1,remaining:9,subscription:own(),node:{status:'ready'}})}));
  await page.route('**/campus-connect/subscriptions/**', route => route.fulfill({status:200,body:'',headers:{'X-Campus-Node-Name':'MAXCOURSE Campus'}}));
  await page.goto(origin+'/campus-connect/');
  await page.locator('.subscription-card').waitFor();
  assert(await page.locator('.subscription-card #own-client').count()===1, 'Copying a subscription depends on an off-screen client selection');
  await page.locator('#own-client').selectOption('clash');
  await page.getByRole('button',{name:'复制订阅',exact:true}).click();
  assert((await page.evaluate(() => window.__clientChoiceCopied)).endsWith('.yaml'), 'Visible client selection did not determine copied format');
  assert(await page.locator('input[name=client][value=clash]').isChecked(), 'Guide client selection did not synchronize');
  online = 1;
  await page.locator('#own-client').focus();
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await page.locator('.subscription-metrics').filter({hasText:'1 / 2'}).waitFor();
  assert(await page.locator('#own-client').inputValue()==='clash', 'Status refresh reset selected client');
  assert(await page.locator('#own-client').evaluate(node => node===document.activeElement), 'Automatic status refresh removed keyboard focus');
  await page.locator('input[name=client][value=stash]').check();
  assert(await page.locator('#own-client').inputValue()==='stash', 'Subscription selector did not follow the guide');
  await page.getByRole('button',{name:'二维码',exact:true}).click();
  await page.locator('#subscription-qr svg').waitFor();
  assert(await page.locator('#qr-format').inputValue()==='yaml', 'QR default differs from selected client');
  await page.locator('#import-close').click();
  await page.locator('#own-client').selectOption('shadowrocket');
  await page.getByRole('button',{name:'复制订阅',exact:true}).click();
  assert((await page.evaluate(() => window.__clientChoiceCopied)).endsWith('.txt'), 'Switching back retained YAML format');
  return {selectionBesideActions:true,copyFormat:true,guideSynced:true,refreshPreserved:true,qrFormat:true};
}
