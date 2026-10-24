"""Accumulate a target amount of actual test-command runtime without sleep padding.

Default target is two hours (7,200 seconds). Runs only offline tests/stubs and does
not access production credentials, network APIs, databases, or real-order endpoints.
This is a validation utility, not a production service entrypoint.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parent


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def base_env(db_path: str) -> dict[str, str]:
    env = dict(os.environ)
    # Never carry configured service credentials into the validation child processes.
    for key in list(env):
        upper = key.upper()
        if upper.startswith("KALSHI_") and upper not in {"KALSHI_BASE_URL"}:
            env.pop(key, None)
        elif any(token in upper for token in ("GAINZALGO", "WEBHOOK_SECRET", "API_KEY", "PRIVATE_KEY", "PEM")):
            env.pop(key, None)
    env.update({
        "DATABASE_URL": "",
        "BOT_DB_PATH": db_path,
        "TRADING_MODE": "paper",
        "LIVE_TRADING_ENABLED": "false",
        "MARKET_ANALYSIS_PROVIDER_URL": "",
        "MARKET_ANALYSIS_PROVIDER_API_KEY": "",
        "MARKET_ANALYSIS_AUTO_START": "false",
        "MARKET_SCANNER_AUTO_START": "false",
        "MARKET_AUTOPAPER_ENABLED": "false",
        "PAPER_WORKER_ENABLED": "false",
        "PYTHONUNBUFFERED": "1",
    })
    return env


def write_report(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a cumulative test-runtime soak; no sleep padding is counted.")
    parser.add_argument("--target-test-seconds", type=float, default=7200.0,
                        help="Sum of wall-clock durations for actual test commands (default: 7200 seconds).")
    parser.add_argument("--report", type=Path, default=Path("validation_soak_result.json"))
    parser.add_argument("--log", type=Path, default=Path("validation_soak.log"))
    args = parser.parse_args()
    target = max(1.0, float(args.target_test_seconds))
    report = args.report.resolve()
    log_path = args.log.resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "status": "running", "started_at_utc": stamp(), "ended_at_utc": None,
        "target_test_command_seconds": target, "completed_test_command_seconds": 0.0,
        "cycles_completed": 0, "commands_completed": 0, "command_counts": {},
        "command_seconds": {}, "failures": [], "notes": [
            "Counted durations are the actual wall-clock runtime of subprocess test commands; no sleep padding is added.",
            "Runs offline/mock tests only. Does not hit Kalshi, Render, or live order endpoints.",
        ],
    }
    command_counts: dict[str, int] = defaultdict(int)
    command_seconds: dict[str, float] = defaultdict(float)
    # A fresh local DB per pytest invocation limits cross-run state leakage. Smoke test
    # builds and destroys its own temporary DB; the other tests run offline with all
    # production credentials stripped.
    sequence: list[tuple[str, list[str]]] = [
        ("compile", [sys.executable, "-m", "compileall", "-q", "."]),
        ("pytest_full_suite", [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]),
        ("market_decision_property_stress", [sys.executable, "stress_market_decision_engine.py"]),
        ("phase456_safety_stress", [sys.executable, "stress_phase456_readiness.py"]),
        ("webhook_paper_smoke", [sys.executable, "smoke_shared_alert_flow.py"]),
        ("dashboard_js_syntax", [sys.executable, "validate_dashboard_js.py"]),
    ]
    cycle = 0
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"BTC bot cumulative test runtime soak started {state['started_at_utc']}\n")
        log.write(f"target command runtime seconds={target}\n")
        log.flush()
        while state["completed_test_command_seconds"] < target:
            cycle += 1
            for name, command in sequence:
                if state["completed_test_command_seconds"] >= target:
                    break
                with tempfile.TemporaryDirectory(prefix="btc-validation-db-") as dbdir:
                    env = base_env(str(Path(dbdir) / "journal.sqlite3"))
                    log.write(f"\n[{stamp()}] cycle={cycle} test={name} command={' '.join(command)}\n")
                    log.flush()
                    started = time.monotonic()
                    try:
                        process = subprocess.run(command, cwd=ROOT, env=env, text=True,
                                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                 timeout=900, check=False)
                        elapsed = time.monotonic() - started
                        output = process.stdout or ""
                        log.write(output)
                        if output and not output.endswith("\n"):
                            log.write("\n")
                        log.write(f"[{stamp()}] exit={process.returncode} elapsed_command_seconds={elapsed:.3f}\n")
                        log.flush()
                        state["completed_test_command_seconds"] = round(state["completed_test_command_seconds"] + elapsed, 3)
                        state["commands_completed"] += 1
                        command_counts[name] += 1
                        command_seconds[name] = round(command_seconds[name] + elapsed, 3)
                        if process.returncode != 0:
                            tail = "\n".join(output.splitlines()[-60:])
                            state["status"] = "failed"
                            state["ended_at_utc"] = stamp()
                            state["failures"].append({"cycle": cycle, "test": name, "exit_code": process.returncode,
                                                       "elapsed_seconds": round(elapsed, 3), "tail": tail})
                            state["cycles_completed"] = cycle - 1
                            state["command_counts"] = dict(command_counts)
                            state["command_seconds"] = dict(command_seconds)
                            write_report(report, state)
                            print(f"FAIL: {name} exit={process.returncode}; elapsed test command runtime={state['completed_test_command_seconds']:.1f}s", flush=True)
                            print(tail, flush=True)
                            return process.returncode or 1
                    except subprocess.TimeoutExpired as exc:
                        elapsed = time.monotonic() - started
                        state["completed_test_command_seconds"] = round(state["completed_test_command_seconds"] + elapsed, 3)
                        state["status"] = "failed_timeout"
                        state["ended_at_utc"] = stamp()
                        state["failures"].append({"cycle": cycle, "test": name, "elapsed_seconds": round(elapsed, 3),
                                                   "error": "test_command_timeout"})
                        state["command_counts"] = dict(command_counts)
                        state["command_seconds"] = dict(command_seconds)
                        write_report(report, state)
                        print(f"TIMEOUT: {name}; elapsed test command runtime={state['completed_test_command_seconds']:.1f}s", flush=True)
                        return 2
            state["cycles_completed"] = cycle
            state["command_counts"] = dict(command_counts)
            state["command_seconds"] = dict(command_seconds)
            if cycle % 3 == 0:
                write_report(report, state)
                print(f"SOAK PASS: cycle={cycle}, test-command runtime={state['completed_test_command_seconds']:.1f}/{target:.0f}s, "
                      f"full suites={command_counts['pytest_full_suite']}, stress={command_counts['phase456_safety_stress']}", flush=True)
    state["status"] = "passed" if state["completed_test_command_seconds"] >= target else "incomplete"
    state["ended_at_utc"] = stamp()
    state["command_counts"] = dict(command_counts)
    state["command_seconds"] = dict(command_seconds)
    write_report(report, state)
    print(f"SOAK {state['status'].upper()}: test-command runtime {state['completed_test_command_seconds']:.1f}s; "
          f"cycles={cycle}; full suites={command_counts['pytest_full_suite']}", flush=True)
    print(f"Report: {report}\nLog: {log_path}", flush=True)
    return 0 if state["status"] == "passed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
