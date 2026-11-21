"""检修排班纯逻辑自测：窗口语义、断档校验、改期替换、并发核准、持久化。

只用标准库即可运行：
  python3 -I -m unittest tests.test_maintenance -v
"""

from __future__ import annotations

import csv
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from datetime import timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "app"))

import maintenance as mt  # noqa: E402


def _write_csv(path: str, header: list, rows: list) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def _dt(text: str):
    return mt.parse_utc(text)


class MaintenanceTestBase(unittest.TestCase):
    """标准数据：T1/T2 需要 d1（M1、M2 缓存）；T3 需要 d2（仅 M3 缓存）；
    T4 需要 d3（无任何缓存）；M4 只缓存无人需要的 dX。"""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.job = os.path.join(self.tmp, "job")
        os.makedirs(self.job, exist_ok=True)
        _write_csv(os.path.join(self.job, "tasks.csv"),
                   ["任务号", "层摘要"],
                   [("T1", "d1"), ("T2", "d1"), ("T3", "d2"), ("T4", "d3")])
        _write_csv(os.path.join(self.job, "machines.csv"),
                   ["来源构建机", "层摘要"],
                   [("M1", "d1"), ("M2", "d1"), ("M3", "d2"), ("M4", "dX")])
        mt.set_pre_commit_hook(None)

    def tearDown(self) -> None:
        mt.set_pre_commit_hook(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add(self, machine: str, start: str, end: str) -> mt.Window:
        return mt.add_window(self.job, machine, _dt(start), _dt(end))


class ParseUtcTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(_dt("2026-11-21 08:00").tzinfo, timezone.utc)
        self.assertEqual(_dt("2026-11-21 08:00").hour, 8)
        self.assertEqual(_dt("2026-11-21T08:00:30").second, 30)
        # 尾部 Z 与显式偏移都归一到 UTC
        self.assertEqual(_dt("2026-11-21T08:00Z"), _dt("2026-11-21 08:00"))
        self.assertEqual(_dt("2026-11-21 16:00+08:00"),
                         _dt("2026-11-21 08:00"))
        # 仅日期 -> 当天 UTC 零点
        self.assertEqual(_dt("2026-11-21").hour, 0)

    def test_invalid(self):
        for bad in ("", "  ", "not-a-time", "2026-13-01 08:00"):
            with self.assertRaises(mt.MaintenanceError):
                _dt(bad)


class MergeIntervalsTests(unittest.TestCase):
    def test_overlap_adjacent_disjoint(self):
        merged = mt.merge_intervals([
            (_dt("2026-11-21 01:00"), _dt("2026-11-21 03:00")),
            (_dt("2026-11-21 02:00"), _dt("2026-11-21 04:00")),  # 重叠
            (_dt("2026-11-21 04:00"), _dt("2026-11-21 05:00")),  # 首尾相接
            (_dt("2026-11-21 06:00"), _dt("2026-11-21 07:00")),  # 不相交
        ])
        self.assertEqual(merged, [
            (_dt("2026-11-21 01:00"), _dt("2026-11-21 05:00")),
            (_dt("2026-11-21 06:00"), _dt("2026-11-21 07:00")),
        ])


class WindowSemanticsTests(MaintenanceTestBase):
    def test_register_persists_and_survives_restart(self):
        w = self._add("M1", "2026-11-21 08:00", "2026-11-21 10:00")
        self.assertTrue(os.path.exists(
            os.path.join(self.job, mt.MAINTENANCE_FILE)))
        # 模拟服务重启：重新从磁盘读取，窗口仍在
        loaded = mt.load_windows(self.job)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].id, w.id)
        self.assertEqual(loaded[0].machine, "M1")
        self.assertEqual(loaded[0].start, _dt("2026-11-21 08:00"))
        self.assertEqual(loaded[0].end, _dt("2026-11-21 10:00"))

    def test_single_holder_layer_gaps_immediately(self):
        # d2 只有 M3 一台缓存机，M3 一检修就断档
        with self.assertRaises(mt.GapConflict) as ctx:
            self._add("M3", "2026-11-21 08:00", "2026-11-21 10:00")
        exc = ctx.exception
        self.assertEqual(exc.task_id, "T3")
        self.assertEqual(exc.digest, "d2")
        self.assertEqual(exc.gap_start, _dt("2026-11-21 08:00"))
        self.assertEqual(exc.gap_end, _dt("2026-11-21 10:00"))
        # 拒绝后保留原排班（仍为空）
        self.assertEqual(mt.load_windows(self.job), [])

    def test_gap_requires_all_holders_down_and_end_exclusive(self):
        self._add("M1", "2026-11-21 08:00", "2026-11-21 10:00")
        # 半开区间：M2 从 10:00 开始，与 M1 窗口首尾相接，不算断档
        self._add("M2", "2026-11-21 10:00", "2026-11-21 12:00")
        # M2 提前到 09:30：与 M1 在 [09:30, 10:00) 同时停机 -> 断档
        with self.assertRaises(mt.GapConflict) as ctx:
            self._add("M2", "2026-11-21 09:30", "2026-11-21 12:00")
        self.assertEqual(ctx.exception.task_id, "T1")  # 首个需要 d1 的任务
        self.assertEqual(ctx.exception.digest, "d1")
        self.assertEqual(ctx.exception.gap_start, _dt("2026-11-21 09:30"))
        self.assertEqual(ctx.exception.gap_end, _dt("2026-11-21 10:00"))
        self.assertEqual(len(mt.load_windows(self.job)), 2)

    def test_unneeded_or_uncached_layers_unconstrained(self):
        # M4 只有 dX（无任务需要）-> 任意窗口都允许
        self._add("M4", "2026-11-21 08:00", "2026-11-21 10:00")
        # d3 有任务需要但无任何缓存机 -> 不产生排班约束（无窗口可关联）
        self.assertEqual(len(mt.load_windows(self.job)), 1)

    def test_overlapping_windows_merged_for_accounting(self):
        self._add("M1", "2026-11-21 08:00", "2026-11-21 09:00")
        # 与上一个窗口重叠 -> 合并成 [08:00, 10:30)
        self._add("M1", "2026-11-21 09:30", "2026-11-21 10:30")
        with self.assertRaises(mt.GapConflict) as ctx:
            self._add("M2", "2026-11-21 10:00", "2026-11-21 11:00")
        # 断档时段 = 合并占用 [08:00,10:30) ∩ M2 [10:00,11:00) = [10:00,10:30)
        self.assertEqual(ctx.exception.gap_start, _dt("2026-11-21 10:00"))
        self.assertEqual(ctx.exception.gap_end, _dt("2026-11-21 10:30"))

    def test_invalid_inputs(self):
        with self.assertRaises(mt.MaintenanceError):
            self._add("M1", "2026-11-21 10:00", "2026-11-21 08:00")
        with self.assertRaises(mt.MaintenanceError):
            self._add("M1", "2026-11-21 08:00", "2026-11-21 08:00")
        with self.assertRaises(mt.MaintenanceError):
            self._add("GHOST", "2026-11-21 08:00", "2026-11-21 10:00")
        with self.assertRaises(mt.MaintenanceError):
            self._add("", "2026-11-21 08:00", "2026-11-21 10:00")
        self.assertEqual(mt.load_windows(self.job), [])


