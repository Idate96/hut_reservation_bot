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
8. Optional: add an `alert` block for monitor-only runs. `alert.to` may be a single email, a comma-separated string, or a YAML list; it defaults to `contact.email`. `alert.command` can be any shell command and receives `HUT_ALERT_TO`, `HUT_ALERT_SUBJECT`, `HUT_ALERT_BODY`, `HUT_ALERT_HUT_NAME`, `HUT_ALERT_CHECK_IN`, and `HUT_ALERT_CHECK_OUT` in the environment.
9. Set `alert.any_night: true` if `check_in` and `check_out` define a monitoring window and you want alerts for any single free night inside that window. For example, `2026-04-03 -> 2026-04-06` expands to checks for `2026-04-03 -> 2026-04-04`, `2026-04-04 -> 2026-04-05`, and `2026-04-05 -> 2026-04-06`.
10. Update the `SELECTORS` map inside `book.py` if the UI changes.

## Run
```
python book.py --config config.yaml --dry-run
python book.py --config config.yaml
python book.py --config config.yaml --pause-at-payment
python book.py --config config.yaml --confirm-submit --pause-at-payment --pause-seconds 300
python book.py --config config.yaml --alert-only
```
Screenshots are saved to `./screens` on every step.

## Polling
```
python book.py --config config_konkordia_2026-04-03.yaml --poll --interval-seconds 300 --jitter-seconds 30
python book.py --config config_oberaarjoch_2026-04-05_06_alert.yaml --alert-only --poll
```
Use `--max-attempts N` to stop after N checks. Polling retries only when dates are unavailable; any other error stops immediately. Enable `allow_waitlist` if you want to continue even when full.

## Alerting
`--alert-only` stops after the availability step. If the requested dates are open, the bot emits an alert instead of continuing to the booking form.

Alert deduping is stateful: by default the bot stores one JSON file per checked slot under `.alert_state/`. While a slot stays open, the bot suppresses repeated emails if the visible free-place count is unchanged. It sends again when the visible count changes, or after the slot goes `closed` and later reopens. Use `--alert-force-send` to bypass that.

The repo now includes `send_alert_email.py`, which sends through SMTP only. For Gmail, use a Google app password and set:

- `ALERT_SMTP_HOST=smtp.gmail.com`
- `ALERT_SMTP_PORT=587`
- `ALERT_SMTP_SECURITY=starttls`
- `ALERT_SMTP_USERNAME=your_email@gmail.com`
- `ALERT_SMTP_PASSWORD=your_16_char_app_password`
- `ALERT_EMAIL_FROM=your_email@gmail.com`

Example automatic alert command:
```
alert:
  to:
    - "ada@example.com"
  command: ".venv/bin/python send_alert_email.py"
```

This is fully headless. No browser needs to stay open for email alerts.

## Multi-config (round-robin)
```
python book.py \
  --config config_konkordia_2026-04-03.yaml \
  --config config_finsteraarhorn_2026-04-03_05.yaml \
  --config config_oberaarjoch_2026-04-05.yaml \
  --poll --interval-seconds 300 --jitter-seconds 30
```
With multiple configs, the bot cycles through each one per interval. If you omit `--poll`, it runs once per config.  
When multiple configs have polling enabled, their `poll_*` settings must match unless you pass `--poll`.

## Notes
- The current selectors target the Italian UI (placeholders/labels like `Di cui bambini`, `Vegetariani`).
- SAC login redirects to `portal.sac-cas.ch`; selectors may change over time.
- If the site uses CAPTCHA/2FA, the script will fail by design.
- Use responsibly and respect the website’s terms of service.
