import subprocess
import unittest
from unittest.mock import patch

from log_viewer import PAGE, bounded_line_count, client_is_private, read_journal, service_status


class LogViewerTests(unittest.TestCase):
    def test_embedded_javascript_preserves_newline_escapes(self):
        self.assertIn(r"raw.split('\n')", PAGE)
        self.assertIn(r".join('\n')", PAGE)

    def test_allows_private_and_loopback_clients(self):
        self.assertTrue(client_is_private("10.0.0.25"))
        self.assertTrue(client_is_private("192.168.1.8"))
        self.assertTrue(client_is_private("127.0.0.1"))

    def test_rejects_public_and_invalid_clients(self):
        self.assertFalse(client_is_private("8.8.8.8"))
        self.assertFalse(client_is_private("not-an-address"))

    def test_bounds_requested_line_count(self):
        self.assertEqual(bounded_line_count("800"), 800)
        self.assertEqual(bounded_line_count("999999"), 2000)
        self.assertEqual(bounded_line_count("0"), 1)
        self.assertEqual(bounded_line_count("invalid"), 400)

    @patch("log_viewer.subprocess.run")
    def test_journal_command_has_fixed_unit_and_bounded_arguments(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "logs\n", "")

        self.assertEqual(read_journal("opticharge.service", 400), "logs\n")

        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["journalctl", "-u", "opticharge.service"])
        self.assertIn("400", command)
        self.assertNotIn("shell", run.call_args.kwargs)

    @patch("log_viewer.subprocess.run")
    def test_service_status_is_read_only(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "active\n", "")

        self.assertEqual(service_status("opticharge.service"), "active")
        self.assertEqual(
            run.call_args.args[0],
            ["systemctl", "is-active", "opticharge.service"],
        )


if __name__ == "__main__":
    unittest.main()
