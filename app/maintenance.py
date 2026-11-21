"""检修排班：在已完成配对作业上登记/改期构建机检修窗口。

语义与约束
- 窗口为 UTC 半开区间 [start, end)：含起点、不含终点；
- 同一台构建机的重叠（含首尾相接）窗口合并计算占用；
- 对"任务所需且已有缓存"的每个镜像层：任一时刻至少保留一台
  持有该层且未在检修的构建机，否则拒绝登记/改期并保留原排班；
- 校验与写入在同一把作业级锁内完成：并发登记/改期按写入时已生效
  （已落盘）的排班核准；改期用新窗口替换原窗口核算；
- 窗口持久化在作业目录 maintenance.json（临时文件 + fsync + 原子
  rename），服务重启后继续生效。

数据来源：直接流式读取该作业上传的 tasks.csv 与 machines.csv（缓存表），
不扫描配对清单 pairs.csv / results/。
"""

from __future__ import annotations

import csv
import json
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Set, Tuple

from join_engine import (MACHINE_SIDE, TASK_SIDE, _atomic_write_json,
                         _resolve_columns, read_csv_header)

MAINTENANCE_FILE = "maintenance.json"

Interval = Tuple[datetime, datetime]


class MaintenanceError(Exception):
    """可预期的排班错误（对应 HTTP 400）。"""


class WindowNotFound(MaintenanceError):
    """改期目标窗口不存在（对应 HTTP 404）。"""


class GapConflict(MaintenanceError):
    """登记/改期会造成断档（对应 HTTP 409），携带一个受影响样例。"""

    def __init__(self, task_id: str, digest: str,
                 gap_start: datetime, gap_end: datetime) -> None:
        self.task_id = task_id
        self.digest = digest
        self.gap_start = gap_start
        self.gap_end = gap_end
        super().__init__(
            f"会断档：任务 {task_id} 所需层摘要 {digest} 在 "
            f"{format_utc(gap_start)} ~ {format_utc(gap_end)} 期间，"
            f"所有缓存该层的构建机都在检修；已保留原排班。")


@dataclass
class Window:
    """一个检修窗口：某台构建机在 [start, end) 期间停机。"""

    id: str
    machine: str
    start: datetime
    end: datetime


# ---------------------------------------------------------------- 时间解析/格式化

def parse_utc(text: Optional[str]) -> datetime:
    """解析值班员输入的 UTC 时间。

    接受 "2026-11-21 08:00"、"2026-11-21T08:00:30"、尾部 Z 或 ±HH:MM 偏移；
    不带偏移一律按 UTC 解释，返回值统一为 UTC 感知时间。
    """
    raw = (text or "").strip()
    if not raw:
        raise MaintenanceError("起止时间不能为空（UTC）。")
    norm = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        dt = datetime.fromisoformat(norm)
    except ValueError:
        raise MaintenanceError(
            f"无法解析时间：{raw!r}，示例格式 2026-11-21 08:00（UTC）。")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _trim_seconds(dt: datetime) -> str:
    out = dt.strftime("%Y-%m-%d %H:%M")
    if dt.second or dt.microsecond:
        out += dt.strftime(":%S")
    return out


def format_utc(dt: datetime) -> str:
    """展示用：统一 UTC，秒为 0 时省略。"""
    return _trim_seconds(dt.astimezone(timezone.utc)) + " UTC"


def format_input(dt: datetime) -> str:
    """表单回填用（不带后缀，可直接被 parse_utc 再解析）。"""
    return _trim_seconds(dt.astimezone(timezone.utc))


# ---------------------------------------------------------------- 区间运算

