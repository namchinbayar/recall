"""Take one snapshot of this machine's vitals."""

from __future__ import annotations

import os
import socket
from datetime import datetime, timezone

import psutil


def _cpu_temp() -> float | None:
    try:
        temps = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        return None
    # Prefer the CPU package sensor; fall back to the first reading of any chip.
    for key in ("k10temp", "coretemp", "zenpower", "cpu_thermal"):
        if key in temps and temps[key]:
            for entry in temps[key]:
                if entry.label in ("Tctl", "Tdie", "Package id 0", ""):
                    return float(entry.current)
            return float(temps[key][0].current)
    for entries in temps.values():
        if entries:
            return float(entries[0].current)
    return None


def _disks() -> dict[str, float]:
    out: dict[str, float] = {}
    seen_devices: set[str] = set()
    # Shortest mountpoint first so a btrfs root subvolume wins over its siblings.
    for part in sorted(psutil.disk_partitions(all=False), key=lambda p: len(p.mountpoint)):
        if part.fstype in ("squashfs", "overlay") or part.mountpoint.startswith("/boot"):
            continue
        if part.device in seen_devices:
            continue
        seen_devices.add(part.device)
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue
        # Mongo field names cannot contain '.'; '/' is fine but ugly in Charts.
        key = "root" if part.mountpoint == "/" else part.mountpoint.strip("/").replace("/", "_")
        out[key] = round(usage.percent, 1)
    return out


def take(host: str | None = None) -> dict:
    load1, load5, load15 = os.getloadavg()
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    net = psutil.net_io_counters()
    return {
        "ts": datetime.now(timezone.utc),
        "meta": {"host": host or socket.gethostname(), "kind": "system"},
        "cpu_pct": psutil.cpu_percent(interval=1.0),
        "load1": round(load1, 2),
        "load5": round(load5, 2),
        "load15": round(load15, 2),
        "mem_pct": vm.percent,
        "mem_used_gb": round(vm.used / 1e9, 2),
        "swap_pct": swap.percent,
        "disk_pct": _disks(),
        "cpu_temp": _cpu_temp(),
        "net_rx_gb": round(net.bytes_recv / 1e9, 3),
        "net_tx_gb": round(net.bytes_sent / 1e9, 3),
        "procs": len(psutil.pids()),
        "uptime_h": round((datetime.now(timezone.utc).timestamp() - psutil.boot_time()) / 3600, 1),
    }
