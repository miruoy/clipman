import logging

import dbus
import dbus.service

import clipman.shell_bridge as shell_bridge

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

    def _history_items(self):
        """Menu rows as (id, is_image, preview, content_type) tuples."""
        items = []
        for entry in self.app.db.get_entries(limit=self._menu_history_limit()):
            is_image = entry.get("content_type") == "image"
            text = entry.get("content_text") or ""
            preview = "" if is_image else " ".join(text.split())
            items.append((
                entry["id"], is_image, preview[:_MENU_PREVIEW_CHARS],
                entry.get("content_type") or "text",
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

    @dbus.service.method(IFACE, in_signature="", out_signature="a(ibss)")
    def GetHistory(self):
        """Recent entries for the shell panel menu (newest first).

        Each row is (id, is_image, preview, content_type); the preview is
        collapsed to one line and truncated so long clips stay off the bus.
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
