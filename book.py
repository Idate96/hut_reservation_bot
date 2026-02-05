import argparse
import os
import random
import re
import sys
import time
from datetime import datetime
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
    "add_reservation_button": ".add_button, button:has-text('AGGIUNGI PRENOTAZIONE')",
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
    "terms_checkbox": "input[aria-label*='GTC'], input[aria-label*='CGC']",
    "privacy_checkbox": "input[aria-label*='Privacy']",
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
        "auto_poll_if_full": auto_poll_if_full,
        "poll_interval_seconds": poll_interval_seconds,
        "poll_jitter_seconds": poll_jitter_seconds,
        "poll_max_attempts": poll_max_attempts,
        "stay": stay_out,
    }


def load_credentials():
    load_dotenv()
    username = os.getenv("HUT_USERNAME")
    password = os.getenv("HUT_PASSWORD")
    if not username or not password:
        raise ValueError("HUT_USERNAME and HUT_PASSWORD must be set in .env")
    return username, password


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


def choose_hut_option(page, hut_name):
    hut_input = must_locator(page, SELECTORS["hut_input"], "hut_input", DEFAULT_TIMEOUT_MS)
    set_value(hut_input, hut_name)
    page.keyboard.press("Enter")
    page.wait_for_timeout(500)
    options = page.locator(SELECTORS["hut_options"])
    try:
        page.wait_for_selector(SELECTORS["hut_options"], timeout=DEFAULT_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        option_texts = []
        if hut_input.first.input_value().strip():
            return hut_name
        raise RuntimeError("No hut options available after search")
    option_texts = [options.nth(i).inner_text().strip() for i in range(options.count())]
    if not option_texts:
        if hut_input.first.input_value().strip():
            return hut_name
        raise RuntimeError("No hut options available after search")

    exact_matches = [i for i, text in enumerate(option_texts) if text == hut_name]
    if len(exact_matches) == 1:
        options.nth(exact_matches[0]).click()
        return option_texts[exact_matches[0]]

    contains_matches = [i for i, text in enumerate(option_texts) if hut_name.lower() in text.lower()]
    if len(contains_matches) == 1:
        options.nth(contains_matches[0]).click()
        return option_texts[contains_matches[0]]

    raise RuntimeError(f"Ambiguous hut selection for '{hut_name}'. Options: {option_texts}")


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


def choose_people_input(page, room_type):
    def first_visible(locator):
        for i in range(locator.count()):
            if locator.nth(i).is_visible():
                return locator.nth(i)
        return locator.first if locator.count() else None

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
                selected = first_visible(panel_inputs)
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
                selected = first_visible(field_inputs)
                if selected is None:
                    raise RuntimeError(f"Room field '{label_text}' has no input")
                return selected

    inputs = page.locator(SELECTORS["people_input"])
    if inputs.count() == 0:
        raise RuntimeError("No people input found")
    if room_type:
        keywords = ROOM_TYPE_KEYWORDS.get(room_type, [room_type])
        for i in range(inputs.count()):
            label = (inputs.nth(i).get_attribute("aria-label") or "").lower()
            if any(keyword in label for keyword in keywords):
                return inputs.nth(i)
        raise RuntimeError(f"No people input matched room_type '{room_type}'")
    if inputs.count() == 1:
        return inputs.first
    raise RuntimeError("Multiple people inputs available; set preferences.room_type")


def fill_input_or_validate(locator, value, field_name):
    if locator is None:
        return
    try:
        locator.scroll_into_view_if_needed()
    except Exception:
        pass
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
    return re.sub(r"\s+", " ", value or "").strip().lower()


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


def find_date_range_input(page):
    labeled = find_input_by_labels(page, ["Data Da - A", "Data (da - a)", "Date from - to", "Date range"])
    if labeled is not None:
        return labeled

    candidates = page.locator("input[placeholder*='Data'], input[aria-label*='Data']")
    for i in range(candidates.count()):
        loc = candidates.nth(i)
        try:
            if loc.is_visible():
                return loc
        except Exception:
            continue
    if candidates.count() > 0:
        return candidates.first
    raise RuntimeError("Date range input not found on availability step")


def expected_date_range_value(check_in, check_out):
    return f"{format_date_for_ui(check_in)} - {format_date_for_ui(check_out)}"


def date_range_matches(value, expected_value):
    normalized = normalize_date_text(value)
    expected = normalize_date_text(expected_value)
    left, right = [part.strip() for part in expected.split("-", 1)]
    return left in normalized and right in normalized


def ensure_expected_date_range(page, check_in, check_out, allow_alternative_dates):
    expected = expected_date_range_value(check_in, check_out)
    date_input = find_date_range_input(page)
    try:
        current = date_input.input_value()
    except Exception:
        current = date_input.get_attribute("value") or ""
    if date_range_matches(current, expected):
        return
    if allow_alternative_dates:
        return
    select_date_range(page, check_in, check_out)
    date_input = find_date_range_input(page)
    current = date_input.input_value()
    if not date_range_matches(current, expected):
        raise RuntimeError(
            f"Date range changed unexpectedly. Expected '{expected}', got '{normalize_date_text(current)}'."
        )


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
        "button:has-text('AVANTI')",
        "button:has-text('Continua')",
    ]
    return first_enabled_or_visible(page, selectors, "availability_next_button")


