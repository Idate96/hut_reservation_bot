import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
import unicodedata
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from dotenv import load_dotenv
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


BASE_URL = "https://www.hut-reservation.org"
LIST_URL = "https://www.hut-reservation.org/reservation/list"
DEFAULT_TIMEOUT_MS = 15000
WIZARD_TIMEOUT_MS = 30000

SELECTORS = {
    "login_username": "input[autocomplete='username']",
    "login_password": "input[autocomplete='current-password']",
    "login_submit": "#nextButton",
    "login_sac_button": "#sacButton",
    "sac_username": "input#person_login_identity",
    "sac_password": "input#person_password",
    "sac_submit": "button[type='submit']",
    "add_reservation_button": ".add_button, button:has-text('AGGIUNGI PRENOTAZIONE'), button:has-text('ADD RESERVATION'), button:has-text('RESERVIERUNG HINZUFUGEN'), button:has-text('RESERVIERUNG HINZUFÜGEN')",
    "hut_input": "#hutInput",
    "hut_options": "mat-option",
    "add_reservation_ok": "button:has-text('OK')",
    "date_picker_toggle": "#cy-datePicker__toggle button",
    "people_input": "input[data-test*='people-input']",
    "next_check_availability": "[data-test='button-next-check-availability']",
    "next_availability_alt": "[data-test='button-next-availability']",
    "next_overnight": "[data-test='button-next-overnight-stay']",
    "next_personal": "[data-test='button-next-personal-data']",
    "next_summary": "[data-test='button-next-summary']",
    "terms_checkbox": "input[aria-label*='GTC'], input[aria-label*='CGC'], input[aria-label*='AGB']",
    "privacy_checkbox": "input[aria-label*='Privacy'], input[aria-label*='Datenschutz']",
}

ROOM_TYPE_KEYWORDS = {
    "dorm": ["dorm", "dormitorio", "dortoir", "dormitory", "schlafsaal", "lager"],
    "private": ["priv", "zimmer", "private", "chambre", "room", "camera"],
}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, action="append")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--screenshot-dir", default="screens")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--pause-at-payment", action="store_true")
    parser.add_argument("--pause-seconds", type=int, default=0)
    parser.add_argument("--confirm-submit", action="store_true")
    parser.add_argument("--poll", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--max-attempts", type=int, default=0)
    parser.add_argument("--jitter-seconds", type=int, default=0)
    parser.add_argument("--alert-only", action="store_true")
    parser.add_argument("--notify-command")
    parser.add_argument("--alert-state-dir", default=".alert_state")
    parser.add_argument("--alert-force-send", action="store_true")
    return parser.parse_args()


def clone_args(args, **overrides):
    data = vars(args).copy()
    data.update(overrides)
    return argparse.Namespace(**data)


def require_str(data, key, context):
    if key not in data or not isinstance(data[key], str) or not data[key].strip():
        raise ValueError(f"{context}.{key} is required and must be a non-empty string")
    return data[key].strip()


def require_int(data, key, context):
    if key not in data:
        raise ValueError(f"{context}.{key} is required and must be an integer")
    try:
        value = int(data[key])
    except Exception as exc:
        raise ValueError(f"{context}.{key} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{context}.{key} must be >= 1")
    return value


def require_bool(data, key, context):
    if key not in data or not isinstance(data[key], bool):
        raise ValueError(f"{context}.{key} is required and must be a boolean")
    return data[key]


def optional_bool(data, key, default=False):
    if key not in data:
        return default
    if not isinstance(data[key], bool):
        raise ValueError(f"config.{key} must be a boolean")
    return data[key]


def optional_int(data, key, default=0):
    if key not in data or data[key] is None:
        return default
    try:
        value = int(data[key])
    except Exception as exc:
        raise ValueError(f"config.{key} must be an integer") from exc
    if value < 0:
        raise ValueError(f"config.{key} must be >= 0")
    return value


def optional_positive_int(data, key, default):
    value = optional_int(data, key, default)
    if value <= 0:
        raise ValueError(f"config.{key} must be >= 1")
    return value


def optional_str(data, key):
    if key not in data or data[key] is None:
        return None
    if not isinstance(data[key], str):
        raise ValueError(f"config.{key} must be a string")
    value = data[key].strip()
    return value if value else None


def optional_str_list(data, key, context):
    if key not in data or data[key] is None:
        return None
    value = data[key]
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",") if part.strip()]
        return items or None
    if isinstance(value, list):
        items = []
        for idx, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"{context}.{key}[{idx}] must be a non-empty string")
            items.append(item.strip())
        return items or None
    raise ValueError(f"{context}.{key} must be a string or list of strings")


def require_dict(data, key, context):
    if key not in data or not isinstance(data[key], dict):
        raise ValueError(f"{context}.{key} is required and must be a dict")
    return data[key]


def parse_date(value, key):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except Exception as exc:
        raise ValueError(f"{key} must be in YYYY-MM-DD format") from exc


def load_config(path):
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError("Config must be a YAML mapping")

    login_provider = require_str(data, "login_provider", "config").lower()
    if login_provider not in {"default", "sac"}:
        raise ValueError("config.login_provider must be 'default' or 'sac'")

    hut_name = require_str(data, "hut_name", "config")
    check_in_str = require_str(data, "check_in", "config")
    check_out_str = require_str(data, "check_out", "config")
    party_size = require_int(data, "party_size", "config")

    check_in = parse_date(check_in_str, "check_in")
    check_out = parse_date(check_out_str, "check_out")
    if check_out <= check_in:
        raise ValueError("check_out must be after check_in")

    contact = require_dict(data, "contact", "config")
    contact_out = {
        "first_name": require_str(contact, "first_name", "contact"),
        "last_name": require_str(contact, "last_name", "contact"),
        "email": require_str(contact, "email", "contact"),
        "phone": require_str(contact, "phone", "contact"),
        "address_line1": require_str(contact, "address_line1", "contact"),
        "city": require_str(contact, "city", "contact"),
        "postal_code": require_str(contact, "postal_code", "contact"),
        "country": require_str(contact, "country", "contact"),
    }

    preferences = data.get("preferences") or {}
    if not isinstance(preferences, dict):
        raise ValueError("preferences must be a dict if provided")
    preferences_out = {
        "room_type": preferences.get("room_type"),
        "remarks": preferences.get("remarks"),
    }

    alert = data.get("alert") or {}
    if not isinstance(alert, dict):
        raise ValueError("alert must be a dict if provided")

    half_board = require_bool(data, "half_board", "config")
    allow_alternative_dates = optional_bool(data, "allow_alternative_dates", default=False)
    allow_waitlist = optional_bool(data, "allow_waitlist", default=False)
    if "auto_poll_if_full" in data:
        auto_poll_if_full = optional_bool(data, "auto_poll_if_full", default=False)
    else:
        auto_poll_if_full = not allow_waitlist
    poll_interval_seconds = optional_positive_int(data, "poll_interval_seconds", 300)
    poll_jitter_seconds = optional_int(data, "poll_jitter_seconds", 0)
    poll_max_attempts = optional_int(data, "poll_max_attempts", 0)
    if poll_jitter_seconds < 0:
        raise ValueError("config.poll_jitter_seconds must be >= 0")
    if poll_max_attempts < 0:
        raise ValueError("config.poll_max_attempts must be >= 0")

    stay_out = {
        "children_count": optional_int(data, "children_count", 0),
        "guides_count": optional_int(data, "guides_count", 0),
        "vegetarian_count": optional_int(data, "vegetarian_count", 0),
        "lunch_packages": optional_int(data, "lunch_packages", 0),
        "group_name": optional_str(data, "group_name"),
        "access_to_hut": optional_str(data, "access_to_hut"),
        "allergies": optional_str(data, "allergies"),
        "comments": optional_str(data, "comments"),
        "direction": optional_str(data, "direction"),
    }

    if "accept_terms" not in data:
        raise ValueError("config.accept_terms is required and must be true")
    if data["accept_terms"] is not True:
        raise ValueError("config.accept_terms must be true to proceed")

    return {
        "login_provider": login_provider,
        "hut_name": hut_name,
        "check_in": check_in_str,
        "check_out": check_out_str,
        "party_size": party_size,
        "contact": contact_out,
        "preferences": preferences_out,
        "half_board": half_board,
        "allow_alternative_dates": allow_alternative_dates,
        "allow_waitlist": allow_waitlist,
        "require_positive_free_places": optional_bool(data, "require_positive_free_places", default=False),
        "auto_poll_if_full": auto_poll_if_full,
        "poll_interval_seconds": poll_interval_seconds,
        "poll_jitter_seconds": poll_jitter_seconds,
        "poll_max_attempts": poll_max_attempts,
        "stay": stay_out,
        "alert": {
            "to": optional_str_list(alert, "to", "alert") or [contact_out["email"]],
            "command": optional_str(alert, "command"),
            "any_party_size": optional_bool(alert, "any_party_size", default=True),
            "any_night": optional_bool(alert, "any_night", default=False),
        },
    }


def load_credentials():
    load_dotenv()
    username = os.getenv("HUT_USERNAME")
    password = os.getenv("HUT_PASSWORD")
    if not username or not password:
        raise ValueError("HUT_USERNAME and HUT_PASSWORD must be set in .env")
    return username, password


def expand_alert_only_configs(configs, args):
    if not args.alert_only:
        return configs

    expanded = []
    for config in configs:
        if not config["alert"].get("any_night"):
            expanded.append(config)
            continue

        start = parse_date(config["check_in"], "check_in")
        end = parse_date(config["check_out"], "check_out")
        current = start
        while current < end:
            nightly = deepcopy(config)
            next_day = current + timedelta(days=1)
            nightly["check_in"] = current.isoformat()
            nightly["check_out"] = next_day.isoformat()
            nightly["_alert_parent_range"] = {
                "check_in": config["check_in"],
                "check_out": config["check_out"],
            }
            expanded.append(nightly)
            current = next_day
    return expanded


def snap(page, screenshot_dir, step, label):
    if screenshot_dir is None:
        return step
    path = screenshot_dir / f"{step + 1:02d}_{label}.png"
    page.screenshot(path=str(path), full_page=True)
    return step + 1


def must_locator(page, selector, name, timeout_ms):
    locator = page.locator(selector)
    try:
        locator.first.wait_for(state="visible", timeout=timeout_ms)
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(f"Missing or hidden element for {name}: {selector}") from exc
    return locator


def set_value(locator, value):
    locator.first.click()
    locator.first.fill(str(value))


def click_submit(locator):
    locator.first.click(force=True, no_wait_after=True)


def set_select_or_input(locator, value):
    tag = locator.first.evaluate("el => el.tagName.toLowerCase()")
    if tag == "select":
        locator.first.select_option(str(value))
    else:
        locator.first.click()
        locator.first.fill(str(value))


class AvailabilityNotFoundError(RuntimeError):
    pass


def format_date_for_ui(date_str):
    date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
    return date_obj.strftime("%d.%m.%Y")


