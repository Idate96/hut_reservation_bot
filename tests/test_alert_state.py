import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import book


def make_config():
    return {
        "hut_name": "Test Hut, CH",
        "check_in": "2026-04-04",
        "check_out": "2026-04-05",
        "party_size": 1,
        "preferences": {"room_type": "dorm"},
        "alert": {
            "to": ["a@example.com"],
            "command": ".venv/bin/python send_alert_email.py",
            "any_party_size": True,
            "any_night": False,
        },
    }


def make_args(tmpdir):
    return argparse.Namespace(
        dry_run=False,
        alert_force_send=False,
        alert_state_dir=tmpdir,
        alert_only=True,
        notify_command=None,
    )


class AlertStateTests(unittest.TestCase):
    def test_same_count_is_suppressed_but_reopen_resends(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config()
            args = make_args(tmpdir)

            with patch("book.run_notify_command") as notify:
                first = book.handle_open_alert(config, args, result={"free_places": 1})
                second = book.handle_open_alert(config, args, result={"free_places": 1})
                book.record_closed_state(config, args, "full")
                third = book.handle_open_alert(config, args, result={"free_places": 1})

            self.assertEqual(first, "command")
            self.assertEqual(second, "suppressed")
            self.assertEqual(third, "command")
            self.assertEqual(notify.call_count, 2)

            state_path = Path(tmpdir) / f"{book.config_tag(config)}.json"
            state = book.load_alert_state(state_path)
            self.assertEqual(state["status"], "open")
            self.assertEqual(state["last_open_free_places"], 1)

    def test_changed_open_count_resends(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config()
            args = make_args(tmpdir)

            with patch("book.run_notify_command") as notify:
                first = book.handle_open_alert(config, args, result={"free_places": 1})
                second = book.handle_open_alert(config, args, result={"free_places": 2})
                third = book.handle_open_alert(config, args, result={"free_places": 2})

            self.assertEqual(first, "command")
            self.assertEqual(second, "command")
            self.assertEqual(third, "suppressed")
            self.assertEqual(notify.call_count, 2)


if __name__ == "__main__":
    unittest.main()
