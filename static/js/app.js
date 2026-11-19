const msg = document.getElementById('msg');
const view = document.getElementById('view');
const guardBanner = document.getElementById('guard-banner');
const guardChip = document.getElementById('guard-chip');
const strategyChip = document.getElementById('strategy-chip');
let contracts = [];
let catalog = null;
let strategy = {key: 'lock', label: '锁表切流'};
let guard = {ok: true, halted: false};

const STRATEGY_TEXT = {lock: '锁表切流', dual: '旁路双写'};

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: {'Content-Type': 'application/json', ...(opts.headers || {})},
    ...opts
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    const err = new Error(j.error || ('HTTP ' + r.status));
    err.payload = j;
    err.status = r.status;
    throw err;
  }
  return j;
}

function esc(text) {
  return String(text ?? '').replace(/[&<>"']/g, ch => (
    {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]
  ));
}

function setMsg(t, isErr = false) {
  msg.textContent = t || '';
  msg.className = isErr ? 'err' : 'muted';
}

function chipClass(status) {
  if (status === '通过' || status === '已对齐' || status === '现行' || status === '已应用') return 'chip ok';
  if (status === '未通过' || status === '未知列名' || status === '失败' || status === '双现行停机') return 'chip bad';
  if (status === '双写过渡' || status === '旁路双写' || status === '双写' || status === '旧列停写' || status === '已停写') return 'chip warn';
  return 'chip';
}

function applyGuard(g) {
  guard = g || {ok: true, halted: false, duplicates: []};
  guardChip.textContent = guard.halted ? '双现行 · 已停机' : '现行唯一';
  guardChip.className = guard.halted ? 'chip bad' : 'chip ok';
  if (guard.halted) {
    const names = (guard.duplicates || []).map(d =>
      `${esc(d.table_name)}（${d.live_versions.map(v => 'v' + v).join('、')}）`).join('；');
    guardBanner.className = 'banner bad';
    guardBanner.innerHTML = `双现行停机：${names}。版号与目录现行必须同源，先人工清点现行契约再切流。`;
  } else {
    guardBanner.className = 'banner hidden';
    guardBanner.textContent = '';
  }
}

function applyStrategy(s) {
  if (!s) return;
  strategy = {key: s.strategy || 'lock', label: s.strategy_label || STRATEGY_TEXT[s.strategy] || '锁表切流'};
  strategyChip.textContent = `切流方式 · ${strategy.label}`;
}

async function loadMeta() {
  const meta = await api('/api/meta');
  document.getElementById('lab-name').textContent = meta.lab || '契约换版台';
  applyStrategy({strategy: meta.strategy, strategy_label: STRATEGY_TEXT[meta.strategy]});
  applyGuard(meta.guard);
}

async function loadContracts() {
  const data = await api('/api/contracts');
  contracts = data.items || [];
}

async function loadCatalog() {
  const data = await api('/api/catalog');
  catalog = data.catalog;
  applyStrategy({strategy: catalog.strategy, strategy_label: catalog.strategy_label});
}

function liveCell(c) {
  if (c.duplicate_live) return `<span class="chip bad">双现行 ${c.live_versions.map(v => 'v' + v).join('/')}</span>`;
  if (c.live_version == null) return '<span class="muted">无现行</span>';
  return `v${c.live_version}`;
}

function lastApplyCell(c) {
  const last = c.last_apply;
  if (!last) return '<span class="muted">未切过</span>';
  const cls = last.result === '失败' ? 'bad' : 'ok';
  const reason = last.result === '失败' ? `<div class="fail">失败因：${esc(last.failure_reason)}</div>` : '';
  return `<span class="chip ${cls}">${esc(last.result)}${last.strategy ? ' · ' + esc(last.strategy) : ''}</span>${reason}`;
}

