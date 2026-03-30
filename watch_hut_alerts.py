#!/usr/bin/env python3
import argparse
import shlex
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import book


DEFAULT_CONFIGS = [
    "config_konkordia_2026-04-03_06_alert.yaml",
    "config_finsteraarhorn_2026-04-03_06_alert.yaml",
    "config_oberaarjoch_2026-04-03_06_alert.yaml",
]


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", action="append", default=[])
    parser.add_argument("--session", default="hut_alerts")
    parser.add_argument("--check-interval-seconds", type=int, default=3600)
    parser.add_argument("--startup-grace-seconds", type=int, default=1200)
    parser.add_argument("--max-progress-age-seconds", type=int, default=1800)
    parser.add_argument("--max-state-age-seconds", type=int, default=7200)
    parser.add_argument("--state-dir", default=".alert_state")
    parser.add_argument("--log-file", default="logs/hut_alerts.log")
    parser.add_argument("--watchdog-log-file", default="logs/hut_alerts_watchdog.log")
    return parser.parse_args()


def now_text():
    return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime())


def append_log(path, message):
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{now_text()}] {message}"
    print(line, flush=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def tmux_session_exists(session):
    result = subprocess.run(
        ["tmux", "has-session", "-t", session],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def poller_running():
    result = subprocess.run(
        ["pgrep", "-af", r".venv/bin/python book.py .*--alert-only.*--poll.*--headless"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0, result.stdout.strip()


def build_expected_state_files(config_paths, state_dir):
    args = SimpleNamespace(alert_only=True)
    configs = [book.load_config(path) for path in config_paths]
    expanded = book.expand_alert_only_configs(configs, args)
    return [Path(state_dir) / f"{book.config_tag(cfg)}.json" for cfg in expanded]


def file_age_seconds(path):
    return time.time() - path.stat().st_mtime


def start_monitor(session, config_paths, log_file):
    repo_dir = Path(__file__).resolve().parent
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    Path(log_file).write_text("", encoding="utf-8")

    cmd = [".venv/bin/python", "book.py"]
    for config_path in config_paths:
        cmd.extend(["--config", config_path])
    cmd.extend(["--alert-only", "--poll", "--headless"])
    shell_cmd = (
        f"cd {shlex.quote(str(repo_dir))} && "
        f"{' '.join(shlex.quote(part) for part in cmd)} "
        f"2>&1 | tee -a {shlex.quote(log_file)}"
    )

    subprocess.run(
        ["tmux", "kill-session", "-t", session],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(["tmux", "new-session", "-d", "-s", session, shell_cmd], check=True)


def health_status(args):
    session_ok = tmux_session_exists(args.session)
    process_ok, process_text = poller_running()
    if not session_ok or not process_ok:
        reasons = []
        if not session_ok:
            reasons.append("tmux session missing")
        if not process_ok:
            reasons.append("poller process missing")
        return False, ", ".join(reasons), process_text

    log_path = Path(args.log_file)
    if log_path.exists() and file_age_seconds(log_path) <= args.startup_grace_seconds:
        return True, "startup grace", process_text

    expected = build_expected_state_files(args.config or DEFAULT_CONFIGS, args.state_dir)
    existing = [path for path in expected if path.exists()]
    if not existing:
        return False, "no alert state files found", process_text

    newest_age = min(file_age_seconds(path) for path in existing)
    if newest_age > args.max_progress_age_seconds:
        return False, f"no fresh state update in {int(newest_age)}s", process_text

    stale = [path.name for path in expected if not path.exists() or file_age_seconds(path) > args.max_state_age_seconds]
    if stale:
        return False, f"stale or missing state files: {', '.join(stale)}", process_text

    return True, "healthy", process_text


def main():
    args = parse_args()
    config_paths = args.config or DEFAULT_CONFIGS

    append_log(args.watchdog_log_file, f"watchdog started for session {args.session}")
    while True:
        healthy, reason, process_text = health_status(args)
        if healthy:
            append_log(args.watchdog_log_file, f"health ok: {reason}")
        else:
            append_log(args.watchdog_log_file, f"health failed: {reason}")
            if process_text:
                append_log(args.watchdog_log_file, f"existing process info: {process_text}")
            start_monitor(args.session, config_paths, args.log_file)
            append_log(args.watchdog_log_file, f"restarted session {args.session}")
        time.sleep(args.check_interval_seconds)


if __name__ == "__main__":
    main()
