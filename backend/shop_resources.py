"""Read-only samples of tenant-owned supervised Worker processes.

No process discovery by arbitrary PID, no inventory/file-tree scans, no Worker
startup, and no historical metrics database. CPU is percent of one logical core.
"""
from __future__ import annotations

import math
import os
import threading
import time
from pathlib import Path


class SampleUnavailable(ValueError):
    pass


class ProcReader:
    def __init__(self, root="/proc"):
        self.root = Path(root)

    @staticmethod
    def _text(path):
        with path.open(encoding="utf-8") as source:
            value = source.read(16385)
        if len(value) > 16384:
            raise SampleUnavailable("invalid_sample")
        return value

    def _stat(self, pid):
        if type(pid) is not int or pid <= 0:
            raise SampleUnavailable("invalid_identity")
        raw = self._text(self.root / str(pid) / "stat")
        closing = raw.rfind(")")
        if closing < 0 or raw.split(" ", 1)[0] != str(pid):
            raise SampleUnavailable("invalid_sample")
        values = raw[closing + 2:].split()
        if len(values) < 22:
            raise SampleUnavailable("invalid_sample")
        result = {"cpu_ticks": int(values[11]) + int(values[12]), "start_ticks": int(values[19]),
                  "vms_bytes": int(values[20]), "rss_pages": int(values[21])}
        if any(value < 0 for value in result.values()):
            raise SampleUnavailable("invalid_sample")
        return result

    def identity(self, pid):
        return str(self._stat(pid)["start_ticks"])

    def read(self, pid):
        value = self._stat(pid)
        hz, page_size = os.sysconf("SC_CLK_TCK"), os.sysconf("SC_PAGE_SIZE")
        uptime = float(self._text(self.root / "uptime").split()[0])
        if hz <= 0 or page_size <= 0 or not math.isfinite(uptime):
            raise SampleUnavailable("invalid_sample")
        limit = None
        for line in self._text(self.root / str(pid) / "limits").splitlines():
            if line.startswith("Max address space"):
                token = line[len("Max address space"):].split()[0]
                limit = None if token == "unlimited" else int(token)
                break
        else:
            raise SampleUnavailable("limit_unavailable")
        return {"identity": str(value["start_ticks"]), "cpu_seconds": value["cpu_ticks"] / hz,
                "rss_bytes": value["rss_pages"] * page_size, "vms_bytes": value["vms_bytes"],
                "uptime_seconds": max(0.0, uptime - value["start_ticks"] / hz),
                "memory_limit_bytes": limit}


