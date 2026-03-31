#!/usr/bin/env python3
import argparse
import traceback

import book


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--interval-seconds", type=int, default=120)
    parser.add_argument("--jitter-seconds", type=int, default=0)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--confirm-submit", action="store_true")
    parser.add_argument("--pause-seconds", type=int, default=600)
    parser.add_argument("--screenshot-dir", default="screens")
    return parser.parse_args()


def make_run_args(args):
    return argparse.Namespace(
        config=[args.config],
        dry_run=False,
        screenshot_dir=args.screenshot_dir,
        headless=args.headless,
        pause_at_payment=False,
        pause_seconds=args.pause_seconds,
        confirm_submit=args.confirm_submit,
        poll=True,
        interval_seconds=args.interval_seconds,
        max_attempts=0,
        jitter_seconds=args.jitter_seconds,
        alert_only=False,
        notify_command=None,
        alert_state_dir=".alert_state",
        alert_force_send=False,
    )


def main():
    args = parse_args()
    config = book.load_config(args.config)
    username, password = book.load_credentials()
    run_args = make_run_args(args)

    attempt = 0
    while True:
        attempt += 1
        try:
            result = book.run_attempt(config, username, password, run_args, attempt_index=attempt)
            status = (result or {}).get("status")
            print(f"Attempt {attempt}: success status={status}", flush=True)
            return
        except book.AvailabilityNotFoundError as exc:
            print(f"Attempt {attempt}: unavailable: {exc}", flush=True)
        except Exception as exc:
            print(f"Attempt {attempt}: retrying after error: {exc}", flush=True)
            traceback.print_exc()

        wait_time = args.interval_seconds
        print(f"Retrying in {wait_time}s.", flush=True)
        book.time.sleep(wait_time)


if __name__ == "__main__":
    main()
