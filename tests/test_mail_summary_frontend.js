const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');
const path = require('node:path');
const root = path.resolve(__dirname,'..');
const tick=()=>new Promise(resolve=>setTimeout(resolve,0));
const messages=Array.from({length:12},(_,i)=>({id:`m${i}`,subject:i===0?'<img src=x onerror=alert(1)>':'邮件 '+i,sender:'教务处',sender_address:'registry@example.edu',received_at:'2026-09-24T10:00:00Z',snippet:'报名通知'}));
async function harness({failure=false}={}) {
  const dom=new JSDOM(fs.readFileSync(path.join(root,'mail-summary/index.html'),'utf8'),{url:'http://localhost/mail-summary/',runScripts:'outside-only'});
  const calls=[];
  dom.window.fetch=async(url,opts={})=>{
    calls.push({url,opts});
    let status=200, data={};
    if(url==='/api/user') data={user:{id:1,username:'test',ispace_username:'s123456789'}};
    else if(url.endsWith('/status')) data={username:'s123456789',connected:true,csrf:'csrf-test',credential_saved:false};
    else if(url.endsWith('/inbox')) data={messages,total_unread:99,page:0,has_next:true};
    else if(url.endsWith('/ai-status')) data={model:'test-model'};
    else if(url.endsWith('/summarize')) {
      if(failure){status=502;data={error:'生成失败，请重试。'};}
      else data={summarized_count:1,total_unread:99,digest:{overview:'报名提醒',credits:2,items:[{id:'m0',summary:'<script>bad()</script>',priority:'action',action:'提交申请',deadline:'9 月 26 日'}]},messages:[{...messages[0],body:'原文',has_images:true}]};
    }
    return {ok:status===200,status,json:async()=>data};
  };
  dom.window.eval(fs.readFileSync(path.join(root,'mail-summary/mail.js'),'utf8'));
  await tick();await tick();return {dom,calls};
}
test('renders inbox as text, caps selection, cites source, sends CSRF',async()=>{
 const {dom,calls}=await harness(); const d=dom.window.document;
 assert.equal(d.querySelectorAll('.message-row').length,12);
 assert.equal(d.querySelectorAll('.message-row img').length,0);
 assert.equal(d.querySelectorAll('.message-row input:checked').length,10);
 const eleventh=d.querySelectorAll('.message-row input')[10];eleventh.click();assert.equal(eleventh.checked,false);
 d.getElementById('summarize').click();await tick();await tick();
 assert.match(d.getElementById('digest-output').textContent,/本次 1 \/ 99/);
 assert.equal(d.querySelectorAll('#digest-output script').length,0);
 assert.match(d.querySelector('details').textContent,/原文/);
 assert.equal(calls.find(c=>c.url.endsWith('/summarize')).opts.headers['X-Mail-CSRF'],'csrf-test');
 d.getElementById('disconnect').click();await tick();await tick();
 assert.equal(d.querySelectorAll('.message-row').length,0);
 assert.equal(d.getElementById('digest-output').textContent,'');dom.window.close();
});
test('failed summary is visible and retry re-enabled',async()=>{
 const {dom}=await harness({failure:true});const d=dom.window.document;d.getElementById('summarize').click();await tick();await tick();
 assert.match(d.getElementById('notice').textContent,/生成失败/);assert.equal(d.getElementById('summarize').disabled,false);dom.window.close();
});
test('homepage and toolbox omit the mail entry with React',()=>{
 const dom=new JSDOM('<div id="test"></div>',{url:'http://localhost/',runScripts:'outside-only'});const w=dom.window;
 w.eval(fs.readFileSync(path.join(root,'vendor/react.production.min.js'),'utf8'));
 w.eval(fs.readFileSync(path.join(root,'vendor/react-dom.production.min.js'),'utf8'));
 w.eval(fs.readFileSync(path.join(root,'vendor/babel.min.js'),'utf8'));
 const html=fs.readFileSync(path.join(root,'index.html'),'utf8');const src=html.match(/<script type="text\/babel"[^>]*>([\s\S]*?)<\/script>/)[1];
 const names=[...src.matchAll(/\bIcon:\s*(\w+)/g)].map(x=>x[1]);
 const start=src.indexOf('const HomeView ='); const end=src.indexOf('const ToolboxView =');const toolboxEnd=src.indexOf('\n        const ',end+20);
 const portion=src.slice(start,toolboxEnd);
 const iconStub='const '+[...new Set([...names,...[...portion.matchAll(/<([A-Z]\w*)/g)].map(x=>x[1]).filter(x=>x!=="React"),'ArrowRight','ArrowUpRight','ArrowLeft','Download','MapIcon','Bell','Mail','Sparkles','Search','Check','X','ChevronRight'])].join('=()=>React.createElement("span"),')+'=()=>React.createElement("span");';
 w.eval(w.Babel.transform('const {useState,useEffect,useRef}=React;'+iconStub+portion+';window.TestHomeView=HomeView;ReactDOM.render(React.createElement(ToolboxView,{onOpenView:()=>{}}),document.getElementById("test"));',{presets:['react']}).code);
 assert.doesNotMatch(w.document.getElementById('test').textContent,/AI 邮件总结/);
 assert.equal(w.document.querySelector('a[href="/mail-summary/"]'),null);
 w.ReactDOM.render(w.React.createElement(w.TestHomeView,{user:null,onNavigate:()=>{},onOpenLogin:()=>{}}),w.document.getElementById('test'));
 assert.doesNotMatch(w.document.getElementById('test').textContent,/AI 邮件总结/);
 assert.equal(w.document.querySelector('a[href="/mail-summary/"]'),null);
 dom.window.close();
});
