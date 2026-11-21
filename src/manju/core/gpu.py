# -*- coding: utf-8 -*-
"""GPU 监控：显存占用、利用率、算力消耗。

数据来源：
    1. nvidia-smi（NVIDIA 显卡）— 最准确
    2. pynvml（如果装了）
    3. WMI（Windows，只有粗略信息）

算力消耗（单任务）：
    GPU 秒 = 任务运行时长 × GPU 平均利用率
    用来回答"这一集用了多少卡时"

用法：
    from manju.core.gpu import gpu_monitor

    # 采样一次
    snap = gpu_monitor.snapshot()
    # {"available": True, "gpus": [{"name": ..., "mem_used_mb": ..., "util_pct": ...}]}

    # 任务级算力统计
    gpu_monitor.start_task("job123")
    ... 跑任务 ...
    usage = gpu_monitor.stop_task("job123")
    # {"gpu_seconds": 1234.5, "avg_util": 78.3, "peak_mem_mb": 5120}
"""
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


def _run(cmd, timeout=5):
    """执行命令，返回 stdout。失败返回空字符串。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, encoding='utf-8', errors='replace')
        if r.returncode == 0:
            return r.stdout or ""
    except Exception:  # noqa: BLE001
        pass
    return ""


@dataclass
class GpuInfo:
    index: int = 0
    name: str = ""
    mem_total_mb: float = 0.0
    mem_used_mb: float = 0.0
    mem_free_mb: float = 0.0
    util_pct: float = 0.0
    temp_c: float = 0.0
    power_w: float = 0.0
    processes: List[Dict] = field(default_factory=list)

    def to_dict(self):
        return {
            "index": self.index,
            "name": self.name,
            "mem_total_mb": round(self.mem_total_mb, 1),
            "mem_used_mb": round(self.mem_used_mb, 1),
            "mem_free_mb": round(self.mem_free_mb, 1),
            "mem_pct": round(self.mem_used_mb / self.mem_total_mb * 100, 1)
                       if self.mem_total_mb else 0.0,
            "util_pct": round(self.util_pct, 1),
            "temp_c": round(self.temp_c, 1),
            "power_w": round(self.power_w, 1),
            "processes": self.processes,
        }


class GpuMonitor:
    """GPU 采样 + 任务级算力统计。"""

    def __init__(self, sample_interval=2.0):
        self.sample_interval = sample_interval
        self._lock = threading.RLock()
        self._tasks: Dict[str, Dict] = {}
        self._samples: Dict[str, List[Dict]] = {}
        self._monitor_thread = None
        self._stop_flag = threading.Event()
        self._nvidia_smi = shutil.which("nvidia-smi") or "nvidia-smi"
        self._available = None      # 缓存可用性

    # ---------------- 采样 ----------------
    def available(self):
        """GPU 监控是否可用（缓存结果）。"""
        if self._available is not None:
            return self._available
        out = _run([self._nvidia_smi, "--query-gpu=name", "--format=csv,noheader"])
        self._available = bool(out.strip())
        return self._available

    def snapshot(self):
        """采样一次所有 GPU。"""
        if not self.available():
            return {"available": False, "gpus": [], "ts": time.time()}

        gpus = []
        # 1. 基础信息
        base = _run([
            self._nvidia_smi,
            "--query-gpu=index,name,memory.total,memory.used,memory.free,"
            "utilization.gpu,temperature.gpu,power.draw",
            "--format=csv,noheader,nounits",
        ])
        for line in base.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 8:
                continue
            try:
                g = GpuInfo(
                    index=int(parts[0]),
                    name=parts[1],
                    mem_total_mb=float(parts[2]),
                    mem_used_mb=float(parts[3]),
                    mem_free_mb=float(parts[4]),
                    util_pct=float(parts[5]),
                    temp_c=float(parts[6]),
                    power_w=float(parts[7]) if parts[7] not in ("N/A", "[N/A]") else 0.0,
                )
                gpus.append(g)
            except (ValueError, IndexError):
                continue

        # 2. 进程信息
        procs = _run([
            self._nvidia_smi,
            "--query-compute-apps=gpu_uuid,pid,process_name,used_memory",
            "--format=csv,noheader,nounits",
        ])
        proc_map = {}
        for line in procs.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 4:
                continue
            proc_map.setdefault(parts[0], []).append({
                "pid": parts[1],
                "name": parts[2],
                "mem_mb": float(parts[3]) if parts[3].replace('.', '').isdigit() else 0,
            })

        # 3. UUID 映射（用 index 查 uuid）
        uuid_out = _run([
            self._nvidia_smi, "--query-gpu=index,uuid", "--format=csv,noheader"])
        idx_uuid = {}
        for line in uuid_out.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) == 2:
                idx_uuid[int(parts[0])] = parts[1]

        for g in gpus:
            g.processes = proc_map.get(idx_uuid.get(g.index, ""), [])

        return {
            "available": True,
            "gpus": [g.to_dict() for g in gpus],
            "ts": time.time(),
            "summary": self._summary(gpus),
        }

    def _summary(self, gpus):
        if not gpus:
            return {}
        total_mem = sum(g.mem_total_mb for g in gpus)
        used_mem = sum(g.mem_used_mb for g in gpus)
        avg_util = sum(g.util_pct for g in gpus) / len(gpus)
        return {
            "gpu_count": len(gpus),
            "mem_total_mb": round(total_mem, 1),
            "mem_used_mb": round(used_mem, 1),
            "mem_pct": round(used_mem / total_mem * 100, 1) if total_mem else 0.0,
            "avg_util_pct": round(avg_util, 1),
            "peak_temp_c": round(max(g.temp_c for g in gpus), 1),
            "power_w": round(sum(g.power_w for g in gpus), 1),
        }

    # ---------------- 任务级算力统计 ----------------
    def start_task(self, task_id):
        """开始记录某任务的 GPU 消耗。"""
        with self._lock:
            self._tasks[task_id] = {
                "started_ts": time.time(),
                "samples": [],
                "peak_mem_mb": 0.0,
                "peak_util": 0.0,
                "sum_util": 0.0,
                "n_samples": 0,
            }

    def sample_task(self, task_id):
        """给正在跑的任务采一次样（会被后台线程调用）。"""
        if not self.available():
            return
        snap = self.snapshot()
        if not snap.get("available"):
            return
        s = snap.get("summary") or {}
        util = s.get("avg_util_pct", 0.0)
        mem = s.get("mem_used_mb", 0.0)

        with self._lock:
            t = self._tasks.get(task_id)
            if not t:
                return
            t["samples"].append({"ts": time.time(), "util": util, "mem": mem})
            if len(t["samples"]) > 2000:
                t["samples"] = t["samples"][-2000:]
            t["peak_mem_mb"] = max(t["peak_mem_mb"], mem)
            t["peak_util"] = max(t["peak_util"], util)
            t["sum_util"] += util
            t["n_samples"] += 1

    def stop_task(self, task_id):
        """结束记录，返回算力统计。"""
        with self._lock:
            t = self._tasks.pop(task_id, None)
        if not t:
            return {"available": False}
        elapsed = time.time() - t["started_ts"]
        avg_util = (t["sum_util"] / t["n_samples"]) if t["n_samples"] else 0.0
        # GPU 秒 = 墙上时间 × (平均利用率/100)
        gpu_seconds = elapsed * (avg_util / 100.0)
        return {
            "available": True,
            "wall_seconds": round(elapsed, 1),
            "gpu_seconds": round(gpu_seconds, 1),
            "avg_util_pct": round(avg_util, 1),
            "peak_util_pct": round(t["peak_util"], 1),
            "peak_mem_mb": round(t["peak_mem_mb"], 1),
            "samples": t["n_samples"],
        }

    def task_usage(self, task_id):
        """查询正在跑的任务的实时算力消耗。"""
        with self._lock:
            t = self._tasks.get(task_id)
        if not t:
            return None
        elapsed = time.time() - t["started_ts"]
        avg_util = (t["sum_util"] / t["n_samples"]) if t["n_samples"] else 0.0
        return {
            "wall_seconds": round(elapsed, 1),
            "gpu_seconds": round(elapsed * avg_util / 100.0, 1),
            "avg_util_pct": round(avg_util, 1),
            "peak_mem_mb": round(t["peak_mem_mb"], 1),
            "samples": t["n_samples"],
        }

    # ---------------- 后台采样 ----------------
    def start_background(self, interval=None):
        """启动后台采样线程（给正在跑的任务用）。"""
        if self._monitor_thread and self._monitor_thread.is_alive():
            return
        self._stop_flag.clear()
        iv = interval or self.sample_interval

        def loop():
            while not self._stop_flag.is_set():
                with self._lock:
                    ids = list(self._tasks.keys())
                for tid in ids:
                    try:
                        self.sample_task(tid)
                    except Exception:  # noqa: BLE001
                        pass
                self._stop_flag.wait(iv)

        self._monitor_thread = threading.Thread(
            target=loop, name="gpu-monitor", daemon=True)
        self._monitor_thread.start()

    def stop_background(self):
        self._stop_flag.set()


# 全局单例
gpu_monitor = GpuMonitor()
