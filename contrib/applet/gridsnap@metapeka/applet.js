// GridSnap — панельный апплет Cinnamon.
//
// Апплет не содержит своей логики раскладки: он лишь зовёт существующие
// CLI-скрипты GridSnap из каталога, заданного в настройках:
//   * zone-tiler.py list           — зоны и рабочие области;
//   * zone-tiler.py apply <зона>   — вложить активное окно в зону;
//   * zone-tiler.py arrange|undo   — разложить окна / вернуть как было;
//   * zone-editor.py               — визуальный редактор сетки;
//   * setup-hotkeys.py             — сочетания клавиш Cinnamon.
// Сетка, правила и история раскладок остаются в одном месте — в самом GridSnap,
// поэтому апплет ничего не знает про формат zones.conf и не дублирует его.

const Applet = imports.ui.applet;
const PopupMenu = imports.ui.popupMenu;
const Settings = imports.ui.settings;
const Util = imports.misc.util;
const Main = imports.ui.main;
const GLib = imports.gi.GLib;
const Gio = imports.gi.Gio;

const TILER = "zone-tiler.py";
const EDITOR = "zone-editor.py";
const HOTKEYS = "setup-hotkeys.py";
const DEFAULT_DIR = "~/thinkrail/gridsnape";

// Строка зоны из `zone-tiler.py list`: "  main  x=0  y=40  1605x1040".
const ZONE_RE = /^\s*(\S+)\s+x=(-?\d+)\s+y=(-?\d+)\s+(\d+)x(\d+)\s*$/;
// Строка рабочей области: "рабочая область монитора 0: 0,40 1920x1040".
const WORKAREA_RE = /^рабочая область монитора\s+(\d+):\s+(-?\d+),(-?\d+)\s+(\d+)x(\d+)/;

// Развернуть "~" и "~/" в абсолютный путь: настройки хранят путь строкой.
function expandHome(path) {
    if (!path)
        return "";
    if (path === "~")
        return GLib.get_home_dir();
    if (path.startsWith("~/"))
        return GLib.build_filenamev([GLib.get_home_dir(), path.slice(2)]);
    return path;
}

class GridSnapApplet extends Applet.TextIconApplet {
    constructor(metadata, orientation, panel_height, instance_id) {
        super(orientation, panel_height, instance_id);

        this.metadata = metadata;
        this.appletDir = metadata.path;

        this.settings = new Settings.AppletSettings(this, metadata.uuid, instance_id);
        this.settings.bind("gridsnap-dir", "gridsnap_dir", this._on_settings_changed);
        this.settings.bind("python-bin", "python_bin", this._on_settings_changed);
        this.settings.bind("use-label", "use_label", this._on_settings_changed);
        this.settings.bind("label-text", "label_text", this._on_settings_changed);

        this.menuManager = new PopupMenu.PopupMenuManager(this);
        this.menu = new Applet.AppletPopupMenu(this, orientation);
        this.menuManager.addMenu(this.menu);

        // Две копии пунктов: своё меню по левому клику и контекстное меню
        // Cinnamon по правому. Обе обновляются при открытии.
        this._sections = [];
        this._addSection(this.menu);
        this._addSection(this._applet_context_menu);

        this.menu.connect("open-state-changed", (_menu, open) => {
            if (open)
                this._refresh();
        });
        this._applet_context_menu.connect("open-state-changed", (_menu, open) => {
            if (open)
                this._refresh();
        });

        this.setAllowedLayout(Applet.AllowedLayout.BOTH);
        this._on_settings_changed();
    }

    // ------------------------------------------------------------- настройки

    _gridsnapDir() {
        return expandHome(this.gridsnap_dir || DEFAULT_DIR);
    }

    _pythonBin() {
        return (this.python_bin || "").trim() || "python3";
    }

    _on_settings_changed() {
        if (this.use_label)
            this.set_applet_label(this.label_text || "GridSnap");
        else
            this.set_applet_label("");

        let icon = GLib.build_filenamev([this.appletDir, "icon.png"]);
        if (GLib.file_test(icon, GLib.FileTest.EXISTS))
            this.set_applet_icon_path(icon);
        else
            this.set_applet_icon_symbolic_name("view-grid-symbolic");

        this.set_applet_tooltip(_("GridSnap — зоны окон"));
    }

    // ----------------------------------------------------------------- меню

    _addSection(menu) {
        let section = {};

        section.header = new PopupMenu.PopupMenuItem("GridSnap", { reactive: false });
        section.header.setSensitive(false);
        menu.addMenuItem(section.header);
        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        menu.addMenuItem(this._action(_("Открыть редактор"), () => this._spawn(EDITOR, [])));
        menu.addMenuItem(this._action(_("Применить сочетания"), () => this._spawn(HOTKEYS, [])));
        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        section.zones = new PopupMenu.PopupSubMenuMenuItem(_("Зоны"));
        menu.addMenuItem(section.zones);

        section.workareas = new PopupMenu.PopupSubMenuMenuItem(_("Рабочие области"));
        menu.addMenuItem(section.workareas);

        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        menu.addMenuItem(this._action(_("Разложить окна по правилам"),
                                      () => this._spawn(TILER, ["arrange"])));
        menu.addMenuItem(this._action(_("Отменить последнюю раскладку"),
                                      () => this._spawn(TILER, ["undo"])));

        this._sections.push(section);
        return section;
    }

