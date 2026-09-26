"""全局时序可行性：全量事件区间 × 全部有效约束联立求解。

模型（与 engine 的区间模型一致，且不做任何页面筛选裁剪）
----------------------------------------------------------------
每个事件 i 取其闭区间 [lo_i, hi_i] 内的一个真实时刻 t_i；
每条有效约束边 a→b（含用户声明与同来源 source_order）要求 t_b ≥ t_a。
环允许同刻（≥ 而非严格 >），但环上所有区间必须有公共交集，
否则环内不存在公共时刻——这正是“各边单独可行、整条路径/环无解”的情形。

解的结构
--------
这是一个简单时序问题（STP，差约束 t_b - t_a ≥ 0）：

    t_i ≥ lo_i
    t_b ≥ t_a            （边 a→b）

松弛所有约束得到的最小不动点

    L_i = max(lo_j : j 可达 i)（沿 ≥ 边传播，环内自动取同刻）

即每个事件的**最早可行时刻**。当且仅当对所有 i 有 L_i ≤ hi_i 时系统可行，
且 (L_i) 本身就是一个可行见证（L_i ≥ lo_i，且每条边 L_b ≥ L_a）。

所有权重均为 0，故按 **SCC 缩点 → 缩点 DAG 拓扑序做最大值传播**求解，
O(V+E)；同一 SCC（环）内节点自动取同一时刻——环允许同刻，而该公共时刻
是否落在环内每个区间里由最后的 L_i ≤ hi_i 检查回答（等价于“环内区间
有公共交集”）。Tarjan 与后续全部 BFS 均为迭代实现，5000 事件无递归栈压力。

无解见证
--------
任一 L_k > hi_k 都是矛盾：存在一条 lo 源点 j ⇝ k 的约束路径，
沿途各边在自己的端点区间下单独都可满足（存在同刻点），
但路径起点的最早时刻 lo_j 已经越过路径上某事件 k 的最晚时刻 hi_k，
即路径/环内区间无公共交集。矛盾路径按下列步骤构造：

1. 只保留“紧边” L_v - L_u ≤ TOL（等号被吃满的边），
   多源 BFS 从全部违反点沿紧边反向、走非违反点到达 L_j = lo_j 的源点，
   取跳数最短者，保证证据最短；
2. 在该路径上取第一个违反点截断：截断处之前的每条边都单独可行，
   矛盾来自整条链叠加的下界传递而非任何单条边。
"""
from __future__ import annotations

from collections import deque

from . import engine
from .timeutil import fmt, fmt_delta

TOL = 1e-7


# ------------------------------------------------------------ 核心求解

def _tarjan(n: int, adj: list[list[int]]) -> list[list[int]]:
    """迭代版 Tarjan SCC（5000 节点无递归栈压力）。"""
    index = [-1] * n
    low = [0] * n
    on = [False] * n
    stack: list[int] = []
    comps: list[list[int]] = []
    counter = 0
    for root in range(n):
        if index[root] >= 0:
            continue
        work: list[tuple[int, int]] = [(root, 0)]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on[root] = True
        while work:
            v, pi = work[-1]
            if pi < len(adj[v]):
                w = adj[v][pi]
                work[-1] = (v, pi + 1)
                if index[w] < 0:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on[w] = True
                    work.append((w, 0))
                elif on[w]:
                    low[v] = min(low[v], index[w])
            else:
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[v])
                if low[v] == index[v]:
                    comp: list[int] = []
                    while True:
                        w = stack.pop()
                        on[w] = False
                        comp.append(w)
                        if w == v:
                            break
                    comps.append(comp)
    return comps