class RescheduleTests(MaintenanceTestBase):
    def test_reschedule_replaces_old_window(self):
        w1 = self._add("M1", "2026-11-21 08:00", "2026-11-21 10:00")
        self._add("M2", "2026-11-21 10:00", "2026-11-21 12:00")  # 首尾相接，合法
        # 改期 W1 到与 W2 完全重叠 -> 断档，拒绝且原窗口保留
        with self.assertRaises(mt.GapConflict) as ctx:
            mt.reschedule_window(self.job, w1.id,
                                 _dt("2026-11-21 10:00"),
                                 _dt("2026-11-21 12:00"))
        self.assertEqual(ctx.exception.digest, "d1")
        self.assertEqual(ctx.exception.gap_start, _dt("2026-11-21 10:00"))
        self.assertEqual(ctx.exception.gap_end, _dt("2026-11-21 12:00"))
        loaded = {w.id: w for w in mt.load_windows(self.job)}
        self.assertEqual(loaded[w1.id].start, _dt("2026-11-21 08:00"))
        self.assertEqual(loaded[w1.id].end, _dt("2026-11-21 10:00"))

        # 改期 W1 到 [12,14)：与 W2 首尾相接（半开）-> 通过
        mt.reschedule_window(self.job, w1.id,
                             _dt("2026-11-21 12:00"), _dt("2026-11-21 14:00"))
        # 旧时段 [08,10) 已释放：M2 现在可登记该时段——证明核算时旧窗口已被替换
        self._add("M2", "2026-11-21 08:00", "2026-11-21 10:00")
        self.assertEqual(len(mt.load_windows(self.job)), 3)

    def test_reschedule_keeps_machine_and_id(self):
        w1 = self._add("M1", "2026-11-21 08:00", "2026-11-21 09:00")
        w2 = mt.reschedule_window(self.job, w1.id,
                                  _dt("2026-11-21 12:00"),
                                  _dt("2026-11-21 13:00"))
        self.assertEqual(w2.id, w1.id)
        self.assertEqual(w2.machine, "M1")

    def test_reschedule_missing_window(self):
        with self.assertRaises(mt.WindowNotFound):
            mt.reschedule_window(self.job, "no-such-id",
                                 _dt("2026-11-21 08:00"),
                                 _dt("2026-11-21 09:00"))


class ConcurrencyTests(MaintenanceTestBase):
    def test_concurrent_registration_uses_committed_state(self):
        """两个单独都合法、合并会断档的登记并发提交：恰好一个成功。"""
        barrier = threading.Barrier(2)
        results = []

        def register(machine, start, end):
            barrier.wait()
            try:
                mt.add_window(self.job, machine, _dt(start), _dt(end))
                results.append("ok")
            except mt.GapConflict:
                results.append("conflict")

        # 钩子在锁内、落盘前睡眠：若校验不在锁内，两个线程都会通过校验
        mt.set_pre_commit_hook(lambda: time.sleep(0.3))
        t1 = threading.Thread(target=register, args=(
            "M1", "2026-11-21 08:00", "2026-11-21 12:00"))
        t2 = threading.Thread(target=register, args=(
            "M2", "2026-11-21 10:00", "2026-11-21 14:00"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(sorted(results), ["conflict", "ok"])
        # 磁盘上只落了一个窗口，已生效排班永不断档
        windows = mt.load_windows(self.job)
        self.assertEqual(len(windows), 1)
        self.assertIsNone(mt.find_gap(mt.read_task_needs(self.job),
                                      mt.read_layer_machines(self.job),
                                      windows))


if __name__ == "__main__":
    unittest.main(verbosity=2)
