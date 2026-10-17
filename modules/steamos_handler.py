"""
Safe SteamOS / Linux local diagnostics for the personal assistant.

Only allowlisted read-only (or low-risk) commands are permitted. Arbitrary
shell input from the LLM is never passed directly to a shell.
"""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Any, Callable

import psutil

logger = logging.getLogger(__name__)

# Hard timeout for any subprocess diagnostic.
COMMAND_TIMEOUT_SECONDS = 10


class SteamOSHandler:
    """
    Execute safe local system checks on a SteamOS / Linux host.

    The AI agent should call named tools (e.g. ``get_cpu_stats``) rather than
    free-form shell strings. Free-form input is accepted only via
    ``run_safe_command`` against a fixed allowlist.
    """

    def __init__(self) -> None:
        # Maps command name -> zero-arg callable returning a JSON-serializable dict
        self._tools: dict[str, Callable[[], dict[str, Any]]] = {
            "get_cpu_stats": self.get_cpu_stats,
            "get_memory_stats": self.get_memory_stats,
            "get_disk_stats": self.get_disk_stats,
            "get_system_info": self.get_system_info,
            "get_uptime": self.get_uptime,
            "list_top_processes": self.list_top_processes,
            "check_network": self.check_network,
        }

        # Allowlisted shell diagnostics (argv lists only — never shell=True)
        self._shell_allowlist: dict[str, list[str]] = {
            "uname": ["uname", "-a"],
            "uptime": ["uptime"],
            "df": ["df", "-h", "/"],
            "free": ["free", "-h"],
            "whoami": ["whoami"],
            "hostname": ["hostname"],
        }

    # ------------------------------------------------------------------
    # Tool registry (for agent_core)
    # ------------------------------------------------------------------

    def list_tools(self) -> list[str]:
        """Return names of available SteamOS diagnostic tools."""
        return sorted(self._tools.keys())

    def run_tool(self, name: str) -> dict[str, Any]:
        """Run a named diagnostic tool by registry key."""
        if name not in self._tools:
            raise ValueError(
                f"Unknown SteamOS tool '{name}'. Available: {', '.join(self.list_tools())}"
            )
        logger.info("Running SteamOS tool: %s", name)
        return self._tools[name]()

    def run_safe_command(self, command_name: str) -> dict[str, Any]:
        """
        Run an allowlisted diagnostic shell command.

        Args:
            command_name: Key from the allowlist (e.g. ``df``, ``uname``).

        Returns:
            Dict with command, returncode, stdout, stderr.
        """
        if command_name not in self._shell_allowlist:
            raise ValueError(
                f"Command '{command_name}' is not allowlisted. "
                f"Allowed: {', '.join(sorted(self._shell_allowlist))}"
            )
        argv = self._shell_allowlist[command_name]
        if not shutil.which(argv[0]):
            return {
                "command": command_name,
                "argv": argv,
                "error": f"Executable not found: {argv[0]}",
            }

        logger.info("Running allowlisted command: %s", argv)
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=COMMAND_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "command": command_name,
                "argv": argv,
                "error": f"Timed out after {COMMAND_TIMEOUT_SECONDS}s",
            }

        return {
            "command": command_name,
            "argv": argv,
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
        }

    # ------------------------------------------------------------------
    # psutil-backed diagnostics
    # ------------------------------------------------------------------

    def get_cpu_stats(self) -> dict[str, Any]:
        """CPU utilization and core counts."""
        return {
            "cpu_percent": psutil.cpu_percent(interval=0.5),
            "cpu_count_logical": psutil.cpu_count(logical=True),
            "cpu_count_physical": psutil.cpu_count(logical=False),
            "load_avg": list(psutil.getloadavg()) if hasattr(psutil, "getloadavg") else None,
            "per_cpu_percent": psutil.cpu_percent(interval=0.1, percpu=True),
        }

    def get_memory_stats(self) -> dict[str, Any]:
        """Virtual memory and swap usage."""
        vm = psutil.virtual_memory()
        swap = psutil.swap_memory()
        return {
            "virtual": {
                "total_gb": round(vm.total / (1024**3), 2),
                "available_gb": round(vm.available / (1024**3), 2),
                "used_gb": round(vm.used / (1024**3), 2),
                "percent": vm.percent,
            },
            "swap": {
                "total_gb": round(swap.total / (1024**3), 2),
                "used_gb": round(swap.used / (1024**3), 2),
                "percent": swap.percent,
            },
        }

    def get_disk_stats(self, path: str = "/") -> dict[str, Any]:
        """Disk usage for a mount point (default root)."""
        usage = psutil.disk_usage(path)
        return {
            "path": path,
            "total_gb": round(usage.total / (1024**3), 2),
            "used_gb": round(usage.used / (1024**3), 2),
            "free_gb": round(usage.free / (1024**3), 2),
            "percent": usage.percent,
        }

    def get_system_info(self) -> dict[str, Any]:
        """Host identity and platform details (useful on Steam Deck / SteamOS)."""
        uname = platform.uname()
        boot = datetime.fromtimestamp(psutil.boot_time(), tz=timezone.utc)
        return {
            "hostname": uname.node,
            "system": uname.system,
            "release": uname.release,
            "version": uname.version,
            "machine": uname.machine,
            "processor": uname.processor,
            "python": platform.python_version(),
            "boot_time_utc": boot.isoformat(),
            "is_steamos_hint": "steam" in (uname.release + uname.version).lower()
            or "neptune" in uname.node.lower(),
        }

    def get_uptime(self) -> dict[str, Any]:
        """Seconds and human-readable uptime since boot."""
        boot_ts = psutil.boot_time()
        now = datetime.now(tz=timezone.utc).timestamp()
        seconds = int(now - boot_ts)
        hours, rem = divmod(seconds, 3600)
        minutes, secs = divmod(rem, 60)
        return {
            "uptime_seconds": seconds,
            "uptime_human": f"{hours}h {minutes}m {secs}s",
            "boot_time_utc": datetime.fromtimestamp(boot_ts, tz=timezone.utc).isoformat(),
        }

    def list_top_processes(self, limit: int = 5) -> dict[str, Any]:
        """Top processes by CPU percent (snapshot)."""
        procs: list[dict[str, Any]] = []
        for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
            try:
                info = proc.info
                procs.append(
                    {
                        "pid": info.get("pid"),
                        "name": info.get("name"),
                        "cpu_percent": info.get("cpu_percent") or 0.0,
                        "memory_percent": round(info.get("memory_percent") or 0.0, 2),
                    }
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        procs.sort(key=lambda p: p["cpu_percent"], reverse=True)
        return {"top_by_cpu": procs[:limit]}

    def check_network(self) -> dict[str, Any]:
        """Basic network interface and connection summary."""
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        interfaces: dict[str, Any] = {}
        for name, addr_list in addrs.items():
            interfaces[name] = {
                "up": stats[name].isup if name in stats else None,
                "addresses": [
                    {"family": str(a.family), "address": a.address} for a in addr_list
                ],
            }
        return {
            "interfaces": interfaces,
            "connection_count": len(psutil.net_connections(kind="inet")),
        }
