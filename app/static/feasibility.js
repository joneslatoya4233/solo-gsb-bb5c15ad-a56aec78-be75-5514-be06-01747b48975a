/* 全局时序可行性抽屉 —— 复用 app.js 的 state / overrides / recompute 与
   compare.js / sensitivity.js 的抽屉样式
   （el / esc / toast / focusEvent / shortId / fmtNum / colorFor）。 */
"use strict";

const feasState = {
  result: null,
  query: "",
  renderLimit: 400,
};
const feas$ = (sel) => document.querySelector(sel);

/* --------------------------------------------------------------- 开关 */

function feasOpen() {
  feas$("#feas-overlay").classList.remove("hidden");
  feasRun();
}
function feasClose() {
  feas$("#feas-overlay").classList.add("hidden");
}

/* --------------------------------------------------------------- 求解 */

async function feasRun() {
  const hint = feas$("#feas-hint");
  hint.textContent = "求解中（全量事件 × 全部有效约束）…";
  const res = await fetch("/api/temporal-feasibility", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ overrides: state.overrides || {} }),
  });
  hint.textContent = "";
  if (!res.ok) {
    let msg = await res.text();
    try { msg = JSON.parse(msg).detail || msg; } catch { /* 保留原文 */ }
    feasState.result = null;
    feas$("#feas-result").innerHTML = "";
    return toast("时序可行性求解失败: " + msg, "error");
  }
  feasState.result = await res.json();
  feasRender();
}

/* --------------------------------------------------------------- 渲染 */

function feasRender() {
  const d = feasState.result;
  const box = feas$("#feas-result");
  box.innerHTML = "";
  if (!d) return;
  box.appendChild(feasScope(d));
  box.appendChild(d.feasible ? feasFeasible(d) : feasInfeasible(d));
  box.appendChild(feasReadOnlyNote());
}

function feasScope(d) {
  const blk = el("div", "cmp-block");
  const dirty = ((state.result && state.result.sources) || [])
    .filter((s) => s.dirty).map((s) => esc(s.id));
  blk.innerHTML = `
    <h3>求解范围
      <span class="cmp-count">${d.feasible ? "🟢 可行" : "🔴 不可行"} · 与页面筛选无关 · 只读检查</span>
    </h3>
    <div class="sens-meta">
      <span class="stat">全量事件 <b>${d.scope.event_count}</b></span>
      <span class="stat">有效约束 <b>${d.scope.edge_count}</b></span>
      <span class="stat">自环（忽略）<b>${d.scope.self_loop_count}</b></span>
      <span class="stat">悬空（忽略）<b>${d.scope.dangling_edge_count}</b></span>
    </div>
    <div class="feas-note">${esc(d.scope.note)}</div>
    <div class="feas-note">时钟假设：沿用当前生效值${dirty.length ? `（含未保存调整：<b>${dirty.join("、")}</b>）` : "（无未保存调整）"}。</div>`;
  return blk;
}

function feasFeasible(d) {
  const wrap = el("div");
  const banner = el("div", "feas-verdict ok");
  banner.innerHTML = `✓ ${esc(d.conclusion)}`;
  wrap.appendChild(banner);

  const stat = el("div", "cmp-block");
  stat.innerHTML = `
    <h3>最早可行时刻（见证）
      <span class="cmp-count">取下界沿约束传播的最小不动点；每个时刻均在各自闭区间内且满足全部边</span>
    </h3>
    <div class="sens-meta">
      <span class="stat">事件 <b>${d.stats.event_count}</b></span>
      <span class="stat">取在区间下界的事件 <b>${d.stats.pinned_to_lower_count}</b></span>
      <span class="stat">见证首尾跨度 <b>${fmtNum(d.stats.common_horizon)}s</b></span>
    </div>`;
  const search = el("input", "feas-search");
  search.type = "search";
  search.placeholder = "筛选见证事件：ID / 摘要 / 来源…";
  search.value = feasState.query;
  search.addEventListener("input", () => {
    feasState.query = search.value;
    feasRenderList(d);
  });
  stat.appendChild(search);

  const list = el("div", "feas-witness");
  stat.appendChild(list);
  wrap.appendChild(stat);

  // 延迟到 append 后渲染列表（需要容器引用）
  queueMicrotask(() => feasRenderList(d));
  return wrap;
}

