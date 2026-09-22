import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from clipman.dbus_service import ClipmanDBusService

ROOT = Path(__file__).resolve().parent.parent

# Runs on a private bus from dbus-run-session, so the developer's real
# session bus and daemon are never touched. A child process owns the
# daemon name through the real service class; the parent then tries the
# same and must be refused instead of queued behind the child.
WORKER = textwrap.dedent(
    """
    import subprocess
    import sys
    import textwrap
    from unittest.mock import MagicMock

    import dbus
    import dbus.mainloop.glib

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    from clipman.dbus_service import BUS_NAME, ClipmanDBusService

    HOLDER = textwrap.dedent('''
        import time
        from unittest.mock import MagicMock
        import dbus.mainloop.glib
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        from clipman.dbus_service import ClipmanDBusService
        ClipmanDBusService(MagicMock(), MagicMock())
        print("holding", flush=True)
        time.sleep(30)
    ''')
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "holding"
        assert dbus.SessionBus().name_has_owner(BUS_NAME)
        try:
            ClipmanDBusService(MagicMock(), MagicMock())
        except dbus.exceptions.NameExistsException:
            print("second-owner-refused")
            sys.exit(0)
        print("second-owner-queued")
        sys.exit(1)
    finally:
        holder.kill()
    """
)


@unittest.skipUnless(shutil.which("dbus-run-session"), "dbus-run-session missing")
class TestSingleInstance(unittest.TestCase):
    """A second daemon must be refused the bus name, not queued."""

    def test_second_owner_is_refused(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
        env["PYTHONPATH"] = str(ROOT)
        result = subprocess.run(
            ["dbus-run-session", "--", sys.executable, "-c", WORKER],
            capture_output=True, timeout=30, cwd=ROOT, env=env, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr[-800:])
        self.assertIn("second-owner-refused", result.stdout)


class FakeBusName:
    """Stand-in for dbus.service.BusName so ClipmanDBusService can be built
    without a session bus. Object.__init__ unwraps the connection via
    get_bus(); a MagicMock swallows the export machinery that follows."""

    def __init__(self, *args, **kwargs):
        self._bus = MagicMock()

    def get_bus(self):
        return self._bus


class _ServiceTestCase(unittest.TestCase):
    """Base for in-process service-method tests (no bus required)."""

    def setUp(self):
        bus_patcher = patch("dbus.service.BusName", FakeBusName)
        bus_patcher.start()
        self.addCleanup(bus_patcher.stop)

        tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        self.data_dir = Path(tmpdir) / "clipman"
        db_patcher = patch.multiple(
            "clipman.database",
            DATA_DIR=self.data_dir,
            IMAGES_DIR=self.data_dir / "images",
            DB_PATH=self.data_dir / "clipman.db",
        )
        db_patcher.start()
        self.addCleanup(db_patcher.stop)

        from clipman.database import ClipboardDB

        self.app = MagicMock()
        self.app.db = ClipboardDB()
        self.window = MagicMock()
        self.monitor = MagicMock()
        self.service = ClipmanDBusService(self.window, self.app, self.monitor)


class TestActivateEntry(_ServiceTestCase):
    """ActivateEntry pastes a history row from the shell menu."""

    def test_known_id_pastes_via_the_window(self):
        entry_id = self.app.db.add_entry("text", content_text="paste me")
        self.service.ActivateEntry(entry_id)
        args, _ = self.window._paste_entry.call_args
        self.assertEqual(args[0]["id"], entry_id)
        self.assertEqual(args[0]["content_text"], "paste me")

    def test_unknown_id_is_a_silent_no_op(self):
        self.service.ActivateEntry(424242)
        self.window._paste_entry.assert_not_called()

    def test_out_signature_is_empty(self):
        self.assertEqual(
            ClipmanDBusService.ActivateEntry._dbus_out_signature, "")


class TestMenuActions(_ServiceTestCase):
    """Footer + per-row menu actions operate on the daemon's own state."""

    def test_clear_history_clears_unpinned_entries(self):
        self.app.db.add_entry("text", content_text="clip")
        self.service.ClearHistory()
        self.assertEqual(self.app.db.get_entries(), [])

    def test_delete_entry_removes_one_row(self):
        first = self.app.db.add_entry("text", content_text="one")
        second = self.app.db.add_entry("text", content_text="two")
        self.service.DeleteEntry(first)
        remaining = self.app.db.get_entries()
        self.assertEqual([e["id"] for e in remaining], [second])

    def test_set_incognito_flips_the_monitor(self):
        self.service.SetIncognito(True)
        self.monitor.set_incognito.assert_called_once_with(True)
        self.service.SetIncognito(False)
        self.monitor.set_incognito.assert_called_with(False)


class TestGetHistory(_ServiceTestCase):
    """GetHistory feeds the shell panel menu."""

    def test_out_signature_is_a_list_of_menu_items(self):
        self.assertEqual(
            ClipmanDBusService.GetHistory._dbus_out_signature, "a(ibss)")

    def test_returns_rows_newest_first(self):
        self.app.db.add_entry("text", content_text="first")
        self.app.db.add_entry("text", content_text="second")
        items = self.service.GetHistory()
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0][2], "second")
        self.assertEqual(items[1][2], "first")
        self.assertEqual(items[0][3], "text")

    def test_text_preview_is_single_line_and_truncated(self):
        self.app.db.add_entry("text", content_text="a\nb\tc" + "x" * 200)
        preview = self.service.GetHistory()[0][2]
        self.assertEqual(len(preview), 80)
        self.assertNotIn("\n", preview)
        self.assertNotIn("\t", preview)
        self.assertTrue(preview.startswith("a b c"))

    def test_image_rows_carry_the_flag_and_no_preview(self):
        self.app.db.add_entry("image", image_data=b"\x89PNG fake")
        entry_id, is_image, preview, content_type = self.service.GetHistory()[0]
        self.assertTrue(is_image)
        self.assertEqual(preview, "")
        self.assertEqual(content_type, "image")
        self.assertEqual(entry_id, self.app.db.get_entries()[0]["id"])

    def test_limit_comes_from_the_menu_history_limit_setting(self):
        for i in range(5):
            self.app.db.add_entry("text", content_text=f"clip {i}")
        self.app.db.set_setting("menu_history_limit", "2")
        self.assertEqual(len(self.service.GetHistory()), 2)

    def test_limit_defaults_to_30_when_unset(self):
        for i in range(3):
            self.app.db.add_entry("text", content_text=f"clip {i}")
        self.assertEqual(len(self.service.GetHistory()), 3)  # fewer than 30

    def test_limit_is_clamped_to_the_supported_range(self):
        for i in range(3):
            self.app.db.add_entry("text", content_text=f"clip {i}")
        self.app.db.set_setting("menu_history_limit", "0")
        self.assertEqual(len(self.service.GetHistory()), 1)
        self.app.db.set_setting("menu_history_limit", "-20")
        self.assertEqual(len(self.service.GetHistory()), 1)
        self.app.db.set_setting("menu_history_limit", "500")
        self.assertEqual(len(self.service.GetHistory()), 3)
        self.app.db.set_setting("menu_history_limit", "not-a-number")
        self.assertEqual(len(self.service.GetHistory()), 3)