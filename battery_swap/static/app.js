/* 叉车动力电池换电管理 —— 前端 */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

let state = null;
let currentUser = "wang";
let currentTab = "pool";
let photoData = null;

const api = async (path, body) => {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user: currentUser, ...body }),
  });
  const j = await r.json();
  if (!j.ok) throw new Error(j.error || "请求失败");
  if (j.state) state = j.state;
  return j;
};

const toast = (msg, ok = true) => {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show " + (ok ? "ok" : "error");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.className = "toast"), 3200);
};

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const role = () => state && state.users
  ? state.users[currentUser]
  : null;

/* ---------------- 用户与标签页 ---------------- */
function renderUsers() {
  const sel = $("#userSelect");
  const users = state.users || {};
  if (!sel.options.length || sel.dataset.filled !== "1") {
    sel.innerHTML = Object.entries(users)
      .map(([u, d]) => `<option value="${u}">${esc(d.name)}（${esc(d.role_name)}）</option>`)
      .join("");
    sel.value = currentUser;
    sel.dataset.filled = "1";
  }
  const u = users[currentUser];
  const tag = $("#roleTag");
  tag.textContent = u ? u.role_name : "";
  tag.className = "role-tag " + (u ? u.role : "");
  // 健康页维修操作提示
  const isMaint = u && u.role === "maintainer";
  $$("#healthForm button,#slotFaultForm button,#resetForm button").forEach((b) =>
    b.disabled = !isMaint);
  $("#healthForm .maint-hint")?.remove();
  if (!isMaint) {
    $("#healthForm").insertAdjacentHTML(
      "beforeend",
      `<div class="maint-hint" style="color:#c93b37;font-size:12px">⚠ 调度员/换电员不能修改健康检测结论，请切换「李工（维修）」账号</div>`);
  }
}

$("#userSelect").addEventListener("change", (e) => {
  currentUser = e.target.value;
  renderUsers();
});

$$("#tabs button").forEach((b) =>
  b.addEventListener("click", () => {
    currentTab = b.dataset.tab;
    $$("#tabs button").forEach((x) => x.classList.toggle("active", x === b));
    $$(".tab").forEach((x) => x.classList.toggle("active", x.id === "tab-" + currentTab));
  }));

/* ---------------- 电池卡片 ---------------- */
function batteryCard(b, opts = {}) {
  const socCls = b.soc < 25 ? "low" : b.soc < 55 ? "mid" : "";
  const badges = [];
  if (b.overheating)
    badges.push(`<span class="badge hot">🌡 过热 ${b.temperature.toFixed(1)}℃</span>`);
  if (b.health_status === "隔离") badges.push(`<span class="badge red">健康隔离</span>`);
  if (b.health_status === "退役") badges.push(`<span class="badge gray">已退役</span>`);
  if (b.soh < state.thresholds.soh_min)
    badges.push(`<span class="badge amber">SOH ${b.soh}% 偏低</span>`);
  if (b.charge_interrupted) badges.push(`<span class="badge red">充电中断</span>`);
  if (b.charging) badges.push(`<span class="badge blue">充电中</span>`);
  if (b.bound_forklift) badges.push(`<span class="badge blue">在车 ${esc(b.bound_forklift)}</span>`);
  if (b.mismatch) badges.push(`<span class="badge red">错装</span>`);
  if (b.available) badges.push(`<span class="badge green">可入池</span>`);

  let bindLine = "";
  if (b.bound_forklift) bindLine = `绑定叉车：${esc(b.bound_forklift)}`;
  else if (b.bound_slot)
    bindLine = `绑定槽位：${esc(b.slot_name || b.bound_slot)}`;
  else bindLine = "未绑定（池内待命）";

  const eta =
    b.charging && b.eta_minutes != null
      ? `<span class="eta">预计充满：<b>${b.eta_minutes} 分钟</b></span>`
      : "";
  const reasons = b.exclude_reasons.length
    ? `<div class="reasons">${b.exclude_reasons.map((r) => `<div>${esc(r)}</div>`).join("")}</div>`
    : "";
  const tempCls = b.overheating ? "hot" : "";
  const cls =
    "bat " +
    (b.overheating || b.charge_interrupted || b.mismatch || b.health_status !== "正常"
      ? "blocked "
      : "") +
    (b.charging ? "charging " : "") +
    (b.health_status === "隔离" ? "isolated " : "") +
    (opts.pickable && b.available ? "pickable" : "");
  const click = opts.pickable && b.available ? `data-pick="${b.id}"` : "";
  return `
    <div class="${cls}" ${click}>
      <div><span class="bid">${b.id}</span><span class="bmodel">${esc(b.model)}</span></div>
      <div class="bind ${tempCls}">${esc(bindLine)}</div>
      <div class="soc-row">
        <div class="soc-bar"><div class="soc-fill ${socCls}" style="width:${b.soc}%"></div></div>
        <span class="soc-val">${b.soc}%</span>
      </div>
      <div style="display:flex;justify-content:space-between;gap:8px">
        ${eta}
        <span class="temp ${tempCls}">${b.overheating ? "🌡 " : "🌡 " }温度 <b>${b.temperature.toFixed(1)}℃</b>${
          b.overheating ? " <b style='color:#c93b37'>过热</b>" : ""}</span>
      </div>
      <div class="badges">${badges.join("")}</div>
      ${reasons}
    </div>`;
}

