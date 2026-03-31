#!/usr/bin/env python3
import argparse
import re
import traceback
from copy import deepcopy

from playwright.sync_api import sync_playwright

import book


DATE_RANGE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4} - \d{2}\.\d{2}\.\d{4}$")

IGNORED_RESERVATION_LINES = {
    "more_vert",
    "priority_high",
    "done",
    "lens",
}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--interval-seconds", type=int, default=120)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--confirm-submit", action="store_true")
    parser.add_argument("--pause-seconds", type=int, default=0)
    parser.add_argument("--screenshot-dir", default="screens")
    parser.add_argument("--min-party-size", type=int, default=1)
    parser.add_argument("--post-success-seconds", type=int, default=10)
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
        jitter_seconds=0,
        alert_only=False,
        notify_command=None,
        alert_state_dir=".alert_state",
        alert_force_send=False,
    )


def should_ignore_reservation_line(line):
    normalized = book.normalize_text(line)
    if not normalized:
        return True
    if normalized in IGNORED_RESERVATION_LINES:
        return True
    if normalized.startswith("hai prenotato piu capanne"):
        return True
    if normalized.startswith("ti preghiamo di annullare"):
        return True
    return False


def parse_reservation_block(date_range, lines):
    party_size = None
    booking_id = None
    status = None
    people_index = None

    for idx, line in enumerate(lines):
        if line.startswith("Nr. di persone:"):
            people_index = idx
            try:
                party_size = int(line.split(":", 1)[1].strip())
            except Exception:
                return None
        elif line.startswith("Prenotazione:"):
            booking_id = line.split(":", 1)[1].strip()
        elif line.startswith("Stato:"):
            status = line.split(":", 1)[1].strip()

    if people_index is None or party_size is None or status is None:
        return None

    hut_name = None
    for idx in range(people_index - 1, -1, -1):
        line = lines[idx].strip()
        if should_ignore_reservation_line(line):
            continue
        hut_name = line
        break

    if not hut_name:
        return None

    return {
        "date_range": date_range,
        "hut_name": hut_name,
        "party_size": party_size,
        "booking_id": booking_id,
        "status": status,
    }


def parse_reservation_list_text(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    entries = []
    idx = 0
    while idx < len(lines):
        if not DATE_RANGE_RE.match(lines[idx]):
            idx += 1
            continue
        date_range = lines[idx]
        idx += 1
        block = []
        while idx < len(lines) and not DATE_RANGE_RE.match(lines[idx]):
            block.append(lines[idx])
            idx += 1
        entry = parse_reservation_block(date_range, block)
        if entry is not None:
            entries.append(entry)
    return entries


def fetch_confirmed_party_size(config, username, password, headless):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()
        page.set_default_timeout(book.DEFAULT_TIMEOUT_MS)

        book.login_and_open_list(page, username, password, config["login_provider"])

        body = page.locator("body").inner_text()
        browser.close()

    target_range = f"{book.format_date_for_ui(config['check_in'])} - {book.format_date_for_ui(config['check_out'])}"
    target_hut = book.normalize_text(config["hut_name"]).replace(", ch", "")
    total = 0
    for entry in parse_reservation_list_text(body):
        hut_name = book.normalize_text(entry["hut_name"])
        if hut_name != target_hut:
            continue
        if entry["date_range"] != target_range:
            continue
        if book.normalize_text(entry["status"]) != "confermato":
            continue
        total += entry["party_size"]
    return total


def clone_config_with_party_size(config, party_size):
    cloned = deepcopy(config)
    cloned["party_size"] = party_size
    comments = cloned.get("stay", {}).get("comments")
    if comments:
        cloned["stay"]["comments"] = f"{comments} Progressive target bot size {party_size}."
    return cloned


def main():
    args = parse_args()
    config = book.load_config(args.config)
    target_party_size = config["party_size"]
    username, password = book.load_credentials()
    run_args = make_run_args(args)

    if args.min_party_size < 1 or args.min_party_size > target_party_size:
        raise ValueError("--min-party-size must be between 1 and config.party_size")

    attempt = 0
    cycle = 0
    while True:
        cycle += 1
        try:
            confirmed = fetch_confirmed_party_size(config, username, password, args.headless)
            remaining = target_party_size - confirmed
            print(
                f"Cycle {cycle}: confirmed={confirmed}, target={target_party_size}, remaining={remaining}",
                flush=True,
            )
            if remaining <= 0:
                print("Target reached.", flush=True)
                return
        except Exception as exc:
            print(f"Cycle {cycle}: retrying after state-check error: {exc}", flush=True)
            traceback.print_exc()
            print(f"Retrying in {args.interval_seconds}s.", flush=True)
            book.time.sleep(args.interval_seconds)
            continue

        booked_this_cycle = False
        for party_size in range(remaining, args.min_party_size - 1, -1):
            attempt += 1
            try:
                attempt_config = clone_config_with_party_size(config, party_size)
                result = book.run_attempt(attempt_config, username, password, run_args, attempt_index=attempt)
                status = (result or {}).get("status")
                print(f"Attempt {attempt}: success status={status} size={party_size}", flush=True)
                booked_this_cycle = True
                break
            except book.AvailabilityNotFoundError as exc:
                print(f"Attempt {attempt}: unavailable size={party_size}: {exc}", flush=True)
            except Exception as exc:
                print(f"Attempt {attempt}: retrying after error for size={party_size}: {exc}", flush=True)
                traceback.print_exc()
                break

        wait_time = args.post_success_seconds if booked_this_cycle else args.interval_seconds
        print(f"Retrying in {wait_time}s.", flush=True)
        book.time.sleep(wait_time)


if __name__ == "__main__":
    main()
