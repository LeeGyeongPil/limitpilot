"""Token totals for gateway slots, read from Claude Code's local transcripts.

``~/.claude/projects/**/*.jsonl`` can grow to gigabytes. Instead of re-reading every
file each poll, an incremental scanner keeps a cursor per file
``(inode, size, mtime, offset)`` and only reads bytes appended since the last scan.
The dashboard runs the scan in a background thread and shows the last result meanwhile.

Only model / message id / usage numbers are kept — message content is never stored,
returned or logged.
"""

import collections
import json
import os
import threading
import time
from datetime import datetime

from limitpilot import paths

GATEWAY_USAGE_CACHE_TTL_S = 30.0
DEDUP_MAX_IDS = 50_000   # 메시지 id 중복 제거 표의 상한(넘으면 오래된 것부터 버린다)
_COMPONENTS = ("input", "output", "cacheRead", "cacheCreation")

_gateway_usage_lock = threading.Lock()
_gateway_usage_cache = {}  # (model, addedAt) -> (calculated_at, aggregate)
_scanners = {}             # (model, addedAt) -> _Scanner
_running = set()           # 백그라운드 스캔이 도는 키
_scanner_stats = {"bytesRead": 0, "scans": 0}   # 테스트·bench 용 계측


def _empty():
    return {"input": 0, "output": 0, "cacheRead": 0, "cacheCreation": 0,
            "total": 0, "responses": 0}


class _Scanner:
    """Incremental aggregator for one (model, registration time) pair."""

    def __init__(self, model: str, since: float):
        self.model = model
        self.since = since
        self.cursors = {}   # path -> (inode, size, mtime_ns, offset)
        self.seen = collections.OrderedDict()   # message id -> 성분별 최댓값
        self.totals = dict.fromkeys(_COMPONENTS, 0)

    def _add(self, message_id: str, values: dict) -> None:
        previous = self.seen.pop(message_id, None) or dict.fromkeys(_COMPONENTS, 0)
        merged = {k: max(previous[k], values[k]) for k in _COMPONENTS}
        for k in _COMPONENTS:
            self.totals[k] += merged[k] - previous[k]   # 스트리밍 중복 기록은 차이만 더한다
        self.seen[message_id] = merged
        while len(self.seen) > DEDUP_MAX_IDS:
            self.seen.popitem(last=False)

    def _line(self, line: str) -> None:
        if self.model not in line or '"usage"' not in line:
            return
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(record, dict):
            return
        timestamp = record.get("timestamp")
        if isinstance(timestamp, str) and self.since:
            try:
                if datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp() < self.since:
                    return
            except ValueError:
                pass
        message = record.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        if not isinstance(message, dict) or message.get("model") != self.model \
                or not isinstance(usage, dict):
            return
        message_id = message.get("id") or record.get("uuid")
        if not isinstance(message_id, str) or not message_id:
            return
        raw = {
            "input": usage.get("input_tokens", 0),
            "output": usage.get("output_tokens", 0),
            "cacheRead": usage.get("cache_read_input_tokens", 0),
            "cacheCreation": usage.get("cache_creation_input_tokens", 0),
        }
        values = {k: int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0
                  for k, v in raw.items()}
        self._add(message_id, values)

    def _scan_file(self, path: str) -> None:
        try:
            info = os.stat(path)
        except OSError:
            return
        if info.st_mtime + 1 < self.since:
            return
        cursor = self.cursors.get(path)
        offset = 0
        if cursor and cursor[0] == info.st_ino and info.st_size >= cursor[3]:
            if info.st_size == cursor[1] and info.st_mtime_ns == cursor[2]:
                return  # 바뀐 것 없음 — 한 바이트도 읽지 않는다
            offset = cursor[3]   # 덧붙은 부분만 읽는다
        try:
            with open(path, "rb") as handle:
                handle.seek(offset)
                data = handle.read()
        except OSError:
            return
        _scanner_stats["bytesRead"] += len(data)
        end = data.rfind(b"\n")
        if end < 0:
            # 아직 끝나지 않은 줄뿐 — 다음 스캔에서 다시 읽는다
            self.cursors[path] = (info.st_ino, info.st_size, info.st_mtime_ns, offset)
            return
        for raw_line in data[:end].split(b"\n"):
            self._line(raw_line.decode("utf-8", "replace"))
        self.cursors[path] = (info.st_ino, info.st_size, info.st_mtime_ns, offset + end + 1)

    def scan(self) -> dict:
        _scanner_stats["scans"] += 1
        projects = os.path.join(paths.HOME, ".claude", "projects")
        alive = set()
        try:
            for root, _dirs, files in os.walk(projects):
                for name in files:
                    if name.endswith(".jsonl"):
                        path = os.path.join(root, name)
                        alive.add(path)
                        self._scan_file(path)
        except OSError:
            pass
        for gone in set(self.cursors) - alive:
            del self.cursors[gone]   # 지워진 파일의 커서는 버린다(합계는 유지 — 높은 수위 표시)
        result = dict(self.totals)
        result["total"] = sum(self.totals.values())
        result["responses"] = len(self.seen)
        return result


def _since(added_at) -> float:
    try:
        return datetime.fromisoformat(str(added_at).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _apply_floor(aggregate: dict, floor) -> dict:
    """Never show less than a saved high-water mark (deleted transcripts must not shrink it)."""
    result = dict(aggregate)
    if isinstance(floor, dict):
        for component in _COMPONENTS + ("responses",):
            result[component] = max(int(result.get(component, 0) or 0),
                                     int(floor.get(component, 0) or 0))
        result["total"] = sum(result[k] for k in _COMPONENTS)
    return result


def _run_scan(key) -> dict:
    scanner_key = key + (paths.HOME,)   # 다른 홈(테스트·연습 모드)과 커서를 섞지 않는다
    with _gateway_usage_lock:
        scanner = _scanners.get(scanner_key)
        if scanner is None:
            scanner = _scanners[scanner_key] = _Scanner(key[0], _since(key[1]))
    aggregate = scanner.scan()
    with _gateway_usage_lock:
        _gateway_usage_cache[key] = (time.time(), dict(aggregate))
    return aggregate


def _background(key) -> None:
    try:
        _run_scan(key)
    finally:
        with _gateway_usage_lock:
            _running.discard(key)


def gateway_token_usage(slot: dict, background: bool = False) -> dict:
    """Aggregate local transcript usage for this gateway model since registration.

    ``background=True`` (dashboard) returns the last result at once and refreshes it in
    a worker thread when it is older than ``GATEWAY_USAGE_CACHE_TTL_S``.
    """
    model = (slot or {}).get("modelId")
    added_at = (slot or {}).get("addedAt")
    if not isinstance(model, str) or not model:
        return _empty()
    key = (model, added_at)
    floor = (slot or {}).get("tokenUsage")
    now = time.time()
    with _gateway_usage_lock:
        cached = _gateway_usage_cache.get(key)
        if cached and now - cached[0] < GATEWAY_USAGE_CACHE_TTL_S:
            return _apply_floor(cached[1], floor)
        if background:
            if key not in _running:
                _running.add(key)
                threading.Thread(target=_background, args=(key,), daemon=True,
                                 name="limitpilot-gateway-usage").start()
            return _apply_floor(cached[1] if cached else _empty(), floor)
    return _apply_floor(_run_scan(key), floor)


def forget(model=None) -> None:
    """Drop cached scanners (tests, slot removal)."""
    with _gateway_usage_lock:
        for key in list(_scanners):
            if model is None or key[0] == model:
                _scanners.pop(key, None)
                _gateway_usage_cache.pop(key, None)
