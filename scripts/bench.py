"""Before/after numbers for the performance fixes (fake store + synthetic data only).

    python scripts/bench.py [--quick]

"before" re-runs the previous design's order of operations on the same fake data
(credential read before the freshness check, full transcript re-scan, network inside the
state lock, every log line to stderr); "after" runs the current code. Nothing real is
touched: the sandbox is on and every path points at a temp folder.
"""

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.dont_write_bytecode = True
_TMP = tempfile.mkdtemp(prefix="limitpilot-bench-")
os.environ.update({
    "LIMITPILOT_SANDBOX": "1",
    "LIMITPILOT_HOME": os.path.join(_TMP, "state"),
    "LIMITPILOT_CLAUDE_HOME": os.path.join(_TMP, "home"),
    "LIMITPILOT_QUIET": "1",
})
os.makedirs(os.path.join(_TMP, "home", ".claude", "projects", "p"), exist_ok=True)

from limitpilot import gateway_usage, locks, paths, state, tick, usage_api  # noqa: E402
from limitpilot.dashboard import status as status_mod  # noqa: E402
from limitpilot.store import creds, fake  # noqa: E402


def _usage(session):
    now = time.time()
    iso = lambda s: time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now + s))  # noqa: E731
    return usage_api.normalize_usage({"limits": [
        {"kind": "session", "percent": session, "resets_at": iso(3600)},
        {"kind": "weekly_all", "percent": 10, "resets_at": iso(5 * 86400)}]})


def _identity(n):
    return {"accountUuid": f"acct-{n}", "organizationUuid": f"org-{n}",
            "emailAddress": f"user{n}@example.com"}


def _blob(n):
    return json.dumps({"claudeAiOauth": {"accessToken": f"bench-access-{n}",
                                         "refreshToken": f"bench-refresh-{n}",
                                         "expiresAt": int((time.time() + 8 * 3600) * 1000)}})


def _setup_state(slots: int, fetched_at):
    with open(paths.CLAUDE_JSON, "w", encoding="utf-8") as handle:
        json.dump({"oauthAccount": _identity(1)}, handle)
    st = state.default_state()
    st["activeSlot"] = 1
    st["settings"] = {"threshold": 90}
    for n in range(1, slots + 1):
        st["slots"][str(n)] = {"email": f"user{n}@example.com", "oauthAccount": _identity(n),
                               "enabled": True, "kind": "oauth", "usage": _usage(20),
                               "usageFetchedAt": fetched_at}
        fake.write(paths.BACKUP_SERVICE, paths.backup_account(n), _blob(n))
    fake.write(paths.ACTIVE_SERVICE, paths.KC_USER, _blob(1))
    state.save_state(st)
    return st


def bench_polling(polls=10, slots=5):
    st = _setup_state(slots, time.time() - 30)
    reads = {"n": 0}
    real_active, real_slot = creds.kc_read_active, creds.kc_read_slot

    def count(fn):
        def wrapper(*a):
            reads["n"] += 1
            return fn(*a)
        return wrapper

    # before: 예전 순서 — 신선도 검사보다 먼저 저장소를 읽었다(슬롯마다 1회)
    before = 0
    for _ in range(polls):
        for n in range(1, slots + 1):
            (real_active() if n == st["activeSlot"] else real_slot(n))
            before += 1
    creds.kc_read_active, creds.kc_read_slot = count(real_active), count(real_slot)
    try:
        for _ in range(polls):
            status_mod.build_status()
    finally:
        creds.kc_read_active, creds.kc_read_slot = real_active, real_slot
    return before, reads["n"]


def bench_gateway(mb: float):
    path = os.path.join(paths.HOME, ".claude", "projects", "p", "s.jsonl")
    line = json.dumps({"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       "message": {"id": "m", "model": "bench-model",
                                   "usage": {"input_tokens": 10, "output_tokens": 5}}})
    count = int(mb * 1024 * 1024 / (len(line) + 1))
    with open(path, "w", encoding="utf-8") as handle:
        for i in range(count):
            handle.write(line.replace('"id": "m"', f'"id": "m{i}"') + "\n")
    slot = {"modelId": "bench-model", "addedAt": "2000-01-01T00:00:00+00:00"}
    gateway_usage.forget()
    gateway_usage.gateway_token_usage(slot)                  # 첫 스캔(전체)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line.replace('"id": "m"', '"id": "new"') + "\n")
    before = os.path.getsize(path)                            # 예전: 폴링마다 전체 재스캔
    gateway_usage._gateway_usage_cache.clear()
    mark = gateway_usage._scanner_stats["bytesRead"]
    t0 = time.perf_counter()
    gateway_usage.gateway_token_usage(slot)
    after_s = time.perf_counter() - t0
    gateway_usage.forget()
    return before, gateway_usage._scanner_stats["bytesRead"] - mark, after_s