/* ---------------- 池位图 ---------------- */
function renderPool() {
  const bs = state.pool;
  const available = bs.filter((b) => b.available);
  const charging = bs.filter((b) => b.charging);
  const onFork = bs.filter((b) => b.bound_forklift);
  const blocked = bs.filter(
    (b) =>
      !b.available &&
      !b.charging &&
      !b.bound_forklift);
  $("#zoneAvailable").innerHTML =
    available.map((b) => batteryCard(b, { pickable: true })).join("") || empty("暂无可用满电电池");
  $("#zoneCharging").innerHTML =
    charging.map((b) => batteryCard(b)).join("") || empty("当前无电池在充");
  $("#zoneOnFork").innerHTML = onFork.map((b) => batteryCard(b)).join("");
  $("#zoneBlocked").innerHTML =
    blocked.map((b) => batteryCard(b)).join("") || empty("没有被拦截的电池");
  const hot = bs.filter((b) => b.overheating);
  $("#poolHint").innerHTML = hot.length
    ? `⚠ 过热样例：${hot
        .map((b) => `<b>${b.id}</b> 温度 ${b.temperature.toFixed(1)}℃ 已过热保护、充电中断`)
        .join("；")}。需维修人员降温后在「健康隔离」页复位，才可重新入池。`
    : "六不入规则：过热 / 槽位故障 / 充电中断 / 型号不兼容 / 错装 / 健康度过低，任一命中即退出可用池。";
  $$("#zoneAvailable [data-pick]").forEach((el) =>
    el.addEventListener("click", () => {
      $("#newCode").value = el.dataset.pick;
      $$("#tabs button").find((x) => x.dataset.tab === "swap").click();
      toast("已填入新电池编号 " + el.dataset.pick + "，确认换电单后扫码安装");
    }));
}
const empty = (t) => `<div style="color:#8a929b;font-size:13px;padding:6px">${t}</div>`;

