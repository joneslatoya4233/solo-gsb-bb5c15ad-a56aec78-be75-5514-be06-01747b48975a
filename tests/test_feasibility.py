"""全局时序可行性求解器的纯标准库测试（无需 FastAPI / 网络）。

用法:  python tests/test_feasibility.py
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR = tempfile.mkdtemp(prefix="audit-feas-")
os.environ["DATA_DIR"] = DATA_DIR

from app.db import db, init_db  # noqa: E402
from app.feasibility import temporal_feasibility as tf  # noqa: E402
from app.importer import import_payload, reset_all  # noqa: E402

init_db(load_sample=False)


def _ts(sec: float) -> str:
    m, s = divmod(sec, 60)
    return f"2026-01-01T00:{int(m):02d}:{s:06.3f}Z"


class FeasibleCases(unittest.TestCase):
    def setUp(self):
        reset_all()

    def test_no_edges_each_pinned_at_lower(self):
        import_payload({"events": [
            {"id": "a", "source": "s1", "time": _ts(10)},
            {"id": "b", "source": "s2", "time": _ts(20)},
        ]})
        r = tf()
        self.assertTrue(r["feasible"])
        by = {w["id"]: w for w in r["earliest_times"]}
        self.assertTrue(by["a"]["pinned_to_lower"])
        self.assertTrue(by["b"]["pinned_to_lower"])
        self.assertAlmostEqual(by["a"]["earliest"], by["a"]["lo"], places=6)

    def test_witness_satisfies_intervals_and_edges(self):
        # a[9,11] -> b[19,21]：最早见证 a=9, b=max(19,9)=19
        import_payload({"events": [
            {"id": "a", "source": "s1", "time": _ts(10)},
            {"id": "b", "source": "s2", "time": _ts(20)},
        ], "constraints": [{"from": "a", "to": "b"}]})
        r = tf()
        self.assertTrue(r["feasible"])
        by = {w["id"]: w for w in r["earliest_times"]}
        self.assertAlmostEqual(by["a"]["earliest"], 1767225609.0, places=3)
        self.assertAlmostEqual(by["b"]["earliest"], 1767225619.0, places=3)
        for w in by.values():
            self.assertGreaterEqual(w["earliest"] + 1e-6, w["lo"])
            self.assertLessEqual(w["earliest"] - 1e-6, w["hi"])

    def test_propagation_pushes_earliest_forward(self):
        # a est=10 ±0 → b est=5 ±6=[-1,11]; 边 a->b 迫使 b 最早=10
        import_payload({
            "sources": [{"id": "s1", "uncertainty": 0}, {"id": "s2", "uncertainty": 6}],
            "events": [
                {"id": "a", "source": "s1", "time": _ts(10)},
                {"id": "b", "source": "s2", "time": _ts(5)},
            ],
            "constraints": [{"from": "a", "to": "b"}],
        })
        r = tf()
        self.assertTrue(r["feasible"], r.get("conclusion"))
        by = {w["id"]: w for w in r["earliest_times"]}
        self.assertAlmostEqual(by["b"]["earliest"], by["a"]["earliest"], places=3)
        self.assertFalse(by["b"]["pinned_to_lower"])
        self.assertAlmostEqual(by["b"]["slack"], 1.0, places=3)

    def test_cycle_with_common_intersection_same_instant(self):
        # 2-环，区间相交：允许同刻，取公共点即可
        import_payload({
            "sources": [{"id": "s1", "uncertainty": 2}, {"id": "s2", "uncertainty": 2}],
            "events": [
                {"id": "u", "source": "s1", "time": _ts(0)},
                {"id": "v", "source": "s2", "time": _ts(1)},
            ],
            "constraints": [{"from": "u", "to": "v"}, {"from": "v", "to": "u"}],
        })
        r = tf()
        self.assertTrue(r["feasible"])
        by = {w["id"]: w for w in r["earliest_times"]}
        self.assertAlmostEqual(by["u"]["earliest"], by["v"]["earliest"], places=6)
        self.assertGreaterEqual(by["u"]["earliest"] + 1e-6, by["u"]["lo"])
        self.assertLessEqual(by["u"]["earliest"] - 1e-6, by["u"]["hi"])

    def test_boundary_touch_is_feasible_closed_interval(self):
        # 闭区间端点相接：a.hi == b.lo，t 同取该点可行
        import_payload({
            "sources": [{"id": "s1", "uncertainty": 0}, {"id": "s2", "uncertainty": 0}],
            "events": [
                {"id": "a", "source": "s1", "time": _ts(0)},
                {"id": "b", "source": "s2", "time": _ts(0)},
            ],
            "constraints": [{"from": "a", "to": "b"}],
        })
        self.assertTrue(tf()["feasible"])

    def test_overrides_used_and_no_filter_param(self):
        # 默认假设下不可能的边，调整 offset 后可行；接口不接收筛选字段
        import_payload({"events": [
            {"id": "a", "source": "s1", "time": _ts(10)},
            {"id": "b", "source": "s2", "time": _ts(8)},
        ], "constraints": [{"from": "a", "to": "b"}]})  # u=1: [9,11] -> [7,9] 端点相接可行
        r = tf()
        self.assertTrue(r["feasible"])
        # 拉大 s1 偏差让区间脱离：s1 offset=-10 -> a≈20,[19,21]，边变不可能
        r2 = tf({"s1": {"clock_offset": -10}})
        self.assertFalse(r2["feasible"])
        self.assertEqual(r2["contradiction"]["steps"][0]["from"], "a")
        self.assertEqual(r2["overrides_applied"], {"s1": {"clock_offset": -10}})


class InfeasibleCases(unittest.TestCase):
    def setUp(self):
        reset_all()

    def test_single_impossible_edge(self):
        import_payload({
            "sources": [{"id": "s1", "uncertainty": 0}, {"id": "s2", "uncertainty": 0}],
            "events": [
                {"id": "a", "source": "s1", "time": _ts(10)},
                {"id": "b", "source": "s2", "time": _ts(0)},
            ],
            "constraints": [{"from": "a", "to": "b"}],
        })
        r = tf()
        self.assertFalse(r["feasible"])
        c = r["contradiction"]
        self.assertEqual(c["type"], "impossible_edge")
        self.assertEqual(c["event_path"], ["a", "b"])
        self.assertEqual(c["conflicting_bounds"]["lower_bound_event_id"], "a")
        self.assertEqual(c["conflicting_bounds"]["upper_bound_event_id"], "b")
        self.assertGreater(c["conflicting_bounds"]["excess"], 0)
        step = c["steps"][0]
        self.assertFalse(step["individually_feasible"])
        self.assertIsNone(step["shared_intersection"])
        self.assertIsNotNone(step["constraint_id"])

    def test_path_each_edge_feasible_but_joint_infeasible(self):
        # a[5,6], b[4,7], c[4,4.5]；a→b、b→c 区间都相交（单边可同刻），
        # 但 lo_a=5 沿链传到 c，越过 hi_c=4.5。
        import_payload({
            "sources": [
                {"id": "sA", "uncertainty": 0.5},
                {"id": "sB", "uncertainty": 1.5},
                {"id": "sC", "uncertainty": 0.25},
            ],
            "events": [
                {"id": "a", "source": "sA", "time": _ts(5.5)},
                {"id": "b", "source": "sB", "time": _ts(5.5)},
                {"id": "c", "source": "sC", "time": _ts(4.25)},
            ],
            "constraints": [
                {"from": "a", "to": "b"},
                {"from": "b", "to": "c"},
            ],
        })
        r = tf()
        self.assertFalse(r["feasible"])
        c = r["contradiction"]
        self.assertEqual(c["type"], "path_no_common_intersection")
        self.assertEqual(c["event_path"], ["a", "b", "c"])
        self.assertTrue(c["all_edges_individually_feasible"])
        self.assertEqual(len(c["steps"]), 2)
        for s in c["steps"]:
            self.assertTrue(s["individually_feasible"])
            self.assertIsNotNone(s["shared_intersection"])
            self.assertIsNotNone(s["constraint_id"])
        bnd = c["conflicting_bounds"]
        self.assertAlmostEqual(bnd["lower_bound"], 1767225605.0, places=3)
        self.assertAlmostEqual(bnd["upper_bound"], 1767225604.5, places=3)
        self.assertAlmostEqual(bnd["excess"], 0.5, places=3)

    def test_cycle_no_common_intersection_all_edges_individually_ok(self):
        # 5-环：相邻区间（含 5→1）都相交，但五环无公共交集
        import_payload({
            "sources": [{"id": "sa", "uncertainty": 1}, {"id": "sb", "uncertainty": 1}],
            "events": [
                {"id": "e1", "source": "sa", "time": _ts(1)},
                {"id": "e2", "source": "sb", "time": _ts(2)},
                {"id": "e3", "source": "sa", "time": _ts(3.5)},
                {"id": "e4", "source": "sb", "time": _ts(5)},
                {"id": "e5", "source": "sa", "time": _ts(3)},
            ],
            "constraints": [
                {"from": "e1", "to": "e2"}, {"from": "e2", "to": "e3"},
                {"from": "e3", "to": "e4"}, {"from": "e4", "to": "e5"},
                {"from": "e5", "to": "e1"},
            ],
        })
        r = tf()
        self.assertFalse(r["feasible"])
        c = r["contradiction"]
        self.assertTrue(c["in_cycle"])
        self.assertIn("cycle", c["type"])
        self.assertTrue(c["all_edges_individually_feasible"])
        head = c["head_event"]
        tail = c["tail_event"]
        self.assertEqual(head["bound_role"], "lower_bound_origin")
        self.assertEqual(tail["bound_role"], "upper_bound_violated")
        self.assertIn(head["id"], c["event_path"])
        self.assertIn(tail["id"], c["event_path"])

    def test_contradiction_path_is_tight_and_bounded(self):
        # 传递链 0→1→2→3，区间依次 [i, i+2]，最后加边 3→0：
        # 环内 max lo 来自不同分量时应有界终止；路径上的边 ID 均可在响应中找到。
        import_payload({
            "sources": [{"id": "s1", "uncertainty": 0}, {"id": "s2", "uncertainty": 0}],
            "events": [
                {"id": "p", "source": "s1", "time": _ts(0)},
                {"id": "q", "source": "s2", "time": _ts(1)},
                {"id": "r2", "source": "s1", "time": _ts(2)},
                {"id": "s", "source": "s2", "time": _ts(3)},
            ],
            "constraints": [
                {"from": "p", "to": "q"}, {"from": "q", "to": "r2"},
                {"from": "r2", "to": "s"}, {"from": "s", "to": "p"},
            ],
        })
        r = tf()
        c = r["contradiction"]
        self.assertTrue(c["in_cycle"])
        self.assertLessEqual(len(c["event_path"]), 4)
        self.assertEqual(len(c["steps"]), len(c["event_path"]) - 1)


class ScopeAndSideEffects(unittest.TestCase):
    def setUp(self):
        reset_all()

    def test_self_loops_ignored(self):
        # 悬空约束在导入阶段即被拒绝入库；自环 a→a 会入库但求解时忽略
        reset_all()
        s = import_payload({"events": [
            {"id": "a", "source": "s1", "time": _ts(0)},
        ], "constraints": [
            {"from": "a", "to": "a"},
            {"from": "a", "to": "ghost"},
            {"from": "ghost", "to": "a"},
        ]})
        self.assertEqual(len(s["warnings"]), 2)  # 悬空约束不会入库
        r = tf()
        self.assertTrue(r["feasible"])
        self.assertEqual(r["scope"]["edge_count"], 0)
        self.assertEqual(r["scope"]["self_loop_count"], 1)
        self.assertEqual(r["scope"]["dangling_edge_count"], 0)

    def test_readonly_no_db_mutation(self):
        reset_all()
        import_payload({"events": [
            {"id": "a", "source": "s1", "time": _ts(10)},
            {"id": "b", "source": "s2", "time": _ts(0)},
        ], "constraints": [{"from": "a", "to": "b"}]})
        with db() as conn:
            counts_before = (
                conn.execute("SELECT COUNT(*) n FROM events").fetchone()["n"],
                conn.execute("SELECT COUNT(*) n FROM constraints").fetchone()["n"],
            )
            offsets_before = {row["id"]: (row["clock_offset"], row["uncertainty"])
                              for row in conn.execute(
                                  "SELECT id, clock_offset, uncertainty FROM sources")}
        tf()  # 无解，仍不得写库
        tf({"s1": {"clock_offset": 42.5, "uncertainty": 9}})
        with db() as conn:
            counts_after = (
                conn.execute("SELECT COUNT(*) n FROM events").fetchone()["n"],
                conn.execute("SELECT COUNT(*) n FROM constraints").fetchone()["n"],
            )
            offsets_after = {row["id"]: (row["clock_offset"], row["uncertainty"])
                             for row in conn.execute(
                                 "SELECT id, clock_offset, uncertainty FROM sources")}
        self.assertEqual(counts_before, counts_after)
        self.assertEqual(offsets_before, offsets_after)
        self.assertNotIn(42.5, [v[0] for v in offsets_after.values()])

    def test_full_dataset_independent_of_filters(self):
        # 全量事件都参与，即使存在与“筛选”无关的远距离矛盾
        import_payload({
            "sources": [{"id": "s1", "uncertainty": 0}, {"id": "s2", "uncertainty": 0}],
            "events": [
                {"id": "x1", "source": "s1", "time": _ts(100)},
                {"id": "x2", "source": "s1", "time": _ts(101)},
                {"id": "y1", "source": "s2", "time": _ts(0)},
                {"id": "y2", "source": "s2", "time": _ts(1)},
            ],
            "constraints": [{"from": "x2", "to": "y1"}],
        })
        r = tf()
        self.assertFalse(r["feasible"])
        self.assertEqual(r["scope"]["event_count"], 4)
        self.assertEqual(r["contradiction"]["event_path"], ["x2", "y1"])


class LargeScale(unittest.TestCase):
    def setUp(self):
        reset_all()

    def test_5000_chain_no_recursion(self):
        import sys as _sys
        _sys.setrecursionlimit(300)  # 强制暴露任何隐藏递归
        n = 5000
        events = [{"id": f"e{i}", "source": f"s{i % 8}",
                   "time": 1_700_000_000 + i} for i in range(n)]
        edges = [{"from": f"e{i}", "to": f"e{i+1}"} for i in range(n - 1)]
        import_payload({"events": events, "constraints": edges})
        r = tf()
        self.assertTrue(r["feasible"])
        self.assertEqual(len(r["earliest_times"]), n)
        by = {w["id"]: w for w in r["earliest_times"]}
        self.assertAlmostEqual(by["e4999"]["earliest"], by["e4999"]["lo"], places=3)
        self.assertGreaterEqual(by["e1"]["earliest"] + 1e-6, by["e0"]["earliest"])

    def test_5000_infeasible_returns_short_path(self):
        n = 5000
        events = [{"id": f"e{i}", "source": f"s{i % 8}",
                   "time": 1_700_000_000 + i} for i in range(n)]
        edges = [{"from": f"e{i}", "to": f"e{i+1}"} for i in range(n - 1)]
        edges.append({"from": f"e{n-1}", "to": "e0"})  # 反向边直接不可能
        import_payload({"events": events, "constraints": edges})
        r = tf()
        self.assertFalse(r["feasible"])
        self.assertEqual(len(r["contradiction"]["event_path"]), 2)
        self.assertEqual(r["contradiction"]["event_path"], [f"e{n-1}", "e0"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