def _solve(nodes: list[engine.Node], raw_edges: list[dict]) -> dict:
    """返回最早可行时刻 L 与（仅含有效端点、非自环的）边索引列表。

    所有约束边权重均为 0（t_v ≥ t_u），故最早可行时刻按 SCC 缩点后的
    DAG 拓扑序做最大值传播即可，O(V+E)；同一 SCC（环）内节点自动取同一
    时刻——环允许同刻，而该公共时刻是否落在环内每个区间里由最后的
    L_i ≤ hi_i 检查回答（等价于“环内区间有公共交集”）。
    """
    by_id = {n.raw["id"]: n for n in nodes}
    n = len(nodes)
    lo = [n_.lo for n_ in nodes]
    hi = [n_.hi for n_ in nodes]

    # 全部有效约束：端点存在且非自环（自环 t ≥ t 恒成立，不参与求解）
    edge_rows: list[tuple[int, int, dict]] = []
    adj: list[list[int]] = [[] for _ in range(n)]   # u -> [v, ...]
    for r in raw_edges:
        a, b = by_id.get(r["from_event"]), by_id.get(r["to_event"])
        if a is None or b is None:
            continue
        u, v = a.idx, b.idx
        if u == v:
            continue
        edge_rows.append((u, v, r))
        adj[u].append(v)

    # ---- SCC 缩点 → DAG 上按拓扑序传播最大下界（O(V+E)）----
    comps = _tarjan(n, adj)
    comp_of = [0] * n
    for ci, comp in enumerate(comps):
        for v in comp:
            comp_of[v] = ci
    nc = len(comps)

    # 缩点 DAG（去重平行边）+ Kahn 拓扑序
    dag: list[set[int]] = [set() for _ in range(nc)]
    indeg = [0] * nc
    for u, v, _r in edge_rows:
        cu, cv = comp_of[u], comp_of[v]
        if cu != cv and cv not in dag[cu]:
            dag[cu].add(cv)
            indeg[cv] += 1

    comp_l = [max(lo[v] for v in comps[ci]) for ci in range(nc)]
    q: deque[int] = deque([ci for ci in range(nc) if indeg[ci] == 0])
    while q:
        cu = q.popleft()
        val = comp_l[cu]
        for cv in dag[cu]:
            if comp_l[cv] < val:
                comp_l[cv] = val
            indeg[cv] -= 1
            if indeg[cv] == 0:
                q.append(cv)

    earliest = [comp_l[comp_of[i]] for i in range(n)]
    violating = [i for i in range(n) if earliest[i] > hi[i] + TOL]
    return {"earliest": earliest, "edges": edge_rows, "adj": adj,
            "lo": lo, "hi": hi, "violating": violating}


def _contradiction_path(sol: dict) -> dict | None:
    """从违反点沿紧边反向找最短矛盾路径；系统可行时返回 None。"""
    earliest = sol["earliest"]
    lo, hi = sol["lo"], sol["hi"]
    edge_rows = sol["edges"]
    violating = set(sol["violating"])
    n = len(earliest)

    # 紧边反向邻接：L_v ≥ L_u 且差值在容差内（该约束被吃成等号）
    radj: list[list[tuple[int, int]]] = [[] for _ in range(n)]
    for ei, (u, v, _r) in enumerate(edge_rows):
        if 0 <= earliest[v] - earliest[u] <= TOL:
            radj[v].append((u, ei))

    # 多源 BFS：从全部违反点出发，反向只走非违反点，命中 lo 源点即得最短链。
    # 每个违反点自身就是搜索源，故即便紧链中途经过别的违反点，离 lo 源最近的那个
    # 违反点也必然能无阻断地溯源到某个 L=lo 的起点。
    prev: dict[int, tuple[int, int]] = {}
    q: deque[int] = deque()
    for s in sol["violating"]:
        prev[s] = (-1, -1)
        q.append(s)
    root: int | None = None
    while q and root is None:
        v = q.popleft()
        for u, ei in radj[v]:
            if u in prev or u in violating:
                continue
            prev[u] = (v, ei)
            if abs(earliest[u] - lo[u]) <= TOL:
                root = u
                break
            q.append(u)
    if root is None:
        return None  # 理论上不可达：违反值必由某条 lo 源沿紧边传播而来

    # 正向重建：root(lo 源) -> ... -> 第一个违反点（在该处截断，
    # 截断前每条边都单独可行，矛盾来自下界沿整条链的传递）
    chain: list[int] = [root]
    edge_chain: list[int] = []
    cur = root
    while cur not in violating:
        nxt, ei = prev[cur]
        if nxt < 0:
            break
        edge_chain.append(ei)
        chain.append(nxt)
        cur = nxt
    return {"events": chain, "edge_indexes": edge_chain}


def _can_reach(adj: list[list[int]], src: int, target: int) -> bool:
    """正向可达性（迭代 BFS，无递归），用于判定矛盾路径是否闭合成环。"""
    if src == target:
        return True
    seen = {src}
    q: deque[int] = deque([src])
    while q:
        u = q.popleft()
        for v in adj[u]:
            if v in seen:
                continue
            if v == target:
                return True
            seen.add(v)
            q.append(v)
    return False


# ------------------------------------------------------------ 序列化入口