function feasRenderList(d) {
  const box = feas$("#feas-result");
  const list = box.querySelector(".feas-witness");
  if (!list) return;
  list.innerHTML = "";
  const q = feasState.query.trim().toLowerCase();
  const rows = d.earliest_times.filter((w) => !q
    || w.id.toLowerCase().includes(q)
    || (w.summary || "").toLowerCase().includes(q)
    || w.source_id.toLowerCase().includes(q)
    || (w.event_type || "").toLowerCase().includes(q));
  const shown = rows.slice(0, feasState.renderLimit);
  const frag = document.createDocumentFragment();
  for (const w of shown) frag.appendChild(feasWitnessRow(w));
  list.appendChild(frag);
  const foot = el("div", "feas-list-foot");
  if (rows.length > shown.length) {
    foot.innerHTML = `仅显示前 ${shown.length} / ${rows.length} 条匹配结果，请在上方输入关键字收窄。`;
  } else if (q) {
    foot.textContent = `匹配 ${rows.length} 条。`;
  } else {
    foot.innerHTML = `共 ${rows.length} 个事件（渲染上限 ${feasState.renderLimit}，可搜索）。点击任意行在主时间线定位。`;
  }
  list.appendChild(foot);
  list.querySelectorAll("[data-eid]").forEach((n) =>
    n.addEventListener("click", () => { feasClose(); focusEvent(n.dataset.eid); }));
}

function feasWitnessRow(w) {
  const row = el("div", "feas-row");
  const color = (typeof colorFor === "function") ? colorFor(w.source_id) : "#8ab4f8";
  const span = Math.max(w.hi - w.lo, 1e-9);
  const posPct = Math.min(100, Math.max(0, ((w.earliest - w.lo) / span) * 100));
  row.innerHTML = `
    <span class="feas-node chain-node" data-eid="${esc(w.id)}"
          title="点击在主时间线定位">${esc(shortId(w.id))}</span>
    <span class="feas-time">${esc(w.earliest_text.slice(11, 23))}</span>
    <span class="feas-range" title="闭区间 [lo, hi]，标记为最早可行时刻在该区间内的相对位置">
      <span class="feas-bar"><span class="feas-seg"
        style="background:${color};left:${posPct.toFixed(1)}%"></span></span>
      [${esc(w.lo_text.slice(11, 23))}, ${esc(w.hi_text.slice(11, 23))}]
    </span>
    <span class="feas-slack" title="距区间上界余量">余量 ${esc(w.slack_text)}</span>
    ${w.pinned_to_lower ? '<span class="feas-pin" title="最早时刻被自身区间下界固定">下界</span>' : ""}
    <span class="feas-sum">${esc(w.summary || w.event_type)} · ${esc(w.source_name || w.source_id)}</span>`;
  return row;
}