/* ---------------- 换电队列 ---------------- */
function renderQueue() {
  const sel = $("#reserveFork");
  const prevFork = sel.value;
  sel.innerHTML = state.forklifts
    .map((f) => `<option value="${f.id}">${f.id} ${esc(f.model)}（${esc(f.status)}）</option>`)
    .join("");
  if (prevFork && [...sel.options].some((o) => o.value === prevFork)) sel.value = prevFork;
  $("#queueBody").innerHTML = state.queue
    .map((o) => {
      const old = o.old_battery_id
        ? `${o.old_battery_id}（${o.old_model} ${o.old_soc ?? "-"}%）`
        : "—";
      const nb = o.new_battery_id
        ? `${o.new_battery_id}（${o.new_model} ${o.new_soc}%）`
        : "—";
      const acts =
        o.status === "已预约"
          ? `<button data-act="start" data-id="${o.id}">开始换电</button>
             <button data-act="goto" data-id="${o.id}">前往作业</button>
             <button data-act="cancel" data-id="${o.id}">取消</button>`
          : `<button data-act="goto" data-id="${o.id}">前往作业</button>
             <button data-act="cancel" data-id="${o.id}">取消</button>`;
      return `<tr>
        <td><b>${o.id}</b></td><td>${o.forklift_id} ${esc(o.forklift_model)}</td>
        <td><span class="status-chip status-${o.status}">${o.status}</span></td>
        <td>${esc(old)}${o.old_temp >= state.thresholds.temp_hot ? ` 🌡${o.old_temp.toFixed(1)}℃` : ""}</td>
        <td>${esc(nb)}</td><td>${o.slot_id || "—"}</td>
        <td>${esc(o.operator || "")}</td><td>${esc(o.scheduled_at)}</td><td>${acts}</td>
      </tr>`;
    })
    .join("") || `<tr><td colspan="9" style="color:#8a929b;text-align:center">队列为空</td></tr>`;
  $$("#queueBody [data-act]").forEach((b) =>
    b.addEventListener("click", async () => {
      const id = b.dataset.id;
      try {
        if (b.dataset.act === "start") await api(`/api/orders/${id}/start`, {});
        else if (b.dataset.act === "cancel") {
          if (!confirm("确认取消该换电单？")) return;
          await api(`/api/orders/${id}/cancel`, {});
        } else if (b.dataset.act === "goto") {
          $("#swapOrder").value = id;
          renderSwap();
          $$("#tabs button").find((x) => x.dataset.tab === "swap").click();
          return;
        }
        toast("操作成功");
        renderAll();
      } catch (e) { toast(e.message, false); }
    }));
}

$("#reserveForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/reserve", {
      forklift_id: $("#reserveFork").value,
      note: $("#reserveNote").value,
    });
    $("#reserveNote").value = "";
    toast("预约成功，已进入换电队列");
    renderAll();
  } catch (err) { toast(err.message, false); }
});

/* ---------------- 换电作业页 ---------------- */
const STEPS = [
  ["已预约", "已预约"], ["换电中", "开工"], ["待入槽", "旧电池已取下"],
  ["待安装", "旧电池入槽"], ["待复核", "新电池已安装"], ["已完成", "复核完成"],
];

