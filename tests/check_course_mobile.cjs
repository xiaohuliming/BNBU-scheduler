// Run with Node and jsdom available through NODE_PATH or a local installation.
// Browser layout acceptance is separate; these checks cover interaction state.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const base = path.resolve(__dirname, '..');
const dom = new JSDOM('<button id="trigger">Course</button><div id="root"></div>', {
    url: 'https://example.test', runScripts: 'outside-only', pretendToBeVisual: true,
});
const w = dom.window;
const media = { matches: true, addEventListener: (_, fn) => { media.change = fn; }, removeEventListener: () => {} };
w.matchMedia = () => media;
w.HTMLElement.prototype.scrollIntoView = function () {};
const course = { code: 'AI2003', title: 'Data Structures', units: 3, offered: true,
    sessions: [{ session: '1001', teacher: 'Teacher A', schedule: 'Mon 15:00-16:50', classroom: 'T7-101' }],
};
w.fetch = async url => ({ ok: true, json: async () => url.startsWith('/api/course/') ? course : {} });
for (const name of ['react.production.min.js', 'react-dom.production.min.js', 'babel.min.js']) {
    w.eval(fs.readFileSync(path.join(base, 'vendor', name), 'utf8'));
}
const source = fs.readFileSync(path.join(base, 'index.html'), 'utf8')
    .match(/<script type="text\/babel"[^>]*>([\s\S]*?)<\/script>/)[1]
    .replace(/const root = ReactDOM.createRoot\(document.getElementById\('root'\)\);\s*root.render\(<App \/>\);/, 'window.components = { ExplorerView, CourseInsightModal, CartWidget };');
w.eval(w.Babel.transform(source, { presets: ['react'] }).code);
const root = w.ReactDOM.createRoot(w.document.getElementById('root'));
const render = (name, props) => w.ReactDOM.flushSync(() => root.render(w.React.createElement(w.components[name], props)));
const settle = () => new Promise(resolve => setTimeout(resolve, 20));
const click = el => { assert(el); w.ReactDOM.flushSync(() => el.click()); };
const named = text => Array.from(w.document.querySelectorAll('button')).find(el => el.textContent.trim() === text);

(async () => {
    let details = 0, added = null, removed = null, closed = 0;
    const availableCourses = Array.from({ length: 55 }, (_, i) => ({ code: `AI${i}`, name: `Course ${i}`,
        details: [{ 'Course Title & Session': `Course ${i} (1001)`, 'Teachers': 'Teacher A', 'Class Schedule': i % 2 ? 'Tue' : 'Mon' }],
    }));
    render('ExplorerView', { availableCourses, onViewCourse: () => details++, onAddCourse: c => { added = c; } });
    await settle();
    const toggle = w.document.querySelector('.course-filter-toggle');
    assert.equal(toggle.getAttribute('aria-expanded'), 'false');
    click(toggle);
    const day = w.document.querySelector('[aria-label="上课日期"]');
    w.ReactDOM.flushSync(() => { day.value = 'Mon'; day.dispatchEvent(new w.Event('change', { bubbles: true })); });
    assert.equal(w.document.querySelectorAll('.course-card').length, 12);
    click(toggle);
    assert.equal(toggle.getAttribute('aria-expanded'), 'false');
    assert.equal(day.value, 'Mon');
    assert(w.document.querySelector('.course-result-summary').textContent.includes('28'));
    click(named('详情'));
    assert.equal(details, 1, 'Details button must not bubble into a duplicate card action');
    click(named('加入'));
    assert.equal(added.code, 'AI0');
    assert.equal(details, 1);
    click(named('下一页'));
    assert(w.document.querySelector('.course-card').textContent.includes('AI24'));
    w.ReactDOM.flushSync(() => { media.matches = false; media.change(); });
    assert.equal(w.document.querySelectorAll('.course-card').length, 28);
    assert(w.document.querySelector('.course-card').textContent.includes('AI0'));

    const trigger = w.document.getElementById('trigger');
    trigger.focus();
    render('CourseInsightModal', { initialCode: 'AI2003', onClose: () => closed++, onAddCourse: c => { added = c; } });
    await settle();
    assert.equal(w.document.body.style.overflow, 'hidden');
    assert.equal(w.document.activeElement.getAttribute('aria-label'), '关闭课程详情');
    assert.equal(w.document.querySelectorAll('.course-session-cards > div').length, 1);
    assert(w.document.querySelector('.course-session-cards').textContent.includes('Mon 15:00-16:50'));
    click(named('加入选课购物车'));
    assert.equal(added.code, 'AI2003');
    w.document.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Escape' }));
    assert.equal(closed, 1);
    render('CartWidget', { compact: true, items: [course], open: true, onRemove: code => { removed = code; } });
    assert.equal(w.document.body.style.overflow, '');
    assert.equal(w.document.activeElement, trigger);
    assert(w.document.querySelector('.course-cart-bar').textContent.includes('已选 1 门'));
    click(w.document.querySelector('[aria-label="移除 AI2003"]'));
    assert.equal(removed, 'AI2003');
    console.log('Course UI passed: filters, pagination, single detail action, cart, sessions, focus and scroll restoration.');
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => { root.unmount(); w.close(); });
