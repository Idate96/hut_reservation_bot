#!/usr/bin/env python3

import argparse
import os
import smtplib
import sys
from email.message import EmailMessage

from dotenv import load_dotenv


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--to")
    parser.add_argument("--subject")
    parser.add_argument("--body")
    return parser.parse_args()


def require_text(value, name):
    assert value is not None, f"{name} is required"
    value = value.strip()
    assert value, f"{name} must not be empty"
    return value


def parse_recipients(value):
    recipients = [part.strip() for part in value.split(",") if part.strip()]
    assert recipients, "recipient list must contain at least one address"
    return recipients


def smtp_settings():
    username = require_text(os.getenv("ALERT_SMTP_USERNAME"), "ALERT_SMTP_USERNAME")
    password = require_text(os.getenv("ALERT_SMTP_PASSWORD"), "ALERT_SMTP_PASSWORD")
    sender = os.getenv("ALERT_EMAIL_FROM") or username
    host = os.getenv("ALERT_SMTP_HOST") or "smtp.gmail.com"
    security = (os.getenv("ALERT_SMTP_SECURITY") or "starttls").strip().lower()
    assert security in {"starttls", "ssl"}, "ALERT_SMTP_SECURITY must be starttls or ssl"
    port = int(os.getenv("ALERT_SMTP_PORT") or ("465" if security == "ssl" else "587"))
    return {
        "username": username,
        "password": password,
        "sender": sender,
        "host": host,
        "security": security,
        "port": port,
    }


def message_from_env(args):
    recipients = parse_recipients(require_text(args.to or os.getenv("HUT_ALERT_TO"), "recipient list"))
    subject = require_text(args.subject or os.getenv("HUT_ALERT_SUBJECT"), "subject")
    body = require_text(args.body or os.getenv("HUT_ALERT_BODY"), "body")
    return recipients, subject, body


def send_email(recipients, subject, body):
    settings = smtp_settings()

    message = EmailMessage()
    message["From"] = settings["sender"]
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.set_content(body)

    if settings["security"] == "ssl":
        server = smtplib.SMTP_SSL(settings["host"], settings["port"], timeout=30)
    else:
        server = smtplib.SMTP(settings["host"], settings["port"], timeout=30)

    try:
        server.ehlo()
        if settings["security"] == "starttls":
            server.starttls()
            server.ehlo()
        server.login(settings["username"], settings["password"])
        server.send_message(message)
    finally:
        try:
            server.quit()
        except Exception:
            server.close()

    print(f"alert email sent via smtp to {', '.join(recipients)}")


def main():
    load_dotenv()
    args = parse_args()
    send_email(*message_from_env(args))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"send_alert_email.py failed: {exc}", file=sys.stderr)
        sys.exit(1)