function renderSwap() {
  const sel = $("#swapOrder");
  // queue 为进行中工单，orders 补已完成/已取消；按 id 去重
  const seen = new Set();
  const opts = [...state.queue, ...state.orders].filter((o) => {
    if (seen.has(o.id)) return false;
    seen.add(o.id);
    return true;
  });
  if (sel.dataset.filled !== "1" || [...sel.options].map((o) => o.value).join() !==
      opts.map((o) => o.id).join()) {
    sel.innerHTML = opts
      .map((o) => `<option value="${o.id}">${o.id} · ${o.forklift_id} · ${o.status}</option>`)
      .join("");
    if (!sel.value && opts[0]) sel.value = opts[0].id;
    sel.dataset.filled = "1";
  }
  const o = opts.find((x) => x.id === sel.value) || opts[0];
  if (!o) {
    $("#oldPanel").className = "scan-panel empty";
    $("#oldPanel").textContent = "暂无可执行的换电单，请先在「换电队列」预约";
    $("#newPanel").className = "scan-panel empty";
    $("#newPanel").textContent = "—";
    $("#swapTimeline").innerHTML = "";
    return;
  }
  sel.value = o.id;
  $("#swapStatus").textContent = o.status;
  $("#swapStatus").className = "status-chip status-" + o.status;
  $("#swapFork").textContent =
    `${o.forklift_id}（兼容 ${o.forklift_model || ""}） 换电人：${o.operator || "—"}`;

  // 左：旧电池
  const oldB = o.old_battery_id ? state.pool.find((b) => b.id === o.old_battery_id) : null;
  $("#oldPanel").innerHTML = oldB
    ? panelBattery(oldB, `旧电池 ${o.old_battery_id}${o.slot_id ? " → 已入 " + o.slot_id : ""}`)
    : `<b>待扫码取下</b><div style="color:#8a929b;font-size:12px;margin-top:4px">
       车辆当前电池将在扫码后交接解绑，旧电池随即进入待入槽状态</div>`;
  $("#oldPanel").className = "scan-panel";
  // 槽位速选
  $("#slotsMini").innerHTML = state.slots
    .map((s) => {
      const can = !s.fault && s.status === "空闲" && o.status === "待入槽";
      return `<span class="slot-chip ${can ? "ok" : "bad"}" data-slot="${s.id}"
        title="${esc(s.status)}${s.battery_id ? " · " + s.battery_id : ""}">${s.id} ${
        s.fault ? "⛔故障" : s.status === "空闲" ? "空闲" : "占用:" + (s.battery_id || "")}</span>`;
    })
    .join("");
  $$("#slotsMini [data-slot]").forEach((c) =>
    c.addEventListener("click", () => {
      if (c.classList.contains("ok")) {
        $("#slotCode").value = c.dataset.slot;
        toast("已填入槽位 " + c.dataset.slot);
      }
    }));

  // 右：新电池
  const newB = o.new_battery_id ? state.pool.find((b) => b.id === o.new_battery_id) : null;
  $("#newPanel").innerHTML = newB
    ? panelBattery(newB, `新电池 ${o.new_battery_id}（已装 ${o.forklift_id}）`)
    : `<b>待扫码安装</b><div style="color:#8a929b;font-size:12px;margin-top:4px">
       仅可用池电池可安装：型号兼容、未过热、SOH≥${state.thresholds.soh_min}%、
       未中断、未被他人绑定</div>`;
  $("#newPanel").className = "scan-panel";
  $("#confirmBtn").disabled = o.status !== "待复核";

  // 可用候选（型号兼容于本叉车）
  const fk = state.forklifts.find((f) => f.id === o.forklift_id);
  const accepted = fk ? fk.accepted_models.split(",") : [];
  const cands = state.pool.filter((b) => b.available &&
    (accepted.includes("*") || accepted.includes(b.model)));
  $("#newPick").innerHTML =
    cands.map((b) => batteryCard(b, { pickable: true })).join("") ||
    empty("暂无兼容的可用电池，等待充电完成或维修放行");
  $$("#newPick [data-pick]").forEach((el) =>
    el.addEventListener("click", () => {
      $("#newCode").value = el.dataset.pick;
      toast("已填入 " + el.dataset.pick);
    }));

  // 流程线
  const order = ["已预约", "换电中", "待入槽", "待安装", "待复核", "已完成"];
  const idx = order.indexOf(o.status);
  $("#swapTimeline").innerHTML =
    `<h3>交接流程</h3><div class="tl">` +
    STEPS.map(([st, label], i) => {
      const cls = i < idx || o.status === "已完成" ? "done" : i === idx ? "active" : "";
      const t = st === "已预约" ? o.scheduled_at
        : st === "换电中" ? o.started_at
        : st === "待入槽" ? o.old_scan_at
        : st === "待安装" ? o.slot_scan_at
        : st === "待复核" ? o.new_scan_at
        : o.finished_at;
      return `<span class="step ${cls}">${label}${t ? `<span class="t">${esc(t.slice(11))}</span>` : ""}</span>`;
    }).join("") +
    (o.status === "已取消" ? `<span class="step" style="color:#c93b37">已取消</span>` : "") +
    `</div>`;
}

function panelBattery(b, title) {
  const hot = b.overheating
    ? `<span class="badge hot">🌡 过热 ${b.temperature.toFixed(1)}℃</span>` : "";
  return `
    <div style="font-weight:600;margin-bottom:6px">${esc(title)} ${hot}</div>
    <div style="display:flex;gap:14px;flex-wrap:wrap;font-size:13px">
      <span>编号 <b>${b.id}</b></span><span>型号 ${esc(b.model)}</span>
      <span>电量 <b>${b.soc}%</b></span>
      <span>温度 <b style="${b.overheating ? "color:#c93b37" : ""}">${b.temperature.toFixed(1)}℃${
        b.overheating ? " 过热" : ""}</b></span>
      <span>SOH ${b.soh}%</span>
    </div>`;
}

