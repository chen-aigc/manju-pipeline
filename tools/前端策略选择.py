# -*- coding: utf-8 -*-
"""前端：确认弹窗加策略选择。"""
import io
import sys

sys.stdout.reconfigure(encoding='utf-8')

HTML = r'D:\deepseek文件区\manju\src\manju\web\static\index.html'
c = io.open(HTML, encoding='utf-8').read()
orig = len(c)
changes = []

# ---------- 1. 确认弹窗加策略下拉 ----------
OLD_DLG = '''      <div id="confirmPreview" style="margin:14px 0"></div>
      <div style="margin:16px 0">
        <label style="font-size:13px">请输入 <b id="confirmKeyword"></b> 确认：</label>
        <input id="confirmInput" style="width:100%;margin-top:6px">
      </div>'''

NEW_DLG = '''      <div id="confirmPreview" style="margin:14px 0"></div>

      <!-- 有任务运行时的策略选择 -->
      <div id="confirmPolicyRow" style="display:none;margin:14px 0;
           background:#f0f7ff;border-left:4px solid #1c7ed6;padding:10px 12px">
        <div style="font-size:13px;margin-bottom:6px">
          <b>⚠️ 有任务正在运行</b>，选择处理方式：
        </div>
        <select id="confirmPolicy" style="width:100%;padding:6px">
          <option value="block">阻断 —— 等任务跑完再来（最安全）</option>
          <option value="wait">等待 —— 排队，任务完成后自动回滚</option>
          <option value="cancel">取消 —— 先取消任务，再回滚</option>
          <option value="force">强制 —— 不等任务，直接回滚（有风险）</option>
        </select>
        <div id="confirmPolicyHint" class="muted" style="font-size:11px;margin-top:4px"></div>
      </div>

      <div style="margin:16px 0">
        <label style="font-size:13px">请输入 <b id="confirmKeyword"></b> 确认：</label>
        <input id="confirmInput" style="width:100%;margin-top:6px">
      </div>'''

if OLD_DLG in c:
    c = c.replace(OLD_DLG, NEW_DLG, 1)
    changes.append("弹窗加策略下拉")

# ---------- 2. requestConfirm 支持 jobRunning 参数 ----------
OLD_RC = '''async function requestConfirm(op, params, opts) {
  """返回 Promise，用户确认后 resolve(true)，取消 resolve(false)"""
  opts = opts || {};
  // 1. 申请 token
  let r;
  try {
    r = await fetch('/api/confirm/request', {
      method: 'POST',
      headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
      body: JSON.stringify({ op: op, params: params }),
    });
  } catch (e) { alert('申请确认失败：' + e); return false; }

  const d = await r.json();
  if (!r.ok) { alert(d.detail || '申请确认失败'); return false; }'''
NEW_RC = '''async function requestConfirm(op, params, opts) {
  /* 返回 Promise，用户确认后 resolve(true)，取消 resolve(false)。
     opts.jobRunning 为 true 时显示策略选择下拉。 */
  opts = opts || {};
  // 1. 申请 token
  let r;
  try {
    r = await fetch('/api/confirm/request', {
      method: 'POST',
      headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
      body: JSON.stringify({ op: op, params: params }),
    });
  } catch (e) { alert('申请确认失败：' + e); return false; }

  const d = await r.json();
  if (!r.ok) { alert(d.detail || '申请确认失败'); return false; }'''

if OLD_RC in c:
    c = c.replace(OLD_RC, NEW_RC, 1)
    changes.append("requestConfirm 注释")

# ---------- 3. requestConfirm 里显示策略行 ----------
OLD_SHOW = '''  $('#confirmPreview').innerHTML = pv;
  $('#confirmInput').value = '';
  $('#confirmModal').style.display = 'flex';
  setTimeout(() => document.getElementById('confirmInput')?.focus(), 100);

  return new Promise((resolve) => { CONFIRM.resolve = resolve; });'''
NEW_SHOW = '''  $('#confirmPreview').innerHTML = pv;

  // 有任务运行时显示策略选择
  const polRow = $('#confirmPolicyRow');
  if (polRow) {
    if (opts.jobRunning || (d.preview && d.preview.job_running)) {
      polRow.style.display = '';
      const sel = $('#confirmPolicy');
      const hint = $('#confirmPolicyHint');
      if (sel) {
        sel.value = opts.defaultPolicy || 'block';
        sel.onchange = () => {
          if (hint) {
            hint.textContent = ({
              block: '任务继续跑，回滚不会执行',
              wait: '回滚会排队，任务完成后自动执行',
              cancel: '会先取消所有运行中的任务，可能丢失进度',
              force: '任务会继续跑，可能读到被回滚的旧剧本',
            })[sel.value] || '';
          }
        };
        sel.onchange();
      }
    } else {
      polRow.style.display = 'none';
    }
  }

  $('#confirmInput').value = '';
  $('#confirmModal').style.display = 'flex';
  setTimeout(() => document.getElementById('confirmInput')?.focus(), 100);

  return new Promise((resolve) => { CONFIRM.resolve = resolve; });'''
if OLD_SHOW in c:
    c = c.replace(OLD_SHOW, NEW_SHOW, 1)
    changes.append("弹窗显示策略")

# ---------- 4. 加策略状态与预检查 ----------
OLD_INIT = '''/* ================= 通用确认弹窗 ================= */
let CONFIRM = { resolve: null, token: null, op: null, params: null };'''

