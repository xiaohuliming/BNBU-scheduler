async page => {
 const origin=new URL(page.url()).origin;if(new URL(origin).hostname!=='127.0.0.1')throw new Error('Local tests only');
 const assert=(ok,msg)=>{if(!ok)throw new Error(msg);};const errors=[];page.on('pageerror',e=>errors.push(e.message));
 let user=null,own=null,used=1,claims=0,logins=0,resets=0;
 const tokenA='A'.repeat(43),tokenB='B'.repeat(43),tokenC='C'.repeat(43);
 const url=t=>'https://www.bnbscheduler.top/campus-connect/subscriptions/'+t+'.txt';
 const status=()=>({available:true,user,csrf_token:'fixture-csrf',capacity:10,used,remaining:10-used,test_days:60,device_limit:2,subscription:own});
 await page.route('**/api/**',async route=>{
  const p=new URL(route.request().url()).pathname;
  const send=(body,code=200)=>route.fulfill({status:code,contentType:'application/json',body:JSON.stringify(body)});
  if(p==='/api/campus-connect/status')return send(status());
  if(p==='/api/login/ispace'){
   logins++;const body=route.request().postDataJSON();assert(body.purpose==='campus-connect','Login read unrelated data');user={id:9,display_name:'synthetic-owner',verified:true};return send({success:true});
  }
  if(p==='/api/campus-connect/claim'){
   claims++;assert(route.request().headers()['x-campus-csrf']==='fixture-csrf','Missing CSRF');assert(route.request().postDataJSON().consent===true,'Consent omitted');
   used++;own={id:'fixture',claimed:true,active:true,synced:true,expires_at:Math.floor(Date.now()/1000)+60*86400,devices:[{slot:1,subscription_url:url(tokenA),online:false},{slot:2,subscription_url:url(tokenB),online:true}]};return send(status());
  }
  if(p==='/api/campus-connect/reset-device'){
   resets++;assert(route.request().postDataJSON().slot===1,'Wrong device reset');own.devices[0].subscription_url=url(tokenC);return send(status());
  }
  return send({error:'Blocked fixture'},404);
 });
 await page.route('**/campus-connect/subscriptions/**',route=>route.fulfill({status:200,headers:{'X-Campus-Node-Name':'MAXCOURSE Campus D1','Subscription-Userinfo':'expire='+Math.floor(Date.now()/1000+60*86400)},body:''}));
 await page.goto(origin+'/campus-connect/');
 await page.locator('#quota-label').filter({hasText:'1 / 10'}).waitFor();
 assert(await page.locator('#claim').isDisabled(),'Consent preselected');
 assert(await page.locator('#school-dialog').isHidden(),'Password requested on entry');
 await page.locator('#consent').check();await page.locator('#claim').click();
 await page.locator('#school-dialog').waitFor();
 await page.locator('#school-username').fill('synthetic-school');await page.locator('#school-password').fill('synthetic-password');await page.locator('#login-submit').click();
 await page.locator('#school-dialog').waitFor({state:'hidden'});
 assert(await page.locator('#school-password').inputValue()==='','Password retained');
 await page.locator('#claim').click();await page.locator('.device-card').first().waitFor();
 assert(await page.locator('.device-card').count()===2,'Incorrect device slots');
 assert(claims===1&&logins===1,'Unexpected account requests');
 await page.locator('#claim').click();assert(claims===1,'Repeated click renewed subscription');
 await page.locator('.device-card').first().getByRole('button',{name:'接入此设备'}).click();await page.locator('#result').waitFor();
 await page.locator('.device-card').first().getByRole('button',{name:'重置链接'}).click();await page.locator('#reset-confirm').click();
 await page.locator('#reset-dialog').waitFor({state:'hidden'});assert(resets===1,'Reset not applied');assert(await page.locator('#result').isHidden(),'Old import credentials retained after reset');
 await page.setViewportSize({width:1440,height:1050});await page.evaluate(()=>{document.activeElement?.blur();scrollTo(0,0);});await page.waitForTimeout(300);await page.screenshot({path:'output/campus-macos-desktop.png',fullPage:true});
 await page.setViewportSize({width:390,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Mobile overflow');await page.evaluate(()=>scrollTo(0,0));await page.waitForTimeout(300);await page.screenshot({path:'output/campus-macos-mobile.png',fullPage:true});
 const colors=await page.locator('#claim').evaluate(n=>getComputedStyle(n).backgroundColor);assert(!colors.includes('214, 255, 98'),'Fluorescent green remains');
 own=null;used=10;await page.locator('#refresh-status').click();await page.locator('#quota-label').filter({hasText:'10 / 10'}).waitFor();assert(await page.locator('#claim').isDisabled(),'Full quota accepted new claim');
 assert(!errors.length,errors.join('\n'));return{claimFlow:true,schoolIdentityOnly:true,consent:true,twoDevices:true,idempotent:true,reset:true,quotaFull:true,macosLayout:true,noNeonGreen:true,mobile:true};
}