$("#swapOrder").addEventListener("change", renderSwap);

$$("[data-scan]").forEach((btn) =>
  btn.addEventListener("click", async () => {
    const kind = btn.dataset.scan;
    const codeEl = { old: "#oldCode", slot: "#slotCode", new: "#newCode" }[kind];
    const code = $(codeEl).value.trim();
    if (!code) return toast("请先扫码或填入编号", false);
    try {
      await api("/api/scan", { kind, order_id: $("#swapOrder").value, code });
      $(codeEl).value = "";
      toast({ old: "旧电池已取下并交接", slot: "旧电池已入槽开始充电",
              new: "新电池安装成功，请复核" }[kind]);
      renderAll();
    } catch (e) {
      toast("扫码被拒绝：" + e.message, false);
      renderAll(); // 仍刷新，展示被拒记录
    }
  }));

$("#confirmBtn").addEventListener("click", async () => {
  try {
    await api(`/api/orders/${$("#swapOrder").value}/confirm`, {});
    toast("复核通过：固定合格、可用电量达标，换电完成");
    renderAll();
  } catch (e) { toast(e.message, false); }
});

/* ---------------- 充电记录 ---------------- */
function renderCharge() {
  $("#chargeBody").innerHTML = state.charge_logs
    .map((l) => {
      const st =
        l.status === "已充满"
          ? `<span class="ok-text">已充满</span>`
          : l.status === "充电中"
          ? `<span style="color:#2f6db3;font-weight:600">充电中…</span>`
          : `<span class="bad-text">已中断</span> ${esc(l.interrupt_reason || "")}`;
      return `<tr>
        <td>${l.id}</td><td><b>${l.battery_id}</b></td><td>${l.slot_id}</td>
        <td>${esc(l.started_at)}</td><td>${esc(l.ended_at || "—")}</td>
        <td>${l.start_soc}%</td><td>${l.end_soc ?? "—"}${l.end_soc != null ? "%" : ""}</td>
        <td>${l.energy_kwh.toFixed(2)}</td><td>${st}</td></tr>`;
    })
    .join("");
}

/* ---------------- 健康隔离 ---------------- */
function renderHealth() {
  const keep = (sel, html, value) => {
    const cur = value ?? sel.value;
    sel.innerHTML = html;
    if (cur && [...sel.options].some((o) => o.value === cur)) sel.value = cur;
  };
  keep($("#healthBattery"), state.pool
    .map((b) => `<option value="${b.id}">${b.id} ${b.model} SOH${b.soh}% ${b.health_status}</option>`)
    .join(""));
  keep($("#resetBattery"), state.pool
    .filter((b) => b.overheating || b.charge_interrupted)
    .map((b) => `<option value="${b.id}">${b.id}（${b.temperature.toFixed(1)}℃${
      b.overheating ? " 过热" : ""}）</option>`).join(""));
  keep($("#faultSlot"), state.slots
    .map((s) => `<option value="${s.id}">${s.id} ${esc(s.name)} ${s.status}</option>`).join(""));
  const cur = state.pool.find((b) => b.id === $("#healthBattery").value);
  if (cur && $("#healthSoh").dataset.touched !== "1") $("#healthSoh").value = cur.soh;
  const iso = state.pool.filter((b) => b.health_status !== "正常" || b.soh < state.thresholds.soh_min);
  $("#isolatedCards").innerHTML = iso.map((b) => batteryCard(b)).join("") || empty("无隔离电池");
  $("#healthBody").innerHTML = state.health_checks
    .map((h) => `<tr>
      <td>${esc(h.created_at)}</td><td><b>${h.battery_id}</b></td><td>${h.soh}%</td>
      <td>${h.temperature.toFixed(1)}℃</td>
      <td>${h.result === "合格"
        ? '<span class="ok-text">合格</span>'
        : '<span class="bad-text">隔离</span>'}</td>
      <td>${esc(h.reason || "")}</td><td>${esc(h.inspector)}</td>
      <td>${h.photo_url ? `<a href="${h.photo_url}" target="_blank">
        <img src="${h.photo_url}" width="120" style="border:1px solid #d7dde3;border-radius:3px"></a>` : "—"}</td>
    </tr>`).join("");
}

