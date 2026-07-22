"""LAN scan: probe every address on the local /24 subnet for a printer.

For beginners who don't know their printer's IP address, this reuses the
exact same connection checks the Connect page already does manually
(Moonraker's ``/printer/info`` and Anycubic LAN mode's ``/info``) — just run
concurrently against a whole subnet with short timeouts instead of one host.

This is a plain TCP probe, not a discovery protocol (no mDNS/SSDP/broadcast),
so it only ever finds machines on the *same* subnet as this computer.
Printers reachable only through a separately routed network — for example a
mesh Wi-Fi satellite running in "router mode" rather than "access point
mode" — won't show up here; add those by IP manually instead.
"""

from __future__ import annotations

import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable

from anycubic_toolkit.core.anycubic_lan import probe_lan_mode
from anycubic_toolkit.core.moonraker import DEFAULT_PORT, MoonrakerClient

_SCAN_TIMEOUT = 0.8
_MAX_WORKERS = 64


@dataclass
class ScanHit:
    """One address on the subnet that answered as a printer."""

    host: str
    mode: str  # "moonraker" | "lan"
    name: str = ""


def local_subnet_prefix() -> str:
    """Best-effort ``"a.b.c."`` prefix for this machine's local /24 subnet.

    Assumes a /24 (the default on virtually every home router, including
    mesh systems like TP-Link Deco); returns "" if the local address can't
    be determined at all (e.g. no network connection).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))  # UDP "connect" only resolves a route, sends nothing
        ip = sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()
    parts = ip.split(".")
    return ".".join(parts[:3]) + "." if len(parts) == 4 else ""


def scan_subnet(
    prefix: str, progress: Callable[[int, str], None] | None = None
) -> list[ScanHit]:
    """Probe ``{prefix}1`` .. ``{prefix}254`` concurrently for printers.

    *progress*, if given, is called as ``progress(percent, "done/total")`` —
    the same shape :class:`~anycubic_toolkit.core.workers.FunctionWorker`
    auto-supplies, so this can be run directly as a background worker.
    """
    hosts = [f"{prefix}{i}" for i in range(1, 255)]
    total = len(hosts)
    hits: list[ScanHit] = []
    done = 0
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {pool.submit(_probe_host, host): host for host in hosts}
        for future in as_completed(futures):
            done += 1
            if progress is not None:
                progress(int(done * 100 / total), f"{done}/{total}")
            hit = future.result()
            if hit is not None:
                hits.append(hit)
    hits.sort(key=lambda h: tuple(int(part) for part in h.host.split(".")))
    return hits


def _probe_host(host: str) -> ScanHit | None:
    status = MoonrakerClient(host, DEFAULT_PORT, timeout=_SCAN_TIMEOUT).fetch_status()
    if status.online:
        return ScanHit(host=host, mode="moonraker", name=status.hostname)
    if probe_lan_mode(host, timeout=_SCAN_TIMEOUT):
        return ScanHit(host=host, mode="lan")
    return None
