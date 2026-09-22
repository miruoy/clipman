import logging

import dbus
import dbus.service

import clipman.shell_bridge as shell_bridge

from gi import require_version

require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402

BUS_NAME = "com.clipman.Daemon"
OBJ_PATH = "/com/clipman/Daemon"
IFACE = "com.clipman.Daemon"

logger = logging.getLogger(__name__)

# Panel-menu payload rules. Truncation happens here so full clip content
# never crosses the bus, and the row limit is a daemon-side setting
# (menu_history_limit) so the extension stays limit-agnostic.
_MENU_PREVIEW_CHARS = 80
_DEFAULT_MENU_HISTORY_LIMIT = 30
_MAX_MENU_HISTORY_LIMIT = 100

# Menu-row flag bits (the y field of the GetHistory struct).
MENU_FLAG_PINNED = 0b01
MENU_FLAG_SENSITIVE = 0b10


class ClipmanDBusService(dbus.service.Object):
    def __init__(self, window, app, monitor=None):
        bus = dbus.SessionBus()
        # do_not_queue: a second daemon fails here instead of waiting.
        bus_name = dbus.service.BusName(BUS_NAME, bus, do_not_queue=True)
        super().__init__(bus_name, OBJ_PATH)
        self.window = window
        self.app = app
        self.monitor = monitor

    def _menu_history_limit(self):
        """Clamped menu row count from the menu_history_limit setting."""
        try:
            raw = self.app.db.get_setting("menu_history_limit")
        except Exception:
            logger.debug("menu_history_limit lookup failed", exc_info=True)
            return _DEFAULT_MENU_HISTORY_LIMIT
        try:
            limit = int(raw)
        except (TypeError, ValueError):
            return _DEFAULT_MENU_HISTORY_LIMIT
        return max(1, min(_MAX_MENU_HISTORY_LIMIT, limit))

    def _image_dims(self, image_path):
        """(width, height) from the PNG header, (0, 0) when unknown."""
        if not image_path:
            return (0, 0)
        try:
            info = GdkPixbuf.Pixbuf.get_file_info(image_path)
        except Exception:
            logger.debug("image info read failed", exc_info=True)
            return (0, 0)
        # (fmt, width, height); width/height are -1 when undecodable.
        if info is None or info[0] is None or info[1] < 0:
            return (0, 0)
        return (info[1], info[2])

    def _menu_image_path(self, entry, sensitive):
        """The stored PNG path when the menu may show its thumbnail."""
        if sensitive or entry.get("content_type") != "image":
            return ""
        path = entry.get("image_path") or ""
        from clipman.database import _safe_image_path

        return path if _safe_image_path(path) else ""

    def _history_items(self):
        """Menu rows as (id, content_type, preview, ts, flags, detail1,
        detail2, image_path) tuples — the shape documented on GetHistory."""
        items = []
        for entry in self.app.db.get_entries(limit=self._menu_history_limit()):
            is_image = entry.get("content_type") == "image"
            text = entry.get("content_text") or ""
            sensitive = bool(entry.get("sensitive"))
            preview = "" if is_image or sensitive else " ".join(text.split())
            flags = 0
            if entry.get("pinned"):
                flags |= MENU_FLAG_PINNED
            if sensitive:
                flags |= MENU_FLAG_SENSITIVE
            if is_image:
                width, height = self._image_dims(entry.get("image_path"))
                detail1, detail2 = width, height
            else:
                detail1, detail2 = len(text), 0
            items.append((
                entry["id"],
                entry.get("content_type") or "text",
                preview[:_MENU_PREVIEW_CHARS],
                int(entry.get("accessed_at") or 0),
                flags,
                detail1,
                detail2,
                self._menu_image_path(entry, sensitive),
            ))
        return items

    @dbus.service.method(IFACE, in_signature="", out_signature="")
    def Toggle(self):
        """Show/hide the UI: the panel menu when the extension supports it,
        otherwise the popup window."""
        if not shell_bridge.toggle_menu():
            self.window.toggle()

    @dbus.service.method(IFACE, in_signature="", out_signature="")
    def Show(self):
        # GTK 4 dropped ``show_all`` / ``hide`` — every widget is
        # visible by default, so ``set_visible(True/False)`` is the
        # canonical API.
        self.window.refresh()
        # Shared show path: present + idle grab_focus + extension activate
        # (matches ClipmanWindow.toggle()), so Show() also gets real input
        # focus on GNOME Wayland instead of a visible-but-dead popup.
        self.window._present_focused()

    @dbus.service.method(IFACE, in_signature="", out_signature="")
    def Hide(self):
        self.window.set_visible(False)

    @dbus.service.method(IFACE, in_signature="", out_signature="")
    def Quit(self):
        self.app.quit()

    @dbus.service.method(IFACE, in_signature="ss", out_signature="")
    def NewEntry(self, content_type, content):
        """Called by the GNOME Shell extension when clipboard changes."""
        if self.monitor is None:
            return
        if content_type == "text" and content:
            self.monitor.handle_new_text(content)
        elif content_type == "image":
            self.monitor.handle_new_image()

    @dbus.service.method(IFACE, in_signature="", out_signature="a(usstyuus)")
    def GetHistory(self):
        """Recent entries for the shell panel menu (newest first).

        Each row is (u id, s content_type, s preview, t accessed_at,
        y flags, u detail1, u detail2, s image_path): content_type is
        ``text`` or ``image``; preview is the 80-char single-line preview,
        empty for images and sensitive entries; flags carry bit 0 = pinned
        and bit 1 = sensitive; detail1/detail2 are the char count for text
        or the pixel width/height for images (0 when unknown); image_path
        is the stored PNG path for non-sensitive image rows and empty
        otherwise, so the menu can render a thumbnail. The preview stays
        truncated so long clips never cross the bus.
        """
        return self._history_items()

    @dbus.service.method(IFACE, in_signature="u", out_signature="")
    def ActivateEntry(self, entry_id):
        """Paste one history entry from the shell panel menu.

        Delegates to the window's paste path (copy + keystroke via the
        extension). The popup window is never shown; unknown ids are
        dropped silently — the menu may be showing a stale row.
        """
        entry = self.app.db.get_entry(entry_id)
        if entry is None:
            logger.debug("ActivateEntry: id %s not found", entry_id)
            return
        self.window._paste_entry(entry)

    @dbus.service.method(IFACE, in_signature="", out_signature="")
    def ClearHistory(self):
        """Clear the unpinned history (shell menu footer action)."""
        self.app.db.clear_unpinned()

    @dbus.service.method(IFACE, in_signature="u", out_signature="")
    def DeleteEntry(self, entry_id):
        """Delete one history entry (shell menu per-row action)."""
        self.app.db.delete_entry(entry_id)

    @dbus.service.method(IFACE, in_signature="b", out_signature="")
    def SetIncognito(self, paused):
        """Flip the incognito state (shell menu footer toggle).

        Goes through the monitor so the extension pause bridge fires the
        same way it does from the popup's header toggle.
        """
        if self.monitor is None:
            return
        self.monitor.set_incognito(bool(paused))