def label_matches(text, labels):
    normalized = normalize_text(text)
    for label in labels:
        if normalize_text(label) in normalized:
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


def fill_personal_value(page, label_text, value):
    if value is None:
        return
    field = find_input_by_labels(page, [label_text])
    if field is None:
        raise RuntimeError(f"Missing personal field '{label_text}'")
    if field.is_enabled():
        field.fill(str(value))
        return
    current = (field.input_value() or "").strip()
    if value and value.lower() not in current.lower():
        raise RuntimeError(f"Field '{label_text}' is disabled and does not match config value")


def select_country(page, country_value):
    if not country_value:
        raise RuntimeError("contact.country is required")
    normalized = country_value.strip().lower()
    mapped = None
    if normalized in {"switzerland", "ch", "svizzera", "suisse"}:
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
        if option.count() == 0:
            option = page.locator("mat-option").filter(has_text=country_value)
        if option.count() == 0:
            raise RuntimeError(f"Country option not found for '{country_value}'")
        option.first.click()
        return

    field = find_input_by_labels(page, ["Paese", "Country", "Nazione"])
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


def availability_advanced(page, timeout_ms=8000):
    if wait_for_visible(page, SELECTORS["next_overnight"], timeout_ms=min(1500, timeout_ms)):
        return True
    return wait_for_overnight_form_visible(page, timeout_ms=timeout_ms)


def enable_waitlist_if_present(page):
    waitlist_text = page.locator("text=/lista d['\\u2019 ]?attesa/i, text=/waiting list/i")
    deadline = time.time() + 6.0
    while time.time() < deadline and waitlist_text.count() == 0:
        page.wait_for_timeout(250)
    if waitlist_text.count() == 0:
        return False

    try:
        waitlist_text.first.scroll_into_view_if_needed()
    except Exception:
        pass
    try:
        page.evaluate(
            """
            () => {
                const targets = [
                    document.scrollingElement,
                    document.querySelector(".mat-drawer-content"),
                    document.querySelector("mat-sidenav-content"),
                    document.querySelector("main"),
                    document.body,
                    document.documentElement,
                ].filter(Boolean);
                for (const el of targets) {
                    try { el.scrollTo(0, el.scrollHeight); } catch (e) {}
                }
            }
            """
        )
    except Exception:
        pass

    try:
        role_box = page.get_by_role("checkbox", name=re.compile("lista d['\u2019 ]?attesa|waiting list", re.I))
        if role_box.count() > 0:
            role_box.first.scroll_into_view_if_needed()
            role_box.first.check(force=True)
            return True
    except Exception:
        pass

    label_candidates = [
        "label:has-text('lista d\\'attesa')",
        "label:has-text('lista di attesa')",
        "label:has-text('waiting list')",
    ]
    for selector in label_candidates:
        locator = page.locator(selector)
        if locator.count() == 0:
            continue
        try:
            locator.first.scroll_into_view_if_needed()
        except Exception:
            pass
        try:
            locator.first.click(force=True)
            return True
        except Exception:
            continue

    mat_checkbox = page.locator(
        "mat-checkbox:has-text('lista d\\'attesa'), mat-checkbox:has-text('lista di attesa'), mat-checkbox:has-text('waiting list')"
    )
    if mat_checkbox.count() > 0:
        try:
            mat_checkbox.first.scroll_into_view_if_needed()
        except Exception:
            pass
        try:
            mat_checkbox.first.click(force=True)
            return True
        except Exception:
            pass

    try:
        js_clicked = page.evaluate(
            """
            () => {
                const matcher = /lista d['\\u2019 ]?attesa|waiting list/i;
                const all = Array.from(document.querySelectorAll("body *"));
                const textEl = all.find((el) => matcher.test(el.textContent || ""));
                if (!textEl) return false;

                const label = textEl.closest("label") || textEl.querySelector("label");
                const forId = label ? label.getAttribute("for") : null;
                const labelInput = label ? label.querySelector("input[type='checkbox']") : null;
                const forInput = forId ? document.getElementById(forId) : null;
                const matInput = textEl.closest("mat-checkbox")?.querySelector("input[type='checkbox']");
                const nearby =
                    textEl.closest("div, section, form, mat-dialog-container")?.querySelector("input[type='checkbox']");
                const input = labelInput || forInput || matInput || nearby || document.querySelector("input[type='checkbox']");
                if (!input) return false;

                const target = label || input.closest("mat-checkbox") || input;
                if (target) target.click();
                input.checked = true;
                input.dispatchEvent(new Event("input", { bubbles: true }));
                input.dispatchEvent(new Event("change", { bubbles: true }));
                return input.checked || input.getAttribute("aria-checked") === "true";
            }
            """
        )
        return bool(js_clicked)
    except Exception:
        return False


