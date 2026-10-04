async page => {
 const origin=new URL(page.url()).origin;if(new URL(origin).hostname!=='127.0.0.1')throw new Error('Local tests only');
 const assert=(ok,msg)=>{if(!ok)throw new Error(msg);};const errors=[],requests=[];page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>requests.push(r.url()));
 let user=null,own=null,used=1,claims=0,logins=0,resets=0,node='ready';
 const tokenA='A'.repeat(43),tokenC='C'.repeat(43);
 const url=(t,format='txt')=>'https://www.bnbscheduler.top/campus-connect/subscriptions/'+t+'.'+format;
 const status=()=>({available:true,user,csrf_token:'fixture-csrf',capacity:10,used,remaining:10-used,test_days:60,device_limit:2,subscription:own,node:{name:'MAXCOURSE Campus',status:node,checked_at:Math.floor(Date.now()/1000)}});
 await page.addInitScript(()=>Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>{window.__copiedForTest=text;}},configurable:true}));
 await page.route('**/api/**',async route=>{
  const p=new URL(route.request().url()).pathname;
  const send=(body,code=200)=>route.fulfill({status:code,contentType:'application/json',body:JSON.stringify(body)});
  if(p==='/api/campus-connect/status')return send(status());
  if(p==='/api/login/ispace'){
   logins++;assert(route.request().postDataJSON().purpose==='campus-connect','Login read unrelated data');user={id:9,display_name:'synthetic-owner',verified:true};return send({success:true});
  }
  if(p==='/api/campus-connect/claim'){
   claims++;assert(route.request().headers()['x-campus-csrf']==='fixture-csrf','Missing CSRF');assert(route.request().postDataJSON().consent===true,'Consent omitted');
   used++;own={id:'fixture',claimed:true,active:true,synced:true,expires_at:Math.floor(Date.now()/1000)+60*86400,subscription_url:url(tokenA),online_sessions:1};return send(status());
  }
  if(p==='/api/campus-connect/reset-subscription'){
   resets++;assert(!('slot' in route.request().postDataJSON()),'Device slots retained');own.subscription_url=url(tokenC);return send(status());
  }
  return send({error:'Blocked fixture'},404);
 });
 await page.route('**/campus-connect/subscriptions/**',route=>route.fulfill({status:200,headers:{'X-Campus-Node-Name':'MAXCOURSE Campus','Subscription-Userinfo':'expire='+Math.floor(Date.now()/1000+60*86400)},body:''}));
 await page.goto(origin+'/campus-connect/');
 await page.locator('#quota-label').filter({hasText:'1 / 10'}).waitFor();
 assert(await page.locator('#claim').isDisabled(),'Consent preselected');assert(await page.locator('#school-dialog').isHidden(),'Password requested on entry');
 await page.locator('#consent').check();await page.locator('#claim').click();await page.locator('#school-dialog').waitFor();
 await page.locator('#school-username').fill('synthetic-school');await page.locator('#school-password').fill('synthetic-password');await page.locator('#login-submit').click();
 await page.locator('#school-dialog').waitFor({state:'hidden'});assert(await page.locator('#school-password').inputValue()==='','Password retained');
 await page.locator('#claim').click();await page.locator('.subscription-card').waitFor();
 assert(await page.locator('.subscription-card').count()===1,'More than one subscription shown');assert(await page.locator('#application').isHidden(),'Claim form still dominates claimed view');
 assert((await page.locator('.subscription-metrics').innerText()).includes('1 / 2'),'Online count missing');assert(claims===1&&logins===1,'Unexpected account requests');
 await page.locator('#refresh-status').click();assert(claims===1,'Refresh renewed subscription');
 await page.getByRole('button',{name:'复制订阅',exact:true}).click();assert(await page.evaluate(()=>window.__copiedForTest)===url(tokenA),'Wrong copied subscription');
 await page.getByRole('button',{name:'一键导入',exact:true}).click();await page.locator('#import-dialog').waitFor();
 const links=await page.locator('[data-import-client]').evaluateAll(nodes=>Object.fromEntries(nodes.map(n=>[n.dataset.importClient,n.href])));
 assert(atob(links.shadowrocket.slice('shadowrocket://add/sub://'.length).split('?')[0])===url(tokenA),'Wrong Shadowrocket URI');
 for(const [client,scheme] of [['clash','clash-verge:'],['flclash','flclash:'],['stash','stash:']]){
  const link=new URL(links[client]);assert(link.protocol===scheme&&link.hostname==='install-config','Wrong client scheme '+client);assert(link.searchParams.get('url')===url(tokenA,'yaml'),'Wrong imported format '+client);
  assert(decodeURIComponent(link.search.slice(link.search.indexOf('url=')+4))===url(tokenA,'yaml'),'Clash Verge URL must be last parameter');
 }
 assert(await page.locator('.import-warning').isVisible(),'Full configuration warning missing');
 await page.locator('#import-close').click();await page.locator('#import-dialog').waitFor({state:'hidden'});
 assert(await page.locator('#import-url').inputValue()==='','Closed modal retained link');
 await page.getByRole('button',{name:'二维码',exact:true}).click();await page.locator('#subscription-qr svg').waitFor();
 await page.addScriptTag({path:'tests/vendor/jsqr.js'});
 const decode=async()=>page.evaluate(async()=>{
  const svg=document.querySelector('#subscription-qr svg');const image=new Image();image.src='data:image/svg+xml;base64,'+btoa(new XMLSerializer().serializeToString(svg));await image.decode();
  const canvas=document.createElement('canvas');canvas.width=600;canvas.height=600;const ctx=canvas.getContext('2d');ctx.fillStyle='#fff';ctx.fillRect(0,0,600,600);ctx.drawImage(image,0,0,600,600);const data=ctx.getImageData(0,0,600,600);return jsQR(data.data,600,600)?.data;
 });
 assert(await decode()===url(tokenA),'QR does not decode to subscription');
 await page.locator('#qr-format').selectOption('yaml');assert(await decode()===url(tokenA,'yaml'),'QR format change failed');
 await page.locator('#copy-import').click();assert(await page.evaluate(()=>window.__copiedForTest)===url(tokenA,'yaml'),'Modal copied wrong format');
 await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{value:{writeText:async()=>{throw new Error('Fixture denial');}},configurable:true}));
 await page.locator('#copy-import').click();assert((await page.locator('#import-feedback').innerText()).includes('长按'),'Clipboard denial has no fallback');
 await page.locator('#import-close').click();await page.locator('#import-dialog').waitFor({state:'hidden'});
 assert(!await page.locator('#subscription-qr svg').count(),'QR retained after close');
 await page.getByRole('button',{name:'重置订阅',exact:true}).click();await page.locator('#reset-confirm').click();await page.locator('#reset-dialog').waitFor({state:'hidden'});
 assert(resets===1,'Reset not applied');assert(await page.locator('#result').isHidden(),'Old import result retained after reset');
 await page.getByRole('button',{name:'二维码',exact:true}).click();await page.locator('#subscription-qr svg').waitFor();assert(await decode()===url(tokenC),'Reset retained old QR credential');
 own.active=false;await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));await page.locator('#import-dialog').waitFor({state:'hidden'});assert(!await page.locator('#subscription-qr svg').count(),'Revocation retained QR');
 own.active=true;node='unavailable';await page.locator('#refresh-status').click();await page.locator('#node-status').filter({hasText:'暂不可用'}).waitFor();
 node='ready';await page.locator('#refresh-status').click();await page.locator('#node-status').filter({hasText:'正常'}).waitFor();
 for(const width of [320,390,768,1440]){await page.setViewportSize({width,height:900});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Overflow at '+width);}
 await page.emulateMedia({reducedMotion:'reduce'});
 await page.setViewportSize({width:1440,height:1000});await page.evaluate(()=>{document.activeElement?.blur();scrollTo(0,0);});await page.waitForTimeout(3600);await page.screenshot({path:'output/campus-portal-desktop.png',fullPage:true});
 await page.setViewportSize({width:390,height:844});await page.evaluate(()=>scrollTo(0,0));await page.screenshot({path:'output/campus-portal-mobile.png',fullPage:true});
 const color=await page.getByRole('button',{name:'一键导入',exact:true}).evaluate(n=>getComputedStyle(n).backgroundColor);assert(color==='rgb(213, 197, 236)','Pastel palette missing');
 assert(await page.evaluate(t=>!JSON.stringify(Object.entries(localStorage)).includes(t)&&!JSON.stringify(Object.entries(sessionStorage)).includes(t),tokenC),'Credential persisted');
 await page.evaluate(()=>window.dispatchEvent(new Event('pagehide')));assert(await page.locator('#import-url').inputValue()==='','Exit retained private link');
 own=null;used=10;await page.locator('#refresh-status').click();await page.locator('#quota-label').filter({hasText:'10 / 10'}).waitFor();assert(await page.locator('#claim').isDisabled(),'Full quota accepted claim');
 assert(requests.every(url=>new URL(url).origin===origin),'Page sent credential to an external service');assert(!errors.length,errors.join('\n'));
 return{claimFlow:true,singleSubscription:true,privateLocalQR:true,qrDecoded:true,fourClientSchemes:true,clipboardFallback:true,reset:true,revocationCleanup:true,realNodeStates:true,pastelPalette:true,mobile:true,nativeAppsLaunched:0};
}
