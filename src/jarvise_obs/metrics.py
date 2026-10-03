"""In-process Prometheus text exposition (no prometheus_client dependency)."""

from __future__ import annotations

import threading
import time
from typing import Any

_LOCK = threading.Lock()
_COUNTERS: dict[str, float] = {}
_GAUGES: dict[str, float] = {}
_HIST_SUM: dict[str, float] = {}
_HIST_COUNT: dict[str, float] = {}


def inc_counter(name: str, *, amount: float = 1.0, **labels: str) -> None:
    key = _key(name, labels)
    with _LOCK:
        _COUNTERS[key] = _COUNTERS.get(key, 0.0) + float(amount)


def set_gauge(name: str, value: float, **labels: str) -> None:
    key = _key(name, labels)
    with _LOCK:
        _GAUGES[key] = float(value)


def observe_job(job: str, *, ok: bool, duration_s: float) -> None:
    inc_counter("jarvise_job_runs_total", job=job, result="ok" if ok else "error")
    key = _key("jarvise_job_duration_seconds", {"job": job})
    with _LOCK:
        _HIST_SUM[key] = _HIST_SUM.get(key, 0.0) + max(0.0, float(duration_s))
        _HIST_COUNT[key] = _HIST_COUNT.get(key, 0.0) + 1.0


def render_prometheus(*, extra_gauges: dict[str, float] | None = None) -> str:
    """Return Prometheus text 0.0.4 payload."""
    lines: list[str] = [
        "# HELP jarvise_up Process is up",
        "# TYPE jarvise_up gauge",
        "jarvise_up 1",
        "# HELP jarvise_scrape_time_seconds Unix time of scrape",
        "# TYPE jarvise_scrape_time_seconds gauge",
        f"jarvise_scrape_time_seconds {time.time():.3f}",
    ]
    with _LOCK:
        counters = dict(_COUNTERS)
        gauges = dict(_GAUGES)
        hist_sum = dict(_HIST_SUM)
        hist_count = dict(_HIST_COUNT)
    if extra_gauges:
        for name, value in extra_gauges.items():
            gauges[_key(name, {})] = float(value)

    emitted_help: set[str] = set()
    for key, value in sorted(counters.items()):
        name, label_str = _split(key)
        if name not in emitted_help:
            lines.append(f"# HELP {name} Counter")
            lines.append(f"# TYPE {name} counter")
            emitted_help.add(name)
        lines.append(f"{name}{{{label_str}}} {value}" if label_str else f"{name} {value}")

    emitted_help.clear()
    for key, value in sorted(gauges.items()):
        name, label_str = _split(key)
        if name not in emitted_help:
            lines.append(f"# HELP {name} Gauge")
            lines.append(f"# TYPE {name} gauge")
            emitted_help.add(name)
        lines.append(f"{name}{{{label_str}}} {value}" if label_str else f"{name} {value}")

    emitted_help.clear()
    for key, total in sorted(hist_sum.items()):
        name, label_str = _split(key)
        count = hist_count.get(key, 0.0)
        base = name
        if base not in emitted_help:
            lines.append(f"# HELP {base} Job duration seconds (sum/count)")
            lines.append(f"# TYPE {base} summary")
            emitted_help.add(base)
        suffix = f"{{{label_str}}}" if label_str else ""
        lines.append(f"{base}_sum{suffix} {total}")
        lines.append(f"{base}_count{suffix} {count}")

    lines.append("")
    return "\n".join(lines)


def _key(name: str, labels: dict[str, str]) -> str:
    if not labels:
        return name
    parts = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(labels.items()))
    return f"{name}|{parts}"


def _split(key: str) -> tuple[str, str]:
    if "|" not in key:
        return key, ""
    name, labels = key.split("|", 1)
    return name, labels


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def snapshot_for_tests() -> dict[str, Any]:
    with _LOCK:
        return {
            "counters": dict(_COUNTERS),
            "gauges": dict(_GAUGES),
            "hist_sum": dict(_HIST_SUM),
            "hist_count": dict(_HIST_COUNT),
        }