def temporal_feasibility(overrides: dict | None = None) -> dict:
    """全量事件区间与全部有效约束联立，不受页面筛选影响。

    overrides 为页面当前（含未保存）的来源假设，与 /api/recompute 同一口径。
    本函数只读重算，不写库、不改写事件或假设。
    """
    stored_sources, raw_events, raw_edges = engine.load_model()
    sources = engine._effective_sources(stored_sources, overrides)
    nodes = engine._build_nodes(raw_events, sources)
    for i, n_ in enumerate(nodes):
        n_.idx = i
    ids = [n.raw["id"] for n in nodes]
    sol = _solve(nodes, raw_edges)
    earliest, edge_rows = sol["earliest"], sol["edges"]
    lo, hi = sol["lo"], sol["hi"]

    n_events, n_edges = len(nodes), len(edge_rows)
    id_set = set(ids)
    self_loops = sum(1 for r in raw_edges
                     if r.get("from_event") == r.get("to_event")
                     and r.get("from_event") in id_set)
    dangling = sum(1 for r in raw_edges
                   if r.get("from_event") not in id_set
                   or r.get("to_event") not in id_set)

    base = {
        "analysis_type": "global-temporal-feasibility",
        "scope": {
            "event_count": n_events,
            "edge_count": n_edges,
            "self_loop_count": self_loops,
            "dangling_edge_count": dangling,
            "note": "全量事件闭区间 × 全部有效约束联立；忽略页面筛选；"
                    "边 a→b 要求 t_b≥t_a，环允许同刻但环内区间须有公共交集。",
        },
        "assumptions": {
            sid: {k: round(float(s[k]), 6) for k in engine.SOURCE_PARAMS}
            for sid, s in sources.items()
        },
        "overrides_applied": overrides or {},
    }

    if not sol["violating"]:
        # (L_i) 即可行见证：L_i ∈ [lo_i, hi_i] 且每条边 L_b ≥ L_a
        pinned = sum(1 for i in range(n_events) if abs(earliest[i] - lo[i]) <= TOL)
        witness = []
        order = sorted(range(n_events),
                       key=lambda i: (-earliest[i],
                                      nodes[i].raw["source_id"], ids[i]))
        for i in order:
            n_ = nodes[i]
            witness.append({
                "id": ids[i],
                "source_id": n_.raw["source_id"],
                "source_name": n_.source.get("name", n_.raw["source_id"]),
                "summary": n_.raw.get("summary", ""),
                "event_type": n_.raw["event_type"],
                "lo": round(lo[i], 6), "hi": round(hi[i], 6),
                "lo_text": fmt(lo[i]), "hi_text": fmt(hi[i]),
                "earliest": round(earliest[i], 6),
                "earliest_text": fmt(earliest[i]),
                "slack": round(hi[i] - earliest[i], 6),
                "slack_text": fmt_delta(hi[i] - earliest[i]),
                "pinned_to_lower": abs(earliest[i] - lo[i]) <= TOL,
            })
        return {
            **base,
            "feasible": True,
            "conclusion": (
                f"可行：{n_events} 个事件可在各自闭区间内取到一组时刻，"
                f"同时满足全部 {n_edges} 条 t_b≥t_a 约束（环允许同刻）。"
            ),
            "stats": {
                "event_count": n_events,
                "edge_count": n_edges,
                "pinned_to_lower_count": pinned,
                "common_horizon": round(max(earliest) - min(earliest), 6)
                if n_events else 0.0,
            },
            "earliest_times": witness,
        }

    # ---- 无解：构造带事件ID / 约束ID / 两端冲突界限的矛盾路径 ----
    cp = _contradiction_path(sol)
    if cp is None:  # 防御性回退：任一违反点 + 其界限
        k = sol["violating"][0]
        chain, edge_idxs = [k], []
    else:
        chain, edge_idxs = cp["events"], cp["edge_indexes"]

    head_i, tail_i = chain[0], chain[-1]
    steps: list[dict] = []
    for pos, ei in enumerate(edge_idxs):
        u, v, r = edge_rows[ei]
        # 单边可行性：存在 t_u=t_v∈[lo_u,hi_u]∩[lo_v,hi_v]
        ov_lo, ov_hi = max(lo[u], lo[v]), min(hi[u], hi[v])
        individually_feasible = ov_lo <= ov_hi + TOL
        steps.append({
            "position": pos + 1,
            "constraint_id": r.get("id"),
            "from": ids[u], "to": ids[v],
            "kind": r.get("kind", "causal"),
            "note": r.get("note", ""),
            "gap_lo": round(lo[v] - hi[u], 6),
            "gap_hi": round(hi[v] - lo[u], 6),
            "gap_lo_text": fmt_delta(lo[v] - hi[u]),
            "gap_hi_text": fmt_delta(hi[v] - lo[u]),
            "individually_feasible": individually_feasible,
            "shared_intersection": {
                "lo": round(ov_lo, 6), "hi": round(ov_hi, 6),
                "lo_text": fmt(ov_lo), "hi_text": fmt(ov_hi),
            } if individually_feasible else None,
        })

    head, tail = nodes[head_i], nodes[tail_i]
    excess = earliest[tail_i] - hi[tail_i]
    chain_sources = {nodes[i].raw["source_id"] for i in chain}
    # 矛盾链能否闭合成环：tail 沿约束边能否回到 head
    in_cycle = (head_i != tail_i
                and _can_reach(sol["adj"], tail_i, head_i))
    n_hops = max(len(chain) - 1, 1)
    all_individually_ok = bool(steps) and all(s["individually_feasible"] for s in steps)

    if steps and not all_individually_ok and len(steps) == 1:
        s0 = steps[0]
        ctype = "impossible_edge"
        explanation = (
            f"不可行：约束 {ids[head_i]} → {ids[tail_i]}（{s0['kind']}）本身即不可能"
            f"——终点最晚 {fmt(hi[tail_i])} 仍早于起点最早 {fmt(lo[head_i])}"
            f"（最有利间隔 {s0['gap_hi_text']} < 0），两端闭区间没有公共点。"
        )
    else:
        ctype = "cycle_no_common_intersection" if in_cycle \
            else "path_no_common_intersection"
        explanation = (
            f"不可行：约束路径 {ids[head_i]} ⇝ {ids[tail_i]}（{n_hops} 跳、"
            f"{len(chain_sources)} 个来源）要求时刻只增不减，但起点最早也必须取 "
            f"{fmt(lo[head_i])}，沿 {n_hops} 条边传递后终点时刻 ≥ "
            f"{fmt(earliest[tail_i])}，已越过其闭区间上界 {fmt(hi[tail_i])}"
            f"（超出 {fmt_delta(excess)}）。"
        )
        if all_individually_ok:
            explanation += "路径上每条边就两端区间而言都能取到同刻、单独可行，矛盾只在联立后暴露。"
        if in_cycle:
            explanation += "该链首尾闭合为环：环允许同刻，但环内区间没有公共交集。"

    return {
        **base,
        "feasible": False,
        "conclusion": explanation,
        "stats": {
            "event_count": n_events,
            "edge_count": n_edges,
            "violating_event_count": len(sol["violating"]),
        },
        "contradiction": {
            "type": ctype,
            "in_cycle": in_cycle,
            "head_event": {
                "id": ids[head_i],
                "source_id": head.raw["source_id"],
                "summary": head.raw.get("summary", ""),
                "lo": round(lo[head_i], 6), "hi": round(hi[head_i], 6),
                "lo_text": fmt(lo[head_i]), "hi_text": fmt(hi[head_i]),
                "earliest": round(earliest[head_i], 6),
                "earliest_text": fmt(earliest[head_i]),
                "bound_role": "lower_bound_origin",
            },
            "tail_event": {
                "id": ids[tail_i],
                "source_id": tail.raw["source_id"],
                "summary": tail.raw.get("summary", ""),
                "lo": round(lo[tail_i], 6), "hi": round(hi[tail_i], 6),
                "lo_text": fmt(lo[tail_i]), "hi_text": fmt(hi[tail_i]),
                "earliest": round(earliest[tail_i], 6),
                "earliest_text": fmt(earliest[tail_i]),
                "bound_role": "upper_bound_violated",
            },
            "conflicting_bounds": {
                "lower_bound_event_id": ids[head_i],
                "lower_bound": round(lo[head_i], 6),
                "lower_bound_text": fmt(lo[head_i]),
                "upper_bound_event_id": ids[tail_i],
                "upper_bound": round(hi[tail_i], 6),
                "upper_bound_text": fmt(hi[tail_i]),
                "excess": round(excess, 6),
                "excess_text": fmt_delta(excess),
            },
            "event_path": [ids[i] for i in chain],
            "path_length_edges": n_hops,
            "steps": steps,
            "all_edges_individually_feasible": all_individually_ok,
            "explanation": explanation,
        },
    }