function feasInfeasible(d) {
  const c = d.contradiction;
  const b = c.conflicting_bounds;
  const wrap = el("div");

  const banner = el("div", "feas-verdict bad");
  banner.innerHTML = `✗ ${esc(d.conclusion)}`;
  wrap.appendChild(banner);

  const stat = el("div", "cmp-block");
  stat.innerHTML = `
    <h3>矛盾路径
      <span class="cmp-count">${c.in_cycle ? "闭合成环 · 环内区间无公共交集" : "下界沿路径传递越过终点上界"}
        · 违反上界的事件 ${d.stats.violating_event_count} 个 · 路径 ${c.path_length_edges} 跳</span>
    </h3>`;

  // 两端冲突界限
  const bounds = el("div", "feas-bounds");
  bounds.innerHTML = `
    <div class="feas-bound from" title="路径起点事件的区间下界（被下界传播固定）">
      <div class="feas-bound-tag">起点下界 lo</div>
      <span class="chain-node" data-eid="${esc(b.lower_bound_event_id)}">${esc(shortId(b.lower_bound_event_id))}</span>
      <b>${esc(b.lower_bound_text.slice(11, 23))}</b>
    </div>
    <div class="feas-bound-excess">越过 <b>${esc(b.excess_text)}</b> ⇢</div>
    <div class="feas-bound to" title="路径终点事件的区间上界（最早可行时刻已超出）">
      <div class="feas-bound-tag">终点上界 hi</div>
      <span class="chain-node bad" data-eid="${esc(b.upper_bound_event_id)}">${esc(shortId(b.upper_bound_event_id))}</span>
      <b>${esc(b.upper_bound_text.slice(11, 23))}</b>
    </div>`;
  stat.appendChild(bounds);

  // 逐步路径：事件节点 + 约束ID + 单边可行性 / 同刻公共交集
  const path = el("div", "feas-path");
  const steps = c.steps || [];
  if (steps.length) {
    const head = steps[0].from;
    path.appendChild(feasPathNode(head, "起点"));
    for (const s of steps) {
      const arrow = el("div", "feas-step");
      const indiv = s.individually_feasible;
      const si = s.shared_intersection;
      arrow.innerHTML = `
        <span class="feas-edge ${indiv ? "ok" : "bad"}"
          title="约束 #${esc(s.constraint_id)} ${esc(s.kind)}${s.note ? " · " + esc(s.note) : ""}
单边可行性：${indiv ? "两端闭区间有公共点（可同刻）" : "两端闭区间无公共点（该边单独即不可能）"}
gap ∈ [${esc(s.gap_lo_text)}, ${esc(s.gap_hi_text)}]${si ? `
同刻交集 [${esc(si.lo_text.slice(11, 23))}, ${esc(si.hi_text.slice(11, 23))}]` : ""}">
          <span class="feas-edge-id">#${esc(s.constraint_id)} ${esc(s.kind)}</span>
          <span class="feas-edge-mark">${indiv ? "→ 单边可行" : "→ 单边不可能"}</span>
        </span>`;
      path.appendChild(arrow);
      path.appendChild(feasPathNode(s.to, s.to === b.upper_bound_event_id ? "越界终点" : ""));
    }
  } else {
    path.appendChild(feasPathNode(b.lower_bound_event_id, "起点"));
    path.appendChild(feasPathNode(b.upper_bound_event_id, "越界终点"));
  }
  stat.appendChild(path);

  const allIndiv = c.all_edges_individually_feasible && steps.length;
  const note = el("div", "feas-note " + (allIndiv ? "warn" : ""));
  note.innerHTML = allIndiv
    ? "⚠ 路径上<b>每条边单独都可行</b>（两端区间存在同刻公共点），矛盾只在全部约束联立后暴露：" +
      "起点最早时刻沿链传递后越过了终点的最晚时刻。"
    : "其中至少一条边就两端区间而言单独即不可能（已在该步标红）；其余边界给出联立后的传递界限。";
  stat.appendChild(note);

  const jump = el("div", "finding-jump");
  jump.innerHTML = `<button class="btn tiny primary">🔍 在主时间线定位终点事件 ${esc(shortId(b.upper_bound_event_id))}</button>`;
  jump.querySelector("button").addEventListener("click", () => {
    feasClose(); focusEvent(b.upper_bound_event_id);
  });
  stat.appendChild(jump);
  wrap.appendChild(stat);

  // 事件节点点击统一关闭抽屉并定位主时间线
  queueMicrotask(() => {
    const card = feas$("#feas-result");
    card.querySelectorAll(".chain-node[data-eid]").forEach((n) =>
      n.addEventListener("click", () => { feasClose(); focusEvent(n.dataset.eid); }));
  });
  return wrap;
}

function feasPathNode(eid, role) {
  const node = el("div", "feas-path-node");
  node.innerHTML = `
    <span class="chain-node ${role === "越界终点" ? "bad" : ""}" data-eid="${esc(eid)}"
          title="点击在主时间线定位">${esc(shortId(eid))}</span>
    ${role ? `<span class="feas-node-role">${esc(role)}</span>` : ""}`;
  return node;
}

function feasReadOnlyNote() {
  const n = el("div", "feas-readonly");
  n.textContent = "本检查为只读分析：不会新增 / 删除 / 修改任何事件、约束或时钟假设；如需调整，请在左栏改动后重新求解。";
  return n;
}

/* --------------------------------------------------------------- 绑定 */

feas$("#btn-feasibility").addEventListener("click", feasOpen);
feas$("#feas-close").addEventListener("click", feasClose);
feas$("#feas-run").addEventListener("click", feasRun);
feas$("#feas-overlay").addEventListener("click", (e) => {
  if (e.target.id === "feas-overlay") feasClose();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !feas$("#feas-overlay").classList.contains("hidden")) feasClose();
});
