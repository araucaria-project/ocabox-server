"""Runtime diagnostics — temporary, used to investigate the FD/socket-leak
hypothesis behind cascade ALPACA "not responding" errors on production `tic`
when one ALPACA host (e.g. iris dome) is unreachable (2026-04-25).

Periodically samples the live process and emits a single-line WARNING with:

    DIAG fds=N (sockets=M, limit=soft/hard)
         tcp[STATE=count, ...]
         tasks=K
         top_peers=ip:port×count, ...

Linux-only (reads `/proc/self/fd` and `/proc/self/net/{tcp,tcp6}`); on other
platforms it skips with a one-line warning and otherwise no-ops, so leaving the
hookup in main.py is safe across dev / prod.

Hypothesis check:
- If `fds` grows monotonically over minutes/hours while the process keeps
  running → leak confirmed.
- If `top_peers` is dominated by the dead host (e.g. `192.168.7.139:11111` for
  iris dome) with many SYN_SENT/CLOSE_WAIT entries → confirms aiohttp requests
  to that host are piling up sockets without being timed out.

Wire-up (one line in `main.py`, just before `loop.run_until_complete(...)`):

    from obsrv.utils.runtime_diagnostics import schedule_runtime_diagnostics
    schedule_runtime_diagnostics(loop, interval=60.0)
"""
import asyncio
import logging
import os
import resource
import sys
from collections import Counter

logger = logging.getLogger("runtime_diag")

# Numeric TCP states from include/net/tcp_states.h, as exposed in /proc/net/tcp
_TCP_STATE = {
    "01": "ESTABLISHED",
    "02": "SYN_SENT",
    "03": "SYN_RECV",
    "04": "FIN_WAIT1",
    "05": "FIN_WAIT2",
    "06": "TIME_WAIT",
    "07": "CLOSE",
    "08": "CLOSE_WAIT",
    "09": "LAST_ACK",
    "0A": "LISTEN",
    "0B": "CLOSING",
}


def _count_fds() -> tuple[int, int]:
    """Return (total_fds, socket_fds). -1 on error. socket_fds is best-effort."""
    fd_dir = "/proc/self/fd"
    try:
        entries = os.listdir(fd_dir)
    except OSError:
        return -1, -1
    sockets = 0
    for e in entries:
        try:
            if os.readlink(os.path.join(fd_dir, e)).startswith("socket:"):
                sockets += 1
        except OSError:
            continue
    return len(entries), sockets


def _hex_addr_to_str(hex_addr: str) -> str:
    """Convert `<hex_ip>:<hex_port>` from /proc/net/tcp[6] into `ip:port`."""
    try:
        ip_hex, port_hex = hex_addr.split(":")
        port = int(port_hex, 16)
        b = bytes.fromhex(ip_hex)
        if len(b) == 4:  # IPv4, little-endian
            ip = ".".join(str(x) for x in reversed(b))
        else:  # IPv6, little-endian per 32-bit word
            words = [b[i:i + 4] for i in range(0, 16, 4)]
            ip = ":".join("".join(f"{x:02x}" for x in reversed(w)) for w in words)
        return f"{ip}:{port}"
    except Exception:
        return hex_addr


def _read_proc_net_tcp(path: str) -> tuple[Counter, Counter]:
    """Return (states, non_listen_peers) parsed from `/proc/self/net/tcp[6]`."""
    states: Counter = Counter()
    peers: Counter = Counter()
    try:
        with open(path) as f:
            next(f, None)  # header
            for line in f:
                cols = line.split()
                if len(cols) < 4:
                    continue
                remote_hex = cols[2]
                state_hex = cols[3].upper()
                state = _TCP_STATE.get(state_hex, state_hex)
                states[state] += 1
                if state != "LISTEN":
                    peers[_hex_addr_to_str(remote_hex)] += 1
    except OSError:
        pass
    return states, peers


def _snapshot() -> str:
    fds, sockets = _count_fds()
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        limit_str = f"{soft}/{hard}"
    except Exception:
        limit_str = "?/?"
    s4, p4 = _read_proc_net_tcp("/proc/self/net/tcp")
    s6, p6 = _read_proc_net_tcp("/proc/self/net/tcp6")
    states = s4 + s6
    peers = p4 + p6
    state_str = ",".join(f"{k}={v}" for k, v in sorted(states.items())) or "-"
    top_peers = ",".join(f"{p}x{n}" for p, n in peers.most_common(5)) or "-"
    try:
        n_tasks = len(asyncio.all_tasks())
    except RuntimeError:
        n_tasks = -1
    return (f"DIAG fds={fds} (sockets={sockets}, limit={limit_str}) "
            f"tcp[{state_str}] tasks={n_tasks} top_peers={top_peers}")


async def _diag_loop(interval: float) -> None:
    logger.warning(f"DIAG started (interval={interval:.0f}s, pid={os.getpid()})")
    while True:
        try:
            logger.warning(_snapshot())
        except Exception:
            logger.exception("DIAG snapshot failed")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.warning("DIAG stopped")
            raise


def schedule_runtime_diagnostics(loop: asyncio.AbstractEventLoop,
                                 interval: float = 60.0) -> asyncio.Task | None:
    """Schedule the diagnostic task on `loop`. No-op on non-Linux platforms.

    Safe to call before `loop.run_until_complete(...)` — the task starts when
    the loop starts running.
    """
    if not sys.platform.startswith("linux"):
        logger.warning("DIAG skipped (Linux-only, platform=%s)", sys.platform)
        return None
    return loop.create_task(_diag_loop(interval))