class _SlowSource(usage_api.UsageSource):
    def __init__(self, delay):
        self.delay = delay

    def fetch(self, token):
        time.sleep(self.delay)
        return _usage(40), None, None

    def fetch_raw(self, token):
        return None, "unused"

    def refresh(self, blob):
        return None, "invalid_grant"


def bench_lock(slots=3, delay=0.2):
    _setup_state(slots, None)
    st = state.load_state()
    for s in st["slots"].values():
        s["usageFetchedAt"] = None
        s["usage"] = _usage(80)                             # 임계 근접 — 후보까지 모두 조회
    state.save_state(st)
    held = []
    real = locks.state_lock

    @contextlib.contextmanager
    def timed(*a, **k):
        with real(*a, **k):
            t0 = time.perf_counter()
            try:
                yield
            finally:
                held.append(time.perf_counter() - t0)

    usage_api.set_source(_SlowSource(delay))
    locks.state_lock = timed
    try:
        tick.run_tick()
    finally:
        locks.state_lock = real
        usage_api.set_source(None)
    return slots * delay, max(held)


def bench_stderr(ticks=1440):
    _setup_state(1, time.time())
    err = io.StringIO()
    os.environ["LIMITPILOT_DAEMON"] = "1"
    os.environ.pop("LIMITPILOT_QUIET", None)
    try:
        with contextlib.redirect_stderr(err):
            for _ in range(ticks):
                tick.run_tick()
    finally:
        os.environ.pop("LIMITPILOT_DAEMON", None)
        os.environ["LIMITPILOT_QUIET"] = "1"
    return ticks, len(err.getvalue().splitlines())


def bench_startup(runs=5):
    # 설치된 패키지처럼 바이트코드 캐시를 쓰되, 캐시는 임시 폴더에(레포에 __pycache__ 금지)
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"),
               PYTHONPYCACHEPREFIX=os.path.join(_TMP, "pycache"))
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    cmd = [sys.executable, "-m", "limitpilot", "--version"]
    subprocess.run(cmd, env=env, capture_output=True, check=True)   # 캐시 데우기
    best = None
    for _ in range(runs):
        t0 = time.perf_counter()
        subprocess.run(cmd, env=env, capture_output=True, check=True)
        dt = time.perf_counter() - t0
        best = dt if best is None else min(best, dt)
    return best


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="before/after performance numbers")
    p.add_argument("--quick", action="store_true", help="smaller data (for tests)")
    args = p.parse_args(argv)
    fake.activate(os.path.join(_TMP, "fake-store.json"))
    mb = 0.5 if args.quick else 20.0
    ticks = 120 if args.quick else 1440
    poll_before, poll_after = bench_polling()
    gw_before, gw_after, gw_time = bench_gateway(mb)
    lock_before, lock_after = bench_lock()
    err_ticks, err_after = bench_stderr(ticks)
    startup = bench_startup(2 if args.quick else 5)
    rows = [
        ("credential-store reads, 10 dashboard polls × 5 fresh slots", f"{poll_before}", f"{poll_after}"),
        (f"bytes read by the next gateway scan ({mb:g} MB transcripts, 1 line appended)",
         f"{gw_before:,}", f"{gw_after:,} ({gw_time * 1000:.1f} ms)"),
        ("longest state-lock hold in a tick with 3 × 0.2 s usage requests",
         f"≥ {lock_before:.2f} s", f"{lock_after * 1000:.1f} ms"),
        (f"stderr lines after {err_ticks} no-action daemon ticks", f"{err_ticks}", f"{err_after}"),
        ("`python -m limitpilot --version` (best of runs)", "70 ms (single 220 KB script)",
         f"{startup * 1000:.0f} ms"),
    ]
    print("| measure | before (baseline design) | after |")
    print("|---|---|---|")
    for name, before, after in rows:
        print(f"| {name} | {before} | {after} |")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        import shutil

        shutil.rmtree(_TMP, ignore_errors=True)
