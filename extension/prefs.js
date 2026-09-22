import Adw from 'gi://Adw';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';

// The popup's Wayland app_id / wm_class (clipman/app.py application_id).
const DAEMON_BUS_NAME = 'com.clipman.Daemon';
const DAEMON_OBJECT_PATH = '/com/clipman/Daemon';

// Mirror of the daemon's prefs whitelist (GetSetting/SetSetting only
// accept these keys). Defaults match window.py/preferences.py.
const SETTING_DEFAULTS = {
    menu_history_limit: 30,
    show_count_badges: true,
    incognito_on_launch: false,
    sensitive_autoclear: true,
    sensitive_timeout: 30,
};

// GNOME 50 instantiates `new prefsModule.default({...metadata, dir, path})`
// and then calls `prefsObj.fillPreferencesWindow(window)` on the instance.
// GNOME 45-49 instead call the module-level init()/fillPreferencesWindow()
// functions. Support both: the default-exported class is the GNOME 50 entry
// point, the function exports are the 45-49 entry point. Everything built
// must be a page inside the Adw.PreferencesWindow the shell provides.
class ClipmanPreferences {
    constructor(metadata) {
        this.metadata = metadata;
    }

    fillPreferencesWindow(window) {
        window.add(new ClipmanPreferencesPage());
    }
}

export default ClipmanPreferences;

export function init() {}

export function fillPreferencesWindow(window) {
    window.add(new ClipmanPreferencesPage());
}

const ClipmanPreferencesPage = GObject.registerClass(
class ClipmanPreferencesPage extends Adw.PreferencesPage {
    constructor() {
        super({
            title: 'General',
            icon_name: 'edit-paste-symbolic',
        });
        this._daemonAvailable = false;

        // --- History ---------------------------------------------------
        this._historyGroup = new Adw.PreferencesGroup({
            title: 'History',
            description: 'What the panel menu and popup window show.',
        });
        this.add(this._historyGroup);

        this._menuLimitRow = Adw.SpinRow.new_with_range(1, 100, 1);
        this._menuLimitRow.title = 'Panel menu size';
        this._menuLimitRow.subtitle =
            'Number of items shown in the top-bar dropdown';
        this._historyGroup.add(this._menuLimitRow);

        this._badgesRow = new Adw.SwitchRow({
            title: 'Count badges',
            subtitle: 'Show the entry count next to section headers',
        });
        this._historyGroup.add(this._badgesRow);

        // --- Privacy ---------------------------------------------------
        const privacyGroup = new Adw.PreferencesGroup({
            title: 'Privacy',
        });
        this.add(privacyGroup);

        this._incognitoRow = new Adw.SwitchRow({
            title: 'Start in incognito mode',
            subtitle: 'Pause clipboard recording when the daemon starts',
        });
        privacyGroup.add(this._incognitoRow);

        this._autoclearRow = new Adw.SwitchRow({
            title: 'Auto-clear sensitive entries',
            subtitle:
                'Drop password-manager and marked-sensitive clips automatically',
        });
        privacyGroup.add(this._autoclearRow);

        this._timeoutRow = Adw.SpinRow.new_with_range(10, 300, 5);
        this._timeoutRow.title = 'Sensitive entry lifetime';
        this._timeoutRow.subtitle = 'Seconds before sensitive clips are purged';
        privacyGroup.add(this._timeoutRow);

        this._connectRows();
        this._loadSettings();
    }

    _connectRows() {
        this._menuLimitRow.connect('changed', row => {
            this._saveSetting('menu_history_limit', row.value);
        });
        this._badgesRow.connect('notify::active', row => {
            this._saveSetting('show_count_badges', row.active);
        });
        this._incognitoRow.connect('notify::active', row => {
            this._saveSetting('incognito_on_launch', row.active);
        });
        this._autoclearRow.connect('notify::active', row => {
            this._saveSetting('sensitive_autoclear', row.active);
        });
        this._timeoutRow.connect('changed', row => {
            this._saveSetting('sensitive_timeout', row.value);
        });
    }

    // ---- D-Bus ----------------------------------------------------------

    _getSetting(key) {
        return new Promise(resolve => {
            Gio.DBus.session.call(
                DAEMON_BUS_NAME,
                DAEMON_OBJECT_PATH,
                DAEMON_BUS_NAME,
                'GetSetting',
                new GLib.Variant('(s)', [key]),
                new GLib.VariantType('(s)'),
                Gio.DBusCallFlags.NO_AUTO_START,
                -1,
                null,
                (connection, result) => {
                    try {
                        const [value] =
                            connection.call_finish(result).deepUnpack();
                        resolve(value);
                    } catch {
                        resolve(null);
                    }
                }
            );
        });
    }

    _saveSetting(key, value) {
        if (!this._daemonAvailable)
            return;
        // Bools are stored the way window.py reads them back.
        const stored = typeof value === 'boolean'
            ? (value ? 'true' : 'false')
            : String(value);
        Gio.DBus.session.call(
            DAEMON_BUS_NAME,
            DAEMON_OBJECT_PATH,
            DAEMON_BUS_NAME,
            'SetSetting',
            new GLib.Variant('(ss)', [key, stored]),
            null,
            Gio.DBusCallFlags.NO_AUTO_START,
            -1,
            null,
            (connection, result) => {
                try {
                    connection.call_finish(result);
                } catch (e) {
                    console.debug(
                        `clipman: SetSetting(${key}) failed: ${e.message}`);
                }
            }
        );
    }

    async _loadSettings() {
        for (const key of Object.keys(SETTING_DEFAULTS)) {
            const raw = await this._getSetting(key);
            if (raw === null) {
                // Daemon absent: show why and leave the rows insensitive.
                if (!this._daemonAvailable) {
                    this._markDaemonUnavailable();
                }
                return;
            }
            this._daemonAvailable = true;
            this._applySetting(key, raw);
        }
    }

    _applySetting(key, raw) {
        const def = SETTING_DEFAULTS[key];
        switch (key) {
        case 'menu_history_limit':
            this._menuLimitRow.value = raw ? Number(raw) : def;
            break;
        case 'show_count_badges':
            this._badgesRow.active = raw ? raw === 'true' : def;
            break;
        case 'incognito_on_launch':
            this._incognitoRow.active = raw ? raw === 'true' : def;
            break;
        case 'sensitive_autoclear':
            this._autoclearRow.active = raw ? raw === 'true' : def;
            break;
        case 'sensitive_timeout':
            this._timeoutRow.value = raw ? Number(raw) : def;
            break;
        }
    }

    _markDaemonUnavailable() {
        // Adw.Banner is not allowed inside a PreferencesPage/Group; an
        // ActionRow carries the same warning.
        const warning = new Adw.ActionRow({
            title: 'Clipman daemon is not running — settings cannot be loaded',
        });
        warning.add_css_class('warning');
        this._historyGroup.add(warning);
        for (const row of [
            this._menuLimitRow,
            this._badgesRow,
            this._incognitoRow,
            this._autoclearRow,
            this._timeoutRow,
        ]) {
            row.sensitive = false;
        }
    }
});
