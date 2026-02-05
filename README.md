# Hut Reservation Bot (hut-reservation.org)

Simple research-grade automation to book huts on `hut-reservation.org` using Python + Playwright.  
It is intentionally strict: missing inputs or UI changes cause a hard failure.

## Setup
```
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install
```

## Configure
1. Copy `.env.example` to `.env` and fill credentials.
2. Copy `config.example.yaml` to `config.yaml` and fill your reservation details.
3. Set `login_provider` to `sac` (SAC login) or `default` (hut-reservation login).
4. Fill stay options like `half_board`, `allow_alternative_dates`, and counts (`children_count`, `guides_count`, `vegetarian_count`, `lunch_packages`), plus `group_name` and optional notes.
5. Use `allow_waitlist: true` to join the waiting list when it’s offered for your date.
6. Use `auto_poll_if_full: true` to retry every 5 minutes when dates are unavailable or no waiting list is offered (`poll_interval_seconds: 300`).
7. If you want polling only, set `allow_waitlist: false`.
5. Update the `SELECTORS` map inside `book.py` if the UI changes.

## Run
```
python book.py --config config.yaml --dry-run
python book.py --config config.yaml
python book.py --config config.yaml --pause-at-payment
python book.py --config config.yaml --confirm-submit --pause-at-payment --pause-seconds 300
```
Screenshots are saved to `./screens` on every step.

## Polling
```
python book.py --config config_konkordia_2026-04-03.yaml --poll --interval-seconds 300 --jitter-seconds 30
```
Use `--max-attempts N` to stop after N checks. Polling retries only when dates are unavailable; any other error stops immediately. Enable `allow_waitlist` if you want to continue even when full.

## Notes
- The current selectors target the Italian UI (placeholders/labels like `Di cui bambini`, `Vegetariani`).
- SAC login redirects to `portal.sac-cas.ch`; selectors may change over time.
- If the site uses CAPTCHA/2FA, the script will fail by design.
- Use responsibly and respect the website’s terms of service.
