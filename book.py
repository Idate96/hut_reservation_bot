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
    parser.add_argument("--config", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--screenshot-dir", default="screens")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--poll", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--max-attempts", type=int, default=0)
    parser.add_argument("--jitter-seconds", type=int, default=30)
    return parser.parse_args()


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
    match = re.match(r"^(\d{2})/(\d{4})$", value)
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
    target = datetime.strptime(target_date, "%Y-%m-%d").date()
    period = page.locator(".mat-calendar-period-button").first
    next_btn = page.locator("button.mat-calendar-next-button").first
    prev_btn = page.locator("button.mat-calendar-previous-button").first
    for _ in range(24):
        current_text = period.inner_text().strip()
        current_year, current_month = parse_calendar_period(current_text)
        if (current_year, current_month) == (target.year, target.month):
            return
        if (current_year, current_month) < (target.year, target.month):
            next_btn.click()
        else:
            prev_btn.click()
        page.wait_for_timeout(200)
    raise RuntimeError("Unable to navigate calendar to target month")


def choose_hut_option(page, hut_name):
    hut_input = must_locator(page, SELECTORS["hut_input"], "hut_input", DEFAULT_TIMEOUT_MS)
    set_value(hut_input, hut_name)
    page.wait_for_timeout(500)
    page.wait_for_selector(SELECTORS["hut_options"], timeout=DEFAULT_TIMEOUT_MS)
    options = page.locator(SELECTORS["hut_options"])
    option_texts = [options.nth(i).inner_text().strip() for i in range(options.count())]
    if not option_texts:
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
    toggle.first.click()
    page.wait_for_selector(".mat-calendar-period-button", timeout=DEFAULT_TIMEOUT_MS)
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


def fill_by_placeholder(page, placeholder, value):
    if value is None:
        return
    locator = page.locator(f"input[placeholder='{placeholder}'], textarea[placeholder='{placeholder}']")
    if locator.count() == 0:
        raise RuntimeError(f"Missing field with placeholder '{placeholder}'")
    locator.first.fill(str(value))


def select_half_board(page, half_board):
    label = "Sì" if half_board else "No"
    radio = page.locator("mat-radio-button", has_text=label)
    if radio.count() == 0:
        raise RuntimeError("Half board radio buttons not found")
    radio.first.click()


def fill_personal_value(page, label_text, value):
    if value is None:
        return
    locator = page.locator(f"input[aria-label='{label_text}'], textarea[aria-label='{label_text}']")
    if locator.count() == 0:
        raise RuntimeError(f"Missing personal field '{label_text}'")
    field = locator.first
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

    select = page.locator("mat-select").first
    select.click()
    page.wait_for_timeout(300)

    option = page.locator("mat-option", has_text=option_text)
    if option.count() == 0:
        option = page.locator("mat-option").filter(has_text=country_value)
    if option.count() == 0:
        raise RuntimeError(f"Country option not found for '{country_value}'")
    option.first.click()


def run_attempt(config, username, password, args, attempt_index=1):
    screenshot_dir = Path(args.screenshot_dir) if args.screenshot_dir else None
    if screenshot_dir is not None and args.poll:
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
        add_button.first.click()
        step = snap(page, screenshot_dir, step, "reservation_list")

        chosen_hut = choose_hut_option(page, config["hut_name"])
        must_locator(page, SELECTORS["add_reservation_ok"], "add_reservation_ok", DEFAULT_TIMEOUT_MS).first.click()
        step = snap(page, screenshot_dir, step, f"hut_selected_{chosen_hut.replace(' ', '_')}")

        select_date_range(page, config["check_in"], config["check_out"])
        step = snap(page, screenshot_dir, step, "dates_selected")

        people_input = choose_people_input(page, config["preferences"].get("room_type"))
        people_input.fill(str(config["party_size"]))
        page.keyboard.press("Tab")
        step = snap(page, screenshot_dir, step, "people_set")

        next_check = must_locator(page, SELECTORS["next_check_availability"], "next_check_availability", DEFAULT_TIMEOUT_MS)
        if next_check.first.is_disabled():
            if config["allow_alternative_dates"]:
                must_locator(page, SELECTORS["next_availability_alt"], "next_availability_alt", DEFAULT_TIMEOUT_MS).first.click()
                page.wait_for_timeout(1000)
            else:
                raise AvailabilityNotFoundError(
                    "Requested dates not available. Set allow_alternative_dates: true to continue."
                )
        if next_check.first.is_disabled():
            raise AvailabilityNotFoundError("Availability check is still disabled after selecting dates and people.")
        next_check.first.click()
        step = snap(page, screenshot_dir, step, "availability_checked")

        select_half_board(page, config["half_board"])
        fill_by_placeholder(page, "Di cui bambini", config["stay"]["children_count"])
        fill_by_placeholder(page, "Di cui guide alpine", config["stay"]["guides_count"])
        fill_by_placeholder(page, "Vegetariani", config["stay"]["vegetarian_count"])
        fill_by_placeholder(page, "Pacchetto lunch", config["stay"]["lunch_packages"])
        fill_by_placeholder(page, "Nome di gruppo", config["stay"]["group_name"])
        fill_by_placeholder(page, "Accesso al rifugio", config["stay"]["access_to_hut"])
        fill_by_placeholder(page, "Allergie e intolleranze", config["stay"]["allergies"])
        fill_by_placeholder(page, "Commenti", config["stay"]["comments"])
        must_locator(page, SELECTORS["next_overnight"], "next_overnight", DEFAULT_TIMEOUT_MS).first.click()
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

        next_summary = must_locator(page, SELECTORS["next_summary"], "next_summary", DEFAULT_TIMEOUT_MS)
        if next_summary.first.is_disabled():
            raise RuntimeError("Summary step incomplete; next button disabled.")
        next_summary.first.click()
        step = snap(page, screenshot_dir, step, "payment_step")
        browser.close()


def main():
    args = parse_args()
    if args.interval_seconds <= 0:
        raise ValueError("interval_seconds must be > 0")
    if args.jitter_seconds < 0:
        raise ValueError("jitter_seconds must be >= 0")
    if args.max_attempts < 0:
        raise ValueError("max_attempts must be >= 0")

    config = load_config(args.config)
    username, password = load_credentials()

    if args.poll:
        attempt = 0
        while True:
            attempt += 1
            try:
                run_attempt(config, username, password, args, attempt_index=attempt)
                print("Booking flow completed.")
                return
            except AvailabilityNotFoundError as exc:
                if args.max_attempts and attempt >= args.max_attempts:
                    raise
                wait_time = args.interval_seconds + (random.randint(0, args.jitter_seconds) if args.jitter_seconds else 0)
                print(f"Attempt {attempt}: {exc}. Retrying in {wait_time}s.")
                time.sleep(wait_time)
    else:
        run_attempt(config, username, password, args, attempt_index=1)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
