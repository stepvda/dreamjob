"""Turn a harness run into a report a person can read.

``conftest.py`` writes ``out/run.json`` when the session ends and calls
:func:`write_reports`, which produces:

``out/report.md``
    The narrative: every step in the order it happened, with its outcome, how
    long it took and a link to the screenshot taken when it finished; then the
    screens that were visited, and every console error and failed request
    grouped by the screen it happened on.

``out/report.json``
    The same run, normalised, for anything that wants to diff two runs or
    graph them.

Kept free of any dependency on the harness (and therefore on Playwright) so
the report can be regenerated from a run file alone::

    python3 tests/e2e/report.py [out/run.json]
"""

from __future__ import annotations

import json
import sys
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any

E2E_DIR = Path(__file__).resolve().parent
OUT_DIR = E2E_DIR / "out"
RUN_JSON = OUT_DIR / "run.json"
REPORT_MD = OUT_DIR / "report.md"
REPORT_JSON = OUT_DIR / "report.json"

STATUS_MARK = {"passed": "✓", "failed": "✗", "running": "…", "skipped": "–"}


# ---------------------------------------------------------------------------
# Shaping
# ---------------------------------------------------------------------------


def _duration(ms: float | int | None) -> str:
    ms = float(ms or 0)
    if ms < 1000:
        return f"{ms:.0f}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    minutes, seconds = divmod(ms / 1000, 60)
    return f"{minutes:.0f}m {seconds:02.0f}s"


def _cell(text: Any, limit: int = 160) -> str:
    """One table cell: no pipes, no newlines, no unbounded stack traces."""
    flat = " ".join(str(text if text is not None else "").split())
    flat = flat.replace("|", "\\|")
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _elapsed(meta: dict[str, Any]) -> str:
    started, finished = meta.get("started_at"), meta.get("finished_at")
    if not (started and finished):
        return "—"
    try:
        delta = datetime.fromisoformat(finished) - datetime.fromisoformat(started)
    except ValueError:
        return "—"
    return _duration(delta.total_seconds() * 1000)


def _screen_key(entry: dict[str, Any]) -> tuple[str, str]:
    return entry.get("screen") or "unknown", entry.get("path") or "?"


