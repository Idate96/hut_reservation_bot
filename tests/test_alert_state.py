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


class FakeInput:
    def __init__(self, visible):
        self._visible = visible

    def is_visible(self):
        return self._visible


class FakeLocatorCollection:
    def __init__(self, items):
        self._items = items

    def count(self):
        return len(self._items)

    def nth(self, index):
        return self._items[index]


class AlertStateTests(unittest.TestCase):
    def test_text_indicates_overlap_dialog_in_italian(self):
        text = "Hai gia una prenotazione per lo stesso giorno. Vuoi procedere comunque?"
        self.assertTrue(book.text_indicates_overlap_dialog(text))

    def test_text_indicates_multi_booking_dialog(self):
        text = (
            "Prenotazione multipla rilevata! Hai gia prenotato uno o piu rifugi per questo periodo. "
            "MODIFICA DATA ALLE MIE PRENOTAZIONI IGNORA"
        )
        self.assertTrue(book.text_indicates_overlap_dialog(text))

    def test_text_indicates_overlap_dialog_does_not_match_generic_submit_text(self):
        text = "Si prega di verificare i dati della prenotazione e inviare."
        self.assertFalse(book.text_indicates_overlap_dialog(text))

    def test_label_matches_does_not_confuse_nome_with_cognome(self):
        self.assertTrue(book.label_matches("Nome", ["Nome"]))
        self.assertFalse(book.label_matches("Cognome", ["Nome"]))

    def test_extract_free_places_from_text_can_target_room_label(self):
        text = (
            "Data | Posti Liberi | Ven | 03.04.2026 | 8 | "
            "Dormitorio: | 8 | Camera doppia: | 0"
        )

        self.assertEqual(book.extract_free_places_from_text(text), 8)
        self.assertEqual(book.extract_free_places_from_text(text, room_label="Dormitorio"), 8)
        self.assertEqual(book.extract_free_places_from_text(text, room_label="Camera doppia"), 0)

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

    def test_error_state_preserves_last_open_count(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config()
            args = make_args(tmpdir)

            with patch("book.run_notify_command"):
                book.handle_open_alert(config, args, result={"free_places": 7})

            book.record_error_state(config, args, RuntimeError("Date range input(s) not found on availability step"))

            state_path = Path(tmpdir) / f"{book.config_tag(config)}.json"
            state = book.load_alert_state(state_path)
            self.assertEqual(state["status"], "error")
            self.assertEqual(state["last_open_free_places"], 7)
            self.assertEqual(
                state["last_reason"],
                "Date range input(s) not found on availability step",
            )

    def test_payload_includes_room_breakdown_for_any_room_alert(self):
        config = make_config()
        config["preferences"]["room_type"] = None
        args = make_args("/tmp/unused")

        payload = book.build_alert_payload(
            config,
            args,
            result={
                "free_places": 6,
                "room_counts": [
                    {"label": "Dormitorio", "free_places": 4},
                    {"label": "Camera doppia", "free_places": 2},
                ],
            },
        )

        self.assertIn("Visible free places detected: 6", payload["body"])
        self.assertIn(
            "Visible free places by room category: Dormitorio: 4, Camera doppia: 2",
            payload["body"],
        )

    def test_first_visible_locator_does_not_fallback_to_hidden_input(self):
        locator = FakeLocatorCollection([FakeInput(False)])
        self.assertIsNone(book.first_visible_locator(locator))

    def test_first_visible_locator_returns_visible_input(self):
        visible = FakeInput(True)
        locator = FakeLocatorCollection([FakeInput(False), visible])
        self.assertIs(book.first_visible_locator(locator), visible)


if __name__ == "__main__":
    unittest.main()
