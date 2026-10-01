/* 叉车换电管理前端（零依赖） */
const $ = (s, r=document) => r.querySelector(s);
const $$ = (s, r=document) => [...r.querySelectorAll(s)];

const state = { tab:'pool', pool:null, vehicles:[], queue:[], swaps:[], charges:[],
                health:[], selectedQueue:null, batteries:[] };

const STATUS_LABEL = {loose:'待装', charging:'充电中', on_vehicle:'在车上', isolated:'已隔离'};
const VSTATE = {idle:'空闲', working:'作业中', awaiting_swap:'待换电', swapping:'换电中'};

const flame = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2c1 4-3 5-3 9 0 1 .4 2 1 2.6C9.4 13 8 11.4 8 9c-2 2-3 4.2-3 6.5C5 19 8 22 12 22s7-3 7-6.5C19 11 15 7.5 12 2z"/></svg>';
const clock = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>';

function hhmm(ts){
  if(!ts) return '—';
  const d = new Date(ts*1000);
  return String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0')+
         ':'+String(d.getSeconds()).padStart(2,'0');
}
function dt(ts){ return ts ? new Date(ts*1000).toLocaleString('zh-CN',{hour12:false}) : '—'; }
function esc(s){ return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

function toast(msg, ok=true){
  const el = document.createElement('div');
  el.className = ok ? 'ok' : 'err';
  el.textContent = (ok?'✓ ':'✕ ') + msg;
  $('#toast').appendChild(el);
  setTimeout(()=>el.remove(), 3600);
}

async function api(path, body){
  const opt = body ? {method:'POST', headers:{'Content-Type':'application/json'},
                      body:JSON.stringify({actor:$('#actor').value || '未署名',
                                           role:$('#role').value, ...body})}
                   : undefined;
  const res = await fetch(path, opt);
  const data = await res.json().catch(()=>({}));
  if(!res.ok) throw new Error(data.error || ('HTTP '+res.status));
  return data;
}

/* ---------- 电池卡片 ---------- */
function batteryCard(b, opts={}){
  const hot = b.block_reasons.includes('overheat');
  const cls = ['batt'];
  if(b.isolated) cls.push('isolated');
  else if(b.usable) cls.push('usable');
  else if(b.status==='charging') cls.push('charging');
  else if(b.block_reasons.length) cls.push('blocked');

  const tags = [];
  if(b.status==='on_vehicle') tags.push('<span class="tag info">在车上 '+esc(b.bound_to?.target||'')+'</span>');
  if(b.status==='charging') tags.push('<span class="tag warn">⚡ 充电中</span>');
  if(b.status==='isolated') tags.push('<span class="tag iso">⛔ 已隔离</span>');
  if(hot) tags.push('<span class="tag hot">'+flame+'高温 '+esc(b.temperature.toFixed(1))+'℃</span>');
  for(const r of b.block_reasons){
    if(r==='overheat') continue;
    if(r==='low_soc') tags.push('<span class="tag warn">电量不足</span>');
    else if(r==='isolated') tags.push('<span class="tag iso">健康隔离</span>');
    else tags.push('<span class="tag iso">'+esc(b.block_labels[b.block_reasons.indexOf(r)])+'</span>');
  }
  if(b.usable) tags.push('<span class="tag ok">✓ 可入可用池</span>');
  tags.push('<span class="tag">健康度 '+b.health_score+'</span>');

  const eta = b.eta ? `<div class="eta">${clock} 预计充满 ${hhmm(b.eta)}</div>` : '';
  const tempCls = hot || b.temperature>=50 ? 'hot' : '';
  const socLow = b.soc<60;
  return `<div class="${cls.join(' ')}" data-code="${esc(b.code)}">
    <div><span class="code">${esc(b.code)}</span><span class="model">${esc(b.model)}</span></div>
    <div class="soc-line">
      <div class="soc-bar"><i class="${socLow?'low':''}" style="width:${b.soc}%"></i></div>
      <span class="soc-val">${b.soc}%</span>
    </div>
    <span class="temp ${tempCls}">${hot?flame+'<span>高温</span> ':''}${b.temperature.toFixed(1)}℃</span>
    <div class="tags">${tags.join('')}</div>
    ${eta}
    ${opts.action||''}
  </div>`;
}

/* ---------- 池位图 ---------- */
async function loadPool(){
  const [ov, pool] = await Promise.all([api('/api/overview'), api('/api/pool')]);
  state.overview = ov; state.pool = pool;
  state.batteries = await api('/api/batteries');
  if(state.tab==='pool') renderPool();
  if(state.tab==='swap') renderSwapStage();
}

function renderPool(){
  const c = state.overview.counts;
  $('#pool-stats').innerHTML = [
    ['电池总数', c.batteries, ''],
    ['可入池', c.usable, 'ok'],
    ['充电中', c.charging, ''],
    ['在车上', c.on_vehicle, ''],
    ['隔离', c.isolated, c.isolated?'alarm':''],
    ['过热', c.overheat, c.overheat?'alarm':''],
  ].map(([l,n,cls])=>`<div class="stat ${cls}"><div class="n">${n}</div><div class="l">${l}</div></div>`).join('');

  $('#slots').innerHTML = state.pool.slots.map(s=>{
    const head = `<div class="slot-head">
        <b>${esc(s.code)}</b>
        ${s.serviceable ? '<span class="tag ok">正常</span>'
                        : '<span class="tag hot">⚠ 槽位故障</span>'}
      </div>
      <div class="muted">${esc(s.charger_id)}${s.fault_reason?' · '+esc(s.fault_reason):''}</div>`;
    return `<div class="slot ${s.serviceable?'':'fault'}">${head}
      ${s.battery ? batteryCard(s.battery)
                   : (s.serviceable ? '<div class="empty">空槽位 · 可上架充电</div>'
                                    : '<div class="empty">维修中 · 停止充电信号</div>')}
    </div>`;
  }).join('');

  $('#loose').innerHTML = state.pool.loose.length
    ? state.pool.loose.map(b=>batteryCard(b)).join('')
    : '<div class="muted">暂无未上架电池</div>';

  $('#isolated').innerHTML = state.pool.isolated.length
    ? state.pool.isolated.map(b=>batteryCard(b)).join('')
    : '<div class="muted">当前没有隔离电池</div>';
}

/* ---------- 换电队列 ---------- */
async function loadVehiclesQueue(){
  const [v, q] = await Promise.all([api('/api/vehicles'), api('/api/queue')]);
  state.vehicles = v; state.queue = q;
  if(state.tab==='swap') renderQueue();
  renderVehicleSelects();
}

function renderVehicleSelects(){
  const opts = state.vehicles.map(v=>`<option value="${esc(v.code)}">${esc(v.code)} · ${esc(v.model)} · 需${esc(v.battery_model)} · ${VSTATE[v.work_state]||v.work_state}</option>`).join('');
  if($('#enq-vehicle')) $('#enq-vehicle').innerHTML = opts;
  if($('#hc-battery')) $('#hc-battery').innerHTML =
    state.batteries.map(b=>`<option value="${esc(b.code)}">${esc(b.code)} (${esc(b.model)}) 健康${b.health_score}</option>`).join('');
}

function renderQueue(){
  const box = $('#queue-list');
  if(!state.queue.length){ box.innerHTML = '<div class="muted">当前没有待处理预约</div>'; renderSwapStage(); return; }
  box.innerHTML = state.queue.map(q=>{
    const v = state.vehicles.find(x=>x.code===q.vehicle_code);
    const active = state.selectedQueue===q.id;
    return `<div class="queue-item ${active?'active':''}" data-qid="${q.id}">
      <div>
        <div class="qv">${esc(q.vehicle_code)} <span class="muted">${esc(v? v.model+' · 兼容 '+v.battery_model:'')}</span></div>
        <div class="qmeta">作业间隙 ${esc(q.window||'未填')} · 预约人 ${esc(q.requested_by)} · ${hhmm(q.requested_at)}</div>
      </div>
      <div style="flex:1"></div>
      <span class="pill ${q.status}">${q.status==='pending'?'待处理':'换电中'}</span>
    </div>`;
  }).join('');
  $$('#queue-list .queue-item').forEach(el=>el.onclick=()=>{
    state.selectedQueue = +el.dataset.qid; renderQueue();
  });
  if(!state.queue.find(q=>q.id===state.selectedQueue)) state.selectedQueue = state.queue[0]?.id ?? null;
  renderSwapStage();
}

function renderSwapStage(){
  const wrap = $('#swap-stage');
  const q = state.queue.find(x=>x.id===state.selectedQueue);
  if(!q){ wrap.innerHTML = '<div class="card muted">没有选中的预约</div>'; return; }
  const v = state.vehicles.find(x=>x.code===q.vehicle_code);
  const ev = q.event || {};
  const oldB = ev.old_battery_code
    ? state.batteries.find(b=>b.code===ev.old_battery_code) : v?.installed_battery;
  const newB = ev.new_battery_code ? state.batteries.find(b=>b.code===ev.new_battery_code) : null;

  // 旧电池列（左）
  let oldHtml;
  if(ev.state && ev.state!=='created'){
    oldHtml = batteryCard(oldB) + `<div class="swap-detail" style="margin-top:8px">
      扫码交接：${hhmm(ev.old_scanned_at)} · ${esc(ev.operator||'')}<br>
      取下电量：${oldB? oldB.soc+'%':'—'} · 状态：已离车待处理</div>`;
  } else if(v?.installed_battery){
    oldHtml = batteryCard(v.installed_battery) + `
      <div class="checklist">
        <div>当前绑定：${esc(v.code)} ↔ ${esc(v.installed_battery.code)}</div>
        <div style="margin:8px 0">取下旧电池前扫码完成交接登记</div>
        <button class="btn" id="btn-scan">📷 扫码取下旧电池</button>
      </div>`;
  } else {
    oldHtml = '<div class="muted">该叉车当前没有电池</div>';
  }

  // 新电池列（右）：只列型号兼容的电池
  const candidates = state.batteries.filter(b=>b.model===v.battery_model);
  let newHtml;
  if(newB){
    newHtml = batteryCard(newB) + `<div class="swap-detail" style="margin-top:8px">
      装入时间：${hhmm(ev.new_installed_at)}<br>
      <div class="checklist">
        <label><input type="checkbox" id="chk-fix" ${ev.fixation_checked?'checked':''}
          ${ev.state==='completed'?'disabled':''}> 已复核机械固定（锁扣到位、无松动）</label>
        <div>可用电量复核：<b>${newB.soc}%</b>（准入线 60%）</div>
        ${ev.state==='completed'
          ? '<div style="color:var(--green);margin-top:6px">✓ 换电完成，叉车恢复作业</div>'
          : '<button class="btn green" id="btn-complete" style="margin-top:8px">复核完成并交车</button>'}
      </div></div>`;
  } else if(ev.state==='old_removed'){
    const usable = candidates.filter(b=>b.usable);
    const blocked = candidates.filter(b=>!b.usable);
    newHtml = `<div class="swap-detail" style="margin-bottom:8px">
        ${esc(v.code)} 需要型号 <b>${esc(v.battery_model)}</b>，错装将被事务拒绝</div>` +
      (usable.map(b=>batteryCard(b,{action:`<button class="btn green sm" data-install="${esc(b.code)}"
         style="margin-top:8px">装用此电池</button>`})).join('')) +
      (blocked.length? `<div class="section-title">不可选（未入可用池）</div>` +
        blocked.map(b=>batteryCard(b)).join('') : '');
  } else {
    newHtml = '<div class="muted">先在左侧扫码取下旧电池</div>';
  }

  wrap.innerHTML = `<div class="swap-stage">
    <div class="swap-col old"><h3>旧电池（取下 · 扫码交接）</h3>${oldHtml}</div>
    <div class="swap-arrow">→</div>
    <div class="swap-col new"><h3>新电池（兼容 · 固定/电量复核）</h3>${newHtml}</div>
  </div>`;

  $('#btn-scan')?.addEventListener('click', async ()=>{
    try{ await api('/api/swap/scan-old',{queue_id:q.id}); toast('旧电池已扫码交接'); await refresh(); }
    catch(e){ toast(e.message,false); }
  });
  $$('#swap-stage [data-install]').forEach(btn=>btn.onclick=async()=>{
    try{
      await api('/api/swap/install',{queue_id:q.id, battery_code:btn.dataset.install});
      toast('已装上新电池 '+btn.dataset.install); await refresh();
    }catch(e){
      toast(e.message,false);
      if(e.message.includes('占用')) await refresh();
    }
  });
  $('#btn-complete')?.addEventListener('click', async ()=>{
    if(!$('#chk-fix').checked){ toast('请先勾选固定复核',false); return; }
    try{ await api('/api/swap/complete',{queue_id:q.id, fixation_ok:true});
         toast('换电完成，叉车已交回作业'); await refresh(); }
    catch(e){ toast(e.message,false); }
  });
}

/* ---------- 换电记录 ---------- */
async function loadSwaps(){
  state.swaps = await api('/api/swaps');
  if(state.tab==='swap'){
    $('#swap-history').innerHTML = tableHtml(
      ['叉车','旧电池','新电池','换电人','扫码时间','装入时间','固定','电量复核','状态'],
      state.swaps.map(e=>[e.vehicle_code, e.old_battery_code||'—', e.new_battery_code||'—',
        e.operator, hhmm(e.old_scanned_at), hhmm(e.new_installed_at),
        e.fixation_checked?'✓':'—', e.soc_checked!=null?e.soc_checked+'%':'—',
        ({completed:'已完成',new_installed:'待复核',old_removed:'待装新',created:'已创建'})[e.state]||e.state]));
  }
}

/* ---------- 充电 ---------- */
async function loadCharges(){
  const [recs, pool] = await Promise.all([api('/api/charges'), api('/api/pool')]);
  state.charges = recs;
  if(state.tab!=='charges') return;
  const loose = state.batteries.filter(b=>b.status==='loose');
  $('#chg-battery').innerHTML = loose.map(b=>`<option value="${esc(b.code)}">${esc(b.code)} (${esc(b.model)}) ${b.soc}%${b.isolated?' · 隔离':''}</option>`).join('')
    || '<option value="">无待装电池</option>';
  $('#chg-slot').innerHTML = pool.slots.map(s=>`<option value="${esc(s.code)}" ${s.serviceable?'':'disabled'}>${esc(s.code)} ${s.serviceable?'(空闲/可用)':'· 故障'}</option>`).join('');
  $('#charge-table').innerHTML = tableHtml(
    ['电池','槽位','操作人','开始','结束','起始电量','结束电量','状态','原因'],
    recs.map(r=>[r.battery_code, r.slot_code, r.operator, dt(r.started_at), dt(r.ended_at),
      r.start_soc+'%', r.end_soc!=null?r.end_soc+'%':'—',
      r.status==='charging'?'<span class="tag warn">充电中</span>'
        : r.status==='completed'?'<span class="tag ok">已完成</span>'
        : '<span class="tag iso">已中断</span>',
      ({charge_interrupted:'充电中断',slot_fault:'槽位故障',overheat:'电池过热',
        health_quarantine:'健康隔离'})[r.reason]||r.reason||'—']));
}

/* ---------- 健康隔离 ---------- */
async function loadHealth(){
  state.health = await api('/api/health');
  if(state.tab!=='health') return;
  const pool = state.pool || await api('/api/pool');
  $('#hc-slot').innerHTML = pool.slots.map(s=>`<option value="${esc(s.code)}">${esc(s.code)}（${s.serviceable?'正常':'故障'}）</option>`).join('');
  $('#health-table').innerHTML = state.health.length ? `<table><thead><tr>
    <th>电池</th><th>结论</th><th>健康度</th><th>原因</th><th>检测照片</th><th>说明</th><th>检测人</th><th>时间</th>
    </tr></thead><tbody>${state.health.map(r=>`<tr>
      <td><b>${esc(r.battery_code)}</b></td>
      <td>${r.result==='pass'?'<span class="tag ok">合格</span>':'<span class="tag iso">隔离</span>'}</td>
      <td>${r.health_score}</td>
      <td>${(r.reasons||'').split(',').filter(Boolean).map(x=>({overheat:'电池过热',low_health:'健康度过低',wrong_install:'错装',charge_interrupted:'充电中断',slot_fault:'槽位故障'})[x]||x).join('、')||'—'}</td>
      <td>${r.photo?`<a href="/photos/${encodeURIComponent(r.photo)}" target="_blank"><img class="health-photo" src="/photos/${encodeURIComponent(r.photo)}" alt="检测照片"></a>`:'—'}</td>
      <td>${esc(r.note||'—')}</td>
      <td>${esc(r.inspector)}</td>
      <td>${dt(r.checked_at)}</td>
    </tr>`).join('')}</tbody></table>
    <p class="muted">健康检测结论由数据库触发器保护，任何修改/删除尝试都会被拒绝。</p>`
    : '<div class="muted">暂无检测记录</div>';
}

/* ---------- 叉车 ---------- */
async function loadVehiclesTable(){
  if(state.tab!=='vehicles') return;
  $('#vehicles').innerHTML = `<table><thead><tr>
    <th>叉车编号</th><th>车型</th><th>兼容电池型号</th><th>司机</th><th>作业状态</th>
    <th>当前电池</th><th>电量</th><th>温度</th><th>电池健康</th></tr></thead><tbody>
    ${state.vehicles.map(v=>{
      const b = v.installed_battery;
      const hot = b && b.block_reasons.includes('overheat');
      return `<tr><td><b>${esc(v.code)}</b></td><td>${esc(v.model)}</td><td>${esc(v.battery_model)}</td>
        <td>${esc(v.driver)}</td><td><span class="tag ${v.work_state==='working'?'ok':v.work_state==='swapping'?'warn':'info'}">${VSTATE[v.work_state]||v.work_state}</span></td>
        <td>${b?esc(b.code):'—'}</td>
        <td>${b?b.soc+'%':'—'}</td>
        <td>${b?`<span class="temp ${hot?'hot':''}">${hot?flame+'高温 ':''}${b.temperature.toFixed(1)}℃</span>`:'—'}</td>
        <td>${b?b.health_score:'—'}</td></tr>`;
    }).join('')}</tbody></table>`;
}

function tableHtml(heads, rows){
  return `<table><thead><tr>${heads.map(x=>`<th>${x}</th>`).join('')}</tr></thead>
  <tbody>${rows.map(r=>`<tr>${r.map(c=>`<td>${c??'—'}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}

/* ---------- 操作绑定 ---------- */
function bindActions(){
  $$('nav.tabs button').forEach(btn=>btn.onclick=()=>{
    state.tab = btn.dataset.tab;
    $$('nav.tabs button').forEach(b=>b.classList.toggle('active', b===btn));
    $$('main > section').forEach(s=>s.hidden = s.id !== 'tab-'+state.tab);
    refresh();
  });

  $('#btn-enqueue').onclick = async()=>{
    try{
      await api('/api/queue',{vehicle_code:$('#enq-vehicle').value, window:$('#enq-window').value});
      toast('预约已加入队列'); $('#enq-window').value=''; await refresh();
    }catch(e){ toast(e.message,false); }
  };
  $('#btn-charge').onclick = async()=>{
    try{
      await api('/api/charge/start',{battery_code:$('#chg-battery').value, slot_code:$('#chg-slot').value});
      toast('已上架开始充电'); await refresh();
    }catch(e){ toast(e.message,false); }
  };
  $('#btn-health').onclick = async()=>{
    try{
      await api('/api/health/check',{battery_code:$('#hc-battery').value, result:$('#hc-result').value,
        health_score:+$('#hc-score').value, reasons:$('#hc-reason').value,
        photo:$('#hc-photo').value, note:$('#hc-note').value});
      toast('健康检测结论已记录'); await refresh();
    }catch(e){ toast(e.message,false); }
  };
  $('#btn-slot-state').onclick = async()=>{
    try{
      const ok = $('#hc-slot-state').value==='ok';
      await api('/api/slot/state',{slot_code:$('#hc-slot').value, serviceable:ok,
        reason:ok?'':$('#hc-slot-reason').value});
      toast(ok?'槽位已恢复':'槽位已标记故障，在充电池已中断'); await refresh();
    }catch(e){ toast(e.message,false); }
  };
}

async function refresh(){
  await Promise.all([
    loadPool().catch(e=>console.warn(e)),
    loadVehiclesQueue().catch(e=>console.warn(e)),
    loadSwaps().catch(e=>console.warn(e)),
    loadCharges().catch(e=>console.warn(e)),
    loadHealth().catch(e=>console.warn(e)),
  ]);
  renderPool(); renderQueue(); loadVehiclesTable();
}

bindActions();
refresh();
setInterval(refresh, 2000);