def find_summary_submit_button(page):
    candidates = [
        SELECTORS["next_summary"],
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

        page.goto(f"{BASE_URL}/login", wait_until="domcontentloaded")
        if config["login_provider"] == "sac":
            sac_button = must_locator(page, SELECTORS["login_sac_button"], "login_sac_button", DEFAULT_TIMEOUT_MS)
            sac_button.first.click()
            page.wait_for_load_state("domcontentloaded")

            user_input = must_locator(page, SELECTORS["sac_username"], "sac_username", DEFAULT_TIMEOUT_MS)
            pass_input = must_locator(page, SELECTORS["sac_password"], "sac_password", DEFAULT_TIMEOUT_MS)
            set_value(user_input, username)
            set_value(pass_input, password)
            must_locator(page, SELECTORS["sac_submit"], "sac_submit", DEFAULT_TIMEOUT_MS).first.click()
        else:
            user_input = must_locator(page, SELECTORS["login_username"], "login_username", DEFAULT_TIMEOUT_MS)
            pass_input = must_locator(page, SELECTORS["login_password"], "login_password", DEFAULT_TIMEOUT_MS)
            set_value(user_input, username)
            set_value(pass_input, password)
            must_locator(page, SELECTORS["login_submit"], "login_submit", DEFAULT_TIMEOUT_MS).first.click()
        step = snap(page, screenshot_dir, step, "login")

        page.goto(LIST_URL, wait_until="domcontentloaded")
        add_button = must_locator(page, SELECTORS["add_reservation_button"], "add_reservation_button", DEFAULT_TIMEOUT_MS)
        ensure_language_it(page)
        add_button.first.click()
        step = snap(page, screenshot_dir, step, "reservation_list")

        chosen_hut = choose_hut_option(page, config["hut_name"])
        must_locator(page, SELECTORS["add_reservation_ok"], "add_reservation_ok", DEFAULT_TIMEOUT_MS).first.click()
        step = snap(page, screenshot_dir, step, f"hut_selected_{chosen_hut.replace(' ', '_')}")
        wait_for_booking_wizard(page, timeout_ms=WIZARD_TIMEOUT_MS)
        step = snap(page, screenshot_dir, step, "wizard_loaded")

        select_date_range(page, config["check_in"], config["check_out"])
        step = snap(page, screenshot_dir, step, "dates_selected")
        ensure_expected_date_range(page, config["check_in"], config["check_out"], config["allow_alternative_dates"])

        set_party_size_inputs(page, config["party_size"], config["preferences"].get("room_type"))
        page.keyboard.press("Tab")
        step = snap(page, screenshot_dir, step, "people_set")
        ensure_expected_date_range(page, config["check_in"], config["check_out"], config["allow_alternative_dates"])

        next_check = find_availability_next_button(page)
        if next_check.is_disabled() and config["allow_waitlist"]:
            ensure_expected_date_range(page, config["check_in"], config["check_out"], allow_alternative_dates=False)
            enabled = enable_waitlist_if_present(page)
            if enabled:
                page.wait_for_timeout(300)
                next_check = find_availability_next_button(page)
        if next_check.is_disabled():
            raise AvailabilityNotFoundError("Availability step cannot continue (button disabled).")
        try:
            next_check.click(timeout=DEFAULT_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            next_check.click(force=True)

        if not availability_advanced(page, timeout_ms=8000):
            continue_button = find_availability_next_button(page)
            if continue_button.is_disabled() and config["allow_waitlist"]:
                ensure_expected_date_range(page, config["check_in"], config["check_out"], allow_alternative_dates=False)
                enabled = enable_waitlist_if_present(page)
                if enabled:
                    page.wait_for_timeout(300)
                    continue_button = find_availability_next_button(page)
            if continue_button.is_disabled():
                raise AvailabilityNotFoundError("Availability step cannot continue (button disabled).")
            continue_button.click()
            page.wait_for_timeout(800)

        if not availability_advanced(page, timeout_ms=8000):
            if config["allow_waitlist"]:
                ensure_expected_date_range(page, config["check_in"], config["check_out"], allow_alternative_dates=False)
                waitlist_enabled = enable_waitlist_if_present(page)
                if not waitlist_enabled:
                    step = snap(page, screenshot_dir, step, "waitlist_not_found")
                    raise AvailabilityNotFoundError(
                        "Requested dates not available and no waiting list option was offered."
                    )
                continue_button = find_availability_next_button(page)
                if continue_button.is_disabled():
                    raise AvailabilityNotFoundError("Waiting list was enabled but continue button is still disabled.")
                continue_button.click()
                page.wait_for_timeout(800)
            elif not config["allow_alternative_dates"]:
                raise AvailabilityNotFoundError(
                    "Requested dates not available. Set allow_alternative_dates or allow_waitlist to continue."
                )

        if not availability_advanced(page, timeout_ms=8000):
            raise AvailabilityNotFoundError("Availability flow did not advance to overnight step.")
        step = snap(page, screenshot_dir, step, "availability_checked")

        wait_for_overnight_form(page)
        select_half_board(page, config["half_board"])
        fill_by_labels(page, ["Di cui bambini", "Bambini"], config["stay"]["children_count"], "children_count")
        fill_by_labels(page, ["Di cui guide alpine", "Guide alpine"], config["stay"]["guides_count"], "guides_count")
        fill_by_labels(page, ["Vegetariani", "Vegetariano"], config["stay"]["vegetarian_count"], "vegetarian_count")
        fill_by_labels(page, ["Pacchetto lunch", "Pacchetto pranzo", "Lunch"], config["stay"]["lunch_packages"], "lunch_packages")
        fill_by_labels(page, ["Nome di gruppo", "Nome del gruppo", "Nome gruppo"], config["stay"]["group_name"], "group_name")
        fill_by_labels(page, ["Accesso al rifugio", "Accesso alla capanna"], config["stay"]["access_to_hut"], "access_to_hut")
        fill_by_labels(page, ["Allergie e intolleranze", "Allergie", "Intolleranze"], config["stay"]["allergies"], "allergies")
        fill_by_labels(page, ["Commenti", "Note", "Osservazioni"], config["stay"]["comments"], "comments")
        next_overnight = must_locator(page, SELECTORS["next_overnight"], "next_overnight", DEFAULT_TIMEOUT_MS).first
        if next_overnight.is_disabled() and config["allow_waitlist"]:
            enabled = enable_waitlist_if_present(page)
            if enabled:
                page.wait_for_timeout(300)
                next_overnight = page.locator(SELECTORS["next_overnight"]).first
        if next_overnight.is_disabled():
            raise RuntimeError("Overnight step incomplete; next button disabled.")
        next_overnight.click()
        step = snap(page, screenshot_dir, step, "overnight_filled")

        fill_personal_value(page, "Nome", config["contact"]["first_name"])
        fill_personal_value(page, "Cognome", config["contact"]["last_name"])
        fill_personal_value(page, "Indirizzo 1", config["contact"]["address_line1"])
        fill_personal_value(page, "CAP", config["contact"]["postal_code"])
        fill_personal_value(page, "Località", config["contact"]["city"])
        fill_personal_value(page, "E-mail", config["contact"]["email"])
        fill_personal_value(page, "Numero di Cellulare", config["contact"]["phone"])
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
            return

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
            return
        submit_btn.click()
        step = snap(page, screenshot_dir, step, "submission_clicked")
        maybe_pause("Paused after submit")
        browser.close()


def config_label(config):
    return f"{config['hut_name']} {config['check_in']} -> {config['check_out']}"


def config_tag(config):
    return slugify(f"{config['hut_name']}_{config['check_in']}_{config['check_out']}")


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
    configs = [load_config(path) for path in args.config]
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
                    run_attempt(config, username, password, config_args, attempt_index=attempt)
                    print("Booking flow completed.")
                    return
                except AvailabilityNotFoundError as exc:
                    if max_attempts and attempt >= max_attempts:
                        raise
                    wait_time = interval_seconds + (random.randint(0, jitter_seconds) if jitter_seconds else 0)
                    print(f"Attempt {attempt}: {exc}. Retrying in {wait_time}s.")
                    time.sleep(wait_time)
        else:
            run_attempt(config, username, password, config_args, attempt_index=1)
        return

    if not poll_enabled:
        for cfg, cfg_args in zip(configs, args_per_config):
            run_attempt(cfg, username, password, cfg_args, attempt_index=1)
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
            run_attempt(configs[idx], username, password, args_per_config[idx], attempt_index=1)

    cycle = 0
    while pending:
        cycle += 1
        for idx in list(pending):
            attempt_counts[idx] += 1
            try:
                run_attempt(configs[idx], username, password, args_per_config[idx], attempt_index=attempt_counts[idx])
                print(f"Booking flow completed for {config_label(configs[idx])}.")
                pending.remove(idx)
            except AvailabilityNotFoundError as exc:
                if max_attempts and cycle >= max_attempts:
                    raise
                print(f"{config_label(configs[idx])}: {exc}")
        if not pending:
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
