"""Lean Prometheus text exposition."""

from jarvise_obs.metrics import inc_counter, observe_job, render_prometheus, set_gauge


def test_render_prometheus_includes_up_and_counters() -> None:
    inc_counter("jarvise_job_runs_total", job="ingest", result="ok")
    set_gauge("jarvise_kill_switch", 0.0)
    observe_job("paper_run", ok=True, duration_s=1.25)
    text = render_prometheus()
    assert "jarvise_up 1" in text
    assert "jarvise_job_runs_total" in text
    assert 'job="ingest"' in text
    assert "jarvise_kill_switch" in text
    assert "jarvise_job_duration_seconds_sum" in text
