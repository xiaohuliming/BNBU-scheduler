async page=>{
 if(new URL(page.url()).hostname!=='127.0.0.1')throw new Error('Isolated local test only');
 const assert=(ok,msg)=>{if(!ok)throw new Error(msg);};let reads=0;let own=null;let release;
 const gate=new Promise(resolve=>release=resolve);
 await page.route('**/api/campus-connect/status',r=>{reads++;return r.fulfill({status:200,contentType:'application/json',body:JSON.stringify({available:true,user:{id:77,display_name:'exit-fixture'},csrf_token:'fixture',capacity:10,used:1,remaining:9,test_days:60,device_limit:2,subscription:own,node:{status:'ready'}})});});
 await page.route('**/api/campus-connect/claim',async r=>{await gate;own={claimed:true,active:true,synced:true,expires_at:Math.floor(Date.now()/1000)+60*86400,subscription_url:'https://www.bnbscheduler.top/campus-connect/subscriptions/'+'E'.repeat(43)+'.txt'};try{await r.fulfill({status:200,contentType:'application/json',body:JSON.stringify({subscription:own})});}catch(_){} });
 await page.goto('http://127.0.0.1:5023/campus-connect/');await page.locator('#quota-label').filter({hasText:'/ 10'}).waitFor();await page.locator('#consent').check();await page.locator('#claim').click();
 const before=reads;await page.evaluate(()=>dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true})));release();await page.waitForTimeout(350);
 assert(reads===before,'Exited mutation restarted private status request');assert(await page.locator('.subscription-card').count()===0,'Late response restored private subscription');
 await page.evaluate(()=>dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true})));await page.locator('.subscription-card').waitFor();assert(reads>before,'Return did not confirm committed result');
 return{exitAbortsPendingRequest:true,noPrivateResurrection:true,returnChecksActualResult:true};
}
