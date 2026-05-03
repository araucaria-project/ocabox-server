"""Runtime diagnostics — temporary, used to investigate cascade ALPACA "not
responding" errors on production `tic` (2026-04-25).

Original FD-leak hypothesis was falsified by 4h+ of flat fds. Current working
hypothesis: a transient network event leaves the long-running process in an
unrecoverable state — possibly memory pressure, GC stalls, or event-loop lag
that causes many in-flight aiohttp requests to time out simultaneously even
though `curl` from the same host works fine. Restart of `tic` recovers it.

Periodically samples the live process and emits a single-line WARNING with:

    DIAG fds=N (sockets=M, limit=soft/hard)
         rss=NMB lag_ms=X
         tcp[STATE=count, ...]
         tasks=K gc=(g0,g1,g2 coll=Total)
         top_peers=ip:port×count, ...

Linux-only (reads `/proc/self/fd`, `/proc/self/net/{tcp,tcp6}`,
`/proc/self/status`); on other platforms it skips with a one-line warning and
otherwise no-ops, so leaving the hookup in main.py is safe across dev / prod.

Hypothesis checks (look for spikes correlated with cascade ERRORs):
- `rss` climbing monotonically over hours → memory leak.
- `lag_ms` spiking from single-digit ms to hundreds → event-loop stall (most
  likely cause of "many hosts fail at once while curl works").
- `gc=(...)` showing high gen-2 generation pressure → GC pauses → stalls.
- `top_peers` dominated by one host with `SYN_SENT`/`CLOSE_WAIT` entries →
  socket pile-up to a dead host.

Wire-up (one line in `main.py`, just before `loop.run_until_complete(...)`):

    from obsrv.utils.runtime_diagnostics import schedule_runtime_diagnostics
    schedule_runtime_diagnostics(loop, interval=60.0)
"""
import asyncio
import gc
import logging
import os
import resource
import sys
import time
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


def _read_rss_kb() -> int:
    """Return resident set size in KB from /proc/self/status. -1 on error."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return -1


def _gc_summary() -> str:
    """Return per-generation pending-object counts and total collections so far."""
    try:
        gen_counts = gc.get_count()  # (gen0, gen1, gen2)
        stats = gc.get_stats()
        total_coll = sum(s.get('collections', 0) for s in stats)
        return f"{','.join(str(c) for c in gen_counts)} coll={total_coll}"
    except Exception:
        return "?"


async def _measure_loop_lag_ms(samples: int = 3, target_s: float = 0.05) -> float:
    """Schedule short sleeps and return the worst overshoot in milliseconds.

    A healthy event loop overshoots a 50ms sleep by well under 5ms. A stalled
    loop (heavy GC pause, blocked-on-sync-call coroutine, etc.) can overshoot
    by hundreds of ms — that's the signature of "many simultaneous aiohttp
    timeouts while curl from the same host works fine".
    """
    peak = 0.0
    for _ in range(samples):
        t0 = time.monotonic()
        await asyncio.sleep(target_s)
        actual = time.monotonic() - t0
        peak = max(peak, max(0.0, actual - target_s) * 1000.0)
    return peak


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


def _snapshot(peak_lag_ms: float) -> str:
    fds, sockets = _count_fds()
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        limit_str = f"{soft}/{hard}"
    except Exception:
        limit_str = "?/?"
    rss_kb = _read_rss_kb()
    rss_str = f"{rss_kb // 1024}MB" if rss_kb >= 0 else "?"
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
            f"rss={rss_str} lag_ms={peak_lag_ms:.1f} "
            f"tcp[{state_str}] tasks={n_tasks} gc=({_gc_summary()}) "
            f"top_peers={top_peers}")


async def _diag_loop(interval: float) -> None:
    logger.warning(f"DIAG started (interval={interval:.0f}s, pid={os.getpid()})")
    while True:
        try:
            # Measure event-loop lag right before sampling so the rest of the
            # snapshot reflects state during the same window.
            peak_lag_ms = await _measure_loop_lag_ms()
            logger.warning(_snapshot(peak_lag_ms))
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
