"""检修排班 Web 层自测：页面入口、登记/改期、断档 409、并发核准、重启保留。

运行（需 Flask）：
  PYTHONPATH=.pylibs:app python3 -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(REPO_ROOT, "app"),
           os.path.join(REPO_ROOT, ".pylibs")):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

import app as webapp  # noqa: E402
import maintenance as mt  # noqa: E402


def _csv_bytes(header: list, rows: list) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


class MaintenanceWebTests(unittest.TestCase):
    """任务 T1 需要 d1；M1、M2 都缓存 d1（单任一台检修不断档，同时检修必断档）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        webapp.JOBS_ROOT = os.path.join(self.tmp, "jobs")
        os.makedirs(webapp.JOBS_ROOT, exist_ok=True)
        webapp._recovered = False
        webapp._threads.clear()
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()
        mt.set_pre_commit_hook(None)
        self.job_id = self._make_done_job()
        self.jdir = os.path.join(webapp.JOBS_ROOT, self.job_id)

    def tearDown(self) -> None:
        mt.set_pre_commit_hook(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_done_job(self) -> str:
        data = {
            "tasks_csv": (io.BytesIO(_csv_bytes(
                ["任务号", "层摘要"], [("T1", "d1")])), "tasks.csv"),
            "machines_csv": (io.BytesIO(_csv_bytes(
                ["来源构建机", "层摘要"],
                [("M1", "d1"), ("M2", "d1")])), "machines.csv"),
        }
        resp = self.client.post("/upload", data=data,
                                content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 302)
        job_id = resp.headers["Location"].rstrip("/").split("/")[-1]
        deadline = time.time() + 30
        while time.time() < deadline:
            body = self.client.get(f"/jobs/{job_id}/status").get_json()
            if body["phase"] in ("done", "failed"):
                self.assertEqual(body["phase"], "done")
                return job_id
            time.sleep(0.05)
        self.fail("作业未在限定时间内完成")

    def _register(self, machine, start, end, client=None):
        return (client or self.client).post(
            f"/jobs/{self.job_id}/maintenance",
            data={"machine": machine, "start": start, "end": end})

    def _reschedule(self, window_id, start, end, client=None):
        return (client or self.client).post(
            f"/jobs/{self.job_id}/maintenance/{window_id}/reschedule",
            data={"start": start, "end": end})

    # ---------------------------------------------------------- 入口与基本流程

    def test_done_job_page_has_maintenance_entry(self):
        resp = self.client.get(f"/jobs/{self.job_id}")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("检修排班", html)
        self.assertIn(f"/jobs/{self.job_id}/maintenance", html)
        self.assertIn('value="M1"', html)
        self.assertIn('value="M2"', html)

    def test_maintenance_rejected_before_job_done(self):
        # 构造一个未完成作业：排班入口不应对其开放
        other = os.path.join(webapp.JOBS_ROOT, "unfinished")
        os.makedirs(other)
        with open(os.path.join(other, "state.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"phase": "joining"}, fh)
        resp = self.client.post("/jobs/unfinished/maintenance", data={
            "machine": "M1", "start": "2026-11-21 08:00",
            "end": "2026-11-21 10:00"})
        self.assertEqual(resp.status_code, 409)

    def test_register_then_survives_restart(self):
        resp = self._register("M1", "2026-11-21 08:00", "2026-11-21 10:00")
        self.assertEqual(resp.status_code, 302)
        # 落盘文件存在
        self.assertTrue(os.path.exists(
            os.path.join(self.jdir, mt.MAINTENANCE_FILE)))
        html = self.client.get(
            f"/jobs/{self.job_id}").get_data(as_text=True)
        self.assertIn("2026-11-21 08:00 UTC", html)
        self.assertIn("2026-11-21 10:00 UTC", html)

        # 模拟服务重启：清空进程内状态、换全新客户端，窗口仍在
        webapp._recovered = False
        webapp._threads.clear()
        fresh = webapp.app.test_client()
        html = fresh.get(f"/jobs/{self.job_id}").get_data(as_text=True)
        self.assertIn("2026-11-21 08:00 UTC", html)
        self.assertEqual(len(mt.load_windows(self.jdir)), 1)

    def test_gap_rejected_with_details_and_schedule_kept(self):
        self.assertEqual(
            self._register("M1", "2026-11-21 08:00",
                           "2026-11-21 10:00").status_code, 302)
        # M2 同时段检修 -> 两台缓存机都停机，断档
        resp = self._register("M2", "2026-11-21 09:00", "2026-11-21 11:00")
        self.assertEqual(resp.status_code, 409)
        html = resp.get_data(as_text=True)
        self.assertIn("T1", html)                     # 受影响任务号
        self.assertIn("d1", html)                     # 层摘要
        self.assertIn("2026-11-21 09:00 UTC", html)   # 断档起点
        self.assertIn("2026-11-21 10:00 UTC", html)   # 断档终点
        # 原排班保留：仍只有 M1 的窗口
        windows = mt.load_windows(self.jdir)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].machine, "M1")

    def test_bad_input_rejected(self):
        resp = self._register("M1", "2026-11-21 10:00", "2026-11-21 08:00")
        self.assertEqual(resp.status_code, 400)
        resp = self._register("M1", "garbage", "2026-11-21 08:00")
        self.assertEqual(resp.status_code, 400)
        resp = self._register("GHOST", "2026-11-21 08:00", "2026-11-21 09:00")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(mt.load_windows(self.jdir), [])

    def test_reschedule_flow(self):
        self._register("M1", "2026-11-21 08:00", "2026-11-21 10:00")
        self._register("M2", "2026-11-21 20:00", "2026-11-21 22:00")
        w1, w2 = mt.load_windows(self.jdir)

        # 改期 W1 到 [12,16)：合法
        resp = self._reschedule(w1.id, "2026-11-21 12:00", "2026-11-21 16:00")
        self.assertEqual(resp.status_code, 302)
        moved = {w.id: w for w in mt.load_windows(self.jdir)}[w1.id]
        self.assertEqual(moved.start, mt.parse_utc("2026-11-21 12:00"))

        # 改期 W2 到 [14,18)：与 W1 [12,16) 在 [14,16) 同时停机 -> 409，原窗口保留
        resp = self._reschedule(w2.id, "2026-11-21 14:00", "2026-11-21 18:00")
        self.assertEqual(resp.status_code, 409)
        html = resp.get_data(as_text=True)
        self.assertIn("T1", html)
        self.assertIn("2026-11-21 14:00 UTC", html)
        self.assertIn("2026-11-21 16:00 UTC", html)
        kept = {w.id: w for w in mt.load_windows(self.jdir)}[w2.id]
        self.assertEqual(kept.start, mt.parse_utc("2026-11-21 20:00"))

        # 不存在的窗口 -> 404
        self.assertEqual(
            self._reschedule("no-such", "2026-11-21 00:00",
                             "2026-11-21 01:00").status_code, 404)

    # ---------------------------------------------------------- 并发核准

    def test_concurrent_registration_approved_at_write_time(self):
        """M1[08,12) 与 M2[10,14) 单独都合法、合并断档：并发提交恰好一成一败。"""
        barrier = threading.Barrier(2)
        outcomes = []

        def register(machine, start, end):
            client = webapp.app.test_client()
            barrier.wait()
            resp = self._register(machine, start, end, client=client)
            outcomes.append(resp.status_code)

        # 锁内、落盘前睡眠：校验若不在锁内，两个请求都会通过
        mt.set_pre_commit_hook(lambda: time.sleep(0.3))
        t1 = threading.Thread(target=register,
                              args=("M1", "2026-11-21 08:00", "2026-11-21 12:00"))
        t2 = threading.Thread(target=register,
                              args=("M2", "2026-11-21 10:00", "2026-11-21 14:00"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(sorted(outcomes), [302, 409])
        windows = mt.load_windows(self.jdir)
        self.assertEqual(len(windows), 1)
        # 已生效排班永不断档
        self.assertIsNone(mt.find_gap(mt.read_task_needs(self.jdir),
                                      mt.read_layer_machines(self.jdir),
                                      windows))

    def test_concurrent_reschedule_approved_at_write_time(self):
        """两个改期单独都合法、合并断档：并发提交恰好一成一败，败者保留原窗口。"""
        self._register("M1", "2026-11-21 08:00", "2026-11-21 10:00")
        self._register("M2", "2026-11-21 20:00", "2026-11-21 22:00")
        w1, w2 = mt.load_windows(self.jdir)
        barrier = threading.Barrier(2)
        outcomes = []

        def reschedule(window_id, start, end):
            client = webapp.app.test_client()
            barrier.wait()
            resp = self._reschedule(window_id, start, end, client=client)
            outcomes.append(resp.status_code)

        mt.set_pre_commit_hook(lambda: time.sleep(0.3))
        # W1 -> [12,16) 与 W2 -> [14,18)：各自相对对方原窗口合法，合并后 [14,16) 断档
        t1 = threading.Thread(target=reschedule,
                              args=(w1.id, "2026-11-21 12:00", "2026-11-21 16:00"))
        t2 = threading.Thread(target=reschedule,
                              args=(w2.id, "2026-11-21 14:00", "2026-11-21 18:00"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(sorted(outcomes), [302, 409])
        final = {w.id: w for w in mt.load_windows(self.jdir)}
        # 恰好一个被改期，另一个保留原窗口
        moved = sum(1 for w, orig in ((final[w1.id], w1), (final[w2.id], w2))
                    if w.start != orig.start)
        self.assertEqual(moved, 1)
        self.assertIsNone(mt.find_gap(mt.read_task_needs(self.jdir),
                                      mt.read_layer_machines(self.jdir),
                                      list(final.values())))


if __name__ == "__main__":
    unittest.main(verbosity=2)
