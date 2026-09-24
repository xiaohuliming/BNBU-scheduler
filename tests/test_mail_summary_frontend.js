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
 const start=src.indexOf('const WeeklyMailBrief ='),end=src.indexOf('const ToolboxView ='),stop=src.indexOf('\n        const ',end+20),portion=src.slice(start,stop);
 const icons=[...new Set([...src.matchAll(/\bIcon:\s*(\w+)/g)].map(x=>x[1]).concat([...portion.matchAll(/<([A-Z]\w*)/g)].map(x=>x[1])))].filter(x=>!['React','WeeklyMailBrief'].includes(x));
 const stub='const '+icons.map(name=>`${name}=()=>React.createElement('span')`).join(',')+';';
 w.eval(w.Babel.transform('const {useState,useEffect}=React;'+stub+portion+';window.MailTest={WeeklyMailBrief,HomeView,ToolboxView};',{presets:['react']}).code);
 const render=(component,user)=>w.ReactDOM.render(w.React.createElement(w.MailTest[component],{user,onNavigate:()=>{},onOpenLogin:()=>{},onOpenView:()=>{}}),w.document.getElementById('test'));
 return {dom,w,render};
}
const user={id:1,ispace_username:'s123456789',username:'fixture'};
const ready=(id=1,copy='若计划留校使用实验室，请先完成假期登记。')=>({state:'ready',user_id:id,brief:{generated_at:1800000000,mail_count:65,complete:true,items:[{text:copy,sources:[{subject:'假期实验室安排',sender:'学院',received_at:'2026-09-24T00:00:00Z'}]}]}});
const response=data=>({ok:true,json:async()=>data});

test('signed-in homepage automatically shows compact highlights with sources',async()=>{
 const calls=[];const {dom,w,render}=setup(async(url,opts)=>{calls.push({url,opts});return response(ready());});
 render('HomeView',user);await tick();await tick();
 const d=w.document;
 assert.equal(calls[0].url,'/api/mail-brief');assert.equal(calls[0].opts.cache,'no-store');
 assert.match(d.querySelector('.weekly-mail-brief').textContent,/若计划留校/);
 assert.equal(d.querySelectorAll('.weekly-mail-brief summary').length,1);
 assert.match(d.querySelector('.weekly-mail-brief details').textContent,/假期实验室安排/);
 assert.equal(d.querySelector('.weekly-mail-brief button'),null);
 assert.equal(d.querySelector('a[href="/mail-summary/"]'),null);
 assert.doesNotMatch(d.querySelector('.weekly-mail-brief').textContent,/连接邮箱|选择邮件|生成摘要|积分/);
 dom.window.close();
});
test('visitors and local-only accounts see no mailbox feature or tool entry',async()=>{
 let calls=0;const {dom,w,render}=setup(async()=>{calls++;return response(ready());});
 for(const account of [null,{id:2,username:'local'}]){
  render('HomeView',account);await tick();assert.equal(w.document.querySelector('.weekly-mail-brief'),null);
 }
 render('ToolboxView',user);assert.equal(w.document.querySelector('a[href="/mail-summary/"]'),null);
 assert.equal(calls,0);dom.window.close();
});
test('account switching and logout never display a previous account response',async()=>{
 const pending=[];const {dom,w,render}=setup(()=>new Promise(resolve=>pending.push(resolve)));
 render('WeeklyMailBrief',user);await tick();
 render('WeeklyMailBrief',{...user,id:2});await tick();
 pending[1](response(ready(2,'当前账号的提醒')));await tick();await tick();
 pending[0](response(ready(1,'前一个账号的私密内容')));await tick();await tick();
 assert.match(w.document.body.textContent,/当前账号的提醒/);assert.doesNotMatch(w.document.body.textContent,/私密内容/);
 render('WeeklyMailBrief',null);assert.equal(w.document.querySelector('.weekly-mail-brief'),null);dom.window.close();
});
test('mail and model text cannot execute HTML; service errors stay unobtrusive',async()=>{
 const {dom,w,render}=setup(async()=>response(ready(1,'<img src=x onerror=alert(1)>')));
 render('WeeklyMailBrief',user);await tick();await tick();
 assert.equal(w.document.querySelector('.weekly-mail-brief img'),null);
 assert.match(w.document.body.textContent,/<img/);dom.window.close();
 const failed=setup(async()=>({ok:false}));failed.render('HomeView',user);await tick();await tick();
 assert.match(failed.w.document.body.textContent,/Course & Day/);assert.equal(failed.w.document.querySelector('.weekly-mail-brief'),null);failed.dom.window.close();
});
