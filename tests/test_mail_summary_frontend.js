const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {JSDOM}=require('jsdom');
const root=path.resolve(__dirname,'..');
const tick=()=>new Promise(resolve=>setTimeout(resolve,0));
function setup(fetch){
 const dom=new JSDOM('<div id="test"></div>',{url:'https://example.test/',runScripts:'outside-only'}),w=dom.window;
 w.fetch=fetch;
 for(const file of ['react.production.min.js','react-dom.production.min.js','babel.min.js'])w.eval(fs.readFileSync(path.join(root,'vendor',file),'utf8'));
 const html=fs.readFileSync(path.join(root,'index.html'),'utf8'),src=html.match(/<script type="text\/babel"[^>]*>([\s\S]*?)<\/script>/)[1];
 const start=src.indexOf('const SettingsView ='),end=src.indexOf('const ToolboxView ='),stop=src.indexOf('\n        const ',end+20);
 const portion=src.slice(start,stop)+src.slice(src.indexOf('const AuthModal ='),src.indexOf('const FACULTY_LABEL ='));
 const icons=[...new Set([...src.matchAll(/\bIcon:\s*(\w+)/g)].map(x=>x[1]).concat([...portion.matchAll(/<([A-Z]\w*)/g)].map(x=>x[1])))].filter(x=>!['React','AuthModal'].includes(x));
 const stub='const '+icons.map(name=>`${name}=()=>React.createElement('span')`).join(',')+';';
 w.eval(w.Babel.transform('const {useState,useEffect}=React;'+stub+portion+';window.MailTest={HomeView,ToolboxView,SettingsView,AuthModal};',{presets:['react']}).code);
 const render=(component,user,props={})=>w.ReactDOM.render(w.React.createElement(w.MailTest[component],{user,onNavigate:()=>{},onOpenLogin:()=>{},onOpenView:()=>{},...props}),w.document.getElementById('test'));
 return {dom,w,render};
}
const user={id:1,ispace_username:'s123456789',username:'fixture'};
const response=data=>({ok:true,json:async()=>data});
const recorder=()=>{const calls=[];return {calls,fetch:async(url,opts)=>{calls.push({url,opts});return response({});}};};
const mailCalls=calls=>calls.filter(c=>String(c.url).includes('/api/mail-brief'));

// The weekly mail brief is retired: no surface may show it or ask the server for it.
test('homepage shows no mail brief for any visitor and never calls the mail API',async()=>{
 const {calls,fetch}=recorder();const {dom,w,render}=setup(fetch);
 for(const account of [user,null,{id:2,username:'local'}]){
  render('HomeView',account);await tick();await tick();
  assert.equal(w.document.querySelector('.weekly-mail-brief'),null);
  assert.doesNotMatch(w.document.body.textContent,/最近一周的邮件|邮件摘要|验证 iSpace/);
  assert.match(w.document.body.textContent,/Course & Day/);
 }
 assert.equal(mailCalls(calls).length,0);dom.window.close();
});
test('toolbox has no mail summary entry',async()=>{
 const {fetch}=recorder();const {dom,w,render}=setup(fetch);
 render('ToolboxView',user);assert.equal(w.document.querySelector('a[href="/mail-summary/"]'),null);dom.window.close();
});
test('settings drops the mail switch but keeps unlink and the privacy notice',async()=>{
 const {calls,fetch}=recorder();const {dom,w,render}=setup(fetch);
 render('SettingsView',user);await tick();await tick();
 assert.doesNotMatch(w.document.body.textContent,/最近一周的邮件/);
 assert.equal(w.document.querySelector('#mail-brief-setting-label'),null);
 assert.equal(mailCalls(calls).length,0);
 assert.match(w.document.body.textContent,/删除已添加的 iSpace 账号/);
 assert.match(w.document.body.textContent,/服务器同步时能解密/);
 assert.ok(w.document.querySelector('a[href="/privacy/"]'));dom.window.close();
});
test('school login form can still open on the iSpace tab with a prefilled account',async()=>{
 const {fetch}=recorder();const {dom,w,render}=setup(fetch);
 render('AuthModal',user,{initialTab:'ispace',initialUsername:user.ispace_username});await tick();
 assert.match(w.document.querySelector('h2').textContent,/iSpace Login/);
 assert.equal(w.document.querySelector('input[type="text"]').value,user.ispace_username);
 assert.equal(w.document.querySelector('input[type="password"]').value,'');
 assert.doesNotMatch(w.document.body.textContent,/邮件摘要/);dom.window.close();
});
