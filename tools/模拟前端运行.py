# -*- coding: utf-8 -*-
"""模拟浏览器执行前端 JS 的初始化链，找运行时错误。

思路：
    1. 用 node + 最小 DOM 桩（stub）执行 JS
    2. 调用 DOMContentLoaded 的初始化逻辑
    3. 捕获所有异常
"""
import io
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding='utf-8')

JS = r'D:\deepseek文件区\manju\_web_check.js'
NODE = r"C:\Users\ASUS\.ai-manager\runtimes\node\24.22.0\node.exe"

js = io.open(JS, encoding='utf-8').read()

# 构造一个测试脚本：
# - 提供最小 DOM 桩
# - 提供 fetch 桩（返回假数据）
# - 执行原 JS
# - 触发 DOMContentLoaded
test = r'''
// ============ 最小 DOM 桩 ============
const CALLS = [];
const ERRORS = [];

class FakeElement {
  constructor(tag) {
    this.tagName = (tag || 'div').toUpperCase();
    this.children = [];
    this.style = {};
    this.dataset = {};
    this.classList = {
      _set: new Set(),
      add(c) { this._set.add(c); },
      remove(c) { this._set.delete(c); },
      toggle(c, on) { if (on) this._set.add(c); else this._set.delete(c); },
      contains(c) { return this._set.has(c); },
    };
    this.value = '';
    this.textContent = '';
    this.innerHTML = '';
    this.options = [];
    this.files = null;
    this.selectedOptions = [{ text: 'test' }];
    this.checked = false;
    this._listeners = {};
  }
  appendChild(c) { this.children.push(c); return c; }
  addEventListener(ev, fn) {
    (this._listeners[ev] = this._listeners[ev] || []).push(fn);
  }
  removeEventListener() {}
  querySelectorAll() { return []; }
  querySelector() { return null; }
  scrollIntoView() {}
  focus() {}
  onkeydown() {}
  setAttribute(k, v) { this['_attr_' + k] = v; }
  getAttribute(k) { return this['_attr_' + k]; }
  removeAttribute(k) { delete this['_attr_' + k]; }
}

const ELEMENTS = {};
function getEl(id) {
  if (!ELEMENTS[id]) {
    ELEMENTS[id] = new FakeElement('div');
    ELEMENTS[id].id = id;
  }
  return ELEMENTS[id];
}

global.document = {
  getElementById: (id) => getEl(id),
  querySelector: (sel) => {
    if (sel && sel.startsWith('#')) return getEl(sel.slice(1));
    return new FakeElement('div');
  },
  querySelectorAll: (sel) => {
    if (sel === '[data-policy]') return [];
    if (sel === 'nav button') return [];
    if (sel.includes('data-idx')) return [];
    if (sel.includes('data-detail')) return [];
    if (sel.includes('data-restore')) return [];
    if (sel.includes('data-purge')) return [];
    return [];
  },
  addEventListener: (ev, fn) => {
    CALLS.push(['document.addEventListener', ev]);
    if (ev === 'DOMContentLoaded') {
      setTimeout(async () => {
        try { await fn(); }
        catch (e) { ERRORS.push('DOMContentLoaded: ' + e); }
      }, 0);
    }
  },
  createElement: (tag) => new FakeElement(tag),
  body: new FakeElement('body'),
  documentElement: new FakeElement('html'),
};

global.window = {
  addEventListener: () => {},
  removeEventListener: () => {},
  location: { reload: () => {}, href: '', search: '', hash: '' },
  fetch: null,
  scrollTo: () => {},
  scroll: () => {},
  innerWidth: 1280,
  innerHeight: 800,
  matchMedia: () => ({ matches: false, addEventListener: () => {} }),
  getComputedStyle: () => ({ getPropertyValue: () => '' }),
  requestAnimationFrame: (fn) => { try { fn(0); } catch (e) {} return 0; },
  cancelAnimationFrame: () => {},
};

global.localStorage = {
  _d: {},
  getItem(k) { return this._d[k] || null; },
  setItem(k, v) { this._d[k] = String(v); },
  removeItem(k) { delete this._d[k]; },
};

global.navigator = { language: 'zh-CN' };

// fetch 桩：返回合理假数据
global.fetch = async (url, opts) => {
  CALLS.push(['fetch', url]);
  const fake = {
    '/api/auth/status': { enabled: false, roles: [], has_users: true },
    '/api/auth/me': { name: 'admin', role: 'admin', display: '管理员' },
    '/api/rollback/policy': { default: 'block', per_op: {}, policies: [], waiting: 0 },
    '/api/health': { ok: true, providers: {} },
    '/api/projects': { projects: [], ok: true },
    '/api/jobs': [],
    '/api/jobs/queue': { running: [], queued: [], running_count: 0, queued_count: 0 },
    '/api/progress/all': { count: 0, jobs: [] },
    '/api/gpu': { available: false, gpus: [], summary: {} },
    '/api/spend': { daily: { today: 0 }, jobs: [], budget: {} },
    '/api/alerts': { count: 0, alerts: [], stats: {} },
    '/api/trash': { items: [], stats: { count: 0 } },
    '/api/resources': { total_mb: 0, entries: [], dirs: [], by_kind: {}, quota_alerts: [], policy: {} },
    '/api/users': { users: [] },
    '/api/settings': {},
    '/api/plugins': { kinds: {}, total: 0, failed: 0 },
    '/api/capabilities': { '3d': {}, radio: {} },
  };
  let data = {};
  for (const k of Object.keys(fake)) {
    if (url && url.includes(k)) { data = fake[k]; break; }
  }
  return {
    ok: true,
    status: 200,
    json: async () => data,
    text: async () => JSON.stringify(data),
    clone() { return this; },
  };
};

global.FormData = class { append() {} };
global.URLSearchParams = class { constructor(){} };
global.Notification = class { static requestPermission(){} };
global.confirm = () => true;
global.alert = (m) => CALLS.push(['alert', String(m).slice(0, 80)]);
global.prompt = (m, d) => d || 'test';
global.setTimeout = (fn, ms) => { try { fn(); } catch (e) { ERRORS.push('setTimeout: ' + e); } return 0; };
global.setInterval = () => 0;
global.clearInterval = () => {};
global.clearTimeout = () => {};

// ============ 执行原 JS ============
'''