$("#healthBattery").addEventListener("change", (e) => {
  const b = state.pool.find((x) => x.id === e.target.value);
  if (b) $("#healthSoh").value = b.soh;
});
$("#healthSoh").addEventListener("input", () => ($("#healthSoh").dataset.touched = "1"));
$("#healthPhoto").addEventListener("change", () => {
  const f = $("#healthPhoto").files[0];
  if (!f) return;
  const r = new FileReader();
  r.onload = () => {
    photoData = r.result;
    $("#photoPreview").src = r.result;
    $("#photoPreview").style.display = "block";
  };
  r.readAsDataURL(f);
});
$("#healthForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/health", {
      battery_id: $("#healthBattery").value,
      soh: Number($("#healthSoh").value),
      result: $("#healthResult").value,
      reason: $("#healthReason").value,
      photo: photoData,
    });
    $("#healthReason").value = "";
    photoData = null;
    $("#healthPhoto").value = "";
    $("#photoPreview").style.display = "none";
    toast("检测结论已登记");
    renderAll();
  } catch (err) { toast(err.message, false); }
});
$("#slotFaultForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/slot-fault", {
      slot_id: $("#faultSlot").value,
      fault: $("#faultMode").value === "1",
      reason: $("#faultReason").value,
    });
    $("#faultReason").value = "";
    toast("槽位处置完成");
    renderAll();
  } catch (err) { toast(err.message, false); }
});
$("#resetForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/overheat-reset", { battery_id: $("#resetBattery").value });
    toast("过热/中断标记已复位，电池可重新入槽充电");
    renderAll();
  } catch (err) { toast(err.message, false); }
});

/* ---------------- 叉车状态 ---------------- */
function renderForks() {
  $("#forkBody").innerHTML = state.forklifts
    .map((f) => {
      const hot = f.battery_hot;
      return `<tr>
      <td><b>${f.id}</b></td><td>${esc(f.model)}</td><td>${esc(f.accepted_models)}</td>
      <td>${f.current_battery_id || "—"}${f.battery_model ? ` <small>${esc(f.battery_model)}</small>` : ""}</td>
      <td>${f.battery_soc ?? "—"}% · <span style="${hot ? "color:#c93b37;font-weight:700" : ""}">
        ${f.battery_temp != null ? f.battery_temp.toFixed(1) + "℃" : "—"}${hot ? " 🌡过热" : ""}</span></td>
      <td><span class="status-chip fork-${f.status}">${esc(f.status)}</span></td></tr>`;
    })
    .join("");
  const phaseName = { OLD_REMOVE: "取下旧电池", SLOT_IN: "旧电池入槽", NEW_INSTALL: "装新电池" };
  $("#scanBody").innerHTML = state.scan_events
    .map((s) => `<tr>
      <td>${esc(s.created_at)}</td><td>${esc(s.order_id || "")}</td>
      <td>${phaseName[s.phase] || s.phase}</td>
      <td>${esc(s.battery_id || "")}</td><td>${esc(s.slot_id || "")}</td>
      <td>${s.result === "成功"
        ? '<span class="ok-text">✓ 成功</span>'
        : '<span class="bad-text">✗ 拒绝</span>'}</td>
      <td>${esc(s.detail || "")}</td><td>${esc(s.operator)}</td></tr>`)
    .join("");
}

/* ---------------- 总刷新 ---------------- */
function renderAll() {
  if (!state) return;
  renderUsers();
  renderPool();
  renderQueue();
  renderSwap();
  renderCharge();
  renderHealth();
  renderForks();
}

async function poll() {
  try {
    const r = await fetch("/api/state");
    state = await r.json();
    renderAll();
  } catch (e) {
    /* 服务重启中，忽略本轮 */
  }
}
poll();
setInterval(poll, 3000);