function renderContracts() {
  const tables = [...new Set(contracts.map(c => c.table_name))];
  view.innerHTML = `
    <div class="grid">
      ${tables.map(table => {
        const rows = contracts.filter(c => c.table_name === table);
        const head = rows[0];
        return `
        <section class="card">
          <div class="row spread">
            <h3>${esc(table)}</h3>
            <div class="row">
              <span class="mini">契约现行 ${liveCell(head)}</span>
              <span class="mini">目录现行 ${head.catalog_live_version == null ? '—' : 'v' + head.catalog_live_version}</span>
              <span class="mini">读指针 ${head.read_version == null ? '—' : 'v' + head.read_version}</span>
              <span class="${chipClass(head.cutover_mode)}">${esc(head.cutover_mode)}</span>
            </div>
          </div>
          <table class="tbl">
            <thead><tr><th>版号</th><th>状态</th><th>字段</th><th>说明</th><th>最近切流</th></tr></thead>
            <tbody>
              ${rows.map(c => `
                <tr>
                  <td>v${c.version_no}</td>
                  <td><span class="${chipClass(c.status === '现行' ? '通过' : '')}">${esc(c.status)}</span></td>
                  <td class="muted">${(c.fields || []).map(f => esc(f.field_name) + ':' + esc(f.field_type)).join(' · ')}</td>
                  <td class="muted">${esc(c.note || '')}</td>
                  <td>${lastApplyCell(c)}</td>
                </tr>`).join('')}
            </tbody>
          </table>
        </section>`;
      }).join('')}
    </div>`;
}

async function renderFields() {
  view.innerHTML = `
    <div class="card grid">
      <label>契约版本
        <select id="fid">${contracts.map(c =>
          `<option value="${c.id}">${esc(c.table_name)} v${c.version_no} · ${esc(c.status)}</option>`).join('')}</select>
      </label>
    </div>
    <div id="fields-holder"></div>`;
  const holder = document.getElementById('fields-holder');
  const load = () => showFields(document.getElementById('fid').value, holder);
  document.getElementById('fid').onchange = load;
  const first = contracts.find(c => c.status === '草稿') || contracts[0];
  if (first) await showFields(first.id, holder);
  else holder.innerHTML = '<p class="muted">暂无契约。</p>';
}

async function showFields(cid, holder) {
  if (!cid) return;
  holder.innerHTML = '<p class="muted">读取字段详情…</p>';
  try {
    const d = (await api(`/api/contracts/${cid}/fields`)).detail;
    const c = d.contract;
    const liveText = d.duplicate_live
      ? `<span class="chip bad">双现行 ${d.live_versions.map(v => 'v' + v).join('/')}</span>`
      : (d.live_version == null ? '<span class="muted">无现行</span>' : 'v' + d.live_version);
    holder.innerHTML = `
      <section class="card">
        <div class="row spread">
          <h3>${esc(c.table_name)} · v${c.version_no} <span class="chip">${esc(c.status)}</span></h3>
          <div class="row">
            <span class="mini">契约现行 ${liveText}</span>
            <span class="mini">目录现行 ${d.catalog_live_version == null ? '—' : 'v' + d.catalog_live_version}</span>
            <span class="mini">读指针 ${d.read_version == null ? '—' : 'v' + d.read_version}</span>
            <span class="${chipClass(d.cutover_mode)}">${esc(d.cutover_mode)}</span>
          </div>
        </div>
        <table class="tbl">
          <thead><tr><th>字段</th><th>本版类型</th><th>现行类型</th><th>目录写状态</th><th>清洗作业引用</th></tr></thead>
          <tbody>${d.fields.map(f => `
            <tr>
              <td>${esc(f.field_name)}</td>
              <td>${esc(f.field_type)}</td>
              <td>${f.live_type ? esc(f.live_type) : '<span class="muted">—</span>'}</td>
              <td><span class="${chipClass(f.catalog_state)}">${esc(f.catalog_state)}</span></td>
              <td class="muted small">${f.referenced_by.map(esc).join('、') || '—'}</td>
            </tr>`).join('') || '<tr><td colspan="5" class="muted">无字段</td></tr>'}</tbody>
        </table>
      </section>`;
  } catch (e) {
    holder.innerHTML = `<p class="err">${esc(e.message)}</p>`;
  }
}