wrapper = test + js + r'''

// ============ 等待初始化完成 ============
(async () => {
  await new Promise(r => global.setTimeout(r, 100));
  await new Promise(r => setTimeout(r, 200));

  console.log('=== 执行结果 ===');
  console.log('  fetch 调用数:', CALLS.filter(c => c[0] === 'fetch').length);
  console.log('  alert 数:', CALLS.filter(c => c[0] === 'alert').length);
  console.log('  错误数:', ERRORS.length);
  if (ERRORS.length) {
    console.log('  错误列表:');
    for (const e of ERRORS) console.log('    ❌', String(e).slice(0, 200));
  } else {
    console.log('  ✅ 初始化无错误');
  }

  // 列出调过的 fetch 接口
  const urls = [...new Set(CALLS.filter(c => c[0] === 'fetch').map(c => c[1]))];
  console.log();
  console.log('  调用的接口（前 20）:');
  for (const u of urls.slice(0, 20)) {
    console.log('    ', String(u).slice(0, 70));
  }
})();
'''

out = os.path.join(os.environ['TEMP'], 'web_sim_test.js')
io.open(out, 'w', encoding='utf-8').write(wrapper)

print("测试脚本:", out)
print("运行中...")
print()

r = subprocess.run([NODE, out], capture_output=True, text=True,
                   encoding='utf-8', errors='replace', timeout=60)
if r.stdout:
    print(r.stdout)
if r.stderr:
    print("=== stderr ===")
    print(r.stderr[:3000])
