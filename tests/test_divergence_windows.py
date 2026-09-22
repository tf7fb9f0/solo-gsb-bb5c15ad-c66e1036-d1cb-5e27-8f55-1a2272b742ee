"""POST /divergence-windows 分裂窗口单元测试（仅依赖标准库 + 本项目模块）。

运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest

from app.serial import (
    HALF,
    MAX_SERIAL,
    MOD,
    OBSERVATION_SERIAL_CONFLICT,
    ObservationOrderError,
    check_divergence_observation_order,
    evaluate_divergence_windows,
    forward_distance,
    has_common_strict_successor,
)


class CheckDivergenceObservationOrderTests(unittest.TestCase):
    def test_non_decreasing_ok(self):
        check_divergence_observation_order("ns-a", [(1, 10), (2, 20), (2, 20)])
        check_divergence_observation_order("ns-a", [])
        check_divergence_observation_order("ns-a", [(0, 0)])

    def test_same_timestamp_same_serial_allowed(self):
        # 同刻同值的重复上报静默允许
        check_divergence_observation_order(
            "ns-a", [(10, 5), (10, 5), (10, 5), (20, 6)]
        )

    def test_out_of_order_rejected(self):
        with self.assertRaises(ObservationOrderError) as ctx:
            check_divergence_observation_order(
                "ns-b", [(10, 1), (30, 2), (20, 3)]
            )
        detail = ctx.exception.detail
        self.assertEqual(detail["error"], "observation_timestamp_out_of_order")
        self.assertEqual(detail["replica"], "ns-b")
        self.assertEqual(detail["index"], 2)
        self.assertEqual(detail["timestamp"], 20)
        self.assertEqual(detail["previous_timestamp"], 30)

    def test_same_timestamp_different_serial_rejected(self):
        with self.assertRaises(ObservationOrderError) as ctx:
            check_divergence_observation_order(
                "ns-a", [(10, 1), (10, 2)]
            )
        detail = ctx.exception.detail
        self.assertEqual(detail["error"], OBSERVATION_SERIAL_CONFLICT)
        self.assertEqual(detail["replica"], "ns-a")
        self.assertEqual(detail["index"], 1)
        self.assertEqual(detail["timestamp"], 10)
        self.assertEqual(detail["serials"], [1, 2])

    def test_conflict_after_identical_duplicate_rejected(self):
        with self.assertRaises(ObservationOrderError) as ctx:
            check_divergence_observation_order(
                "ns-a", [(10, 5), (10, 5), (10, 6)]
            )
        self.assertEqual(
            ctx.exception.detail["error"], OBSERVATION_SERIAL_CONFLICT
        )


class HasCommonStrictSuccessorTests(unittest.TestCase):
    def test_close_values_feasible(self):
        self.assertTrue(has_common_strict_successor([100, 150]))

    def test_single_and_empty_always_feasible(self):
        self.assertTrue(has_common_strict_successor([HALF]))
        self.assertTrue(has_common_strict_successor([]))

    def test_antipodal_pair_infeasible(self):
        # 相差正好 2^31 的正对点：无共同严格后继
        self.assertFalse(has_common_strict_successor([0, HALF]))
        self.assertFalse(has_common_strict_successor([10, (10 + HALF) % MOD]))

    def test_spread_of_exactly_half_infeasible(self):
        # 开弧长度只有 2^31-1，跨度达到半环即不可行
        self.assertFalse(has_common_strict_successor([0, HALF - 1, 1]))
        self.assertFalse(has_common_strict_successor([5, HALF + 5]))

    def test_integer_range_trap_wraparound_feasible(self):
        # 不能用普通整数极差：4294967290 与 5 整数极差约 4.29e9，
        # 但在环上仅相距 11，存在共同严格后继
        self.assertTrue(
            has_common_strict_successor([4294967290, 4294967295, 5])
        )
        self.assertTrue(has_common_strict_successor([MAX_SERIAL, 0]))

    def test_wraparound_but_spread_over_half_infeasible(self):
        # 有值聚集在 0 两侧（4294967290 与 5，整数极差约 4.29e9 却环上相邻）：
        # 2147483653 与 5 的前向距离恰为 HALF（正对点，(5 - HALF) mod 2^32
        # = 2147483653），三个值没有共同开弧——整数极差法对这类环上分裂
        # 完全失效
        antipode_of_5 = (5 - HALF) % MOD
        self.assertEqual(forward_distance(antipode_of_5, 5), HALF)
        self.assertFalse(
            has_common_strict_successor([4294967290, 5, antipode_of_5])
        )
        # 三点均匀跨环（无正对点、两两整数极差仅约 1.43e9 < 2^31，
        # 整数极差法会误判为“跨度很小”）：任意长 2^31-1 的弧都无法同时
        # 容纳三点，故不存在共同严格后继
        self.assertFalse(
            has_common_strict_successor([0, HALF // 2, HALF - 1 + HALF // 2])
        )

    def test_three_replicas_no_common_successor(self):
        # 三个值各占一段，开弧交集为空
        self.assertFalse(has_common_strict_successor([0, HALF // 2, HALF]))


class EvaluateDivergenceWindowsTests(unittest.TestCase):
    def test_no_divergence_returns_empty(self):
        windows = evaluate_divergence_windows(
            ["ns-a", "ns-b"],
            [
                [(10, 100), (20, 101)],
                [(10, 150), (20, 102)],
            ],
        )
        self.assertEqual(windows, [])

    def test_wraparound_integer_range_trap_no_window(self):
        # 整数极差巨大但环上相邻：全程可行，不产生窗口
        windows = evaluate_divergence_windows(
            ["ns-a", "ns-b"],
            [
                [(0, 4294967290), (10, 0)],
                [(0, 4294967295), (10, 5)],
            ],
        )
        self.assertEqual(windows, [])

    def test_divergence_then_recovery(self):
        windows = evaluate_divergence_windows(
            ["ns-a", "ns-b"],
            [
                [(1000, 100), (1030, 100), (1060, 101)],
                [(1000, 150), (1030, 100 + HALF), (1060, 102)],
            ],
        )
        self.assertEqual(len(windows), 1)
        start_at, end_at, duration, entries = windows[0]
        self.assertEqual(start_at, 1030)
        self.assertEqual(end_at, 1060)
        self.assertEqual(duration, 30)
        self.assertEqual(entries, [("ns-a", 100), ("ns-b", 100 + HALF)])

    def test_unrecovered_window_null_end_and_duration(self):
        windows = evaluate_divergence_windows(
            ["ns-a", "ns-b"],
            [
                [(0, 4294967290), (10, 0), (20, HALF)],
                [(0, 4294967295), (10, 5), (20, 0)],
            ],
        )
        self.assertEqual(len(windows), 1)
        start_at, end_at, duration, entries = windows[0]
        self.assertEqual(start_at, 20)
        self.assertIsNone(end_at)
        self.assertIsNone(duration)
        self.assertEqual(entries, [("ns-a", HALF), ("ns-b", 0)])

    def test_values_carried_forward_between_updates(self):
        # ns-a 只在 10 上报 0；ns-b 在 20 跳到正对点 -> a 的值被携带，
        # 状态 (0, HALF) 不可行；40 时 b 回到 2，状态 (0, 2) 恢复
        windows = evaluate_divergence_windows(
            ["ns-a", "ns-b"],
            [
                [(10, 0)],
                [(10, 0), (20, HALF), (40, 2)],
            ],
        )
        self.assertEqual(len(windows), 1)
        start_at, end_at, duration, entries = windows[0]
        self.assertEqual((start_at, end_at, duration), (20, 40, 20))
        # 进入窗口时 a 携带的仍是 10 时刻的 0
        self.assertEqual(entries, [("ns-a", 0), ("ns-b", HALF)])

    def test_entry_snapshot_fixed_at_window_start(self):
        # 20 开窗时 entries = (a=0, b=HALF)；30 时 a 更新但窗口仍未恢复，
        # entries 不随窗口内更新变化
        windows = evaluate_divergence_windows(
            ["ns-a", "ns-b"],
            [
                [(10, 0), (30, 1)],
                [(10, 0), (20, HALF), (40, 2)],
            ],
        )
        self.assertEqual(len(windows), 1)
        _start, end, _dur, entries = windows[0]
        self.assertEqual(end, 40)
        self.assertEqual(entries, [("ns-a", 0), ("ns-b", HALF)])

    def test_consecutive_infeasible_states_merged_into_one_window(self):
        # 20/30/40 连续不可行，合并为单一窗口 [20, 50)
        windows = evaluate_divergence_windows(
            ["ns-a", "ns-b"],
            [
                [(10, 0), (20, 0), (30, 1), (40, 1), (50, 2)],
                [(10, 0), (20, HALF), (30, HALF), (40, HALF + 1), (50, 3)],
            ],
        )
        self.assertEqual(len(windows), 1)
        start_at, end_at, duration, _entries = windows[0]
        self.assertEqual((start_at, end_at, duration), (20, 50, 30))

    def test_separated_infeasible_spells_are_separate_windows(self):
        windows = evaluate_divergence_windows(
            ["ns-a", "ns-b"],
            [
                [(10, 0), (20, 0), (30, 0), (40, HALF), (50, HALF)],
                [(10, 0), (20, HALF), (30, 1), (40, 1), (50, HALF)],
            ],
        )
        self.assertEqual(len(windows), 2)
        self.assertEqual(
            (windows[0][0], windows[0][1], windows[0][2]), (20, 30, 10)
        )
        self.assertEqual(
            (windows[1][0], windows[1][1], windows[1][2]), (40, 50, 10)
        )

    def test_same_timestamp_updates_merged_atomically(self):
        # 20 时刻 a 与 b 同时从 0 跳到 HALF：若顺序处理会先出现正对点
        # (HALF, 0) 的中间状态，原子合并后联合状态 (HALF, HALF) 可行 -> 无窗口
        windows = evaluate_divergence_windows(
            ["ns-a", "ns-b"],
            [
                [(10, 0), (20, HALF)],
                [(10, 0), (20, HALF)],
            ],
        )
        self.assertEqual(windows, [])

        # 对照：两个更新拆到不同时刻，中间状态持续到下一时刻 -> 有窗口
        windows_split = evaluate_divergence_windows(
            ["ns-a", "ns-b"],
            [
                [(10, 0), (20, HALF), (30, HALF)],
                [(10, 0), (20, 0), (30, HALF)],
            ],
        )
        self.assertEqual(len(windows_split), 1)
        self.assertEqual(
            (windows_split[0][0], windows_split[0][1]), (20, 30)
        )

    def test_replica_without_observation_not_participating(self):
        # ns-b 从不上报：ns-a 单值恒可行，无窗口
        windows = evaluate_divergence_windows(
            ["ns-a", "ns-b"], [[(10, 0), (20, HALF)], []]
        )
        self.assertEqual(windows, [])

    def test_late_joining_replica_can_open_window(self):
        # ns-a 单独推进始终可行；30 时 ns-b 首次上报 0，与 a=HALF 正对 -> 开窗，
        # entries 包含此刻双方的快照
        windows = evaluate_divergence_windows(
            ["ns-a", "ns-b"],
            [
                [(10, 0), (20, HALF)],
                [(30, 0), (40, HALF)],
            ],
        )
        self.assertEqual(len(windows), 1)
        start_at, end_at, _dur, entries = windows[0]
        self.assertEqual((start_at, end_at), (30, 40))
        self.assertEqual(entries, [("ns-a", HALF), ("ns-b", 0)])

    def test_replica_joining_during_window_not_added_to_entries(self):
        # 30 开窗（a=HALF, b=0）；40 时 ns-c 才首次上报，窗口仍未恢复，
        # ns-c 不补入窗口 entries；50 全部为 HALF 恢复
        windows = evaluate_divergence_windows(
            ["ns-a", "ns-b", "ns-c"],
            [
                [(10, 0), (20, HALF), (50, HALF)],
                [(30, 0), (50, HALF)],
                [(40, 0), (50, HALF)],
            ],
        )
        self.assertEqual(len(windows), 1)
        start_at, end_at, duration, entries = windows[0]
        self.assertEqual((start_at, end_at, duration), (30, 50, 20))
        self.assertEqual(entries, [("ns-a", HALF), ("ns-b", 0)])

    def test_identical_same_timestamp_duplicates_deduped(self):
        windows = evaluate_divergence_windows(
            ["ns-a", "ns-b"],
            [
                [(10, 0), (10, 0), (10, 0)],
                [(20, HALF)],
            ],
        )
        self.assertEqual(len(windows), 1)
        self.assertEqual(
            (windows[0][0], windows[0][1]), (20, None)
        )
        self.assertEqual(windows[0][3], [("ns-a", 0), ("ns-b", HALF)])

    def test_interleaved_timelines_from_different_replicas(self):
        # 时间戳来自不同副本交错，合并顺序不影响结果
        windows = evaluate_divergence_windows(
            ["ns-a", "ns-b"],
            [
                [(10, 0), (30, HALF)],
                [(20, HALF), (40, 0), (60, 2)],
            ],
        )
        # 20: a 携带 0、b=HALF 正对 -> 开窗；30: (HALF, HALF) 相等可行 -> [20,30)
        # 40: (HALF, 0) 正对 -> 开窗；60: (HALF, 2) 开弧交于 HALF+1 可行
        # -> [40,60)
        self.assertEqual(len(windows), 2)
        self.assertEqual(
            (windows[0][0], windows[0][1], windows[0][2]), (20, 30, 10)
        )
        self.assertEqual(
            (windows[1][0], windows[1][1], windows[1][2]), (40, 60, 20)
        )

    def test_entries_order_follows_replica_input_order(self):
        windows = evaluate_divergence_windows(
            ["ns-z", "ns-a"],
            [
                [(10, 0), (20, 0)],
                [(10, 0), (20, HALF)],
            ],
        )
        self.assertEqual(windows[0][3], [("ns-z", 0), ("ns-a", HALF)])


# 路由层测试：直接以函数方式调用 FastAPI 端点（无需 httpx/TestClient）。
try:
    from fastapi import HTTPException as _HTTPException
    from pydantic import ValidationError as _ValidationError

    from app.main import app, divergence_windows
    from app.schemas import DivergenceWindowsRequest

    _HAS_FASTAPI = True
except ImportError:  # pragma: no cover - CI 中 fastapi 必装
    _HAS_FASTAPI = False


@unittest.skipUnless(_HAS_FASTAPI, "fastapi 未安装")
class DivergenceWindowsEndpointTests(unittest.TestCase):
    def _request(self, payload):
        return divergence_windows(DivergenceWindowsRequest.model_validate(payload))

    def test_endpoint_divergence_then_recovery(self):
        resp = self._request(
            {
                "replicas": [
                    {
                        "name": "ns-a",
                        "observations": [
                            {"timestamp": 1000, "serial": 100},
                            {"timestamp": 1030, "serial": 100},
                            {"timestamp": 1060, "serial": 101},
                        ],
                    },
                    {
                        "name": "ns-b",
                        "observations": [
                            {"timestamp": 1000, "serial": 150},
                            {"timestamp": 1030, "serial": 100 + HALF},
                            {"timestamp": 1060, "serial": 102},
                        ],
                    },
                ]
            }
        )
        self.assertEqual(len(resp.windows), 1)
        window = resp.windows[0]
        self.assertEqual(window.start_at, 1030)
        self.assertEqual(window.end_at, 1060)
        self.assertEqual(window.duration_seconds, 30)
        self.assertEqual(
            [(e.name, e.serial) for e in window.replicas],
            [("ns-a", 100), ("ns-b", 100 + HALF)],
        )

    def test_endpoint_unrecovered_window_nulls(self):
        resp = self._request(
            {
                "replicas": [
                    {
                        "name": "ns-a",
                        "observations": [
                            {"timestamp": 0, "serial": 4294967290},
                            {"timestamp": 10, "serial": 0},
                            {"timestamp": 20, "serial": HALF},
                        ],
                    },
                    {
                        "name": "ns-b",
                        "observations": [
                            {"timestamp": 0, "serial": 4294967295},
                            {"timestamp": 10, "serial": 5},
                            {"timestamp": 20, "serial": 0},
                        ],
                    },
                ]
            }
        )
        (window,) = resp.windows
        self.assertEqual(window.start_at, 20)
        self.assertIsNone(window.end_at)
        self.assertIsNone(window.duration_seconds)

    def test_endpoint_wraparound_no_window(self):
        resp = self._request(
            {
                "replicas": [
                    {"name": "ns-a", "observations": [
                        {"timestamp": 0, "serial": 4294967290},
                        {"timestamp": 10, "serial": 0},
                    ]},
                    {"name": "ns-b", "observations": [
                        {"timestamp": 0, "serial": 4294967295},
                        {"timestamp": 10, "serial": 5},
                    ]},
                ]
            }
        )
        self.assertEqual(resp.windows, [])

    def test_endpoint_same_timestamp_atomic_merge(self):
        resp = self._request(
            {
                "replicas": [
                    {"name": "ns-a", "observations": [
                        {"timestamp": 10, "serial": 0},
                        {"timestamp": 20, "serial": HALF},
                    ]},
                    {"name": "ns-b", "observations": [
                        {"timestamp": 10, "serial": 0},
                        {"timestamp": 20, "serial": HALF},
                    ]},
                ]
            }
        )
        self.assertEqual(resp.windows, [])

    def test_endpoint_duplicate_replica_name_422(self):
        with self.assertRaises(_HTTPException) as ctx:
            self._request(
                {
                    "replicas": [
                        {"name": "ns-a", "observations": [
                            {"timestamp": 1, "serial": 1}]},
                        {"name": "ns-a", "observations": [
                            {"timestamp": 2, "serial": 1}]},
                    ]
                }
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(
            ctx.exception.detail["error"], "duplicate_replica_name"
        )

    def test_endpoint_out_of_order_timestamp_422(self):
        with self.assertRaises(_HTTPException) as ctx:
            self._request(
                {
                    "replicas": [
                        {"name": "ns-a", "observations": [
                            {"timestamp": 20, "serial": 1},
                            {"timestamp": 10, "serial": 1},
                        ]},
                    ]
                }
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(
            ctx.exception.detail["error"],
            "observation_timestamp_out_of_order",
        )
        self.assertEqual(ctx.exception.detail["replica"], "ns-a")

    def test_endpoint_same_timestamp_serial_conflict_422(self):
        with self.assertRaises(_HTTPException) as ctx:
            self._request(
                {
                    "replicas": [
                        {"name": "ns-a", "observations": [
                            {"timestamp": 10, "serial": 1},
                            {"timestamp": 10, "serial": 2},
                        ]},
                    ]
                }
            )
        self.assertEqual(ctx.exception.status_code, 422)
        detail = ctx.exception.detail
        self.assertEqual(detail["error"], OBSERVATION_SERIAL_CONFLICT)
        self.assertEqual(detail["replica"], "ns-a")
        self.assertEqual(detail["timestamp"], 10)
        self.assertEqual(detail["serials"], [1, 2])

    def test_endpoint_same_timestamp_same_serial_allowed(self):
        resp = self._request(
            {
                "replicas": [
                    {"name": "ns-a", "observations": [
                        {"timestamp": 10, "serial": 5},
                        {"timestamp": 10, "serial": 5},
                    ]},
                    {"name": "ns-b", "observations": [
                        {"timestamp": 10, "serial": 5},
                    ]},
                ]
            }
        )
        self.assertEqual(resp.windows, [])

    def test_request_validation_422(self):
        base = {
            "replicas": [
                {"name": "ns-a", "observations": [
                    {"timestamp": 1, "serial": 1}]},
            ]
        }

        def mutated_replica(**replica_changes):
            import copy

            payload = copy.deepcopy(base)
            payload["replicas"] = [
                dict(payload["replicas"][0], **replica_changes)
            ]
            return payload

        invalid_payloads = [
            {"replicas": []},
            mutated_replica(name="   "),
            mutated_replica(observations=[{"timestamp": -1, "serial": 1}]),
            mutated_replica(observations=[{"timestamp": 1.5, "serial": 1}]),
            mutated_replica(observations=[{"timestamp": True, "serial": 1}]),
            mutated_replica(observations=[{"timestamp": "1", "serial": 1}]),
            mutated_replica(observations=[{"serial": 1}]),
            mutated_replica(observations=[{"timestamp": 1, "serial": -1}]),
            mutated_replica(observations=[{"timestamp": 1, "serial": 2**32}]),
            mutated_replica(observations=[{"timestamp": 1, "serial": True}]),
            mutated_replica(observations=[{"timestamp": 1, "serial": "7x"}]),
        ]
        for payload in invalid_payloads:
            with self.assertRaises(
                _ValidationError, msg=f"payload={payload}"
            ):
                DivergenceWindowsRequest.model_validate(payload)

    def test_openapi_lists_divergence_windows_with_examples(self):
        schema = app.openapi()
        self.assertIn("/divergence-windows", schema["paths"])
        self.assertIn("post", schema["paths"]["/divergence-windows"])
        request_schema = schema["components"]["schemas"][
            "DivergenceWindowsRequest"
        ]
        self.assertIn("examples", request_schema)
        self.assertGreaterEqual(len(request_schema["examples"]), 1)
        # 示例本身必须能通过请求模型校验
        for example in request_schema["examples"]:
            DivergenceWindowsRequest.model_validate(example)


if __name__ == "__main__":
    unittest.main()
