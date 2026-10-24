"""Resilient name resolution for school hosts.

On the campus network the macOS system resolver sometimes takes exactly 30 s
per lookup for ``*.nwafu.edu.cn`` (observed 2026-10-23) while the same DNS
servers answer ``dig`` instantly. Every request then costs half a minute.

:func:`install` wraps ``socket.getaddrinfo`` for school hosts only:
1. in-process + on-disk cache (addresses honour the record TTL, min 5 min),
2. the system lookup, given ``fast_timeout`` seconds,
3. if that is slow: a direct UDP A-record query to the configured
   nameservers (from ``scutil --dns`` / ``/etc/resolv.conf``).
Other hosts are untouched.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import sys
import random
import re
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

SCHOOL_SUFFIXES = (".nwafu.edu.cn", ".nwsuaf.edu.cn")
_orig_getaddrinfo = socket.getaddrinfo
_lock = threading.Lock()
_mem: dict[str, tuple[float, list[str]]] = {}
_installed = False
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="lw-dns")


def _cache_file() -> Path:
    from .config import state_dir

    return state_dir() / "dns_cache.json"


def _load_disk() -> None:
    try:
        data = json.loads(_cache_file().read_text("utf-8"))
        for host, (exp, ips) in data.items():
            _mem.setdefault(host, (exp, ips))
    except (OSError, ValueError):
        pass


def _save_disk() -> None:
    try:
        _cache_file().write_text(json.dumps(_mem), "utf-8")
    except OSError:
        pass


def nameservers() -> list[str]:
    """IPv4 DNS servers of this machine (macOS scutil, Linux resolv.conf, Windows PowerShell)."""
    servers: list[str] = []
    cmds = []
    if sys.platform == "darwin":
        cmds.append((["scutil", "--dns"], r"nameserver\[\d+\]\s*:\s*([0-9.]+)"))
    if os.name == "nt":
        cmds.append((["powershell", "-NoProfile", "-Command",
                      "Get-DnsClientServerAddress -AddressFamily IPv4 | ForEach-Object { $_.ServerAddresses }"], r"(\d+\.\d+\.\d+\.\d+)"))
    for cmd, pat in cmds:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5).stdout
            servers += re.findall(pat, out)
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        servers += re.findall(r"^nameserver\s+([0-9.]+)", Path("/etc/resolv.conf").read_text("utf-8", errors="replace"), re.M)
    except OSError:
        pass
    seen, out_list = set(), []
    for s in servers:
        if s not in seen:
            seen.add(s)
            out_list.append(s)
    return out_list


def query_a(host: str, server: str, timeout: float = 2.0) -> tuple[list[str], int]:
    """Minimal DNS A query (follows the CNAME chain inside the answer)."""
    qid = random.randint(0, 0xFFFF)
    qname = b"".join(bytes([len(p)]) + p.encode() for p in host.rstrip(".").split(".")) + b"\x00"
    packet = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0) + qname + struct.pack(">HH", 1, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(packet, (server, 53))
        data, _ = s.recvfrom(4096)
    rid, flags, qd, an, _ns, _ar = struct.unpack(">HHHHHH", data[:12])
    if rid != qid or flags & 0x000F:
        return [], 0
    off = 12
    for _ in range(qd):
        off = _skip_name(data, off) + 4
    ips, ttl = [], 3600
    for _ in range(an):
        off = _skip_name(data, off)
        rtype, _cls, rttl, rdlen = struct.unpack(">HHIH", data[off : off + 10])
        off += 10
        if rtype == 1 and rdlen == 4:
            ips.append(socket.inet_ntoa(data[off : off + 4]))
            ttl = min(ttl, rttl)
        off += rdlen
    return ips, ttl


def _skip_name(data: bytes, off: int) -> int:
    while True:
        ln = data[off]
        if ln == 0:
            return off + 1
        if ln & 0xC0 == 0xC0:
            return off + 2
        off += ln + 1


def resolve(host: str, fast_timeout: float = 2.0) -> list[str]:
    now = time.time()
    with _lock:
        hit = _mem.get(host)
    if hit and hit[0] > now:
        return hit[1]
    fut = _pool.submit(_orig_getaddrinfo, host, 443, socket.AF_INET, socket.SOCK_STREAM)
    ips: list[str] = []
    ttl = 1800
    try:
        ips = sorted({ai[4][0] for ai in fut.result(timeout=fast_timeout)})
    except Exception:  # noqa: BLE001 - slow or failed: go direct
        for ns in nameservers():
            try:
                ips, ttl = query_a(host, ns)
            except OSError:
                continue
            if ips:
                break
    if not ips and hit:  # stale is better than nothing
        return hit[1]
    if ips:
        with _lock:
            _mem[host] = (now + max(ttl, 300), ips)
            _save_disk()
    return ips


def _patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if isinstance(host, (bytes, bytearray)):
        host = host.decode()
    if isinstance(host, str) and host.endswith(SCHOOL_SUFFIXES) and family in (0, socket.AF_INET, socket.AF_UNSPEC):
        ips = resolve(host)
        if ips:
            stype = type or socket.SOCK_STREAM
            return [(socket.AF_INET, stype, proto or 6, "", (ip, int(port or 0))) for ip in ips]
    return _orig_getaddrinfo(host, port, family, type, proto, flags)


def install() -> None:
    global _installed
    if _installed:
        return
    _load_disk()
    socket.getaddrinfo = _patched_getaddrinfo
    _installed = True