def build(run: dict[str, Any]) -> dict[str, Any]:
    """Normalise a raw run into the shape the report renders."""
    meta = run.get("meta", {})
    steps = run.get("steps", [])
    console = run.get("console", [])
    page_errors = run.get("page_errors", [])
    requests = run.get("failed_requests", [])

    # Chromium logs an expected HTTP failure as a console error too; the
    # harness flags those, and the report counts them apart from real ones.
    errors = [
        c for c in console if c.get("level") == "error" and not c.get("expected")
    ] + list(page_errors)
    expected_console = [c for c in console if c.get("level") == "error" and c.get("expected")]
    warnings = [c for c in console if c.get("level") in ("warning", "warn")]
    unexpected = [r for r in requests if not r.get("expected")]

    # Steps grouped under the test that ran them, in run order.
    by_test: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for entry in steps:
        by_test.setdefault(entry.get("test") or "(no test)", []).append(entry)

    tests = []
    seen = set()
    for record in run.get("tests", []):
        nodeid = record.get("nodeid", "")
        seen.add(nodeid)
        tests.append({**record, "steps": by_test.get(nodeid, [])})
    # A test that died in setup has steps but no call report; still show it.
    for nodeid, entries in by_test.items():
        if nodeid not in seen:
            tests.append(
                {
                    "nodeid": nodeid,
                    "name": nodeid.rsplit("::", 1)[-1],
                    "outcome": "failed" if any(s["status"] == "failed" for s in entries) else "—",
                    "duration_ms": sum(s.get("duration_ms", 0) for s in entries),
                    "message": None,
                    "steps": entries,
                }
            )

    # Which screens were visited, and what went wrong on each.
    screens: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()

    def bucket(entry: dict[str, Any]) -> dict[str, Any]:
        key = _screen_key(entry)
        if key not in screens:
            screens[key] = {
                "screen": key[0],
                "path": key[1],
                "visits": 0,
                "steps": 0,
                "console_errors": 0,
                "warnings": 0,
                "failed_requests": 0,
                "first_seen": entry.get("at") or entry.get("started_at"),
            }
        return screens[key]

    for entry in run.get("navigations", []):
        bucket(entry)["visits"] += 1
    for entry in steps:
        bucket(entry)["steps"] += 1
    for entry in errors:
        bucket(entry)["console_errors"] += 1
    for entry in warnings:
        bucket(entry)["warnings"] += 1
    for entry in unexpected:
        bucket(entry)["failed_requests"] += 1

    def group(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: OrderedDict[tuple[str, str], list[dict[str, Any]]] = OrderedDict()
        for entry in entries:
            grouped.setdefault(_screen_key(entry), []).append(entry)
        return [
            {"screen": key[0], "path": key[1], "items": items} for key, items in grouped.items()
        ]

    summary = {
        "tests": len(tests),
        "tests_passed": sum(1 for t in tests if t.get("outcome") == "passed"),
        "tests_failed": sum(1 for t in tests if t.get("outcome") == "failed"),
        "steps": len(steps),
        "steps_passed": sum(1 for s in steps if s.get("status") == "passed"),
        "steps_failed": sum(1 for s in steps if s.get("status") == "failed"),
        "screens": len(screens),
        "console_errors": len(errors),
        "console_warnings": len(warnings),
        "console_errors_expected": len(expected_console),
        "failed_requests": len(unexpected),
        "expected_failures": len(requests) - len(unexpected),
        "screenshots": len(run.get("screenshots", [])),
        "correlation_ids": len({c.get("value") for c in run.get("correlation_ids", [])}),
        "log_checks": len(run.get("log_checks", [])),
        "log_checks_failed": sum(1 for c in run.get("log_checks", []) if not c.get("found")),
        "elapsed": _elapsed(meta),
    }

    return {
        "meta": meta,
        "summary": summary,
        "tests": tests,
        "screens": list(screens.values()),
        "console_errors_by_screen": group(errors),
        "console_warnings": warnings,
        "failed_requests_by_screen": group(unexpected),
        "expected_failures": [r for r in requests if r.get("expected")],
        "log_checks": run.get("log_checks", []),
        "correlation_ids": run.get("correlation_ids", []),
        "screenshots": run.get("screenshots", []),
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_markdown(data: dict[str, Any]) -> str:
    meta = data["meta"]
    summary = data["summary"]
    out: list[str] = []
    add = out.append

    add("# Dream Job — end-to-end run")
    add("")
    verdict = "all green" if not (summary["tests_failed"] or summary["steps_failed"]) else "failures"
    add(
        f"*{meta.get('started_at', 'unknown time')} · {summary['tests']} tests · "
        f"{summary['steps']} steps · {summary['screens']} screens · "
        f"{summary['elapsed']} · {verdict}*"
    )
    add("")
    add("| | |")
    add("|---|---|")
    add(f"| App | {_cell(meta.get('base_url', '?'))} |")
    add(f"| API | {_cell(meta.get('api_url', '?'))} |")
    if meta.get("health"):
        health = meta["health"]
        add(
            f"| Backend | env `{_cell(health.get('env'))}`, "
            f"LLM `{_cell(health.get('llm_provider'))}`, "
            f"mail `{_cell(health.get('mail_backend'))}` |"
        )
    if meta.get("persona"):
        persona = meta["persona"]
        add(f"| Persona | {_cell(persona.get('display_name'))} <{_cell(persona.get('email'))}> |")
    add(f"| Viewport | {meta.get('viewport', {}).get('width', '?')}"
        f"x{meta.get('viewport', {}).get('height', '?')} |")
    add(f"| Tests | {summary['tests_passed']} passed, {summary['tests_failed']} failed |")
    add(f"| Steps | {summary['steps_passed']} passed, {summary['steps_failed']} failed |")
    add(f"| Console errors | {summary['console_errors']}"
        f" ({summary['console_warnings']} warnings,"
        f" {summary.get('console_errors_expected', 0)} expected) |")
    add(f"| Failed requests | {summary['failed_requests']}"
        f" ({summary['expected_failures']} expected) |")
    add(f"| Screenshots | {summary['screenshots']} |")
    add(f"| Log assertions | {summary['log_checks']} checked,"
        f" {summary['log_checks_failed']} unmet |")
    add("")

    add("## What was exercised")
    add("")
    if not data["tests"]:
        add("_Nothing ran._")
        add("")
    for test in data["tests"]:
        mark = STATUS_MARK.get(test.get("outcome"), "?")
        add(f"### {mark} {_cell(test.get('nodeid') or test.get('name'))}"
            f" — {test.get('outcome', '?')} in {_duration(test.get('duration_ms'))}")
        add("")
        if not test["steps"]:
            add("_No steps were recorded for this test._")
            add("")
        for index, step in enumerate(test["steps"], start=1):
            indent = "    " * int(step.get("depth", 0))
            shots = " ".join(
                f"[shot]({shot})" for shot in step.get("screenshots", [])
            )
            add(
                f"{indent}{index}. {STATUS_MARK.get(step.get('status'), '?')} **{_cell(step['name'])}**"
                f" — {_duration(step.get('duration_ms'))} — {_cell(step.get('screen', '—'))}"
                + (f" — {shots}" if shots else "")
            )
            noise = []
            if step.get("console_errors"):
                noise.append(f"{step['console_errors']} console error(s)")
            if step.get("failed_requests"):
                noise.append(f"{step['failed_requests']} failed request(s)")
            if noise:
                add(f"{indent}   - {', '.join(noise)}")
            if step.get("error"):
                add(f"{indent}   - **{_cell(step['error'], 400)}**")
        add("")
        if test.get("message"):
            add("```")
            add(str(test["message"]))
            add("```")
            add("")

    add("## Screens visited")
    add("")
    if data["screens"]:
        add("| Screen | Route | Visits | Steps | Console errors | Failed requests |")
        add("|---|---|---:|---:|---:|---:|")
        for screen in sorted(data["screens"], key=lambda s: s["screen"]):
            add(
                f"| {_cell(screen['screen'])} | `{_cell(screen['path'])}` | {screen['visits']} |"
                f" {screen['steps']} | {screen['console_errors']} | {screen['failed_requests']} |"
            )
    else:
        add("_No screen was recorded._")
    add("")

    add("## Console errors by screen")
    add("")
    if not data["console_errors_by_screen"]:
        add("None — the console stayed clean for the whole run.")
        add("")
    for group in data["console_errors_by_screen"]:
        add(f"### {_cell(group['screen'])} (`{_cell(group['path'])}`)")
        add("")
        for item in group["items"]:
            add(f"- `{item.get('level', 'error')}` {_cell(item.get('text'), 300)}")
            if item.get("step"):
                add(f"  - during **{_cell(item['step'])}** ({_cell(item.get('test'))})")
            if item.get("source"):
                add(f"  - at `{_cell(item['source'])}`")
        add("")

    add("## Failed requests by screen")
    add("")
    if not data["failed_requests_by_screen"]:
        add("None — every request the app made came back.")
        add("")
    for group in data["failed_requests_by_screen"]:
        add(f"### {_cell(group['screen'])} (`{_cell(group['path'])}`)")
        add("")
        add("| Method | Request | Result | During |")
        add("|---|---|---|---|")
        for item in group["items"]:
            outcome = item.get("status") or item.get("reason") or "failed"
            add(
                f"| {_cell(item.get('method', '?'))} | {_cell(item.get('request_url'))} |"
                f" {_cell(outcome)} | {_cell(item.get('step') or '—')} |"
            )
        add("")

    add("## Log assertions (NFR-701)")
    add("")
    if data["log_checks"]:
        add("| Expected | Found | Where |")
        add("|---|---|---|")
        for check in data["log_checks"]:
            where = Path(check["file"]).name if check.get("file") else "—"
            add(
                f"| `{_cell(check.get('pattern'))}` |"
                f" {'✓' if check.get('found') else '✗'} | {_cell(where)} |"
            )
    else:
        add("_No test asserted on the server log this run._")
    add("")

    ids = {c.get("value"): c for c in data["correlation_ids"]}
    add("## Correlation ids (NFR-701)")
    add("")
    if ids:
        add(f"{len(ids)} distinct id(s) seen on responses; the first few:")
        add("")
        add("| Id | Header | Screen |")
        add("|---|---|---|")
        for value, entry in list(ids.items())[:15]:
            add(f"| `{_cell(value)}` | `{_cell(entry.get('header'))}` |"
                f" {_cell(entry.get('screen'))} |")
    else:
        add("_No response carried a correlation header. NFR-701 expects one per request._")
    add("")

    add("## Screenshots")
    add("")
    if data["screenshots"]:
        for shot in data["screenshots"]:
            status = "" if shot.get("ok", True) else " _(could not be taken)_"
            add(f"- [{shot['index']:03d} {_cell(shot['name'])}]({shot['relative']}){status}")
    else:
        add("_None._")
    add("")

    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def write_reports(run: dict[str, Any], out_dir: Path = OUT_DIR) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    data = build(run)
    markdown_path = out_dir / REPORT_MD.name
    json_path = out_dir / REPORT_JSON.name
    markdown_path.write_text(render_markdown(data), encoding="utf-8")
    json_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return markdown_path, json_path


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    source = Path(args[0]).resolve() if args else RUN_JSON
    if not source.exists():
        print(
            f"No run to report on at {source}.\n"
            "Run the suite first: PYTHONPATH=backend python3 -m pytest tests/e2e -v",
            file=sys.stderr,
        )
        return 1
    run = json.loads(source.read_text(encoding="utf-8"))
    markdown_path, json_path = write_reports(run, source.parent)
    print(f"{markdown_path}\n{json_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
