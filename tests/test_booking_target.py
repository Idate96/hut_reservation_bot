import unittest

import book_until_target


class BookingTargetTests(unittest.TestCase):
    def test_parse_reservation_list_text_extracts_entries(self):
        text = """
        09.04.2026 - 10.04.2026
        more_vert
        priority_high
        Hai prenotato più capanne per questo periodo.
        Finsteraarhornhütte SAC
        Nr. di persone: 1
        Prenotazione: 6029096
        Stato: Annullato
        09.04.2026 - 10.04.2026
        more_vert
        Oberaarjochhütte SAC
        Nr. di persone: 2
        Prenotazione: 6029999
        Stato: Confermato
        """

        entries = book_until_target.parse_reservation_list_text(text)

        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["hut_name"], "Finsteraarhornhütte SAC")
        self.assertEqual(entries[0]["party_size"], 1)
        self.assertEqual(entries[0]["status"], "Annullato")
        self.assertEqual(entries[1]["hut_name"], "Oberaarjochhütte SAC")
        self.assertEqual(entries[1]["party_size"], 2)
        self.assertEqual(entries[1]["status"], "Confermato")


if __name__ == "__main__":
    unittest.main()
