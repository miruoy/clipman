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


class TestTogglePrefersTheMenu(_ServiceTestCase):
    """Toggle opens the panel menu when the extension supports it."""

    def test_menu_bridge_handled_the_toggle(self):
        with patch("clipman.dbus_service.shell_bridge.toggle_menu",
                   return_value=True) as toggle_menu:
            self.service.Toggle()
        toggle_menu.assert_called_once_with()
        self.window.toggle.assert_not_called()

    def test_window_fallback_when_the_extension_is_absent(self):
        with patch("clipman.dbus_service.shell_bridge.toggle_menu",
                   return_value=False):
            self.service.Toggle()
        self.window.toggle.assert_called_once_with()


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
    """GetHistory feeds the shell panel menu.

    Row shape ``(u id, s content_type, s preview, t accessed_at, y flags,
    u detail1, u detail2, s image_path)``:

    - ``content_type`` distinguishes image rows (``image`` vs ``text``).
    - ``preview`` is the 80-char single-line text preview; empty for
      images and sensitive entries (the menu never sees their content).
    - ``accessed_at`` is the entry's epoch timestamp so the menu can
      render the same relative times as the window.
    - ``flags``: bit 0 = pinned, bit 1 = sensitive.
    - ``detail1``/``detail2``: char count for text (detail2 always 0),
      pixel width/height for images (0 when unknown).
    - ``image_path`` is the stored PNG path for image rows, empty
      otherwise and never for sensitive entries.
    """

    FLAG_PINNED = 0b01
    FLAG_SENSITIVE = 0b10

    def test_out_signature_is_a_list_of_menu_items(self):
        self.assertEqual(
            ClipmanDBusService.GetHistory._dbus_out_signature,
            "a(usstyuus)")

    def test_returns_rows_newest_first(self):
        self.app.db.add_entry("text", content_text="first")
        self.app.db.add_entry("text", content_text="second")
        items = self.service.GetHistory()
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0][1], "text")
        self.assertEqual(items[0][2], "second")
        self.assertEqual(items[1][2], "first")

    def test_text_preview_is_single_line_and_truncated(self):
        self.app.db.add_entry("text", content_text="a\nb\tc" + "x" * 200)
        preview = self.service.GetHistory()[0][2]
        self.assertEqual(len(preview), 80)
        self.assertNotIn("\n", preview)
        self.assertNotIn("\t", preview)
        self.assertTrue(preview.startswith("a b c"))

    def test_text_rows_carry_the_char_count(self):
        self.app.db.add_entry("text", content_text="hello")
        _id, ctype, _preview, _ts, _flags, n_chars, detail2, image_path = (
            self.service.GetHistory()[0])
        self.assertEqual(ctype, "text")
        self.assertEqual(n_chars, 5)
        self.assertEqual(detail2, 0)
        self.assertEqual(image_path, "")

    def test_rows_carry_the_accessed_timestamp(self):
        self.app.db.add_entry("text", content_text="stamped")
        ts = self.service.GetHistory()[0][3]
        self.assertIsInstance(ts, int)
        self.assertGreater(ts, 0)
        entry = self.app.db.get_entries()[0]
        self.assertEqual(ts, int(entry["accessed_at"]))

    def test_image_rows_carry_dims_and_path(self):
        self.app.db.add_entry("image", image_data=b"\x89PNG fake")
        row = self.service.GetHistory()[0]
        _id, ctype, preview, _ts, _flags, width, height, image_path = row
        self.assertEqual(ctype, "image")
        self.assertEqual(preview, "")
        # The fake PNG is undecodable, so the dims are unknown but present.
        self.assertEqual((width, height), (0, 0))
        self.assertTrue(image_path.endswith(".png"))
        self.assertIn(str(self.data_dir / "images"), image_path)

    def test_pinned_rows_set_the_pinned_flag(self):
        entry_id = self.app.db.add_entry("text", content_text="pinned")
        self.app.db.toggle_pin(entry_id)
        _id, _ctype, _preview, _ts, flags, _n, _d2, _path = (
            self.service.GetHistory()[0])
        self.assertTrue(flags & self.FLAG_PINNED)

    def test_sensitive_rows_are_masked(self):
        self.app.db.add_entry("text", content_text="secret", sensitive=True)
        _id, ctype, preview, _ts, flags, _n, _d2, _path = (
            self.service.GetHistory()[0])
        self.assertEqual(ctype, "text")
        self.assertEqual(preview, "")
        self.assertTrue(flags & self.FLAG_SENSITIVE)

    def test_sensitive_images_hide_the_thumbnail_path(self):
        self.app.db.add_entry(
            "image", image_data=b"\x89PNG fake", sensitive=True)
        _id, ctype, preview, _ts, flags, _w, _h, image_path = (
            self.service.GetHistory()[0])
        self.assertEqual(ctype, "image")
        self.assertEqual(preview, "")
        self.assertTrue(flags & self.FLAG_SENSITIVE)
        self.assertEqual(image_path, "")

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


class TestSettings(_ServiceTestCase):
    """GetSetting/SetSetting expose app settings to the prefs dialog."""

    def test_get_setting_returns_the_stored_value(self):
        self.app.db.set_setting("menu_history_limit", "42")
        self.assertEqual(self.service.GetSetting("menu_history_limit"), "42")

    def test_get_setting_returns_empty_for_unset_keys(self):
        self.assertEqual(self.service.GetSetting("show_count_badges"), "")

    def test_get_setting_rejects_unknown_keys(self):
        self.assertEqual(self.service.GetSetting("not_a_setting"), "")
        self.app.db.set_setting("evil", "1")
        self.assertEqual(self.service.GetSetting("evil"), "")

    def test_set_setting_stores_known_keys(self):
        self.service.SetSetting("menu_history_limit", "7")
        self.assertEqual(self.app.db.get_setting("menu_history_limit"), "7")

    def test_set_setting_rejects_unknown_keys(self):
        self.service.SetSetting("evil", "1")
        self.assertIsNone(self.app.db.get_setting("evil"))

    def test_settings_round_trip_values_used_by_the_prefs_dialog(self):
        for key, value in (
            ("menu_history_limit", "30"),
            ("show_count_badges", "true"),
            ("incognito_on_launch", "false"),
            ("sensitive_autoclear", "true"),
            ("sensitive_timeout", "60"),
        ):
            self.service.SetSetting(key, value)
            self.assertEqual(self.service.GetSetting(key), value)