function draftOptions() {
  const drafts = contracts.filter(c => c.status === '草稿');
  return drafts.map(c => `<option value="${c.id}">${esc(c.table_name)} v${c.version_no}</option>`).join('')
    || '<option value="">（暂无草稿）</option>';
}

function renderCard(card) {
  const c = card.contract;
  const check = card.breaking_check || {};
  const last = card.last_apply;
  const modeLocked = card.strategy_locked && card.cutover_mode !== '初始';
  const mode = card.strategy_locked ? card.cutover_mode : STRATEGY_TEXT[card.strategy_default] || '锁表切流';
  const fields = (card.fields || []).map(f => `<li>${esc(f.field_name)} <span class="muted">${esc(f.field_type)}</span></li>`).join('');
  const cols = (card.catalog_columns || []).map(col =>
    `<li>${esc(col.column_name)} <span class="muted">${esc(col.column_type)}</span>
      <span class="${chipClass(col.write_state)}">${esc(col.write_state)}</span></li>`).join('');
  const jobs = (card.cleaning_jobs || []).map(j => `
    <li>${esc(j.job_name)} <span class="${chipClass(j.status)}">${esc(j.status)}</span>
      <div class="muted small">${esc(j.last_detail)}</div></li>`).join('');
  const failBlock = last && last.result === '失败'
    ? `<div class="fail box"><strong>最近一次失败因</strong><br>${esc(last.failure_reason)}</div>`
    : '';
  return `
    <div class="grid">
      <section class="card">
        <div class="row spread">
          <h3>${esc(c.table_name)} · v${c.version_no} <span class="chip">${esc(c.status)}</span></h3>
          <span class="${chipClass(card.cutover_mode)}">${esc(card.cutover_mode)}</span>
        </div>
        <p class="muted">${esc(c.note || '')}</p>
        <div class="row">
          <label class="inline">切流方式
            <select id="strategy">
              <option value="lock" ${mode === '锁表切流' ? 'selected' : ''} ${modeLocked ? 'disabled' : ''}>锁表切流</option>
              <option value="dual" ${mode === '旁路双写' ? 'selected' : ''} ${modeLocked ? 'disabled' : ''}>旁路双写</option>
            </select>
          </label>
          <span class="mini">${modeLocked ? '这张表已选定命运，不能换另一条' : '选边后入库只认这一套字段'}</span>
        </div>
        <div class="row">
          <button class="primary" id="btn-apply" type="button" ${card.contract.status === '草稿' && !card.duplicate_live ? '' : 'disabled'}>按选定命运切流落库</button>
          <button type="button" id="btn-check">破坏性检查</button>
        </div>
        ${failBlock}
      </section>
      <section class="card">
        <div class="row spread">
          <h3>版号与目录现行</h3>
          <span class="mini">页面给指针时以现行 v${card.live_version ?? '—'} 为准</span>
        </div>
        <div class="row">
          <span class="mini">契约现行 ${card.duplicate_live ? '<span class="chip bad">双现行 ' + card.live_versions.map(v => 'v' + v).join('/') + '</span>' : (card.live_version == null ? '—' : 'v' + card.live_version)}</span>
          <span class="mini">目录现行 ${card.catalog_live_version == null ? '—' : 'v' + card.catalog_live_version}</span>
          <span class="mini">读指针 ${card.read_version == null ? '—' : 'v' + card.read_version}</span>
          <span class="mini">写集 ${(card.write_versions || []).map(v => 'v' + v).join('、') || '—'}</span>
        </div>
        <div class="cols">
          <div><h4>草稿字段</h4><ul class="plain">${fields || '<li class="muted">无</li>'}</ul></div>
          <div><h4>目录列名</h4><ul class="plain">${cols || '<li class="muted">无</li>'}</ul></div>
        </div>
      </section>
      <section class="card">
        <h3>破坏性检查结果</h3>
        ${check.error ? `<p class="err">${esc(check.error)}</p>` : `
          <p>结论：<span class="${check.breaking ? 'chip bad' : 'chip ok'}">${esc(check.verdict || '')}</span></p>
          <p class="muted small">删列：${(check.removed_fields || []).join('、') || '无'}；改类型：${(check.type_changed_fields || []).join('、') || '无'}；新增：${(check.added_fields || []).join('、') || '无'}</p>`}
        <h4>受影响的清洗作业</h4>
        <ul class="plain">${jobs || '<li class="muted">无</li>'}</ul>
      </section>
    </div>`;
}

