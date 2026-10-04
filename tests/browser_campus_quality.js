// Setup: npm pack axe-core@4.10.3 --pack-destination .codex/quality-deps; extract into .codex/quality-deps/axe.
async page=>{
 const origin=new URL(page.url()).origin;if(new URL(origin).hostname!=='127.0.0.1')throw new Error('Local quality checks only');
 const assert=(ok,msg)=>{if(!ok)throw new Error(msg);};const errors=[];page.on('pageerror',e=>errors.push(e.message));
 let failed=false,reads=0;
 const own={id:'quality-fixture',claimed:true,active:true,synced:true,expires_at:Math.floor(Date.now()/1000)+50*86400,subscription_url:'https://www.bnbscheduler.top/campus-connect/subscriptions/'+'Q'.repeat(43)+'.txt',online_sessions:0};
 const response=()=>({available:true,user:{id:81,display_name:'quality-fixture'},csrf_token:'fixture',capacity:10,used:1,remaining:9,test_days:60,device_limit:2,subscription:own,node:{status:'ready'}});
 await page.route('**/api/campus-connect/status',r=>{reads++;return r.fulfill({status:failed?503:200,contentType:'application/json',body:JSON.stringify(failed?{available:false,user:{id:81,display_name:'quality-fixture'},error:'连接暂时不可用，请重试。'}:response())});});
 await page.route('**/campus-connect/subscriptions/**',r=>r.fulfill({status:200,headers:{'X-Campus-Node-Name':'MAXCOURSE Campus'},body:''}));
 await page.goto(origin+'/campus-connect/#node-panel');await page.locator('.subscription-card').waitFor();
 assert(await page.locator('.sidebar a[href="#node-panel"]').getAttribute('aria-current')==='location','Deep link did not update navigation');
 await page.locator('.sidebar a[href="#setup"]').click();assert(await page.locator('.sidebar a[href="#setup"]').getAttribute('aria-current')==='location','Clicked navigation did not update');
 const before=reads;
 await page.evaluate(()=>{dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true}));dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}));});
 await page.locator('.subscription-card').waitFor();assert(reads>before,'Restored page did not refresh');
 await page.locator('input[name=client][value=shadowrocket]').check();
 await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{value:{writeText:async()=>{throw new Error('Denied for test');}},configurable:true}));
 await page.getByRole('button',{name:'复制订阅',exact:true}).click();await page.locator('#import-dialog').waitFor();
 assert(await page.locator('#import-title').innerText()==='复制订阅','Copy fallback not focused on copying');
 assert(await page.locator('#import-url').inputValue()===own.subscription_url,'No usable clipboard fallback');
 assert(await page.locator('#import-url').isVisible(),'Clipboard recovery hides the only usable address');
 assert(await page.locator('#import-url').evaluate(n=>n===document.activeElement && n.selectionStart===0 && n.selectionEnd===n.value.length),'Copy recovery does not focus and select the address');
 assert(await page.locator('#native-pane').isHidden(),'Copy fallback needlessly requested app launch');
 failed=true;await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));await page.locator('#account-error').waitFor();
 assert((await page.locator('#identity').innerText()).includes('已登录'),'Service outage falsely signed user out');assert(await page.locator('#import-dialog').isHidden(),'Outage retained private import modal');
 assert(await page.locator('#claim').isDisabled(),'Outage accepted claim');
 failed=false;await page.locator('#refresh-status').click();await page.locator('.subscription-card').waitFor();
 for(const width of [320,390,768,1024,1440]){await page.setViewportSize({width,height:900});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Overflow '+width);}
 await page.emulateMedia({reducedMotion:'reduce'});assert(await page.locator('.window').evaluate(n=>getComputedStyle(n).animationName)==='none','Reduced motion ignored');
 await page.setViewportSize({width:1440,height:1000});await page.addScriptTag({path:'.codex/quality-deps/axe/package/axe.min.js'});
 const report=await page.evaluate(async()=>{const r=await axe.run(document,{runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21a','wcag21aa']}});return r.violations.map(v=>({id:v.id,impact:v.impact,nodes:v.nodes.map(n=>n.target)}));});
 await page.evaluate(()=>document.documentElement.style.fontSize='200%');assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Text scaling overflow');await page.evaluate(()=>document.documentElement.style.fontSize='');
 assert(!errors.length,errors.join('\n'));
 assert(!report.length,JSON.stringify(report));
 return{bfcacheRestored:true,accurateOutageIdentity:true,privateOutageCleanup:true,clipboardFallback:true,navigation:true,fiveWidths:true,reducedMotion:true,axeViolations:report};
}