    _action(label, callback) {
        let item = new PopupMenu.PopupMenuItem(label);
        item.connect("activate", () => callback());
        return item;
    }

    _placeholder(menu, text) {
        let item = new PopupMenu.PopupMenuItem(text, { reactive: false, style_class: "gridsnap-hint" });
        item.setSensitive(false);
        menu.addMenuItem(item);
    }

    // -------------------------------------------------------------- обновление

    _refresh() {
        this._refreshZones();
        this._refreshWorkareas();
    }

    _refreshZones() {
        for (let section of this._sections)
            section.zones.menu.removeAll();

        this._list(null, (stdout, _stderr, ok) => {
            let zones = [];
            if (ok) {
                for (let line of stdout.split("\n")) {
                    let m = line.match(ZONE_RE);
                    if (m) {
                        zones.push({
                            name: m[1],
                            x: parseInt(m[2], 10),
                            y: parseInt(m[3], 10),
                            w: parseInt(m[4], 10),
                            h: parseInt(m[5], 10),
                        });
                    }
                }
            }

            for (let section of this._sections) {
                section.header.setLabel("GridSnap — " + _("зон:") + " " + zones.length);
                if (!ok || zones.length === 0) {
                    this._placeholder(section.zones.menu,
                        ok ? _("Зон нет") : _("Не удалось прочитать список зон"));
                    continue;
                }
                for (let zone of zones) {
                    let label = zone.name + "   " + zone.w + "×" + zone.h +
                                "   " + zone.x + "," + zone.y;
                    section.zones.menu.addMenuItem(
                        this._action(label, () => this._applyZone(zone.name)));
                }
            }
        });
    }

    _refreshWorkareas() {
        for (let section of this._sections)
            section.workareas.menu.removeAll();

        let monitors = Main.layoutManager.monitors || [];
        if (monitors.length === 0) {
            for (let section of this._sections)
                this._placeholder(section.workareas.menu, _("Мониторы не найдены"));
            return;
        }

        // Несколько вызовов `list --monitor N`; собираем строки по индексам,
        // чтобы порядок не зависел от того, кто ответил первым.
        let lines = [];
        let pending = monitors.length;
        for (let i = 0; i < monitors.length; i++) {
            let index = i;
            this._list(index, (stdout, _stderr, ok) => {
                let first = (stdout.split("\n")[0] || "").trim();
                let m = first.match(WORKAREA_RE);
                if (ok && m) {
                    lines[index] = _("Монитор ") + m[1] + ": " + m[4] + "×" + m[5] +
                                   "   " + m[2] + "," + m[3];
                } else if (ok && first) {
                    lines[index] = first;
                } else {
                    lines[index] = _("Монитор ") + index + ": " + _("нет данных");
                }

                pending--;
                if (pending > 0)
                    return;
                for (let section of this._sections) {
                    for (let text of lines)
                        this._placeholder(section.workareas.menu, text);
                }
            });
        }
    }

    // ------------------------------------------------------------ запуск CLI

    _list(monitor, callback) {
        let args = [TILER, "list"];
        if (monitor !== null && monitor !== undefined)
            args.push("--monitor", String(monitor));
        this._run(args, callback);
    }

    // Запустить скрипт GridSnap и вернуть его вывод в callback(stdout, stderr, ok).
    _run(relArgs, callback) {
        let argv = [this._pythonBin(),
                    GLib.build_filenamev([this._gridsnapDir(), relArgs[0]])];
        for (let i = 1; i < relArgs.length; i++)
            argv.push(relArgs[i]);

        let proc;
        try {
            proc = Gio.Subprocess.new(
                argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE);
        } catch (e) {
            callback("", String(e), false);
            return;
        }

        proc.communicate_utf8_async(null, null, (p, res) => {
            let stdout = "", stderr = "", ok = false;
            try {
                let [, out, err] = p.communicate_utf8_finish(res);
                stdout = out || "";
                stderr = err || "";
                ok = p.get_successful();
            } catch (e) {
                stderr = String(e);
            }
            callback(stdout, stderr, ok);
        });
    }

    // Запустить и забыть: редактор и установка сочетаний живут сами по себе.
    _spawn(script, args) {
        let argv = [this._pythonBin(),
                    GLib.build_filenamev([this._gridsnapDir(), script])];
        for (let arg of args)
            argv.push(arg);
        Util.spawn(argv);
    }

    _applyZone(name) {
        this._run([TILER, "apply", name], (_stdout, stderr, ok) => {
            if (!ok) {
                let message = (stderr || "").trim() || _("не удалось применить зону");
                Main.notify("GridSnap", message);
            }
        });
    }

    // --------------------------------------------------------------- события

    on_applet_clicked(_event) {
        this.menu.toggle();
    }

    on_applet_removed_from_panel() {
        this.settings.finalize();
    }
}

// Точка входа апплета: Cinnamon вызывает main() сама (imports/ui/appletManager.js),
// поэтому «неиспользуемость» в файле — ожидаемое поведение, а не мёртвый код.
// eslint-disable-next-line no-unused-vars
function main(metadata, orientation, panel_height, instance_id) {
    return new GridSnapApplet(metadata, orientation, panel_height, instance_id);
}
