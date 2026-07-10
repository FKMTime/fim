import unittest

from fim.progress import get_progress, progress_log_line, progress_reset


class ProgressLogSeqTests(unittest.TestCase):
    def test_log_seq_increments_on_append(self):
        progress_reset(["Test"])
        progress_log_line("line one")
        first = get_progress()["log_seq"]
        progress_log_line("line two")
        second = get_progress()["log_seq"]
        self.assertGreater(second, first)

    def test_log_seq_resets_with_progress(self):
        progress_reset(["Test"])
        progress_log_line("line one")
        progress_reset(["Again"])
        self.assertEqual(get_progress()["log_seq"], 0)


if __name__ == "__main__":
    unittest.main()
