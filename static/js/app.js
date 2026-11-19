const msg = document.getElementById('msg');
const view = document.getElementById('view');
let contracts = [];

async function api(path, opts={}) {
  const r = await fetch(path, {
    headers: {'Content-Type': 'application/json', ...(opts.headers||{})},
    ...opts
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(j.error || ('HTTP '+r.status)), {body: j});
  return j;
}

function setMsg(t, isErr=false) {
  msg.textContent = t || '';
  msg.className = isErr ? 'err' : 'muted';
}

// 同一命运同一颗芯片：硬闸结论在四处页面用同一份数据、同一个样式
function gateChip(verdict) {
  if (verdict === '破坏性') return '<span class="chip chip-block">硬拒</span>';
  if (verdict === '兼容') return '<span class="chip chip-pass">可升</span>';
  return '<span class="chip chip-idle">未检</span>';
}

async function loadContracts() {
  const data = await api('/api/contracts');
  contracts = data.items || [];
}

function draftOptions() {
  return contracts.filter(c => c.status === '草稿')
    .map(c => `<option value="${c.id}">${c.table_name} v${c.version_no}</option>`).join('');
}

// ① 队列：每张契约卡片带硬闸芯片，草稿旁标同一命运
function renderContracts() {
  view.innerHTML = `<div class="grid">${contracts.map(c => `
    <article class="card">
      <div class="row">
        <h3>${c.table_name} · v${c.version_no}</h3>
        <span class="chip">${c.status}</span>
        ${c.status === '草稿' ? gateChip(c.gate_verdict) : ''}
      </div>
      <p class="muted">${c.note || ''} · 字段 ${c.field_count}</p>
    </article>`).join('')}</div>`;
}

// ② 契约详情（破坏性检查页）：检查结论留存只读历史，破坏性即硬拒芯片
async function renderCheck() {
  const checks = (await api('/api/breaking-checks')).items || [];
  view.innerHTML = `
    <div class="card grid">
      <label>草稿契约
        <select id="cid">${draftOptions()}</select>
      </label>
      <button class="primary" id="btn-check" type="button">运行破坏性检查</button>
      <pre id="out" class="muted"></pre>
    </div>
    <h3 class="section-title">检查报告（只读历史，只追加）</h3>
    <div class="grid" id="checks">${checks.map(k => `
      <article class="card">
        <div class="row">
          <strong>${k.table_name} v${k.to_version}</strong>
          ${k.verdict === '破坏性'
            ? '<span class="chip chip-block">硬拒</span>'
            : '<span class="chip chip-pass">可升</span>'}
        </div>
        <p class="muted">对照 v${k.from_version} · ${k.created_at}</p>
        <p class="muted">删列：${(k.removed_fields||[]).join('、') || '无'} ｜
           改类型：${(k.type_changed_fields||[]).join('、') || '无'} ｜
           新增：${(k.added_fields||[]).join('、') || '无'}</p>
      </article>`).join('') || '<p class="muted">暂无检查报告</p>'}</div>`;
  document.getElementById('btn-check').onclick = async () => {
    try {
      const cid = document.getElementById('cid').value;
      const j = await api(`/api/contracts/${cid}/breaking-check`, {method:'POST', body:'{}'});
      document.getElementById('out').textContent = JSON.stringify(j.check, null, 2);
      const blocked = j.check.verdict === '破坏性';
      setMsg(blocked
        ? `检查结论：破坏性 → 硬闸已锁，升版将被硬拒（删列 ${j.check.removed_fields.join('、')}）`
        : '检查结论：兼容，硬闸放行', blocked);
      await loadContracts();
      await renderCheck();
    } catch (e) { setMsg(e.message, true); }
  };
}

// ③ 换版附页：点升版引擎按硬闸一次判写，失败吐白话短句、亮硬拒芯片
function renderApply() {
  const drafts = contracts.filter(c => c.status === '草稿');
  const lives = contracts.filter(c => c.status === '现行');
  view.innerHTML = `
    <div class="card grid">
      <label>草稿契约
        <select id="cid">${draftOptions()}</select>
      </label>
      <button class="primary" id="btn-apply" type="button">换版应用（硬闸一次判写）</button>
    </div>
    <h3 class="section-title">草稿队列</h3>
    <div class="grid">${drafts.map(c => `
      <article class="card">
        <div class="row">
          <strong>${c.table_name} v${c.version_no}</strong>
          ${gateChip(c.gate_verdict)}
        </div>
        <p class="muted">${c.note || ''}</p>
      </article>`).join('') || '<p class="muted">暂无草稿</p>'}</div>
    <h3 class="section-title">误升回滚（回滚后按硬闸重判）</h3>
    <div class="grid">${lives.map(c => `
      <article class="card">
        <div class="row">
          <strong>${c.table_name} v${c.version_no}</strong>
          <button type="button" class="ghost btn-rollback" data-id="${c.id}">整笔回滚</button>
        </div>
      </article>`).join('') || '<p class="muted">暂无现行契约</p>'}</div>`;
  document.getElementById('btn-apply').onclick = async () => {
    try {
      const cid = document.getElementById('cid').value;
      const j = await api(`/api/contracts/${cid}/apply`, {method:'POST', body:'{}'});
      setMsg(`已应用 ${j.contract.table_name} v${j.contract.version_no}`);
      await loadContracts();
      renderApply();
    } catch (e) {
      // 硬拒：白话短句 + 红芯片，队列同步命运
      setMsg(e.message, true);
      await loadContracts();
      renderApply();
    }
  };
  document.querySelectorAll('.btn-rollback').forEach(btn => {
    btn.onclick = async () => {
      try {
        const j = await api(`/api/contracts/${btn.dataset.id}/rollback`,
          {method:'POST', body:'{}'});
        setMsg(`${j.rollback.reason}（现行 v${j.rollback.restored_live} 已复位）`);
        await loadContracts();
        renderApply();
      } catch (e) { setMsg(e.message, true); }
    };
  });
}

// ④ 对账水线：matched/drifted/blocked 同命运，硬拒数来自同一硬闸
async function renderReconcile() {
  const reports = (await api('/api/reconcile-reports')).items || [];
  view.innerHTML = `
    <div class="card grid">
      <div class="row">
        <button class="primary" id="btn-rec" type="button">跑一轮对账复验</button>
      </div>
      <div class="grid" id="reps">${reports.map(r => `
        <article class="card">
          <div class="row">
            <strong>${r.as_of}</strong>
            ${(r.blocked || 0) > 0
              ? `<span class="chip chip-block">硬拒 ${r.blocked}</span>`
              : '<span class="chip chip-pass">无硬拒</span>'}
          </div>
          <p>${r.summary}</p>
        </article>`).join('') || '<p class="muted">暂无对账</p>'}</div>
    </div>`;
  document.getElementById('btn-rec').onclick = async () => {
    try {
      const j = await api('/api/reconcile-reports', {
        method:'POST', body: JSON.stringify({as_of: '2026-11-18'})
      });
      setMsg(j.report.summary, (j.report.blocked || 0) > 0);
      await renderReconcile();
    } catch (e) { setMsg(e.message, true); }
  };
}

async function show(tab) {
  setMsg('');
  await loadContracts();
  if (tab === 'contracts') renderContracts();
  if (tab === 'check') await renderCheck();
  if (tab === 'apply') renderApply();
  if (tab === 'reconcile') await renderReconcile();
}

document.querySelectorAll('.pills button').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('.pills button').forEach(b => b.classList.remove('on'));
    btn.classList.add('on');
    show(btn.dataset.tab);
  };
});
document.getElementById('btn-logout').onclick = async () => {
  await api('/api/logout', {method:'POST', body:'{}'});
  location.href = '/login';
};
show('contracts');