class ShopResources:
    def __init__(self, db, settings, process_snapshot, identity_validator, *, reader=None,
                 clock=time.monotonic, wall_clock=time.time, interval=5.0):
        self.db, self.settings = db, settings
        self.process_snapshot, self.identity_validator = process_snapshot, identity_validator
        self.reader = reader or ProcReader()
        self.clock, self.wall_clock = clock, wall_clock
        self.interval = max(1.0, float(interval))
        self._cache = {}
        self._lock = threading.RLock()

    @staticmethod
    def _identity(snapshot):
        return (snapshot.get("pid"), snapshot.get("generation"), snapshot.get("identity_token"))

    def _current(self, uid, account):
        current = self.db.get_shop_account(uid, account_id=account["id"])
        return (current is not None and current["account_key"] == account["account_key"]
                and current["generation"] == account["generation"])

    def _sample(self, uid, account, configured_limit):
        key = (uid, int(account["id"]))
        result = {"account_id": int(account["id"]), "key": account["account_key"],
            "name": account["display_name"], "enabled": bool(account["enabled"]),
            "worker_state": "unknown", "mode": None, "metrics_state": "unavailable",
            "cpu_percent": None, "rss_bytes": None, "vms_bytes": None, "uptime_seconds": None,
            "memory_limit_bytes": None, "configured_memory_limit_bytes": configured_limit,
            "pending_restart": None, "sampled_at": None, "message": "暂时无法采样"}
        try:
            first = self.process_snapshot(uid, account["account_key"])
            result["worker_state"] = first.get("state", "unknown")
            result["mode"] = first.get("mode") if first.get("mode") in {"rules", "rules_ai"} else None
            pid = first.get("pid")
            if pid is None:
                self._cache.pop(key, None)
                # An unregistered durable PID can be an orphan awaiting fenced
                # adoption. Do not read it or pretend its consumption is zero.
                if account["runtime_pid"] and first.get("state") == "stopped":
                    result.update(worker_state="unknown", message="进程归属尚未确认")
                    return result
                second = self.process_snapshot(uid, account["account_key"])
                if self._identity(second) != self._identity(first) or second.get("state") != first.get("state") or not self._current(uid, account):
                    raise SampleUnavailable("changed")
                result.update(metrics_state="stopped" if first.get("state") == "stopped" else "sampling",
                    cpu_percent=0, rss_bytes=0, vms_bytes=0, uptime_seconds=0,
                    pending_restart=False, sampled_at=self.wall_clock(), message="未运行" if first.get("state") == "stopped" else "正在切换运行状态")
                if first.get("state") == "stopped":
                    state = account["runtime_state"]
                    result["worker_state"] = "disabled" if not account["enabled"] else state if state in {"waiting_login", "capacity_limited", "degraded"} else "stopped"
                return result
            if type(pid) is not int or pid <= 0 or not self.identity_validator(uid, pid, account["account_key"]):
                raise SampleUnavailable("invalid_identity")
            now = self.clock()
            fingerprint = (*self._identity(first), int(account["generation"]), self.reader.identity(pid))
            cached = self._cache.get(key)
            if cached and cached["fingerprint"] == fingerprint and 0 <= now - cached["at"] < self.interval:
                second = self.process_snapshot(uid, account["account_key"])
                if self._identity(second) != self._identity(first) or not self._current(uid, account):
                    raise SampleUnavailable("changed")
                result.update(cached["metrics"])
                result["pending_restart"] = (result["memory_limit_bytes"] != configured_limit)
                return result
            point = self.reader.read(pid)
            second = self.process_snapshot(uid, account["account_key"])
            if (self._identity(second) != self._identity(first) or point["identity"] != fingerprint[-1]
                    or self.reader.identity(pid) != fingerprint[-1] or not self._current(uid, account)
                    or not self.identity_validator(uid, pid, account["account_key"])):
                raise SampleUnavailable("changed")
            for field in ("cpu_seconds", "rss_bytes", "vms_bytes", "uptime_seconds"):
                if not isinstance(point[field], (int, float)) or isinstance(point[field], bool) or not math.isfinite(point[field]) or point[field] < 0:
                    raise SampleUnavailable("invalid_sample")
            limit = point["memory_limit_bytes"]
            if limit is not None and (type(limit) is not int or limit < 0):
                raise SampleUnavailable("invalid_sample")
            cpu = None
            if cached and cached["fingerprint"] == fingerprint and self.interval <= now - cached["at"] <= 60:
                delta = point["cpu_seconds"] - cached["cpu_seconds"]
                if delta >= 0:
                    cpu = round(100 * delta / (now - cached["at"]), 2)
                    if not math.isfinite(cpu):
                        raise SampleUnavailable("invalid_sample")
            metrics = {"metrics_state": "ready" if cpu is not None else "sampling", "cpu_percent": cpu,
                "rss_bytes": point["rss_bytes"], "vms_bytes": point["vms_bytes"],
                "uptime_seconds": round(point["uptime_seconds"], 1), "memory_limit_bytes": limit,
                "sampled_at": self.wall_clock(), "message": "" if cpu is not None else "CPU采样中"}
            self._cache[key] = {"fingerprint": fingerprint, "at": now,
                "cpu_seconds": point["cpu_seconds"], "metrics": metrics}
            result.update(metrics, pending_restart=limit != configured_limit)
        except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError) as error:
            self._cache.pop(key, None)
            if isinstance(error, SampleUnavailable) and str(error) in {"invalid_identity", "changed"}:
                result["worker_state"] = "unknown"
        return result

    def page(self, uid, cursor=0, limit=50):
        policy = self.settings.read()
        rows, next_cursor, total = self.db.resource_accounts_page(uid, cursor, limit)
        with self._lock:
            now = self.clock()
            self._cache = {key: value for key, value in self._cache.items() if 0 <= now - value["at"] <= 60}
            accounts = [self._sample(uid, row, policy["worker_memory_mib"] * 1024 * 1024) for row in rows]
            while len(self._cache) > 1000:
                oldest = min(self._cache, key=lambda key: self._cache[key]["at"])
                del self._cache[oldest]
        return {"scope": "own_shops", "accounts": accounts, "next_cursor": next_cursor,
            "total": total, "limits": policy, "sample_interval_seconds": self.interval,
            "stale_after_seconds": 15, "sampled_at": self.wall_clock(),
            "cpu_basis": "one_core", "memory_limit_kind": "address_space"}
