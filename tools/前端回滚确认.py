# -*- coding: utf-8 -*-
"""前端：给 3 个回滚操作加确认弹窗。"""
import io
import sys

sys.stdout.reconfigure(encoding='utf-8')

HTML = r'D:\deepseek文件区\manju\src\manju\web\static\index.html'
c = io.open(HTML, encoding='utf-8').read()
orig = len(c)
changes = []

# ---------- 1. restoreVariant（素材页：还原变体） ----------
OLD1 = '''async function restoreVariant(idx, version) {
  const proj = $('#astProject').value;
  const ch = $('#astChapter').value || '1';
  const kind = $('#replaceKind').value;
  try {
    const r = await fetch(
      `/api/assets/${encodeURIComponent(proj)}/${ch}/shot_${String(idx).padStart(3,'0')}/restore`,
      {
        method: 'POST',
        headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
        body: JSON.stringify({ version: version, kind: kind }),
      });
    const d = await r.json();
    if (!r.ok) { alert(d.detail || '还原失败'); return; }
    alert(d.msg || '已还原');
    showShotDetail(idx);
    loadAssets();
  } catch (e) { alert('还原失败：' + e); }
}'''

NEW1 = '''async function restoreVariant(idx, version) {
  const proj = $('#astProject').value;
  const ch = $('#astChapter').value || '1';
  const kind = $('#replaceKind').value;
  const shotId = 'shot_' + String(idx).padStart(3, '0');

  // 确认弹窗
  const params = {
    project: proj, chapter: Number(ch),
    shot: shotId, version: version, kind: kind,
  };
  const ok = await requestConfirm('asset.restore', params);
  if (!ok) return;

  try {
    const r = await fetch(
      `/api/assets/${encodeURIComponent(proj)}/${ch}/${shotId}/restore`,
      {
        method: 'POST',
        headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
        body: JSON.stringify({
          version: version, kind: kind, confirm_token: CONFIRM.token,
        }),
      });
    const d = await r.json();
    if (!r.ok) { alert(d.detail || '还原失败'); return; }
    alert(d.msg || '已还原');
    showShotDetail(idx);
    loadAssets();
  } catch (e) { alert('还原失败：' + e); }
}'''

if OLD1 in c:
    c = c.replace(OLD1, NEW1, 1)
    changes.append("restoreVariant 加确认")

# ---------- 2. chooseShotVersion（版本页：选用版本） ----------
OLD2 = '''async function chooseShotVersion(shotId, version) {
  const proj = $('#verProject').value;
  const ch = $('#verChapter').value || '1';
  try {
    const r = await fetch(
      `/api/versions/${encodeURIComponent(proj)}/${ch}/shots/${encodeURIComponent(shotId)}/choose`,
      {
        method: 'POST',
        headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
        body: JSON.stringify({ version: version }),
      });
    const d = await r.json();
    if (!r.ok) { alert(d.detail || '选用失败'); return; }
    alert(d.msg || '已选用');
    loadShotVersions();
  } catch (e) { alert('选用失败：' + e); }
}'''

NEW2 = '''async function chooseShotVersion(shotId, version) {
  const proj = $('#verProject').value;
  const ch = $('#verChapter').value || '1';

  // 确认弹窗
  const params = {
    project: proj, chapter: Number(ch),
    shot: shotId, version: version, kind: 'image',
  };
  const ok = await requestConfirm('asset.choose', params);
  if (!ok) return;

  try {
    const r = await fetch(
      `/api/versions/${encodeURIComponent(proj)}/${ch}/shots/${encodeURIComponent(shotId)}/choose`,
      {
        method: 'POST',
        headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
        body: JSON.stringify({ version: version, confirm_token: CONFIRM.token }),
      });
    const d = await r.json();
    if (!r.ok) { alert(d.detail || '选用失败'); return; }
    alert(d.msg || '已选用');
    loadShotVersions();
  } catch (e) { alert('选用失败：' + e); }
}'''

if OLD2 in c:
    c = c.replace(OLD2, NEW2, 1)
    changes.append("chooseShotVersion 加确认")

# ---------- 3. restoreTrash（回收站：还原） ----------
OLD3 = '''async function restoreTrash(tid) {
  if (!confirm('确认把这个条目还原到原位置？\\n\\n如果原位置已有同名文件，会自动加时间戳后缀，不会覆盖。')) return;
  try {
    const r = await fetch(`/api/trash/${encodeURIComponent(tid)}/restore`, {
      method: 'POST', headers: authHeaders(),
    });
    const d = await r.json();
    if (!r.ok) { alert(d.detail || '还原失败'); return; }
    const res = d.result || {};
    let msg = `还原完成\\n\\n成功 ${res.restored || 0} 个`;
    if (res.skipped) msg += `\\n跳过 ${res.skipped} 个`;
    alert(msg);
    loadTrash();
  } catch (e) { alert('还原失败：' + e); }
}'''

NEW3 = '''async function restoreTrash(tid) {
  // 确认弹窗
  const params = { trash_id: tid };
  const ok = await requestConfirm('trash.restore', params);
  if (!ok) return;

  try {
    const r = await fetch(`/api/trash/${encodeURIComponent(tid)}/restore`, {
      method: 'POST',
      headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
      body: JSON.stringify({ confirm_token: CONFIRM.token }),
    });
    const d = await r.json();
    if (!r.ok) { alert(d.detail || '还原失败'); return; }
    const res = d.result || {};
    let msg = `还原完成\\n\\n成功 ${res.restored || 0} 个`;
    if (res.skipped) msg += `\\n跳过 ${res.skipped} 个`;
    alert(msg);
    loadTrash();
  } catch (e) { alert('还原失败：' + e); }
}'''

if OLD3 in c:
    c = c.replace(OLD3, NEW3, 1)
    changes.append("restoreTrash 加确认")

# ---------- 4. 批量选用版本也要传 confirm_token ----------
OLD4 = '''  let confirmToken = null;

  // 删除操作需要确认
  if (op === 'delete') {
    const params = { project: proj, chapter: Number(ch), shots: shots };
    const ok = await requestConfirm('batch.delete', params);
    if (!ok) return;
    confirmToken = CONFIRM.token;
  } else {
    const opName = $('#batchOp').selectedOptions[0].text;
    if (!confirm(`确认对 ${shots.length} 个镜头执行「${opName}」？`)) return;
  }'''

NEW4 = '''  let confirmToken = null;

  // 删除和选用版本都需要确认（会覆盖文件）
  if (op === 'delete' || op === 'choose') {
    const chosenOp = (op === 'delete') ? 'batch.delete' : 'asset.choose';
    const params = {
      project: proj, chapter: Number(ch), shots: shots,
      version: ver || null,
    };
    const ok = await requestConfirm(chosenOp, params);
    if (!ok) return;
    confirmToken = CONFIRM.token;
  } else {
    const opName = $('#batchOp').selectedOptions[0].text;
    if (!confirm(`确认对 ${shots.length} 个镜头执行「${opName}」？`)) return;
  }'''

if OLD4 in c:
    c = c.replace(OLD4, NEW4, 1)
    changes.append("批量选用加确认")

io.open(HTML, 'w', encoding='utf-8').write(c)
print("修改:", changes)
print()
print("长度:", orig, "->", len(c))