NEW_INIT = '''/* ================= 回滚策略 ================= */
let POLICY = { default: 'block', per_op: {} };

async function loadRollbackPolicy() {
  try {
    const r = await fetch('/api/rollback/policy', { headers: authHeaders() });
    const d = await r.json();
    POLICY.default = d.default || 'block';
    POLICY.per_op = d.per_op || {};
  } catch (e) { console.warn('读取回滚策略失败', e); }
}

// 读某操作生效的策略
function policyFor(op) {
  return POLICY.per_op[op] || POLICY.default || 'block';
}

// 回滚前预检查（可选调用）
async function checkRollback(kind, data) {
  try {
    const r = await fetch('/api/rollback/check', {
      method: 'POST',
      headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
      body: JSON.stringify(Object.assign({ kind: kind }, data)),
    });
    const d = await r.json();
    if (!r.ok) return null;
    return d;
  } catch (e) { return null; }
}

/* ================= 通用确认弹窗 ================= */
let CONFIRM = { resolve: null, token: null, op: null, params: null,
                policy: 'block' };'''

if OLD_INIT in c:
    c = c.replace(OLD_INIT, NEW_INIT, 1)
    changes.append("加策略状态")

# ---------- 5. doConfirmOk 记录选中的策略 ----------
OLD_OK = '''async function doConfirmOk() {
  if (!$('#confirmKeyword').textContent) {
    const ok = confirm('确认执行此危险操作？');
    if (!ok) { closeConfirm(false); return; }
  }
  $('#confirmModal').style.display = 'none';
  if (CONFIRM.resolve) CONFIRM.resolve(true);
  CONFIRM.resolve = null;
}'''
NEW_OK = '''async function doConfirmOk() {
  // 记录策略选择
  const sel = $('#confirmPolicy');
  const polRow = $('#confirmPolicyRow');
  if (sel && polRow && polRow.style.display !== 'none') {
    CONFIRM.policy = sel.value;
  } else {
    CONFIRM.policy = 'block';
  }

  if (!$('#confirmKeyword').textContent) {
    const ok = confirm('确认执行此危险操作？');
    if (!ok) { closeConfirm(false); return; }
  }
  $('#confirmModal').style.display = 'none';
  if (CONFIRM.resolve) CONFIRM.resolve(true);
  CONFIRM.resolve = null;
}'''
if OLD_OK in c:
    c = c.replace(OLD_OK, NEW_OK, 1)
    changes.append("记录策略选择")

# ---------- 6. rollbackScript 传策略 ----------
OLD_RS = '''  const ok = await requestConfirm('script.rollback', params);
  if (!ok) return;
  try {
    const r = await fetch(
      `/api/versions/${encodeURIComponent(proj)}/${ch}/script/${encodeURIComponent(versionId)}/rollback`,
      {
        method: 'POST',
        headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
        body: JSON.stringify({ confirm_token: CONFIRM.token }),
      });'''

NEW_RS = '''  // 先预检查，看有没有任务在跑
  const pre = await checkRollback('script', {
    name: proj, chapter: Number(ch), version: versionId,
  });
  const jobRunning = pre ? pre.job_running : false;

  const ok = await requestConfirm('script.rollback', params, {
    jobRunning: jobRunning,
    defaultPolicy: policyFor('script'),
  });
  if (!ok) return;

  try {
    const r = await fetch(
      `/api/versions/${encodeURIComponent(proj)}/${ch}/script/${encodeURIComponent(versionId)}/rollback`,
      {
        method: 'POST',
        headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
        body: JSON.stringify({
          confirm_token: CONFIRM.token,
          policy: CONFIRM.policy,
        }),
      });'''

if OLD_RS in c:
    c = c.replace(OLD_RS, NEW_RS, 1)
    changes.append("剧本回滚传策略")

# 处理 queued 响应
OLD_RESP = '''    const d = await r.json();
    if (!r.ok) { alert(d.detail || '回滚失败'); return; }
    alert(d.msg || '已回滚');
    loadVersions();
  } catch (e) { alert('回滚失败：' + e); }
}'''
NEW_RESP = '''    const d = await r.json();
    if (!r.ok) {
      // 409：被阻断，展示阻断原因
      if (r.status === 409 && d.detail) {
        let msg = d.detail;
        if (d.blockers) {
          msg += '\\n\\n' + d.blockers.map(b => '· ' + b.msg).join('\\n');
        }
        alert(msg);
      } else {
        alert(d.detail || '回滚失败');
      }
      return;
    }
    if (d.queued) {
      alert(d.msg || '已排队，等任务完成后自动回滚');
    } else if (d.skipped) {
      alert(d.msg || '无需回滚');
    } else {
      alert(d.msg || '已回滚');
    }
    loadVersions();
  } catch (e) { alert('回滚失败：' + e); }
}'''
if OLD_RESP in c:
    c = c.replace(OLD_RESP, NEW_RESP, 1)
    changes.append("处理 queued 响应")

# ---------- 7. DOMContentLoaded 时加载策略 ----------
OLD_DOM = '''document.addEventListener('DOMContentLoaded', () => {
  const b1 = $('#btnConfirmOk'); if (b1) b1.onclick = doConfirmOk;'''
NEW_DOM = '''document.addEventListener('DOMContentLoaded', async () => {
  await loadRollbackPolicy();
  const b1 = $('#btnConfirmOk'); if (b1) b1.onclick = doConfirmOk;'''
if OLD_DOM in c:
    c = c.replace(OLD_DOM, NEW_DOM, 1)
    changes.append("启动时加载策略")

io.open(HTML, 'w', encoding='utf-8').write(c)
print("修改:")
for x in changes:
    print("  -", x)
print()
print("长度:", orig, "->", len(c))
