"""Collect a single, structured laptop status snapshot.

The collector intentionally has no background loop. It is called only by a
page refresh request and writes no state by itself.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import platform
import re
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, build_opener

import psutil


TZ = datetime.now().astimezone().tzinfo
SYS = Path("/sys")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _run(command: list[str], timeout: float = 3.0) -> str:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _read(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return default


def _number(path: Path) -> float | None:
    raw = _read(path)
    try:
        return float(raw)
    except ValueError:
        return None


def _bytes(value: float | int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(amount) < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{amount:.0f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TiB"


def _seconds(value: float) -> str:
    value = max(0, int(value))
    days, value = divmod(value, 86400)
    hours, value = divmod(value, 3600)
    minutes, _ = divmod(value, 60)
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours or days:
        parts.append(f"{hours}小时")
    parts.append(f"{minutes}分钟")
    return " ".join(parts)


def collect_system() -> dict[str, Any]:
    os_release: dict[str, str] = {}
    for line in _read(Path("/etc/os-release")).splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            os_release[key] = value.strip('"')
    process_count = 0
    thread_count = 0
    try:
        for proc in psutil.process_iter(["num_threads"]):
            process_count += 1
            thread_count += int(proc.info.get("num_threads") or 0)
    except (psutil.Error, OSError):
        pass
    return {
        "hostname": socket.gethostname(),
        "device": _read(Path("/sys/class/dmi/id/product_name"), "ThinkPad T470s"),
        "machine_type": _read(Path("/sys/class/dmi/id/product_version")),
        "os": os_release.get("PRETTY_NAME", platform.platform()),
        "kernel": platform.release(),
        "uptime_seconds": round(time.time() - psutil.boot_time()),
        "uptime_human": _seconds(time.time() - psutil.boot_time()),
        "load": [round(item, 2) for item in os.getloadavg()],
        "processes": process_count,
        "threads": thread_count,
    }


def _cpu_model() -> str:
    for line in _read(Path("/proc/cpuinfo")).splitlines():
        if line.lower().startswith("model name") and ":" in line:
            return re.sub(r"\s+", " ", line.split(":", 1)[1].strip())
    return platform.processor() or "Unknown CPU"


def collect_cpu() -> dict[str, Any]:
    usage = psutil.cpu_percent(interval=0.18, percpu=True)
    per_core_mhz: list[float] = []
    cpu_dirs = sorted((SYS / "devices/system/cpu").glob("cpu[0-9]*"))
    for cpu_dir in cpu_dirs:
        khz = _number(cpu_dir / "cpufreq/scaling_cur_freq")
        per_core_mhz.append(round(khz / 1000, 0) if khz is not None else 0)
    idle = round(100 - (sum(usage) / len(usage) if usage else 0), 1)
    policy = SYS / "devices/system/cpu/cpufreq/policy0"
    return {
        "model": _cpu_model(),
        "physical_cores": psutil.cpu_count(logical=False),
        "logical_cores": psutil.cpu_count(logical=True),
        "usage_percent": round(100 - idle, 1),
        "idle_percent": idle,
        "per_core_percent": [round(item, 1) for item in usage],
        "per_core_mhz": per_core_mhz,
        "governor": _read(policy / "scaling_governor", "unknown"),
        "epp": _read(policy / "energy_performance_preference", "unknown"),
        "power_profile": _run(["powerprofilesctl", "get"], 2) or "unknown",
    }


def collect_temperatures() -> list[dict[str, Any]]:
    temperatures: list[dict[str, Any]] = []
    for zone in sorted((SYS / "class/thermal").glob("thermal_zone*")):
        value = _number(zone / "temp")
        if value is None:
            continue
        celsius = value / 1000 if value > 300 else value
        temperatures.append(
            {
                "name": _read(zone / "type", zone.name),
                "celsius": round(celsius, 1),
            }
        )
    return temperatures


def collect_fan() -> dict[str, Any]:
    raw = _read(Path("/proc/acpi/ibm/fan"))
    result: dict[str, Any] = {"rpm": None, "level": "unknown", "status": "unknown"}
    for line in raw.splitlines():
        key, _, value = line.partition(":")
        value = value.strip()
        if key == "speed":
            try:
                result["rpm"] = int(value)
            except ValueError:
                pass
        elif key == "level":
            result["level"] = value
        elif key == "status":
            result["status"] = value
    return result


def collect_memory() -> dict[str, Any]:
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    return {
        "ram": {
            "total": memory.total,
            "total_human": _bytes(memory.total),
            "used": memory.used,
            "used_human": _bytes(memory.used),
            "available": memory.available,
            "available_human": _bytes(memory.available),
            "percent": memory.percent,
        },
        "swap": {
            "total": swap.total,
            "total_human": _bytes(swap.total),
            "used": swap.used,
            "used_human": _bytes(swap.used),
            "percent": swap.percent,
        },
    }


def collect_disks() -> list[dict[str, Any]]:
    disks: list[dict[str, Any]] = []
    seen: set[str] = set()
    allowed_fs = {"ext4", "xfs", "btrfs", "vfat", "ntfs", "exfat", "fuseblk"}
    for part in psutil.disk_partitions(all=False):
        if part.mountpoint in seen or part.fstype.lower() not in allowed_fs:
            continue
        seen.add(part.mountpoint)
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue
        disks.append(
            {
                "device": part.device,
                "mountpoint": part.mountpoint,
                "filesystem": part.fstype,
                "total": usage.total,
                "total_human": _bytes(usage.total),
                "used": usage.used,
                "used_human": _bytes(usage.used),
                "free": usage.free,
                "free_human": _bytes(usage.free),
                "percent": usage.percent,
            }
        )
    return disks


def _process_rows(sort_key: str) -> list[dict[str, Any]]:
    order = "-%cpu" if sort_key == "cpu" else "-rss"
    output = _run(
        ["ps", "-eo", "pid=,comm=,%cpu=,%mem=,rss=", f"--sort={order}"], 3
    )
    rows = []
    for line in output.splitlines()[:8]:
        parts = line.split(None, 4)
        if len(parts) != 5:
            continue
        pid, name, cpu, memory, rss = parts
        try:
            rows.append(
                {
                    "pid": int(pid),
                    "name": name,
                    "cpu": float(cpu),
                    "memory": float(memory),
                    "rss_human": _bytes(int(rss) * 1024),
                }
            )
        except ValueError:
            continue
    return rows


def collect_processes() -> dict[str, Any]:
    return {"cpu": _process_rows("cpu"), "memory": _process_rows("memory")}


def _external_connectivity() -> dict[str, Any]:
    started = time.monotonic()
    try:
        opener = build_opener()
        response = opener.open(
            Request("https://connectivitycheck.gstatic.com/generate_204", method="GET"),
            timeout=4,
        )
        return {
            "online": response.status in (200, 204),
            "status": response.status,
            "latency_ms": round((time.monotonic() - started) * 1000),
        }
    except Exception:
        return {"online": False, "status": None, "latency_ms": None}


def _listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.35):
            return True
    except OSError:
        return False


def collect_network() -> dict[str, Any]:
    interfaces = []
    stats = psutil.net_if_stats()
    for name, addresses in psutil.net_if_addrs().items():
        ipv4 = [item.address for item in addresses if item.family == socket.AF_INET]
        if not ipv4 or name == "lo":
            continue
        info = stats.get(name)
        interfaces.append(
            {
                "name": name,
                "ipv4": ipv4,
                "up": bool(info and info.isup),
                "speed_mbps": info.speed if info else 0,
            }
        )
    route = _run(["ip", "route", "show", "default"], 2)
    gateway = ""
    match = re.search(r"\bvia\s+(\S+)", route)
    if match:
        gateway = match.group(1)
    return {
        "interfaces": interfaces,
        "gateway": gateway,
        "connectivity": _external_connectivity(),
        "proxy": {"host": "127.0.0.1", "port": 10808, "listening": _listening("127.0.0.1", 10808)},
    }


def _energy_wh(base: Path, stem: str) -> float | None:
    energy = _number(base / stem)
    if energy is not None:
        return energy / 1_000_000
    charge = _number(base / stem.replace("energy", "charge"))
    voltage = _number(base / "voltage_now") or _number(base / "voltage_min_design")
    if charge is not None and voltage is not None:
        return charge * voltage / 1_000_000_000_000
    return None


def collect_batteries() -> dict[str, Any]:
    batteries = []
    for base in sorted((SYS / "class/power_supply").glob("BAT*")):
        design = _energy_wh(base, "energy_full_design")
        full = _energy_wh(base, "energy_full")
        now = _energy_wh(base, "energy_now")
        capacity = _number(base / "capacity")
        health = round(full / design * 100, 1) if full is not None and design else None
        batteries.append(
            {
                "name": base.name,
                "manufacturer": _read(base / "manufacturer"),
                "model": _read(base / "model_name"),
                "status": _read(base / "status", "unknown"),
                "percent": round(capacity, 1) if capacity is not None else None,
                "design_wh": round(design, 2) if design is not None else None,
                "full_wh": round(full, 2) if full is not None else None,
                "now_wh": round(now, 2) if now is not None else None,
                "health_percent": health,
            }
        )
    design_total = sum(item["design_wh"] or 0 for item in batteries)
    full_total = sum(item["full_wh"] or 0 for item in batteries)
    now_total = sum(item["now_wh"] or 0 for item in batteries)
    return {
        "items": batteries,
        "totals": {
            "design_wh": round(design_total, 2),
            "full_wh": round(full_total, 2),
            "now_wh": round(now_total, 2),
            "health_percent": round(full_total / design_total * 100, 1) if design_total else None,
        },
    }


def collect_cron() -> list[str]:
    output = _run(["crontab", "-l"], 3)
    return [line for line in output.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def _timestamp_from_microseconds(value: Any) -> str | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return datetime.fromtimestamp(value / 1_000_000, tz=TZ).isoformat(timespec="seconds")


def _timestamp_from_milliseconds(value: Any) -> str | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return datetime.fromtimestamp(value / 1_000, tz=TZ).isoformat(timespec="seconds")


def _cron_name(command: str) -> str:
    """Return a short user-facing label without exposing the full command line."""
    if "deploy-site.sh" in command:
        return "mitoto 新闻站部署"
    if "download_bing_wallpaper.sh" in command:
        return "下载 Bing 壁纸"
    match = re.search(r"(?:^|[ /])([\w.-]+\.(?:sh|py|js))(?:\s|$)", command)
    return match.group(1) if match else "用户 Cron 任务"


def collect_schedule() -> dict[str, Any]:
    """Collect user-owned schedules and summarize OS maintenance timers.

    The public dashboard intentionally avoids rendering raw command lines: a
    command can contain paths or credentials that do not belong in the UI.
    """
    items: list[dict[str, Any]] = []

    for line in collect_cron():
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        expression, command = " ".join(parts[:5]), parts[5]
        items.append(
            {
                "name": _cron_name(command),
                "source": "用户 Cron",
                "schedule": expression,
                "next_run": None,
                "status": "enabled",
            }
        )

    raw_user_timers = _run(
        ["systemctl", "--user", "list-timers", "--all", "--no-pager", "--output=json"], 5
    )
    try:
        user_timers = json.loads(raw_user_timers)
    except (json.JSONDecodeError, TypeError):
        user_timers = []
    if isinstance(user_timers, list):
        for timer in user_timers:
            if not isinstance(timer, dict) or not timer.get("unit"):
                continue
            items.append(
                {
                    "name": str(timer["unit"]).removesuffix(".timer"),
                    "source": "用户 Systemd",
                    "schedule": str(timer.get("activates") or "timer"),
                    "next_run": _timestamp_from_microseconds(timer.get("next")),
                    "status": "enabled" if timer.get("next") else "idle",
                }
            )

    raw_automations = _run(["openclaw", "cron", "list", "--json"], 10)
    try:
        automations = json.loads(raw_automations).get("jobs", [])
    except (json.JSONDecodeError, AttributeError, TypeError):
        automations = []
    if isinstance(automations, list):
        for job in automations:
            if not isinstance(job, dict):
                continue
            schedule = job.get("schedule", {})
            expression = schedule.get("expr") if isinstance(schedule, dict) else None
            items.append(
                {
                    "name": str(job.get("name") or "OpenClaw 自动任务"),
                    "source": "OpenClaw",
                    "schedule": str(expression or schedule.get("kind") or "自动调度")
                    if isinstance(schedule, dict)
                    else "自动调度",
                    "next_run": _timestamp_from_milliseconds(job.get("nextRunAtMs")),
                    "status": "enabled" if job.get("enabled", True) else "disabled",
                }
            )

    raw_system_timers = _run(
        ["systemctl", "list-timers", "--all", "--no-pager", "--output=json"], 5
    )
    try:
        system_timers = json.loads(raw_system_timers)
    except (json.JSONDecodeError, TypeError):
        system_timers = []

    return {
        "items": items,
        "user_count": len(items),
        "system_timer_count": len(system_timers) if isinstance(system_timers, list) else 0,
    }


def collect_model() -> dict[str, Any]:
    current = "unknown"
    provider = ""
    raw = _run(["openclaw", "sessions", "--active", "120", "--limit", "10", "--json"], 8)
    try:
        sessions = json.loads(raw).get("sessions", [])
        main_sessions = [item for item in sessions if item.get("agentId") == "main"]
        if main_sessions:
            latest = max(main_sessions, key=lambda item: item.get("updatedAt", 0))
            current = latest.get("model") or "unknown"
            provider = latest.get("modelProvider") or ""
    except (json.JSONDecodeError, AttributeError, TypeError):
        pass
    primary = "unknown"
    fallbacks: list[str] = []
    try:
        config = json.loads(_read(Path.home() / ".openclaw/openclaw.json", "{}"))
        model_config = config.get("agents", {}).get("defaults", {}).get("model", {})
        primary = model_config.get("primary") or "unknown"
        configured_fallbacks = model_config.get("fallbacks", [])
        if isinstance(configured_fallbacks, list):
            fallbacks = [str(item) for item in configured_fallbacks]
    except (json.JSONDecodeError, AttributeError, TypeError):
        pass
    return {
        "current": current,
        "provider": provider,
        "runtime": "OpenAI Codex" if current.startswith("gpt-") else "OpenClaw",
        "primary": primary,
        "fallbacks": fallbacks,
    }


def _summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    warnings = []
    temperatures = snapshot.get("temperatures", [])
    hottest = max((item["celsius"] for item in temperatures), default=0)
    if hottest >= 85:
        warnings.append("温度过高")
    elif hottest >= 75:
        warnings.append("温度偏高")
    memory_percent = snapshot.get("memory", {}).get("ram", {}).get("percent", 0)
    if memory_percent >= 90:
        warnings.append("内存压力较高")
    disk_percent = max((item["percent"] for item in snapshot.get("disks", [])), default=0)
    if disk_percent >= 90:
        warnings.append("磁盘空间紧张")
    health = snapshot.get("batteries", {}).get("totals", {}).get("health_percent")
    if health is not None and health < 60:
        warnings.append("电池综合健康度偏低")
    if not snapshot.get("network", {}).get("connectivity", {}).get("online"):
        warnings.append("外网连通性异常")
    return {
        "level": "warning" if warnings else "healthy",
        "title": "需要关注" if warnings else "运行良好",
        "message": "；".join(warnings) if warnings else "负载、温度、内存、磁盘和网络均处于正常状态",
    }


Collector = Callable[[], Any]


def collect_snapshot() -> dict[str, Any]:
    """Collect all independent sections concurrently into one snapshot."""
    started = time.monotonic()
    # Sample CPU before spawning heavier CLI collectors. Otherwise the status
    # command itself would make an idle laptop look busy.
    data: dict[str, Any] = {"cpu": collect_cpu()}
    tasks: dict[str, Collector] = {
        "system": collect_system,
        "temperatures": collect_temperatures,
        "fan": collect_fan,
        "memory": collect_memory,
        "disks": collect_disks,
        "processes": collect_processes,
        "network": collect_network,
        "batteries": collect_batteries,
        "schedule": collect_schedule,
        "model": collect_model,
    }
    errors: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fn): key for key, fn in tasks.items()}
        for future in concurrent.futures.as_completed(futures):
            key = futures[future]
            try:
                data[key] = future.result()
            except Exception as exc:  # isolate one failed collector from the page
                data[key] = [] if key in {"temperatures", "disks"} else {}
                errors[key] = type(exc).__name__
    data["collected_at"] = _now_iso()
    data["collection_ms"] = round((time.monotonic() - started) * 1000)
    data["errors"] = errors
    data["summary"] = _summary(data)
    return data