async function renderApply() {
  view.innerHTML = `
    <div class="card grid">
      <label>草稿契约
        <select id="cid">${draftOptions()}</select>
      </label>
      <button class="primary" id="btn-load" type="button">打开换版卡片</button>
    </div>
    <div id="card-holder"></div>`;
  const holder = document.getElementById('card-holder');
  document.getElementById('btn-load').onclick = () => showCard(document.getElementById('cid').value, holder);
  const first = contracts.find(c => c.status === '草稿');
  if (first) await showCard(first.id, holder);
  else holder.innerHTML = '<p class="muted">没有草稿契约可切流。</p>';
}

async function showCard(cid, holder) {
  if (!cid) return;
  holder.innerHTML = '<p class="muted">读取换版卡片…</p>';
  try {
    const data = await api(`/api/contracts/${cid}/cutover-card`);
    const card = data.card;
    holder.innerHTML = renderCard(card);
    document.getElementById('btn-check').onclick = async () => {
      try {
        const j = await api(`/api/contracts/${cid}/breaking-check`, {method: 'POST', body: '{}'});
        setMsg(`检查结论：${j.check.verdict}`);
        await showCard(cid, holder);
      } catch (e) { setMsg(e.message, true); }
    };
    const applyBtn = document.getElementById('btn-apply');
    applyBtn.onclick = async () => {
      const chosen = document.getElementById('strategy').value;
      applyBtn.disabled = true;
      try {
        const j = await api(`/api/contracts/${cid}/apply`, {
          method: 'POST',
          body: JSON.stringify({strategy: chosen, expected_live_version: card.live_version})
        });
        setMsg(`切流完成：${j.contract.table_name} v${j.to_version}，${j.strategy_label}，读指针 v${j.read_version}`);
        await refreshAll();
        await showCard(cid, holder);
      } catch (e) {
        setMsg(e.message, true);
        await refreshAll();
        await showCard(cid, holder);
      }
    };
  } catch (e) {
    holder.innerHTML = `<p class="err">${esc(e.message)}</p>`;
  }
}

function progressRows(items) {
  if (!items || !items.length) return '<p class="muted">暂无记录</p>';
  return `<table class="tbl">
    <thead><tr><th>范围</th><th>表</th><th>版本</th><th>结论</th><th>说明</th></tr></thead>
    <tbody>${items.map(r => `
      <tr>
        <td>${esc(r.scope)}</td>
        <td>${esc(r.table_name)}</td>
        <td>v${r.version_no}</td>
        <td><span class="${chipClass(r.result)}">${esc(r.result)}</span></td>
        <td class="muted small">${esc(r.detail)}</td>
      </tr>`).join('')}</tbody></table>`;
}