def parse_calendar_period(text):
    """
    Parse the month/year label shown by the datepicker.
    Supports numeric labels like "02/2026" and month-name labels like
    "febbraio 2026" or "February 2026".
    Returns (year, month).
    """
    value = text.strip().lower()
    match = re.match(r"^(\d{1,2})/(\d{4})$", value)
    if match:
        month = int(match.group(1))
        year = int(match.group(2))
        return year, month

    month_map = {
        "gennaio": 1,
        "febbraio": 2,
        "marzo": 3,
        "aprile": 4,
        "maggio": 5,
        "giugno": 6,
        "luglio": 7,
        "agosto": 8,
        "settembre": 9,
        "ottobre": 10,
        "novembre": 11,
        "dicembre": 12,
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
    }
    parts = value.split()
    if len(parts) == 2 and parts[0] in month_map and parts[1].isdigit():
        return int(parts[1]), month_map[parts[0]]
    raise RuntimeError(f"Unrecognized calendar period label: '{text}'")


def ensure_calendar_month(page, target_date):
    """
    Navigate the datepicker to the month of target_date by clicking next/prev.
    Assumes the datepicker is already open.
    """
    def wait_overlay_clear():
        overlay = page.locator(".overlay")
        if overlay.count() == 0:
            return
        try:
            overlay.first.wait_for(state="hidden", timeout=3000)
        except PlaywrightTimeoutError:
            return

    def click_calendar_button(locator, js_selector):
        wait_overlay_clear()
        if locator.count() == 0:
            page.evaluate(
                "(selector) => { const el = document.querySelector(selector); if (el) el.click(); }",
                js_selector,
            )
            page.wait_for_timeout(250)
            return
        try:
            locator.first.click(timeout=DEFAULT_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            try:
                locator.first.click(force=True, timeout=DEFAULT_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                page.evaluate(
                    "(selector) => { const el = document.querySelector(selector); if (el) el.click(); }",
                    js_selector,
                )
        page.wait_for_timeout(250)

    target = datetime.strptime(target_date, "%Y-%m-%d").date()
    next_btn = page.locator("button.mat-calendar-next-button, button[aria-label='Next month']")
    prev_btn = page.locator("button.mat-calendar-previous-button, button[aria-label='Previous month']")
    for _ in range(24):
        period = must_locator(page, ".mat-calendar-period-button", "calendar_period_button", DEFAULT_TIMEOUT_MS).first
        current_text = period.inner_text().strip()
        current_year, current_month = parse_calendar_period(current_text)
        if (current_year, current_month) == (target.year, target.month):
            return
        if (current_year, current_month) < (target.year, target.month):
            click_calendar_button(next_btn, "button.mat-calendar-next-button, button[aria-label='Next month']")
        else:
            click_calendar_button(prev_btn, "button.mat-calendar-previous-button, button[aria-label='Previous month']")
    raise RuntimeError("Unable to navigate calendar to target month")


def wait_for_booking_wizard(page, timeout_ms=WIZARD_TIMEOUT_MS):
    """
    After selecting a hut and clicking OK from the reservation list modal, the UI
    should navigate into the booking wizard (e.g. /reservation/book-hut/.../wizard).
    Without this wait, we can accidentally interact with the reservation list filters
    (which also include date pickers) and get inconsistent results.
    """
    deadline = time.time() + (timeout_ms / 1000)
    while time.time() < deadline:
        url = page.url or ""
        if "/reservation/book-hut/" in url and "/wizard" in url:
            return
        if page.locator(SELECTORS["next_check_availability"]).count() > 0:
            return
        if page.locator("text=/Controlla disponibilit[aà]/i").count() > 0:
            return
        page.wait_for_timeout(250)
    raise RuntimeError(f"Did not reach booking wizard after hut selection. Current URL: {page.url}")


def ensure_authenticated_list(page, timeout_ms=45000):
    deadline = time.time() + (timeout_ms / 1000)
    last_url = page.url or ""
    page.wait_for_timeout(1500)
    while time.time() < deadline:
        page.goto(LIST_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1000)
        last_url = page.url or ""
        if last_url.startswith(LIST_URL):
            return
    raise RuntimeError(f"Login did not establish an authenticated session. Last URL: {last_url}")


def login_and_open_list(page, username, password, login_provider, max_attempts=2):
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            page.goto(f"{BASE_URL}/login", wait_until="domcontentloaded")
            if login_provider == "sac":
                sac_button = must_locator(page, SELECTORS["login_sac_button"], "login_sac_button", DEFAULT_TIMEOUT_MS)
                sac_button.first.click()
                page.wait_for_load_state("domcontentloaded")

                user_input = must_locator(page, SELECTORS["sac_username"], "sac_username", DEFAULT_TIMEOUT_MS)
                pass_input = must_locator(page, SELECTORS["sac_password"], "sac_password", DEFAULT_TIMEOUT_MS)
                set_value(user_input, username)
                set_value(pass_input, password)
                click_submit(must_locator(page, SELECTORS["sac_submit"], "sac_submit", DEFAULT_TIMEOUT_MS))
            else:
                user_input = must_locator(page, SELECTORS["login_username"], "login_username", DEFAULT_TIMEOUT_MS)
                pass_input = must_locator(page, SELECTORS["login_password"], "login_password", DEFAULT_TIMEOUT_MS)
                set_value(user_input, username)
                set_value(pass_input, password)
                click_submit(must_locator(page, SELECTORS["login_submit"], "login_submit", DEFAULT_TIMEOUT_MS))

            ensure_authenticated_list(page)
            return
        except RuntimeError as exc:
            last_exc = exc
            message = str(exc)
            retryable = (
                "Login did not establish an authenticated session" in message
                or "Missing or hidden element for sac_username" in message
                or "Missing or hidden element for sac_password" in message
                or "Missing or hidden element for login_username" in message
                or "Missing or hidden element for login_password" in message
            )
            if not retryable or attempt >= max_attempts:
                raise
            page.wait_for_timeout(1000)

    if last_exc is not None:
        raise last_exc


def choose_hut_option(page, hut_name):
    hut_input = must_locator(page, SELECTORS["hut_input"], "hut_input", DEFAULT_TIMEOUT_MS)
    target_norm = normalize_text(hut_name)

    def set_search_query(query):
        hut_input.first.click()
        hut_input.first.fill("")
        page.wait_for_timeout(100)
        page.keyboard.type(query, delay=35)
        page.wait_for_timeout(500)

    def candidate_queries():
        queries = []

        def add(value):
            if not value:
                return
            value = value.strip()
            if len(value) < 4:
                return
            if value not in queries:
                queries.append(value)

        add(hut_name)
        before_comma = hut_name.split(",")[0].strip()
        add(before_comma)
        before_suffix = re.sub(r"\s+SAC.*$", "", before_comma, flags=re.IGNORECASE).strip()
        add(before_suffix)
        first_word = before_suffix.split()[0] if before_suffix else None
        add(first_word)
        base = before_suffix or before_comma or hut_name
        for length in (16, 12, 8, 5, 4):
            if len(base) >= length:
                add(base[:length])
        return queries

    last_options = []
    for query in candidate_queries():
        set_search_query(query)
        options = page.locator(SELECTORS["hut_options"])
        try:
            page.wait_for_selector(SELECTORS["hut_options"], timeout=2500)
        except PlaywrightTimeoutError:
            continue

        option_texts = [options.nth(i).inner_text().strip() for i in range(options.count())]
        if not option_texts:
            continue
        last_options = option_texts
        option_norms = [normalize_text(text) for text in option_texts]

        exact_matches = [i for i, text in enumerate(option_norms) if text == target_norm]
        if len(exact_matches) == 1:
            options.nth(exact_matches[0]).click()
            page.wait_for_timeout(250)
            return option_texts[exact_matches[0]]

        contains_matches = [i for i, text in enumerate(option_norms) if target_norm in text]
        if len(contains_matches) == 1:
            options.nth(contains_matches[0]).click()
            page.wait_for_timeout(250)
            return option_texts[contains_matches[0]]

    if last_options:
        raise RuntimeError(f"Ambiguous hut selection for '{hut_name}'. Options: {last_options}")
    raise RuntimeError(f"No hut options available after search for '{hut_name}'")


def confirm_hut_selection(page):
    ok_button = must_locator(page, SELECTORS["add_reservation_ok"], "add_reservation_ok", DEFAULT_TIMEOUT_MS).first
    for attempt in range(3):
        try:
            ok_button.click(timeout=DEFAULT_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            ok_button.click(force=True)
        try:
            wait_for_booking_wizard(page, timeout_ms=8000 if attempt < 2 else WIZARD_TIMEOUT_MS)
            return
        except RuntimeError:
            if "/reservation/book-hut/" in (page.url or ""):
                return
            page.keyboard.press("Enter")
            page.wait_for_timeout(600)
    wait_for_booking_wizard(page, timeout_ms=WIZARD_TIMEOUT_MS)


def select_date_range(page, check_in, check_out):
    toggle = must_locator(page, SELECTORS["date_picker_toggle"], "date_picker_toggle", DEFAULT_TIMEOUT_MS)
    try:
        toggle.first.scroll_into_view_if_needed()
    except Exception:
        pass
    try:
        toggle.first.click(timeout=DEFAULT_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        toggle.first.click(force=True)

    if not wait_for_visible(page, ".mat-calendar-period-button", timeout_ms=2500):
        date_input = page.locator("input[placeholder*='Data'], input[aria-label*='Data']")
        if date_input.count() > 0:
            try:
                date_input.first.click(timeout=DEFAULT_TIMEOUT_MS)
            except Exception:
                date_input.first.click(force=True)
        if not wait_for_visible(page, ".mat-calendar-period-button", timeout_ms=2500):
            page.wait_for_selector(".mat-calendar, .mat-datepicker-content", timeout=DEFAULT_TIMEOUT_MS)
            must_locator(page, ".mat-calendar-period-button", "calendar_period_button", DEFAULT_TIMEOUT_MS)
    for date_str in [check_in, check_out]:
        ensure_calendar_month(page, date_str)
        ui_date = format_date_for_ui(date_str)
        selector = f"button.custom-date[class*='{ui_date}']"
        cell = must_locator(page, selector, f"date_{ui_date}", DEFAULT_TIMEOUT_MS)
        if cell.first.get_attribute("aria-disabled") == "true":
            raise AvailabilityNotFoundError(f"Date not available: {ui_date}")
        cell.first.click()
        page.wait_for_timeout(200)
    page.keyboard.press("Escape")


def first_visible_locator(locator):
    for i in range(locator.count()):
        try:
            if locator.nth(i).is_visible():
                return locator.nth(i)
        except Exception:
            continue
    return None


def expand_people_panel(page):
    # Some huts hide the per-category people inputs behind an expansion panel titled
    # "Num. di persone". Expanding it proactively avoids false "no input" failures.
    headers = page.locator("mat-expansion-panel-header")
    for i in range(headers.count()):
        header = headers.nth(i)
        try:
            text = normalize_text(header.inner_text())
        except Exception:
            continue
        if any(normalize_text(label) in text for label in PEOPLE_TOTAL_LABELS):
            panel = header.locator("xpath=ancestor-or-self::mat-expansion-panel[1]")
            try:
                panel_class = panel.get_attribute("class") or ""
            except Exception:
                panel_class = ""
            if "mat-expanded" not in panel_class:
                try:
                    header.click()
                    page.wait_for_timeout(250)
                except Exception:
                    return None
            return panel
    return None


def people_input_context_text(input_loc):
    parts = []
    try:
        parts.append(input_loc.get_attribute("aria-label") or "")
    except Exception:
        pass
    try:
        parts.append(input_loc.get_attribute("placeholder") or "")
    except Exception:
        pass
    try:
        field = input_loc.locator("xpath=ancestor-or-self::mat-form-field[1]")
        if field.count() > 0:
            parts.append(field.first.inner_text())
    except Exception:
        pass
    try:
        panel_header = input_loc.locator(
            "xpath=ancestor-or-self::mat-expansion-panel[1]//mat-expansion-panel-header"
        )
        if panel_header.count() > 0:
            parts.append(panel_header.first.inner_text())
    except Exception:
        pass
    return normalize_text(" ".join(parts))


def people_input_label(input_loc, index):
    try:
        aria = (input_loc.get_attribute("aria-label") or "").strip()
    except Exception:
        aria = ""
    if aria:
        label = aria.split(":", 1)[0].strip()
        if label:
            return label
    return f"Option {index}"


def visible_people_inputs(page):
    inputs = page.locator(SELECTORS["people_input"])
    if inputs.count() == 0:
        expand_people_panel(page)
        inputs = page.locator(SELECTORS["people_input"])
    visible = []
    for i in range(inputs.count()):
        candidate = inputs.nth(i)
        try:
            if candidate.is_visible():
                visible.append(candidate)
        except Exception:
            continue
    return visible


def wait_for_visible_people_inputs(page, timeout_ms=5000):
    deadline = time.time() + (timeout_ms / 1000)
    while time.time() < deadline:
        inputs = visible_people_inputs(page)
        if inputs:
            return inputs
        page.wait_for_timeout(250)
    return visible_people_inputs(page)


def reclassify_party_size_error(exc):
    message = str(exc or "").strip()
    if message == "No people input found":
        return AvailabilityNotFoundError(message)
    if message.startswith("Field 'room_type_people") and message.endswith("is not visible"):
        return AvailabilityNotFoundError("No people input found")
    return None


def choose_people_input(page, room_type):
    def select_room_category_option(room_type_value):
        keywords = ROOM_TYPE_KEYWORDS.get(room_type_value, [room_type_value])
        keywords = [normalize_text(k) for k in keywords if k]
        if not keywords:
            return False

        root = expand_people_panel(page)
        root = root if root is not None else page

        comboboxes = root.locator("[role='combobox']")
        for i in range(comboboxes.count()):
            cb = comboboxes.nth(i)
            try:
                if not cb.is_visible():
                    continue
            except Exception:
                continue

            # Open the dropdown and look for a unique option containing the room-type keyword.
            try:
                cb.click()
            except Exception:
                try:
                    cb.click(force=True)
                except Exception:
                    continue
            try:
                page.wait_for_selector("mat-option", timeout=2000)
            except PlaywrightTimeoutError:
                page.keyboard.press("Escape")
                continue

            options = page.locator("mat-option")
            option_texts = []
            for j in range(options.count()):
                try:
                    option_texts.append(options.nth(j).inner_text().strip())
                except Exception:
                    option_texts.append("")
            option_norms = [normalize_text(t) for t in option_texts]
            matches = [j for j, t in enumerate(option_norms) if any(k in t for k in keywords)]

            if len(matches) == 0:
                page.keyboard.press("Escape")
                continue
            if len(matches) > 1:
                raise RuntimeError(
                    f"Ambiguous room category selection for room_type '{room_type_value}'. Options: {option_texts}"
                )

            options.nth(matches[0]).click()
            page.wait_for_timeout(250)
            return True

        return False

    if room_type:
        keywords = ROOM_TYPE_KEYWORDS.get(room_type, [room_type])

        panels = page.locator("mat-expansion-panel")
        for i in range(panels.count()):
            header = panels.nth(i).locator("mat-expansion-panel-header")
            try:
                header_text = header.inner_text().strip().lower()
            except Exception:
                continue
            if any(keyword in header_text for keyword in keywords):
                panel_class = panels.nth(i).get_attribute("class") or ""
                if "mat-expanded" not in panel_class:
                    header.click()
                    page.wait_for_timeout(200)
                panel_inputs = panels.nth(i).locator("input")
                selected = first_visible_locator(panel_inputs)
                if selected is None:
                    raise RuntimeError(f"No people input found inside room panel '{header_text}'")
                return selected

        fields = page.locator("mat-form-field")
        for i in range(fields.count()):
            label_loc = fields.nth(i).locator("mat-label")
            if label_loc.count() == 0:
                continue
            try:
                label_text = label_loc.first.inner_text().strip().lower()
            except Exception:
                continue
            if any(keyword in label_text for keyword in keywords):
                field_inputs = fields.nth(i).locator("input")
                selected = first_visible_locator(field_inputs)
                if selected is None:
                    raise RuntimeError(f"Room field '{label_text}' has no input")
                return selected

    inputs = page.locator(SELECTORS["people_input"])
    if inputs.count() == 0:
        expand_people_panel(page)
        inputs = page.locator(SELECTORS["people_input"])
    if inputs.count() == 0:
        raise RuntimeError("No people input found")
    if room_type:
        keywords = ROOM_TYPE_KEYWORDS.get(room_type, [room_type])
        keywords_norm = [normalize_text(k) for k in keywords if k]

        matches = []
        for i in range(inputs.count()):
            ctx = people_input_context_text(inputs.nth(i))
            if any(k in ctx for k in keywords_norm):
                matches.append(inputs.nth(i))
        # If we cannot match directly, some huts require selecting a category (mat-select)
        # which then injects the room-type keyword into the field context.
        if not matches and select_room_category_option(room_type):
            inputs = page.locator(SELECTORS["people_input"])
            for i in range(inputs.count()):
                ctx = people_input_context_text(inputs.nth(i))
                if any(k in ctx for k in keywords_norm):
                    matches.append(inputs.nth(i))

        visible_matches = []
        for cand in matches:
            try:
                if cand.is_visible():
                    visible_matches.append(cand)
            except Exception:
                continue
        if len(visible_matches) == 1:
            return visible_matches[0]

        # Pragmatic fallback: if there's exactly one visible people input, use it.
        # This covers huts where the room categories are not named as "dormitorio/zimmer"
        # but the only available option is a dorm-style room.
        visible_any = []
        for i in range(inputs.count()):
            cand = inputs.nth(i)
            try:
                if cand.is_visible():
                    visible_any.append(cand)
            except Exception:
                continue
        if len(visible_any) == 1:
            return visible_any[0]

        raise RuntimeError(f"No people input matched room_type '{room_type}'")
    visible_any = []
    for i in range(inputs.count()):
        cand = inputs.nth(i)
        try:
            if cand.is_visible():
                visible_any.append(cand)
        except Exception:
            continue
    if len(visible_any) == 1:
        return visible_any[0]
    if not visible_any:
        raise RuntimeError("No people input found")
    raise RuntimeError("Multiple people inputs available; set preferences.room_type")


def fill_input_or_validate(locator, value, field_name):
    if locator is None:
        return
    try:
        locator.scroll_into_view_if_needed()
    except Exception:
        pass
    try:
        if not locator.is_visible():
            raise RuntimeError(f"Field '{field_name}' is not visible")
    except Exception as exc:
        if isinstance(exc, RuntimeError):
            raise
    if locator.is_enabled():
        locator.click()
        locator.fill(str(value))
        return
    current = (locator.input_value() or "").strip()
    if str(value) not in current:
        raise RuntimeError(f"Field '{field_name}' is disabled and does not match value '{value}'")


def fill_by_placeholder(page, placeholder, value):
    if value is None:
        return
    locator = page.locator(f"input[placeholder='{placeholder}'], textarea[placeholder='{placeholder}']")
    if locator.count() == 0:
        raise RuntimeError(f"Missing field with placeholder '{placeholder}'")
    locator.first.fill(str(value))


def normalize_text(value):
    value = value or ""
    # Make matching resilient to accents/umlauts in hut names and labels (e.g. "hütte" vs "hutte").
    value = "".join(
        ch for ch in unicodedata.normalize("NFKD", value) if unicodedata.category(ch) != "Mn"
    )
    return re.sub(r"\s+", " ", value).strip().lower()


def scroll_all_scrollables(page, direction="bottom", passes=6, pause_ms=200):
    """
    hut-reservation.org uses nested scroll containers (Angular Material). Playwright's
    full-page screenshot and document scroll often do not touch these, and some UI
    sections (like the waiting-list checkbox) only appear after scrolling.
    """
    js = """
    (direction) => {
        const nodes = [];
        const add = (el) => { if (el && !nodes.includes(el)) nodes.push(el); };

        add(document.scrollingElement);
        add(document.documentElement);
        add(document.body);

        const common = [
            ".mat-drawer-content",
            "mat-sidenav-content",
            ".mat-dialog-content",
            ".cdk-virtual-scroll-viewport",
            "main",
        ];
        for (const sel of common) {
            for (const el of Array.from(document.querySelectorAll(sel))) add(el);
        }

        const all = Array.from(document.querySelectorAll("body *"));
        for (const el of all) {
            try {
                const style = window.getComputedStyle(el);
                if (!style) continue;
                if (!["auto", "scroll"].includes(style.overflowY)) continue;
                if (el.scrollHeight <= el.clientHeight + 50) continue;
                add(el);
            } catch (e) {}
        }

        let changed = false;
        for (const el of nodes) {
            if (!el) continue;
            const before = el.scrollTop;
            if (direction === "bottom") el.scrollTop = el.scrollHeight;
            else if (direction === "top") el.scrollTop = 0;
            else if (typeof direction === "number") el.scrollTop = before + direction;
            if (el.scrollTop !== before) changed = true;
        }
        return changed;
    }
    """
    for _ in range(max(1, int(passes))):
        try:
            changed = bool(page.evaluate(js, direction))
        except Exception:
            changed = False
        page.wait_for_timeout(pause_ms)
        if not changed:
            break


def clear_overlay_backdrops(page, timeout_ms=2000):
    deadline = time.time() + (timeout_ms / 1000)
    while time.time() < deadline:
        backdrops = page.locator(".cdk-overlay-backdrop")
        visible_backdrop = None
        for i in range(backdrops.count()):
            candidate = backdrops.nth(i)
            try:
                if candidate.is_visible():
                    visible_backdrop = candidate
                    break
            except Exception:
                continue
        if visible_backdrop is None:
            return
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        page.wait_for_timeout(150)
        try:
            visible_backdrop.click(force=True, timeout=500)
        except Exception:
            pass
        page.wait_for_timeout(200)


def click_with_overlay_retry(page, locator, timeout_ms=DEFAULT_TIMEOUT_MS):
    clear_overlay_backdrops(page)
    try:
        locator.click(timeout=timeout_ms)
        return
    except PlaywrightTimeoutError:
        clear_overlay_backdrops(page)
    try:
        locator.click(force=True, timeout=timeout_ms)
        return
    except PlaywrightTimeoutError:
        clear_overlay_backdrops(page)
        locator.evaluate("(el) => el.click()")


def slugify(value):
    normalized = normalize_text(value)
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized)
    return normalized.strip("_")


def normalize_date_text(value):
    value = (value or "").strip()
    value = value.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")
    value = value.replace("\u2019", "'")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def extract_ui_dates(text):
    return re.findall(r"\b\d{2}\.\d{2}\.\d{4}\b", normalize_date_text(text))


def find_date_range_inputs(page):
    toggle = page.locator(SELECTORS["date_picker_toggle"])
    if toggle.count() > 0:
        field = toggle.first.locator("xpath=ancestor::mat-form-field[1]")
        if field.count() > 0:
            inputs = field.first.locator("input")
            visible = []
            for i in range(inputs.count()):
                candidate = inputs.nth(i)
                try:
                    if candidate.is_visible():
                        visible.append(candidate)
                except Exception:
                    continue
            if visible:
                return visible

    labeled = page.locator("mat-form-field", has=page.locator("mat-label", has_text=re.compile(r"Data", re.I)))
    for i in range(labeled.count()):
        inputs = labeled.nth(i).locator("input")
        visible = []
        for j in range(inputs.count()):
            candidate = inputs.nth(j)
            try:
                if candidate.is_visible():
                    visible.append(candidate)
            except Exception:
                continue
        if visible:
            return visible

    date_range = page.locator("mat-date-range-input input")
    if date_range.count() > 0:
        visible = []
        for i in range(date_range.count()):
            candidate = date_range.nth(i)
            try:
                if candidate.is_visible():
                    visible.append(candidate)
            except Exception:
                continue
        if visible:
            return visible

    candidates = page.locator("input[placeholder*='Data'], input[aria-label*='Data']")
    visible = []
    for i in range(candidates.count()):
        candidate = candidates.nth(i)
        try:
            if candidate.is_visible():
                visible.append(candidate)
        except Exception:
            continue
    if visible:
        return visible
    raise RuntimeError("Date range input(s) not found on availability step")


def read_date_range_ui_dates(page):
    inputs = find_date_range_inputs(page)
    raw_parts = []
    for input_loc in inputs:
        try:
            raw_parts.append(input_loc.input_value() or "")
        except Exception:
            raw_parts.append(input_loc.get_attribute("value") or "")
    raw = " ".join(raw_parts)
    dates = extract_ui_dates(raw)
    return dates[:2], normalize_date_text(raw)


def ensure_expected_date_range(page, check_in, check_out, allow_alternative_dates):
    expected_start = format_date_for_ui(check_in)
    expected_end = format_date_for_ui(check_out)
    expected = f"{expected_start} - {expected_end}"

    dates, raw = read_date_range_ui_dates(page)
    if len(dates) < 2:
        deadline = time.time() + 3.0
        while time.time() < deadline:
            page.wait_for_timeout(200)
            dates2, raw2 = read_date_range_ui_dates(page)
            if len(dates2) >= 2:
                dates, raw = dates2, raw2
                break
    if len(dates) >= 2 and dates[0] == expected_start and dates[1] == expected_end:
        return
    if len(dates) == 1 and dates[0] == expected_start:
        # Some pages briefly expose only the start date until the UI fully hydrates.
        page.wait_for_timeout(400)
        dates2, raw2 = read_date_range_ui_dates(page)
        if len(dates2) >= 2 and dates2[0] == expected_start and dates2[1] == expected_end:
            return
        dates, raw = dates2, raw2
    if allow_alternative_dates:
        return

    select_date_range(page, check_in, check_out)
    deadline = time.time() + 3.0
    dates, raw = read_date_range_ui_dates(page)
    while len(dates) < 2 and time.time() < deadline:
        page.wait_for_timeout(200)
        dates, raw = read_date_range_ui_dates(page)
    if not (len(dates) >= 2 and dates[0] == expected_start and dates[1] == expected_end):
        raise RuntimeError(f"Date range changed unexpectedly. Expected '{expected}', got '{raw}'.")


def first_enabled_or_visible(page, selectors, name):
    visible_fallback = None
    for selector in selectors:
        locator = page.locator(selector)
        for idx in range(locator.count()):
            candidate = locator.nth(idx)
            try:
                if not candidate.is_visible():
                    continue
                if visible_fallback is None:
                    visible_fallback = candidate
                if not candidate.is_disabled():
                    return candidate
            except Exception:
                continue
    if visible_fallback is not None:
        return visible_fallback
    raise RuntimeError(f"{name} not found. Tried: {selectors}")


def find_availability_next_button(page):
    selectors = [
        SELECTORS["next_check_availability"],
        SELECTORS["next_availability_alt"],
        "button:has-text('WEITER')",
        "button:has-text('Weiter')",
        "button:has-text('AVANTI')",
        "button:has-text('Continua')",
    ]
    return first_enabled_or_visible(page, selectors, "availability_next_button")


def wait_for_availability_next_enabled(page, timeout_ms=4000):
    deadline = time.time() + (timeout_ms / 1000)
    button = find_availability_next_button(page)
    while time.time() < deadline:
        try:
            if not button.is_disabled():
                return button
        except Exception:
            pass
        page.wait_for_timeout(250)
        button = find_availability_next_button(page)
    return button


def find_waitlist_container(page):
    candidates = [
        "text=/lista d['\\u2019 ]?attesa/i",
        "text=/waiting list/i",
    ]
    for sel in candidates:
        loc = page.locator(sel)
        if loc.count() > 0:
            return loc.first
    return None


def label_matches(text, labels):
    normalized = normalize_text(text)
    for label in labels:
        target = normalize_text(label)
        if not target:
            continue
        if normalized == target:
            return True
        pattern = rf"(^|[^a-z0-9]){re.escape(target)}([^a-z0-9]|$)"
        if re.search(pattern, normalized):
            return True
    return False


def find_input_by_labels(page, labels):
    def visible(locator):
        try:
            return locator.is_visible()
        except Exception:
            return False

    for label in labels:
        locator = page.locator(f"input[placeholder='{label}'], textarea[placeholder='{label}']")
        if locator.count() > 0 and visible(locator.first):
            return locator.first

    inputs = page.locator("input, textarea")
    for i in range(inputs.count()):
        placeholder = inputs.nth(i).get_attribute("placeholder") or ""
        if label_matches(placeholder, labels) and visible(inputs.nth(i)):
            return inputs.nth(i)

    for i in range(inputs.count()):
        aria = inputs.nth(i).get_attribute("aria-label") or ""
        if label_matches(aria, labels) and visible(inputs.nth(i)):
            return inputs.nth(i)

    fields = page.locator("mat-form-field")
    for i in range(fields.count()):
        label = fields.nth(i).locator("mat-label")
        if label.count() == 0:
            continue
        try:
            label_text = label.first.inner_text()
        except Exception:
            continue
        if label_matches(label_text, labels):
            field_inputs = fields.nth(i).locator("input, textarea")
            if field_inputs.count() == 0:
                continue
            if visible(field_inputs.first):
                return field_inputs.first

    labels_loc = page.locator("label")
    for i in range(labels_loc.count()):
        try:
            label_text = labels_loc.nth(i).inner_text()
        except Exception:
            continue
        if not label_matches(label_text, labels):
            continue
        for_id = labels_loc.nth(i).get_attribute("for")
        if for_id:
            field = page.locator(f"#{for_id}")
            if field.count() > 0 and visible(field.first):
                return field.first

    return None


PEOPLE_TOTAL_LABELS = [
    "Num. di persone",
    "Numero di persone",
    "Number of people",
    "Persone",
    "Persons",
]


def find_total_people_input(page):
    return find_input_by_labels(page, PEOPLE_TOTAL_LABELS)


def set_party_size_inputs(page, party_size, room_type):
    room_input = choose_people_input(page, room_type)
    fill_input_or_validate(room_input, party_size, "room_type_people")
    total_input = find_total_people_input(page)
    fill_input_or_validate(total_input, party_size, "total_people")
    return room_input


def probe_any_room_free_places(page, party_size):
    inputs = wait_for_visible_people_inputs(page)
    if not inputs:
        raise RuntimeError("No people input found")

    if len(inputs) == 1:
        label = people_input_label(inputs[0], 1)
        set_party_size_inputs(page, party_size, None)
        page.keyboard.press("Tab")
        page.wait_for_timeout(500)
        free_places = wait_for_room_free_places(page, label)
        if free_places == 0:
            free_places = None
        room_counts = []
        if free_places:
            room_counts.append({"label": label, "free_places": free_places})
        return {"free_places": free_places, "room_counts": room_counts}

    room_counts = []
    for idx, current in enumerate(inputs, start=1):
        label = people_input_label(current, idx)
        for reset in inputs:
            fill_input_or_validate(reset, 0, f"room_type_people_reset_{idx}")
        page.wait_for_timeout(250)
        fill_input_or_validate(current, party_size, f"room_type_people_{idx}")
        page.keyboard.press("Tab")
        page.wait_for_timeout(500)
        free_places = wait_for_room_free_places(page, label)
        if free_places:
            room_counts.append({"label": label, "free_places": free_places})

    total_free_places = sum(item["free_places"] for item in room_counts) or None
    return {"free_places": total_free_places, "room_counts": room_counts}


def fill_by_labels(page, labels, value, field_name):
    if value is None:
        return
    if isinstance(value, (int, float)) and value == 0:
        return
    if isinstance(value, str) and not value.strip():
        return
    if not isinstance(labels, (list, tuple)) or not labels:
        raise RuntimeError(f"Labels missing for {field_name}")
    field = find_input_by_labels(page, labels)
    if field is None:
        raise RuntimeError(f"Missing field for {field_name}. Tried labels: {labels}")
    field.fill(str(value))


def select_half_board(page, half_board):
    yes_labels = {"sì", "si", "yes", "ja", "oui"}
    no_labels = {"no", "nein", "non"}
    target_labels = yes_labels if half_board else no_labels

    def radio_text(locator):
        try:
            return locator.inner_text().strip().lower()
        except Exception:
            return ""

    def find_radio_in(container):
        radios = container.locator("mat-radio-button")
        for i in range(radios.count()):
            text = radio_text(radios.nth(i))
            if text in target_labels:
                return radios.nth(i)
        return None

    container = None
    label_locators = [
        "text=/mezza\\s+pensione/i",
        "text=/half\\s+board/i",
        "text=/pensione\\s+completa/i",
    ]
    for label_selector in label_locators:
        label = page.locator(label_selector)
        if label.count() > 0:
            container = label.first.locator("xpath=ancestor-or-self::*[self::form or self::section or self::div][1]")
            break

    if container is not None:
        radio = find_radio_in(container)
        if radio is not None:
            radio.click()
            return

    radio = find_radio_in(page)
    if radio is None:
        raise RuntimeError("Half board radio buttons not found")
    radio.click()


OVERNIGHT_FORM_SELECTORS = [
    "mat-radio-button",
    "input[placeholder='Di cui bambini']",
    "input[placeholder='Di cui guide alpine']",
    "input[placeholder='Vegetariani']",
    "input[placeholder='Pacchetto lunch']",
    "input[placeholder='Nome di gruppo']",
    "input[placeholder='Allergie e intolleranze']",
]


def overnight_form_visible(page):
    return any(page.locator(selector).count() > 0 for selector in OVERNIGHT_FORM_SELECTORS)


def wait_for_overnight_form_visible(page, timeout_ms=20000):
    deadline = time.time() + (timeout_ms / 1000)
    while time.time() < deadline:
        if overnight_form_visible(page):
            return True
        page.wait_for_timeout(300)
    return False


def wait_for_overnight_form(page, timeout_ms=20000):
    if not wait_for_overnight_form_visible(page, timeout_ms=timeout_ms):
        raise RuntimeError("Overnight stay form did not load in time")


def list_missing_required_fields(page):
    """
    Best-effort diagnostics for huts with additional required fields.
    We use the UI convention that required labels often contain '*'.
    """
    missing = []
    fields = page.locator("mat-form-field")

    for i in range(fields.count()):
        field = fields.nth(i)
        label_loc = field.locator("mat-label")
        if label_loc.count() == 0:
            continue
        try:
            label_text = label_loc.first.inner_text().strip()
        except Exception:
            continue
        if "*" not in label_text:
            continue

        inp = field.locator("input, textarea")
        if inp.count() > 0:
            candidate = inp.first
            try:
                if not candidate.is_visible():
                    continue
                value = (candidate.input_value() or "").strip()
            except Exception:
                continue
            if not value:
                missing.append(label_text)
            continue

        combo = field.locator("[role='combobox']")
        if combo.count() > 0:
            candidate = combo.first
            try:
                if not candidate.is_visible():
                    continue
                value = normalize_text(candidate.inner_text())
            except Exception:
                continue
            if not value:
                missing.append(label_text)
            continue

    return missing


def fill_personal_value(page, label_text, value):
    if value is None:
        return
    labels = label_text if isinstance(label_text, (list, tuple)) else [label_text]
    field = find_input_by_labels(page, labels)
    if field is None:
        raise RuntimeError(f"Missing personal field. Tried labels: {labels}")
    if field.is_enabled():
        field.fill(str(value))
        return
    current = (field.input_value() or "").strip()
    if value and value.lower() not in current.lower():
        raise RuntimeError(f"Personal field is disabled and does not match config value. Tried labels: {labels}")


def select_country(page, country_value):
    if not country_value:
        raise RuntimeError("contact.country is required")
    normalized = country_value.strip().lower()
    mapped = None
    # Country dropdown labels depend on the wizard language.
    if normalized in {"switzerland", "ch", "svizzera", "suisse", "schweiz"}:
        mapped = "Svizzera - CH"
    option_text = mapped or country_value
    select = page.locator("mat-select")
    if select.count() > 0:
        select.first.click()
        try:
            page.wait_for_selector("mat-option", timeout=DEFAULT_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            select.first.click(force=True)
            page.wait_for_selector("mat-option", timeout=DEFAULT_TIMEOUT_MS)

        option = page.locator("mat-option", has_text=option_text)
        if option.count() == 0 and mapped:
            # Try the alternative language mapping when the wizard is not Italian.
            alt = None
            if mapped.startswith("Svizzera"):
                alt = "Schweiz - CH"
            elif mapped.startswith("Schweiz"):
                alt = "Svizzera - CH"
            if alt:
                option = page.locator("mat-option", has_text=alt)
        if option.count() == 0:
            option = page.locator("mat-option").filter(has_text=country_value)
        if option.count() == 0:
            raise RuntimeError(f"Country option not found for '{country_value}'")
        option.first.click()
        return

    field = find_input_by_labels(page, ["Paese", "Country", "Nazione", "Land"])
    if field is None:
        raise RuntimeError(f"Country field not found for '{country_value}'")
    if field.is_enabled():
        field.fill(option_text)
        return
    current = (field.input_value() or "").strip().lower()
    if option_text.strip().lower() not in current:
        raise RuntimeError(f"Country field is disabled and does not match '{country_value}'")


def ensure_language_it(page):
    indicators = [
        "AGGIUNGI PRENOTAZIONE",
        "Le mie prenotazioni",
        "Controlla disponibilità",
    ]

    def has_indicator():
        return any(page.locator(f"text={text}").count() > 0 for text in indicators)

    if has_indicator():
        return

    lang_button = page.locator("button:has-text('IT'), a:has-text('IT')")
    if lang_button.count() > 0:
        lang_button.first.click()
        page.wait_for_timeout(500)
        if has_indicator():
            return

    raise RuntimeError("UI language is not Italian. Please switch to IT and retry.")


def ensure_language_any_of(page, allowed_languages):
    """
    Booking wizard content can differ per hut and may be served in IT or DE.
    We fail fast if we end up in an unexpected language that would break label-based selectors.
    """
    allowed = {str(x).upper().strip() for x in allowed_languages}
    if not allowed:
        raise ValueError("allowed_languages must be non-empty")

    # The UI shows a language indicator in the top right (e.g. IT/DE/EN).
    # Prefer reading it; if absent, just enforce presence of at least one known label below.
    lang = None
    for code in ["IT", "DE", "EN", "FR"]:
        if page.locator(f"text={code}").count() > 0:
            # This is a heuristic; the code might appear elsewhere.
            lang = code
            break
    if lang and lang in allowed:
        return

    # If no explicit code found, accept when any of the marker labels exist.
    markers = []
    if "IT" in allowed:
        markers += ["AGGIUNGI PRENOTAZIONE", "Le mie prenotazioni", "Controlla disponibilità", "AVANTI", "INVIA"]
    if "DE" in allowed:
        markers += ["RESERVATION HINZUFÜGEN", "Meine Reservationen", "Verfügbarkeit prüfen", "WEITER", "SENDEN"]
    if any(page.locator(f"text={m}").count() > 0 for m in markers):
        return

    raise RuntimeError(f"UI language not supported for this run. Allowed={sorted(allowed)}")


def wait_for_visible(page, selector, timeout_ms=3000):
    try:
        page.wait_for_selector(selector, state="visible", timeout=timeout_ms)
        return True
    except PlaywrightTimeoutError:
        return False


def find_next_availability_button(page):
    candidates = [
        SELECTORS["next_check_availability"],
        "button:has-text('AVANTI')",
    ]
    for selector in candidates:
        locator = page.locator(selector)
        for idx in range(locator.count()):
            candidate = locator.nth(idx)
            try:
                if candidate.is_visible() and not candidate.is_disabled():
                    return candidate
            except Exception:
                continue
        if locator.count() > 0:
            return locator.first
    raise RuntimeError("Next availability button not found")


def find_availability_continue_button(page):
    candidates = [
        SELECTORS["next_availability_alt"],
        "button:has-text('WEITER')",
        "button:has-text('Weiter')",
        "button:has-text('AVANTI')",
        "button:has-text('Continua')",
    ]
    for selector in candidates:
        locator = page.locator(selector)
        for idx in range(locator.count()):
            candidate = locator.nth(idx)
            try:
                if candidate.is_visible() and not candidate.is_disabled():
                    return candidate
            except Exception:
                continue
        if locator.count() > 0:
            return locator.first
    return None


def find_availability_blocker_text(page):
    patterns = [
        "keine\\s+online-?reservationen\\s+moglich",
        "keine\\s+online-?reservationen\\s+möglich",
        "no\\s+online\\s+reservations?\\s+possible",
        "nessuna\\s+prenotazione\\s+online\\s+possibile",
    ]
    for pattern in patterns:
        locator = page.locator("text=/" + pattern + "/i")
        if locator.count() == 0:
            continue
        try:
            text = locator.first.inner_text().strip()
        except Exception:
            text = ""
        return text or pattern
    return None


FREE_PLACE_HEADERS = ["freie platze", "freie plätze", "free places", "posti liberi"]


def extract_free_places_from_text(text, room_label=None):
    normalized = normalize_text(normalize_date_text(text))
    if not any(header in normalized for header in FREE_PLACE_HEADERS):
        return None

    if room_label:
        label = normalize_text(room_label)
        if not label:
            return None
        matches = [
            int(match.group(1))
            for match in re.finditer(rf"{re.escape(label)}\s*:\s*(?:\|\s*)?(\d+)\b", normalized)
        ]
        if not matches:
            return None
        return max(matches)

    matches = [int(match.group(1)) for match in re.finditer(r":\s*(?:\|\s*)?([1-9]\d*)\b", normalized)]
    if not matches:
        return None
    return max(matches)


def find_positive_free_places(page):
    best = None
    tables = page.locator("table")
    for i in range(tables.count()):
        table = tables.nth(i)
        try:
            if not table.is_visible():
                continue
        except Exception:
            pass
        try:
            text = table.inner_text()
        except Exception:
            continue
        candidate = extract_free_places_from_text(text)
        if candidate is None:
            continue
        if best is None or candidate > best:
            best = candidate
    return best


def find_room_free_places(page, room_label):
    best = None
    tables = page.locator("table")
    for i in range(tables.count()):
        table = tables.nth(i)
        try:
            if not table.is_visible():
                continue
        except Exception:
            pass
        try:
            text = table.inner_text()
        except Exception:
            continue
        candidate = extract_free_places_from_text(text, room_label=room_label)
        if candidate is None:
            continue
        if best is None or candidate > best:
            best = candidate
    return best


def wait_for_stable_free_places(page, read_count, timeout_ms=4000, allow_zero=False):
    deadline = time.time() + (timeout_ms / 1000)
    stable_count = None
    stable_hits = 0
    while time.time() < deadline:
        count = read_count()
        if count is not None and (allow_zero or count > 0):
            if count == stable_count:
                stable_hits += 1
            else:
                stable_count = count
                stable_hits = 1
            if stable_hits >= 2:
                return stable_count
        else:
            stable_count = None
            stable_hits = 0
        page.wait_for_timeout(250)
    return stable_count


def wait_for_positive_free_places(page, timeout_ms=4000):
    return wait_for_stable_free_places(page, lambda: find_positive_free_places(page), timeout_ms=timeout_ms)


def wait_for_room_free_places(page, room_label, timeout_ms=4000):
    return wait_for_stable_free_places(
        page, lambda: find_room_free_places(page, room_label), timeout_ms=timeout_ms, allow_zero=True
    )


def availability_advanced(page, timeout_ms=8000):
    if wait_for_visible(page, SELECTORS["next_overnight"], timeout_ms=min(1500, timeout_ms)):
        return True
    return wait_for_overnight_form_visible(page, timeout_ms=timeout_ms)


def enable_waitlist_if_present(page):
    scroll_all_scrollables(page, direction="bottom", passes=8, pause_ms=150)

    waitlist_patterns = [
        "Continua\\s+e\\s+sarai\\s+messo\\s+in\\s+lista\\s+d['\\u2019 ]?attesa",
        "Warteliste",
        "waiting\\s+list",
        "Wenn\\s+du\\s+fortf[aä]hrst,\\s+wird\\s+die\\s+gesamte\\s+Reservation\\s+auf\\s+die\\s+Warteliste\\s+gesetzt",
    ]
    waitlist_line = page.locator("text=/" + "|".join(waitlist_patterns) + "/i")
    deadline = time.time() + 8.0
    while time.time() < deadline and waitlist_line.count() == 0:
        page.wait_for_timeout(250)
        scroll_all_scrollables(page, direction="bottom", passes=2, pause_ms=150)
    if waitlist_line.count() == 0:
        return False

    target = waitlist_line.first
    try:
        target.scroll_into_view_if_needed()
    except Exception:
        pass

    def click_any(locator):
        try:
            locator.click(timeout=1500)
            return True
        except Exception:
            try:
                locator.click(force=True)
                return True
            except Exception:
                return False

    # Some huts use a plain checkbox with nearby text not bound as a <label>.
    # Try to click the checkbox itself first (in the same nearby container).
    checkbox_near = target.locator(
        "xpath=ancestor-or-self::div[1]//input[@type='checkbox'] | "
        "ancestor-or-self::div[2]//input[@type='checkbox'] | "
        "ancestor-or-self::div[3]//input[@type='checkbox'] | "
        "preceding::input[@type='checkbox'][1] | "
        "following::input[@type='checkbox'][1]"
    )
    if checkbox_near.count() > 0:
        box = checkbox_near.first
        try:
            box.check(force=True)
            if box.is_checked():
                return True
        except Exception:
            if click_any(box):
                try:
                    if box.is_checked():
                        return True
                except Exception:
                    pass

    # Clicking the text itself sometimes toggles the checkbox (label-bound inputs).
    click_any(target)
    page.wait_for_timeout(200)

    checkbox_candidates = [
        target.locator("xpath=ancestor-or-self::label[1]//input[@type='checkbox']"),
        target.locator("xpath=ancestor-or-self::div[1]//input[@type='checkbox']"),
        target.locator("xpath=ancestor-or-self::div[2]//input[@type='checkbox']"),
        target.locator("xpath=preceding::input[@type='checkbox'][1]"),
        target.locator("xpath=following::input[@type='checkbox'][1]"),
    ]
    for cb in checkbox_candidates:
        if cb.count() == 0:
            continue
        box = cb.first
        try:
            box.check(force=True)
        except Exception:
            try:
                box.click(force=True)
            except Exception:
                continue
        try:
            if box.is_checked():
                return True
        except Exception:
            pass

    try:
        js_clicked = page.evaluate(
            """
            () => {
                const lineMatcher = /Continua\\s+e\\s+sarai\\s+messo\\s+in\\s+lista\\s+d['\\u2019 ]?attesa/i;
                const textNodes = Array.from(document.querySelectorAll("body *"));
                const el = textNodes.find((node) => lineMatcher.test((node.textContent || "").trim()));
                if (!el) return false;

                const visible = (node) => {
                    if (!node) return false;
                    const r = node.getBoundingClientRect();
                    if (!r || r.width <= 0 || r.height <= 0) return false;
                    const s = window.getComputedStyle(node);
                    if (!s) return false;
                    if (s.visibility === "hidden" || s.display === "none") return false;
                    return true;
                };

                const root = el.closest("label") || el.closest("div") || el;
                const boxes = Array.from(root.querySelectorAll("input[type='checkbox']")).filter(visible);
                if (boxes.length === 0) {
                    const near = el.closest("div, section, form") || document.body;
                    const nearBoxes = Array.from(near.querySelectorAll("input[type='checkbox']")).filter(visible);
                    if (nearBoxes.length === 0) return false;
                    boxes.push(...nearBoxes);
                }

                const box = boxes[0];
                try { box.click(); } catch (e) {}
                box.checked = true;
                box.dispatchEvent(new Event("input", { bubbles: true }));
                box.dispatchEvent(new Event("change", { bubbles: true }));
                return !!box.checked;
            }
            """
        )
        return bool(js_clicked)
    except Exception:
        return False


def find_summary_submit_button(page):
    candidates = [
        SELECTORS["next_summary"],
        "button:has-text('SENDEN')",
        "button:has-text('Senden')",
        "button:has-text('INVIA')",
        "button:has-text('Invia')",
        "button:has-text('Conferma')",
        "button[type='submit']",
    ]
    for selector in candidates:
        locator = page.locator(selector)
        if locator.count() > 0:
            return locator.first
    raise RuntimeError("Summary submit button not found")


def text_indicates_overlap_dialog(text):
    normalized = normalize_text(text)
    if not normalized:
        return False

    direct_phrases = [
        "prenotazione multipla rilevata",
        "hai gia prenotato uno o piu rifugi per questo periodo",
        "gia una prenotazione",
        "gia un'altra prenotazione",
        "hai gia una prenotazione",
        "hanno gia una prenotazione",
        "stesso giorno",
        "stessa data",
        "same day",
        "same date",
        "already have a reservation",
        "already have another reservation",
        "bereits eine reservierung",
        "bereits schon eine reservierung",
        "an diesem tag",
        "diesem datum",
    ]
    if any(phrase in normalized for phrase in direct_phrases):
        return True

    has_reservation = any(token in normalized for token in ["prenot", "reservation", "reservier", "buchung"])
    has_time_conflict = any(
        token in normalized
        for token in ["stesso giorno", "stessa data", "same day", "same date", "an diesem tag", "diesem datum"]
    )
    has_continue = any(token in normalized for token in ["proced", "contin", "weiter", "fortfahr", "proceed"])
    return has_reservation and has_time_conflict and has_continue


def find_overlap_dialog(page):
    dialogs = page.locator("[role='dialog'], mat-dialog-container, .cdk-overlay-pane")
    for i in range(dialogs.count()):
        dialog = dialogs.nth(i)
        try:
            if not dialog.is_visible():
                continue
            text = dialog.inner_text()
        except Exception:
            continue
        if text_indicates_overlap_dialog(text):
            return dialog
    return None


def click_overlap_dialog_continue(dialog):
    labels = ["Ignora", "Ignore", "Continua", "Procedi", "Prosegui", "Continue", "Proceed", "Weiter", "Ja", "Si", "Sì", "OK"]
    buttons = dialog.locator("button")
    for i in range(buttons.count()):
        button = buttons.nth(i)
        try:
            if not button.is_visible():
                continue
            text = button.inner_text()
        except Exception:
            continue
        if not label_matches(text, labels):
            continue
        try:
            button.click(timeout=DEFAULT_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            button.click(force=True)
        return True
    return False


def is_reservation_list_view(page):
    url = page.url or ""
    if url.startswith(LIST_URL):
        return True
    try:
        body = normalize_text(page.locator("body").inner_text())
    except Exception:
        return False
    return "le mie prenotazioni" in body and "aggiungi prenotazione" in body


def is_payment_step(page):
    selectors = [
        "input[autocomplete='cc-number']",
        "input[name*='cardnumber']",
        "iframe[src*='stripe']",
        "iframe[title*='card']",
    ]
    for selector in selectors:
        locator = page.locator(selector)
        if locator.count() == 0:
            continue
        for i in range(locator.count()):
            candidate = locator.nth(i)
            try:
                if candidate.is_visible():
                    return True
            except Exception:
                return True
    try:
        body = normalize_text(page.locator("body").inner_text())
    except Exception:
        return False
    strong_markers = [
        "perche abbiamo bisogno della tua carta di credito/debito",
        "carta di credito/debito",
        "si prega di verificare i dati della carta di credito",
        "mastercard",
        "visa",
        "scadenza",
    ]
    if any(marker in body for marker in strong_markers):
        return True
    return (
        ("dati carta di credito" in body or "credit card" in body or "kreditkarte" in body)
        and ("numero di carta" in body or "card number" in body or "kartennummer" in body)
    )


def find_payment_action_button(page):
    candidates = [
        "button:has-text('INVIA')",
        "button:has-text('Invia')",
        "button:has-text('SENDEN')",
        "button:has-text('Senden')",
        "button:has-text('AVANTI')",
        "button:has-text('Weiter')",
        "button:has-text('WEITER')",
    ]
    for selector in candidates:
        locator = page.locator(selector)
        for i in range(locator.count()):
            candidate = locator.nth(i)
            try:
                if candidate.is_visible():
                    return candidate
            except Exception:
                continue
    return None


def advance_payment_flow(page, screenshot_dir, step, max_clicks=3):
    clicked = False
    for idx in range(max_clicks):
        if not is_payment_step(page):
            break
        step = snap(page, screenshot_dir, step, f"payment_step_{idx + 1}")
        action = find_payment_action_button(page)
        if action is None:
            break
        try:
            if action.is_disabled():
                break
        except Exception:
            pass
        click_with_overlay_retry(page, action)
        clicked = True
        page.wait_for_timeout(1200)
        if is_reservation_list_view(page):
            break
    return step, clicked


def wait_for_post_submit_state(page, screenshot_dir, step, timeout_ms=15000):
    deadline = time.time() + (timeout_ms / 1000)
    overlap_handled = False

    while time.time() < deadline:
        dialog = find_overlap_dialog(page)
        if dialog is not None:
            step = snap(page, screenshot_dir, step, "overlap_dialog")
            if not click_overlap_dialog_continue(dialog):
                raise RuntimeError("Overlap dialog appeared but no continue button was found.")
            overlap_handled = True
            page.wait_for_timeout(800)
            step = snap(page, screenshot_dir, step, "overlap_dialog_confirmed")
            continue

        if is_payment_step(page):
            return {"status": "payment_required", "overlap_dialog_handled": overlap_handled}, step
        if is_reservation_list_view(page):
            return {"status": "submitted", "overlap_dialog_handled": overlap_handled}, step

        url = page.url or ""
        if url and "/wizard" not in url and not url.startswith(LIST_URL):
            return {"status": "submitted", "overlap_dialog_handled": overlap_handled}, step

        page.wait_for_timeout(250)

    step = snap(page, screenshot_dir, step, "post_submit_unknown")
    return {"status": "submitted_unknown", "overlap_dialog_handled": overlap_handled}, step


def handle_overlap_dialog_if_present(page, screenshot_dir, step):
    dialog = find_overlap_dialog(page)
    if dialog is None:
        return step, False
    step = snap(page, screenshot_dir, step, "overlap_dialog")
    if not click_overlap_dialog_continue(dialog):
        raise RuntimeError("Overlap dialog appeared but no continue button was found.")
    page.wait_for_timeout(800)
    step = snap(page, screenshot_dir, step, "overlap_dialog_confirmed")
    return step, True


def run_attempt(config, username, password, args, attempt_index=1):
    screenshot_dir = Path(args.screenshot_dir) if args.screenshot_dir else None
    if screenshot_dir is not None and (args.poll or config["auto_poll_if_full"]):
        screenshot_dir = screenshot_dir / f"attempt_{attempt_index:04d}"
    if screenshot_dir is not None:
        screenshot_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        page = browser.new_page()
        page.set_default_timeout(DEFAULT_TIMEOUT_MS)
        step = 0

        login_and_open_list(page, username, password, config["login_provider"])
        step = snap(page, screenshot_dir, step, "login")
        add_button = must_locator(page, SELECTORS["add_reservation_button"], "add_reservation_button", DEFAULT_TIMEOUT_MS)
        ensure_language_it(page)
        add_button.first.click()
        step = snap(page, screenshot_dir, step, "reservation_list")

        chosen_hut = choose_hut_option(page, config["hut_name"])
        step = snap(page, screenshot_dir, step, f"hut_selected_{chosen_hut.replace(' ', '_')}")
        confirm_hut_selection(page)
        step = snap(page, screenshot_dir, step, "wizard_loaded")
        # Some huts render the wizard in German; support IT/DE for the wizard flow.
        ensure_language_any_of(page, {"IT", "DE"})

        select_date_range(page, config["check_in"], config["check_out"])
        step = snap(page, screenshot_dir, step, "dates_selected")
        ensure_expected_date_range(page, config["check_in"], config["check_out"], config["allow_alternative_dates"])

        blocker_text = find_availability_blocker_text(page)
        if blocker_text:
            raise AvailabilityNotFoundError(f"Requested dates not available: {blocker_text}")

        checked_party_size = effective_party_size(config, args)
        room_type = config["preferences"].get("room_type")
        try:
            if args.alert_only and room_type is None:
                availability_probe = probe_any_room_free_places(page, checked_party_size)
                free_places = availability_probe.get("free_places")
            else:
                set_party_size_inputs(page, checked_party_size, room_type)
                page.keyboard.press("Tab")
                page.wait_for_timeout(500)
                free_places = wait_for_positive_free_places(page)
                availability_probe = {"free_places": free_places}
        except RuntimeError as exc:
            mapped = reclassify_party_size_error(exc)
            if mapped is not None:
                raise mapped
            raise
        step = snap(page, screenshot_dir, step, "people_set")
        if not args.alert_only:
            ensure_expected_date_range(page, config["check_in"], config["check_out"], config["allow_alternative_dates"])

        if args.alert_only:
            if free_places:
                browser.close()
                return {"status": "availability_found", **availability_probe}
            raise AvailabilityNotFoundError("No positive free places shown on availability step.")
        if (
            not args.alert_only
            and config.get("require_positive_free_places")
            and not config["allow_waitlist"]
            and not free_places
        ):
            raise AvailabilityNotFoundError("No positive free places shown on availability step.")

        next_check = find_availability_next_button(page)
        if free_places:
            next_check = wait_for_availability_next_enabled(page)
        if next_check.is_disabled() and config["allow_waitlist"]:
            scroll_all_scrollables(page, direction="bottom", passes=6, pause_ms=150)
            step = snap(page, screenshot_dir, step, "availability_scrolled_for_waitlist")
            ensure_expected_date_range(page, config["check_in"], config["check_out"], allow_alternative_dates=False)
            enabled = enable_waitlist_if_present(page)
            if enabled:
                page.wait_for_timeout(300)
                next_check = find_availability_next_button(page)
        if next_check.is_disabled():
            step = snap(page, screenshot_dir, step, "availability_next_still_disabled")
            raise AvailabilityNotFoundError("Availability step cannot continue (button disabled).")
        click_with_overlay_retry(page, next_check)

        advanced = availability_advanced(page, timeout_ms=8000)
        if not advanced:
            step, handled_overlap = handle_overlap_dialog_if_present(page, screenshot_dir, step)
            if handled_overlap:
                advanced = availability_advanced(page, timeout_ms=8000)

        if not advanced:
            continue_button = find_availability_next_button(page)
            if continue_button.is_disabled() and config["allow_waitlist"]:
                scroll_all_scrollables(page, direction="bottom", passes=6, pause_ms=150)
                step = snap(page, screenshot_dir, step, "availability_continue_scrolled_for_waitlist")
                ensure_expected_date_range(page, config["check_in"], config["check_out"], allow_alternative_dates=False)
                enabled = enable_waitlist_if_present(page)
                if enabled:
                    page.wait_for_timeout(300)
                    continue_button = find_availability_next_button(page)
            if continue_button.is_disabled():
                step = snap(page, screenshot_dir, step, "availability_continue_still_disabled")
                raise AvailabilityNotFoundError("Availability step cannot continue (button disabled).")
            click_with_overlay_retry(page, continue_button)
            page.wait_for_timeout(800)
            advanced = availability_advanced(page, timeout_ms=8000)

        if not advanced:
            step, handled_overlap = handle_overlap_dialog_if_present(page, screenshot_dir, step)
            if handled_overlap:
                advanced = availability_advanced(page, timeout_ms=8000)

        if not advanced:
            if config["allow_waitlist"]:
                ensure_expected_date_range(page, config["check_in"], config["check_out"], allow_alternative_dates=False)
                waitlist_enabled = enable_waitlist_if_present(page)
                if not waitlist_enabled:
                    step = snap(page, screenshot_dir, step, "waitlist_not_found")
                    raise AvailabilityNotFoundError(
                        "Requested dates not available and no waiting list option was offered."
                    )
                step = snap(page, screenshot_dir, step, "waitlist_enabled")
                continue_button = find_availability_next_button(page)
                if continue_button.is_disabled():
                    step = snap(page, screenshot_dir, step, "waitlist_enabled_but_button_disabled")
                    raise AvailabilityNotFoundError("Waiting list was enabled but continue button is still disabled.")
                continue_button.click()
                page.wait_for_timeout(800)
                advanced = availability_advanced(page, timeout_ms=8000)
            elif not config["allow_alternative_dates"]:
                raise AvailabilityNotFoundError(
                    "Requested dates not available. Set allow_alternative_dates or allow_waitlist to continue."
                )

        if not advanced:
            raise AvailabilityNotFoundError("Availability flow did not advance to overnight step.")
        step = snap(page, screenshot_dir, step, "availability_checked")

        if args.alert_only:
            if not availability_probe.get("free_places"):
                raise AvailabilityNotFoundError("No positive free places shown on availability step.")
            browser.close()
            return {"status": "availability_found", **availability_probe}

        wait_for_overnight_form(page)
        select_half_board(page, config["half_board"])
        fill_by_labels(page, ["Di cui bambini", "Bambini", "Davon Kinder", "Kinder"], config["stay"]["children_count"], "children_count")
        fill_by_labels(page, ["Di cui guide alpine", "Guide alpine", "Davon Bergfuhrer", "Bergfuhrer"], config["stay"]["guides_count"], "guides_count")
        fill_by_labels(
            page,
            ["Vegetariani", "Vegetariano", "Vegetarier", "Vegetarisch"],
            config["stay"]["vegetarian_count"],
            "vegetarian_count",
        )
        fill_by_labels(page, ["Pacchetto lunch", "Pacchetto pranzo", "Lunch", "Lunchpaket", "Lunch-Paket", "Lunchpakete"], config["stay"]["lunch_packages"], "lunch_packages")
        fill_by_labels(page, ["Nome di gruppo", "Nome del gruppo", "Nome gruppo", "Gruppenname", "Name der Gruppe"], config["stay"]["group_name"], "group_name")
        fill_by_labels(page, ["Da quale direzione", "Von welcher Richtung", "Von welcher Seite"], config["stay"]["direction"], "direction")
        fill_by_labels(page, ["Accesso al rifugio", "Accesso alla capanna", "Zugang zur Hutte"], config["stay"]["access_to_hut"], "access_to_hut")
        fill_by_labels(page, ["Allergie e intolleranze", "Allergie", "Intolleranze", "Allergien", "Unvertraglichkeiten"], config["stay"]["allergies"], "allergies")
        fill_by_labels(page, ["Commenti", "Note", "Osservazioni", "Kommentare", "Bemerkungen", "Kommentar"], config["stay"]["comments"], "comments")
        next_overnight = must_locator(page, SELECTORS["next_overnight"], "next_overnight", DEFAULT_TIMEOUT_MS).first
        if next_overnight.is_disabled() and config["allow_waitlist"]:
            enabled = enable_waitlist_if_present(page)
            if enabled:
                page.wait_for_timeout(300)
                next_overnight = page.locator(SELECTORS["next_overnight"]).first
        if next_overnight.is_disabled():
            missing = list_missing_required_fields(page)
            if missing:
                print("Overnight step missing required fields:", missing)
            step = snap(page, screenshot_dir, step, "overnight_next_disabled")
            raise RuntimeError("Overnight step incomplete; next button disabled.")
        next_overnight.click()
        step = snap(page, screenshot_dir, step, "overnight_filled")

        fill_personal_value(page, ["Nome", "Vorname"], config["contact"]["first_name"])
        fill_personal_value(page, ["Cognome", "Nachname"], config["contact"]["last_name"])
        fill_personal_value(
            page,
            ["Indirizzo 1", "Adresse 1", "Adresse", "Via e numero civico", "Strasse und Hausnummer"],
            config["contact"]["address_line1"],
        )
        fill_personal_value(page, ["CAP", "PLZ"], config["contact"]["postal_code"])
        fill_personal_value(page, ["Località", "Ort"], config["contact"]["city"])
        fill_personal_value(page, ["E-mail", "E-Mail", "Email"], config["contact"]["email"])
        fill_personal_value(page, ["Numero di Cellulare", "Mobiltelefon", "Handy", "Mobilfunknummer"], config["contact"]["phone"])
        select_country(page, config["contact"]["country"])

        next_personal = must_locator(page, SELECTORS["next_personal"], "next_personal", DEFAULT_TIMEOUT_MS)
        if next_personal.first.is_disabled():
            raise RuntimeError("Personal data step incomplete; next button disabled.")
        next_personal.first.click()
        step = snap(page, screenshot_dir, step, "personal_filled")

        terms_loc = must_locator(page, SELECTORS["terms_checkbox"], "terms_checkbox", DEFAULT_TIMEOUT_MS)
        if not terms_loc.first.is_checked():
            terms_loc.first.check()
        privacy_loc = must_locator(page, SELECTORS["privacy_checkbox"], "privacy_checkbox", DEFAULT_TIMEOUT_MS)
        if not privacy_loc.first.is_checked():
            privacy_loc.first.check()
        step = snap(page, screenshot_dir, step, "summary_checked")

        if args.dry_run:
            browser.close()
            return {"status": "dry_run_ready"}

        def maybe_pause(label):
            if not args.pause_at_payment and args.pause_seconds <= 0:
                return
            seconds = args.pause_seconds if args.pause_seconds > 0 else 600
            print(f"{label}. Keeping browser open for {seconds}s.")
            time.sleep(seconds)

        submit_btn = find_summary_submit_button(page)
        if submit_btn.is_disabled():
            raise RuntimeError("Summary step incomplete; submit button disabled.")
        if not args.confirm_submit:
            print("Reached final submit step. Run with --confirm-submit to click 'Invia'.")
            maybe_pause("Paused before submit")
            browser.close()
            return {"status": "ready_to_submit"}
        click_with_overlay_retry(page, submit_btn)
        step = snap(page, screenshot_dir, step, "submission_clicked")
        if not args.pause_at_payment:
            step, _ = advance_payment_flow(page, screenshot_dir, step)
        post_submit_result, step = wait_for_post_submit_state(page, screenshot_dir, step)
        maybe_pause(f"Paused after submit ({post_submit_result['status']})")
        browser.close()
        return post_submit_result


def config_label(config):
    return f"{config['hut_name']} {config['check_in']} -> {config['check_out']}"


def config_tag(config):
    return slugify(f"{config['hut_name']}_{config['check_in']}_{config['check_out']}")


def iso_now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def alert_state_path(config, args):
    return Path(args.alert_state_dir) / f"{config_tag(config)}.json"


def effective_party_size(config, args):
    if args.alert_only and config["alert"].get("any_party_size", True):
        return 1
    return config["party_size"]


def load_alert_state(path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_alert_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_alert_payload(config, args, result=None):
    check_in = parse_date(config["check_in"], "check_in")
    check_out = parse_date(config["check_out"], "check_out")
    nights = (check_out - check_in).days
    checked_party_size = effective_party_size(config, args)
    any_opening = args.alert_only and checked_party_size == 1 and config["alert"].get("any_party_size", True)
    subject = f"Hut spot open: {config['hut_name']} {config['check_in']} -> {config['check_out']}"
    body_lines = [
        "A hut spot appears to be available for the requested stay.",
        "",
        f"Hut: {config['hut_name']}",
        f"Dates: {config['check_in']} -> {config['check_out']} ({nights} night{'s' if nights != 1 else ''})",
        f"Party size checked: {checked_party_size}",
        f"Room preference: {config['preferences'].get('room_type') or 'any'}",
        f"Detected at: {iso_now()}",
        "",
        "The bot reached the availability flow and could continue for the requested dates.",
        f"Booking site: {LIST_URL}",
    ]
    parent_range = config.get("_alert_parent_range")
    if parent_range:
        body_lines.insert(
            5,
            f"Monitored window: {parent_range['check_in']} -> {parent_range['check_out']} (alerting on any single-night opening).",
        )
    if any_opening:
        body_lines.insert(5, "Alert mode: any opening (the monitor checks for at least 1 available spot).")
    if result and result.get("free_places"):
        body_lines.insert(6, f"Visible free places detected: {result['free_places']}")
    if result and result.get("room_counts"):
        details = ", ".join(f"{item['label']}: {item['free_places']}" for item in result["room_counts"])
        body_lines.insert(7, f"Visible free places by room category: {details}")
    return {
        "to": ", ".join(config["alert"]["to"]),
        "to_list": list(config["alert"]["to"]),
        "subject": subject,
        "body": "\n".join(body_lines),
        "checked_party_size": checked_party_size,
    }


def resolve_notify_command(config, args):
    if args.notify_command is not None:
        value = args.notify_command.strip()
        return value or None
    return config["alert"].get("command")


def run_notify_command(command, payload, config):
    env = os.environ.copy()
    env.update(
        {
            "HUT_ALERT_TO": payload["to"] or "",
            "HUT_ALERT_SUBJECT": payload["subject"],
            "HUT_ALERT_BODY": payload["body"],
            "HUT_ALERT_HUT_NAME": config["hut_name"],
            "HUT_ALERT_CHECK_IN": config["check_in"],
            "HUT_ALERT_CHECK_OUT": config["check_out"],
            "HUT_ALERT_PARTY_SIZE": str(payload.get("checked_party_size") or config["party_size"]),
            "HUT_ALERT_CONFIG_TAG": config_tag(config),
        }
    )
    subprocess.run(command, shell=True, check=True, env=env)


def handle_open_alert(config, args, result=None):
    payload = build_alert_payload(config, args, result=result)
    state_path = alert_state_path(config, args)
    previous = load_alert_state(state_path)
    current_free_places = result.get("free_places") if result else None
    already_open = previous.get("status") == "open" and previous.get("last_notified_at")
    previous_free_places = previous.get("last_open_free_places")

    if args.dry_run:
        print(f"[dry-run] Would alert for {config_label(config)}")
        print(payload["subject"])
        print(payload["body"])
        return "dry-run"

    if already_open and not args.alert_force_send and previous_free_places in {None, current_free_places}:
        previous["last_checked_at"] = iso_now()
        previous["last_open_free_places"] = current_free_places
        save_alert_state(state_path, previous)
        print(f"{config_label(config)}: availability is unchanged; alert already sent.")
        return "suppressed"

    command = resolve_notify_command(config, args)
    try:
        if command:
            run_notify_command(command, payload, config)
            mode = "command"
        else:
            print(payload["subject"])
            print(payload["body"])
            mode = "stdout"
    except Exception as exc:
        save_alert_state(
            state_path,
            {
                "status": "open",
                "last_checked_at": iso_now(),
                "last_notification_error": str(exc),
                "last_notified_at": previous.get("last_notified_at"),
                "last_notification_mode": previous.get("last_notification_mode"),
                "last_subject": payload["subject"],
                "last_to": payload["to"],
                "last_open_free_places": current_free_places,
            },
        )
        print(f"{config_label(config)}: alert delivery failed: {exc}")
        return "failed"

    save_alert_state(
        state_path,
        {
            "status": "open",
            "last_checked_at": iso_now(),
            "last_notified_at": iso_now(),
            "last_notification_mode": mode,
            "last_subject": payload["subject"],
            "last_to": payload["to"],
            "last_notification_error": None,
            "last_open_free_places": current_free_places,
        },
    )
    print(f"{config_label(config)}: open availability alert emitted via {mode}.")
    return mode


def record_closed_state(config, args, reason):
    if args.dry_run:
        print(f"[dry-run] {config_label(config)} unavailable: {reason}")
        return

    state_path = alert_state_path(config, args)
    previous = load_alert_state(state_path)
    save_alert_state(
        state_path,
        {
            "status": "closed",
            "last_checked_at": iso_now(),
            "last_reason": str(reason),
            "last_notified_at": previous.get("last_notified_at"),
            "last_notification_mode": previous.get("last_notification_mode"),
            "last_subject": previous.get("last_subject"),
            "last_to": previous.get("last_to"),
            "last_open_free_places": previous.get("last_open_free_places"),
        },
    )


def record_error_state(config, args, error):
    if args.dry_run:
        print(f"[dry-run] {config_label(config)} error: {error}")
        return

    state_path = alert_state_path(config, args)
    previous = load_alert_state(state_path)
    save_alert_state(
        state_path,
        {
            "status": "error",
            "last_checked_at": iso_now(),
            "last_reason": str(error),
            "last_notified_at": previous.get("last_notified_at"),
            "last_notification_mode": previous.get("last_notification_mode"),
            "last_subject": previous.get("last_subject"),
            "last_to": previous.get("last_to"),
            "last_open_free_places": previous.get("last_open_free_places"),
        },
    )


def summarize_availability_failures(configs, failures):
    parts = []
    for idx, exc in failures:
        parts.append(f"{config_label(configs[idx])}: {exc}")
    return "No config succeeded. " + " | ".join(parts)


def resolve_poll_settings(configs, poll_flags, args):
    if args.poll:
        return args.interval_seconds, args.jitter_seconds, args.max_attempts
    intervals = {cfg["poll_interval_seconds"] for cfg, flag in zip(configs, poll_flags) if flag}
    jitters = {cfg["poll_jitter_seconds"] for cfg, flag in zip(configs, poll_flags) if flag}
    max_attempts_set = {cfg["poll_max_attempts"] for cfg, flag in zip(configs, poll_flags) if flag}
    if not intervals:
        return None
    if len(intervals) > 1 or len(jitters) > 1 or len(max_attempts_set) > 1:
        raise ValueError(
            "Multiple configs with polling require matching poll_* settings or pass --poll with global settings."
        )
    return intervals.pop(), jitters.pop(), max_attempts_set.pop()


def main():
    args = parse_args()
    configs = expand_alert_only_configs([load_config(path) for path in args.config], args)
    username, password = load_credentials()

    poll_flags = [args.poll or cfg["auto_poll_if_full"] for cfg in configs]
    poll_settings = resolve_poll_settings(configs, poll_flags, args)
    poll_enabled = bool(poll_settings)

    args_per_config = []
    if args.screenshot_dir and len(configs) > 1:
        base_dir = Path(args.screenshot_dir)
        for cfg in configs:
            cfg_dir = base_dir / config_tag(cfg)
            args_per_config.append(clone_args(args, screenshot_dir=str(cfg_dir)))
    else:
        args_per_config = [args for _ in configs]

    def on_success(cfg, cfg_args, result):
        if cfg_args.alert_only:
            handle_open_alert(cfg, cfg_args, result=result)
            return
        status = (result or {}).get("status")
        if status == "dry_run_ready":
            print(f"Booking flow reached the final step in dry-run mode for {config_label(cfg)}.")
        elif status == "ready_to_submit":
            print(f"Booking flow reached the final submit step for {config_label(cfg)}.")
        elif status == "submitted":
            print(f"Booking submission clicked for {config_label(cfg)}.")
        else:
            print(f"Booking flow completed for {config_label(cfg)}.")

    def on_unavailable(cfg, cfg_args, exc):
        if cfg_args.alert_only:
            record_closed_state(cfg, cfg_args, exc)
            print(f"{config_label(cfg)}: {exc}")
            return
        raise exc

    def on_error(cfg, cfg_args, exc):
        if cfg_args.alert_only:
            record_error_state(cfg, cfg_args, exc)
            print(f"{config_label(cfg)}: retrying after error: {exc}")
            return
        raise exc

    if len(configs) == 1:
        config = configs[0]
        config_args = args_per_config[0]
        if poll_enabled:
            interval_seconds, jitter_seconds, max_attempts = poll_settings
            if interval_seconds <= 0:
                raise ValueError("interval_seconds must be > 0")
            if jitter_seconds < 0:
                raise ValueError("jitter_seconds must be >= 0")
            if max_attempts < 0:
                raise ValueError("max_attempts must be >= 0")
            attempt = 0
            while True:
                attempt += 1
                try:
                    result = run_attempt(config, username, password, config_args, attempt_index=attempt)
                    on_success(config, config_args, result)
                    if not config_args.alert_only:
                        return
                except AvailabilityNotFoundError as exc:
                    if not config_args.alert_only and max_attempts and attempt >= max_attempts:
                        raise
                    on_unavailable(config, config_args, exc)
                except Exception as exc:
                    if not config_args.alert_only and max_attempts and attempt >= max_attempts:
                        raise
                    on_error(config, config_args, exc)
                if max_attempts and attempt >= max_attempts:
                    return
                wait_time = interval_seconds + (random.randint(0, jitter_seconds) if jitter_seconds else 0)
                print(f"Attempt {attempt} complete. Retrying in {wait_time}s.")
                time.sleep(wait_time)
        else:
            try:
                result = run_attempt(config, username, password, config_args, attempt_index=1)
                on_success(config, config_args, result)
            except AvailabilityNotFoundError as exc:
                if config_args.alert_only:
                    on_unavailable(config, config_args, exc)
                else:
                    raise
            except Exception as exc:
                on_error(config, config_args, exc)
        return

    if not poll_enabled:
        availability_failures = []
        for idx, (cfg, cfg_args) in enumerate(zip(configs, args_per_config)):
            try:
                result = run_attempt(cfg, username, password, cfg_args, attempt_index=1)
                on_success(cfg, cfg_args, result)
                if not cfg_args.alert_only:
                    return
            except AvailabilityNotFoundError as exc:
                if cfg_args.alert_only:
                    on_unavailable(cfg, cfg_args, exc)
                else:
                    availability_failures.append((idx, exc))
                    print(f"{config_label(cfg)}: {exc}")
            except Exception as exc:
                on_error(cfg, cfg_args, exc)
        if availability_failures:
            raise AvailabilityNotFoundError(summarize_availability_failures(configs, availability_failures))
        return

    interval_seconds, jitter_seconds, max_attempts = poll_settings
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be > 0")
    if jitter_seconds < 0:
        raise ValueError("jitter_seconds must be >= 0")
    if max_attempts < 0:
        raise ValueError("max_attempts must be >= 0")

    pending = []
    attempt_counts = [0 for _ in configs]
    for idx, flag in enumerate(poll_flags):
        if flag:
            pending.append(idx)
        else:
            try:
                result = run_attempt(configs[idx], username, password, args_per_config[idx], attempt_index=1)
                on_success(configs[idx], args_per_config[idx], result)
                if not args_per_config[idx].alert_only:
                    return
            except AvailabilityNotFoundError as exc:
                if args_per_config[idx].alert_only:
                    on_unavailable(configs[idx], args_per_config[idx], exc)
                else:
                    raise
            except Exception as exc:
                on_error(configs[idx], args_per_config[idx], exc)

    cycle = 0
    while pending:
        cycle += 1
        for idx in list(pending):
            attempt_counts[idx] += 1
            try:
                result = run_attempt(configs[idx], username, password, args_per_config[idx], attempt_index=attempt_counts[idx])
                on_success(configs[idx], args_per_config[idx], result)
                if not args_per_config[idx].alert_only:
                    return
            except AvailabilityNotFoundError as exc:
                if not args_per_config[idx].alert_only and max_attempts and cycle >= max_attempts:
                    raise
                on_unavailable(configs[idx], args_per_config[idx], exc)
            except Exception as exc:
                if not args_per_config[idx].alert_only and max_attempts and cycle >= max_attempts:
                    raise
                on_error(configs[idx], args_per_config[idx], exc)
        if max_attempts and cycle >= max_attempts:
            return
        wait_time = interval_seconds + (random.randint(0, jitter_seconds) if jitter_seconds else 0)
        print(f"Pending {len(pending)} configs. Retrying in {wait_time}s.")
        time.sleep(wait_time)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        import traceback

        print("ERROR: unhandled exception", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)
