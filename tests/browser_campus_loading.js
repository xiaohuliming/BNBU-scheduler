async page=>{
 const origin=new URL(page.url()).origin;if(new URL(origin).hostname!=='127.0.0.1')throw new Error('Isolated local test only');
 const assert=(ok,msg)=>{if(!ok)throw new Error(msg);};const errors=[];page.on('pageerror',e=>errors.push(e.message));let qrRequests=0,failQR=true,slowLogin=true;
 const token='L'.repeat(43);const own={id:'loading-fixture',claimed:true,active:true,synced:true,expires_at:Math.floor(Date.now()/1000)+40*86400,subscription_url:'https://www.bnbscheduler.top/campus-connect/subscriptions/'+token+'.txt',online_sessions:0};
 await page.route('**/api/campus-connect/status',r=>r.fulfill({status:200,contentType:'application/json',body:JSON.stringify({available:true,user:{id:78,display_name:'loading-fixture'},csrf_token:'fixture',capacity:10,used:1,remaining:9,test_days:60,device_limit:2,subscription:own,node:{status:'ready'}})}));
 await page.route('**/campus-connect/subscriptions/**',r=>r.fulfill({status:200,headers:{'X-Campus-Node-Name':'MAXCOURSE Campus'},body:''}));
 await page.route('**/vendor/qrcode-generator.js*',async r=>{qrRequests++;if(failQR)return r.fulfill({status:404,body:'Missing for test'});await new Promise(resolve=>setTimeout(resolve,250));return r.continue();});
 await page.route('**/api/login',async r=>{if(slowLogin){await new Promise(resolve=>setTimeout(resolve,13000));try{await r.fulfill({status:200,contentType:'application/json',body:'{"success":true}'});}catch(_){}return;}return r.fulfill({status:401,contentType:'application/json',body:'{"error":"Invalid credentials"}'});});
 await page.goto(origin+'/campus-connect/');await page.locator('.subscription-card').waitFor();assert(qrRequests===0,'QR library requested at first paint');
 await page.getByRole('button',{name:'二维码',exact:true}).click();await page.locator('#subscription-qr').filter({hasText:'请复制下方地址'}).waitFor();assert(await page.locator('#import-url').inputValue()===own.subscription_url,'QR failure removed usable link');
 await page.locator('#import-close').click();failQR=false;
 await page.getByRole('button',{name:'二维码',exact:true}).click();await page.locator('#subscription-qr svg').waitFor();assert(qrRequests===2,'Failed QR library could not retry');
 await page.locator('#qr-client [data-client-choice=clash]').click();await page.locator('#import-close').click();await page.waitForTimeout(300);assert(!await page.locator('#subscription-qr svg').count(),'Closed QR resurrected from async render');
 await page.locator('#login-open').click();await page.locator('#login-username').fill('synthetic-local');await page.locator('#login-password').fill('synthetic-password');await page.locator('#login-submit').click();
 assert(await page.locator('#login-submit').isDisabled(),'Pending login allowed duplicate request');
 await page.locator('#login-error').filter({hasText:'超时'}).waitFor({timeout:18000});assert(!await page.locator('#login-submit').isDisabled(),'Timed-out login stayed locked');assert(await page.locator('#login-password').inputValue()==='','Timeout retained password');
 slowLogin=false;await page.locator('#login-password').fill('synthetic-password');await page.locator('#login-submit').click();await page.locator('#login-error').filter({hasText:'用户名或密码不正确'}).waitFor();assert(await page.locator('#login-password').inputValue()==='','Retry retained password');
 await page.locator('#login-close').click();assert(!errors.length,errors.join('\n'));return{qrNotLoadedAtFirstPaint:true,lazyQRRetry:true,copyFallbackOnQRFailure:true,noStaleQR:true,loginTimeoutRecoverable:true,passwordCleared:true};
}