async function renderCatalog() {
  await loadCatalog();
  const tables = (catalog && catalog.tables) || [];
  view.innerHTML = `
    <div class="card grid">
      <div class="row spread">
        <div class="row">
          <button class="primary" id="btn-run" type="button">清洗 + 目录各复跑一轮</button>
          <button type="button" id="btn-progress">只看对齐记录</button>
        </div>
        <span class="mini">当前命运：${esc(catalog ? catalog.strategy_label : '')}</span>
      </div>
      <div id="progress-holder"></div>
    </div>
    ${tables.map(t => `
      <section class="card">
        <div class="row spread">
          <h3>${esc(t.table_name)}</h3>
          <div class="row">
            <span class="${chipClass(t.cutover_mode)}">${esc(t.cutover_mode)}</span>
            <span class="mini">契约现行 v${t.contract_live_version ?? '—'}</span>
            <span class="mini">目录现行 v${t.live_version ?? '—'}</span>
            <span class="mini">读指针 v${t.read_version ?? '—'}</span>
            <span class="mini">写集 ${(t.write_versions || []).map(v => 'v' + v).join('、') || '—'}</span>
          </div>
        </div>
        ${t.duplicate_live ? `<p class="err">双现行：${t.live_versions.map(v => 'v' + v).join('、')}，已停机。</p>` : ''}
        <h4>目录列名</h4>
        <ul class="plain cols2">${t.columns.map(c => `
          <li>${esc(c.column_name)} <span class="muted">${esc(c.column_type)}</span>
            <span class="${chipClass(c.write_state)}">${esc(c.write_state)}</span></li>`).join('') || '<li class="muted">无</li>'}</ul>
        <h4>清洗作业进度</h4>
        <ul class="plain">${t.cleaning_jobs.map(j => `
          <li>${esc(j.job_name)} <span class="${chipClass(j.status)}">${esc(j.status)}</span>
            <span class="muted small">列：${(j.columns || []).join('、')} · 绑 v${j.bound_version ?? '—'}</span>
            <div class="muted small">${esc(j.last_detail)}</div></li>`).join('') || '<li class="muted">无</li>'}</ul>
      </section>`).join('')}`;

  const holder = document.getElementById('progress-holder');
  document.getElementById('btn-run').onclick = async () => {
    try {
      const j = await api('/api/progress-checks', {method: 'POST', body: JSON.stringify({scope: 'both'})});
      const fails = (j.run.failures || []);
      setMsg(j.run.passed ? `第 ${j.run.run_id} 轮：清洗与目录列名全部对齐` : `第 ${j.run.run_id} 轮有 ${fails.length} 项未通过`, !j.run.passed);
      await renderCatalog();
      document.getElementById('progress-holder').innerHTML = progressRows([
        ...(j.run.checks['清洗'] || []), ...(j.run.checks['目录'] || [])
      ]);
    } catch (e) { setMsg(e.message, true); }
  };
  document.getElementById('btn-progress').onclick = async () => {
    const j = await api('/api/progress-checks');
    holder.innerHTML = progressRows(j.items);
  };
}

async function renderReconcile() {
  const reports = (await api('/api/reconcile-reports')).items || [];
  view.innerHTML = `
    <div class="card grid">
      <div class="row">
        <button class="primary" id="btn-rec" type="button">跑一轮对账</button>
      </div>
      <div class="grid" id="reps">${reports.map(r => `
        <article class="card"><strong>${esc(r.as_of)}</strong>
        <p>${esc(r.summary)}</p></article>`).join('') || '<p class="muted">暂无对账</p>'}</div>
    </div>`;
  document.getElementById('btn-rec').onclick = async () => {
    try {
      const j = await api('/api/reconcile-reports', {
        method: 'POST', body: JSON.stringify({as_of: '2026-11-18'})
      });
      setMsg(j.report.summary);
      await renderReconcile();
    } catch (e) { setMsg(e.message, true); }
  };
}

async function refreshAll() {
  await loadMeta();
  await loadContracts();
}

async function show(tab) {
  setMsg('');
  await refreshAll();
  if (tab === 'contracts') renderContracts();
  if (tab === 'fields') await renderFields();
  if (tab === 'apply') await renderApply();
  if (tab === 'catalog') await renderCatalog();
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
  await api('/api/logout', {method: 'POST', body: '{}'});
  location.href = '/login';
};
show('contracts');