def merge_intervals(intervals: List[Interval]) -> List[Interval]:
    """合并重叠或首尾相接的半开区间（同一台机器的占用并集）。"""
    merged: List[List[datetime]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def _intersect_intervals(a: List[Interval], b: List[Interval]) -> List[Interval]:
    """两组已排序、各自不相交的半开区间求交集。"""
    i = j = 0
    out: List[Interval] = []
    while i < len(a) and j < len(b):
        start = max(a[i][0], b[j][0])
        end = min(a[i][1], b[j][1])
        if start < end:
            out.append((start, end))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def find_gap(task_needs: Dict[str, str],
             layer_machines: Dict[str, Set[str]],
             windows: List[Window]
             ) -> Optional[Tuple[str, str, datetime, datetime]]:
    """检查排班是否造成断档，返回一个受影响样例或 None。

    task_needs:     层摘要 -> 需要它的任务号（报表用，取首个）
    layer_machines: 层摘要 -> 持有该层（已有缓存）的构建机集合
    只约束"任务所需且已有缓存"的层：某层所有缓存机同一时刻都在检修，
    交集非空即断档，返回 (任务号, 层摘要, 断档起点, 断档终点)。
    """
    by_machine: Dict[str, List[Interval]] = {}
    for w in windows:
        by_machine.setdefault(w.machine, []).append((w.start, w.end))
    merged = {m: merge_intervals(iv) for m, iv in by_machine.items()}

    for digest in sorted(task_needs):
        holders = layer_machines.get(digest)
        if not holders:
            continue  # 任务需要但无任何缓存：不在排班约束范围
        common: Optional[List[Interval]] = None
        for machine in sorted(holders):
            iv = merged.get(machine, [])
            common = iv if common is None else _intersect_intervals(common, iv)
            if not common:
                break  # 存在全程可用的缓存机，该层不会断档
        if common:
            gap_start, gap_end = common[0]
            return task_needs[digest], digest, gap_start, gap_end
    return None


# ---------------------------------------------------------------- 读取作业上传的两张表

def read_task_needs(job_dir: str) -> Dict[str, str]:
    """流式读任务表：层摘要 -> 首个需要它的任务号（空摘要行跳过）。"""
    path = os.path.join(job_dir, "tasks.csv")
    digest_col, task_col = _resolve_columns(read_csv_header(path), TASK_SIDE)
    needs: Dict[str, str] = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            digest = (row.get(digest_col) or "").strip()
            if digest and digest not in needs:
                needs[digest] = (row.get(task_col) or "").strip()
    return needs


def read_layer_machines(job_dir: str) -> Dict[str, Set[str]]:
    """流式读缓存表：层摘要 -> 持有该层的构建机集合（空摘要行跳过）。"""
    path = os.path.join(job_dir, "machines.csv")
    digest_col, machine_col = _resolve_columns(read_csv_header(path),
                                               MACHINE_SIDE)
    holders: Dict[str, Set[str]] = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            digest = (row.get(digest_col) or "").strip()
            machine = (row.get(machine_col) or "").strip()
            if digest and machine:
                holders.setdefault(digest, set()).add(machine)
    return holders


def list_machines(job_dir: str) -> List[str]:
    """缓存表中出现过的全部构建机（下拉框用）。"""
    machines: Set[str] = set()
    for holders in read_layer_machines(job_dir).values():
        machines.update(holders)
    return sorted(machines)


# ---------------------------------------------------------------- 持久化（重启后仍生效）

def load_windows(job_dir: str) -> List[Window]:
    path = os.path.join(job_dir, MAINTENANCE_FILE)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        windows = []
        for item in data.get("windows", []):
            start = datetime.fromisoformat(item["start"])
            end = datetime.fromisoformat(item["end"])
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            windows.append(Window(id=item["id"], machine=item["machine"],
                                  start=start.astimezone(timezone.utc),
                                  end=end.astimezone(timezone.utc)))
        return windows
    except (KeyError, TypeError, ValueError) as exc:
        raise MaintenanceError(f"排班数据损坏：{MAINTENANCE_FILE}（{exc}）")


def save_windows(job_dir: str, windows: List[Window]) -> None:
    """原子写盘（临时文件 + fsync + rename），重启后排班仍在。"""
    payload = {"windows": [
        {"id": w.id, "machine": w.machine,
         "start": w.start.astimezone(timezone.utc).isoformat(),
         "end": w.end.astimezone(timezone.utc).isoformat()}
        for w in sorted(windows, key=lambda w: (w.machine, w.start, w.id))]}
    _atomic_write_json(os.path.join(job_dir, MAINTENANCE_FILE), payload)


# ---------------------------------------------------------------- 登记/改期（锁内核验）

_LOCKS_GUARD = threading.Lock()
_LOCKS: Dict[str, threading.Lock] = {}

# 仅自测注入：在作业锁内、落盘前调用，用于构造确定的并发交错
_PRE_COMMIT_HOOK: Optional[Callable[[], None]] = None


def set_pre_commit_hook(hook: Optional[Callable[[], None]]) -> None:
    global _PRE_COMMIT_HOOK
    _PRE_COMMIT_HOOK = hook


def _lock_for(job_dir: str) -> threading.Lock:
    key = os.path.abspath(job_dir)
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def _validate_interval(start: datetime, end: datetime) -> None:
    if start is None or end is None:
        raise MaintenanceError("起止时间不能为空（UTC）。")
    if start >= end:
        raise MaintenanceError(
            f"开始时间必须早于结束时间（{format_utc(start)} ~ {format_utc(end)}）。")


def _ensure_no_gap(job_dir: str, effective: List[Window]) -> None:
    """按给定生效排班核算，会断档则抛 GapConflict（不写盘）。"""
    task_needs = read_task_needs(job_dir)
    if not task_needs:
        return
    gap = find_gap(task_needs, read_layer_machines(job_dir), effective)
    if gap is not None:
        raise GapConflict(*gap)


def _pre_commit() -> None:
    if _PRE_COMMIT_HOOK is not None:
        _PRE_COMMIT_HOOK()


def add_window(job_dir: str, machine: Optional[str],
               start: datetime, end: datetime) -> Window:
    """登记窗口。校验与写盘同锁完成：按写入时已生效（已落盘）的排班核准。"""
    machine = (machine or "").strip()
    if not machine:
        raise MaintenanceError("请选择构建机。")
    _validate_interval(start, end)
    with _lock_for(job_dir):
        if machine not in list_machines(job_dir):
            raise MaintenanceError(f"构建机 {machine} 不在该作业的缓存表中。")
        windows = load_windows(job_dir)
        candidate = Window(id=uuid.uuid4().hex, machine=machine,
                           start=start, end=end)
        _ensure_no_gap(job_dir, windows + [candidate])
        _pre_commit()
        windows.append(candidate)
        save_windows(job_dir, windows)
        return candidate


def reschedule_window(job_dir: str, window_id: str,
                      start: datetime, end: datetime) -> Window:
    """改期：用新窗口替换原窗口核算，原窗口的其他属性（机器、id）不变。"""
    _validate_interval(start, end)
    with _lock_for(job_dir):
        windows = load_windows(job_dir)
        old = next((w for w in windows if w.id == window_id), None)
        if old is None:
            raise WindowNotFound(f"窗口不存在：{window_id}")
        candidate = Window(id=old.id, machine=old.machine,
                           start=start, end=end)
        effective = [w for w in windows if w.id != window_id] + [candidate]
        _ensure_no_gap(job_dir, effective)
        _pre_commit()
        save_windows(job_dir, effective)
        return candidate
