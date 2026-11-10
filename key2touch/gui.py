"""Control window (libadwaita); owns the engine process and the HUD.

The main page follows what a user does, top to bottom: edit the layout,
turn mapping on (with its hotkey next to it), pick the display.  Everything
else lives in the advanced settings dialog.
"""

import copy
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import desktop, perms, profile as prof, tray  # noqa: E402
from .mapper import MOD_ORDER  # noqa: E402
from .overlay import MOD_LABEL, MOD_NAME_GROUP, LS, Overlay, combo_label, key_name  # noqa: E402

CSS = b"""
window.key2touch-overlay, window.key2touch-overlay > * { background: transparent; }
.key2touch-bar, .key2touch-panel {
  background: alpha(@window_bg_color, 0.94);
  border-radius: 14px; padding: 8px;
  box-shadow: 0 4px 18px rgba(0,0,0,0.35);
}
.key2touch-panel { padding: 16px; }
.key2touch-toast {
  background: alpha(@window_bg_color, 0.94); border-radius: 99px; padding: 6px 16px;
}
.key2touch-hero { padding: 18px 48px; font-size: 1.25em; font-weight: bold; }
"""


class EngineProc:
    def __init__(self, on_msg, enabled=True):
        env = dict(os.environ)
        env["KEY2TOUCH_ENABLED"] = "1" if enabled else "0"  # start in the remembered state
        env.pop("LD_PRELOAD", None)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        prof.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.log = open(prof.CACHE_DIR / "engine.log", "ab")
        self.p = subprocess.Popen([sys.executable, "-m", "key2touch.engine"], env=env,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log)
        self.on_msg = on_msg
        self.buf = b""
        GLib.io_add_watch(self.p.stdout.fileno(), GLib.PRIORITY_HIGH,
                          GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self._read)

    def _read(self, fd, cond):
        data = os.read(fd, 65536)
        if not data:
            self.on_msg({"ev": "exit", "code": self.p.poll()})
            return False
        self.buf += data
        *lines, self.buf = self.buf.split(b"\n")
        for line in lines:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            try:
                self.on_msg(msg)
            except Exception:  # never let one bad message detach us from the engine
                traceback.print_exc()
        return True

    def send(self, **msg):
        try:
            self.p.stdin.write((json.dumps(msg) + "\n").encode())
            self.p.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass

    def stop(self):
        self.send(cmd="quit")
        try:
            self.p.wait(2)
        except subprocess.TimeoutExpired:
            self.p.kill()


class NullEngine:
    """Stands in until the session may read keyboards; every command is dropped."""

    def send(self, **msg):
        pass

    def stop(self):
        pass


def gdk_outputs():
    """connector -> logical geometry. GDK reports the same logical rectangle
    the compositor maps touch onto and the layer surface fills, on any desktop."""
    out = {}
    mons = Gdk.Display.get_default().get_monitors()
    for i in range(mons.get_n_items()):
        m = mons.get_item(i)
        name = m.get_connector()
        if not name:
            continue
        g = m.get_geometry()
        out[name] = {"x": g.x, "y": g.y, "width": g.width, "height": g.height,
                     "scale": m.get_scale(), "model": m.get_model() or ""}
    return out


APP_ID = "io.github.shorin_kiwata.key2touch"
NEW_PROFILE_ITEM = "＋ 新建配置…"
HOTKEYS = {"toggle": ("开关热键", "在任何窗口下按下都能开关映射"),
           "edit": ("编辑热键", "在游戏里打开布局编辑器，再按一次保存并退出")}
DATA_DIR = Path(__file__).resolve().parent.parent / "data"  # icon when run from source


def open_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    Gio.AppInfo.launch_default_for_uri(path.as_uri(), None)


class Key2Touch(Adw.Application):
    def __init__(self, initial=None, hidden=False):
        super().__init__(application_id=APP_ID)
        self.initial = initial
        self.start_hidden = hidden
        self.tray = None
        self.engine = None
        self.hud = None
        self.editor = None
        self.prefs = None
        self.engine_state = {"active": False, "enabled": True}
        self._syncing = False
        self.capturing = None  # "toggle" | "edit" while recording that hotkey
        self.hk_mods = []  # modifier key names held while capturing the hotkey
        self.hk_solo = False
        self.backend = desktop.detect()
        self.layer_shell = LS.is_supported()
        # KWin + NVIDIA GL drops the alpha channel of GTK4 layer-shell windows.
        # The HUD (live game view) cannot use a static snapshot, so skip it.
        # The layout editor instead paints a desktop snapshot as its opaque
        # background (see overlay.capture_desktop_snapshot).
        self.overlay_broken = self._compositor_breaks_overlay()
        self.synced_after_start = False

    @staticmethod
    def _compositor_breaks_overlay() -> bool:
        """True when GTK4 transparent layer-shell windows render as opaque
        black (KWin + NVIDIA GL on Wayland)."""
        if os.environ.get("XDG_SESSION_TYPE") != "wayland":
            return False
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
        if "KDE" not in desktop and "KWin" not in desktop:
            return False
        # Check for NVIDIA GPU
        try:
            for card in os.listdir("/sys/class/drm"):
                dev = os.path.join("/sys/class/drm", card, "device", "vendor")
                if os.path.exists(dev):
                    with open(dev) as f:
                        if f.read().strip() == "0x10de":
                            return True
        except OSError:
            pass
        return False

    # ================================================================ model
    def profile_names(self):
        return prof.list_profiles()

    def create_profile(self, name, empty=True):
        if not prof.valid_name(name) or prof.exists(name):
            return False
        p = copy.deepcopy(self.profile)
        if empty:
            p["points"] = []
        prof.save(name, p)
        self.reload_profile_list()
        return True

    def save_current(self):
        prof.save(self.name, self.profile)

    def switch_profile(self, name):
        self.name = name
        self.profile = prof.load(name)
        if self.profile["output"] not in self.outputs:
            self.profile["output"] = self.default_output()
        self.settings["last_profile"] = name
        prof.save_settings(self.settings)
        self.load_into_ui()

    def default_output(self):
        o = self.backend.focused_output()
        return o if o in self.outputs else self.out_names[0]

    def apply(self):
        """Persist the profile and push it to the engine / compositor / HUD."""
        self.save_current()
        out = self.outputs[self.profile["output"]]
        self.backend.touch_sync(self.profile["output"])
        self.engine.send(cmd="profile", profile=self.profile, aspect=out["width"] / out["height"])
        self.refresh_banner()
        self.refresh_layout_page()
        self.refresh_status()
        self.rebuild_hud()

    def set_opt(self, k, v):
        if self._syncing:
            return
        self.profile[k] = v
        self.apply()

    # ================================================================ main window
    def do_activate(self):
        if getattr(self, "win", None):
            self.show_window()  # launched again: bring the (possibly hidden) window back
            return
        css = Gtk.CssProvider()
        css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.PREFER_DARK)
        if DATA_DIR.is_dir():
            Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(str(DATA_DIR))
        Gtk.Window.set_default_icon_name(APP_ID)
        self.outputs = gdk_outputs()
        self.out_names = sorted(self.outputs)
        self.settings = prof.load_settings()

        names = prof.list_profiles()
        name = self.initial or self.settings.get("last_profile")
        if not name or not prof.valid_name(name) or (name not in names and not self.initial):
            name = names[0] if names else "默认"
        self.name = name
        self.profile = prof.load(name)
        if self.profile["output"] not in self.outputs:
            self.profile["output"] = self.default_output()
        prof.save(name, self.profile)

        self.win = Adw.ApplicationWindow(application=self, title="key2touch",
                                         default_width=520, default_height=700)
        self.win.connect("close-request", self._on_close)
        # Losing focus mid-capture would otherwise leave mapping disabled.
        self.win.connect("notify::is-active",
                         lambda w, _: self.capturing and not w.is_active() and self._finish_hotkey())
        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        view.add_top_bar(self._build_header())
        self.banner = Adw.Banner()
        self.banner.connect("button-clicked", self._on_banner_button)
        view.add_top_bar(self.banner)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                      margin_top=12, margin_bottom=24, margin_start=12, margin_end=12)
        box.append(self._build_layout_page())
        box.append(self._build_run_group())
        box.append(self._build_output_group())
        box.append(self._build_footer())
        clamp = Adw.Clamp(maximum_size=520, child=box)
        # Without input permissions nothing on the main page can work, so a
        # setup page takes its place until the group membership is in effect.
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER), "main")
        self.stack.add_named(self._build_perm_page(), "perm")
        view.set_content(self.stack)
        self.toasts.set_child(view)
        self.win.set_content(self.toasts)

        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_window_key)
        keys.connect("key-released", self._on_window_key_released)
        self.win.add_controller(keys)

        self._setup_actions()
        # Restore whether mapping was on when the app was last closed.
        enabled = self.settings.get("enabled", True)
        self._syncing = True
        self.enable_row.set_active(enabled)
        self._syncing = False
        self.engine = NullEngine()
        self.load_into_ui()
        ready = self.refresh_perms()  # starts the engine when access works
        self._setup_tray()
        self._watch_profiles()
        if not (self.start_hidden and self.tray and ready):
            self.win.present()  # a missing permission must not stay hidden in the tray

    # ---- input permissions ----
    def _build_perm_page(self):
        self.perm_page = Adw.StatusPage(icon_name="dialog-password-symbolic")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, halign=Gtk.Align.CENTER)
        self.perm_btn = Gtk.Button(halign=Gtk.Align.CENTER)
        self.perm_btn.add_css_class("pill")
        self.perm_btn.add_css_class("suggested-action")
        self.perm_btn.add_css_class("key2touch-hero")
        self.perm_btn.connect("clicked", self._on_perm_btn)
        box.append(self.perm_btn)
        recheck = Gtk.Button(label="重新检测", halign=Gtk.Align.CENTER)
        recheck.add_css_class("flat")
        recheck.connect("clicked", lambda *_: self.refresh_perms() and self.toast("权限已就绪"))
        self.perm_recheck = recheck
        box.append(recheck)
        self.perm_manual = Gtk.Label(wrap=True, selectable=True, justify=Gtk.Justification.CENTER, use_markup=True)
        self.perm_manual.add_css_class("dim-label")
        self.perm_manual.add_css_class("caption")
        box.append(self.perm_manual)
        self.perm_page.set_child(box)
        return self.perm_page

    def refresh_perms(self):
        """Show the setup page while access is missing; start the engine once it works."""
        st = perms.status()
        self.perm = st
        ok = st == perms.OK
        self.stack.set_visible_child_name("main" if ok else "perm")
        self.prof_dd.set_visible(ok)
        self.menu_btn.set_visible(ok)
        if ok:
            if isinstance(self.engine, NullEngine):
                self.engine = EngineProc(self.on_engine, self.settings.get("enabled", True))
                self.apply()
            return True
        user = GLib.markup_escape_text(perms.user())
        manual = f"也可以在终端里运行：<tt>{GLib.markup_escape_text(perms.manual_cmd())}</tt>"
        if st == perms.NEED_GROUP:
            self.perm_page.set_title("需要输入设备权限")
            self.perm_page.set_description(
                "key2touch 要读取键盘、创建虚拟触摸屏，需要把你的账户加入 <b>input</b> 组。\n"
                "加入后，同一账户下运行的程序都能读取键盘输入。")
            self.perm_btn.set_label("加入 input 组")
            self.perm_btn.set_visible(True)
            self.perm_manual.set_label(manual + "，然后重新登录。")
        elif st == perms.RELOGIN:
            self.perm_page.set_icon_name("system-log-out-symbolic")
            self.perm_page.set_title("重新登录后生效")
            self.perm_page.set_description(
                f"已把 <b>{user}</b> 加入 input 组。\n注销并重新登录桌面后，再打开 key2touch。")
            self.perm_btn.set_visible(False)
            self.perm_manual.set_label("")
        else:
            self.perm_page.set_icon_name("dialog-warning-symbolic")
            self.perm_page.set_title("无法访问 /dev/uinput")
            self.perm_page.set_description("已经在 input 组里，但 uinput 模块没加载，或者 udev 规则还没生效。")
            self.perm_btn.set_label("修复")
            self.perm_btn.set_visible(True)
            self.perm_manual.set_label("仍然不行的话，运行 <tt>key2touch check</tt> 查看原因。")
        return False

    def _on_perm_btn(self, *_):
        cmd = perms.grant_cmd() if self.perm == perms.NEED_GROUP else perms.repair_cmd()
        self.perm_btn.set_sensitive(False)
        try:
            proc = Gio.Subprocess.new(cmd, Gio.SubprocessFlags.STDERR_PIPE)
        except GLib.Error:
            self.perm_btn.set_sensitive(True)
            self.toast("找不到 pkexec，请在终端里运行下面的命令")
            return

        def done(p, res):
            self.perm_btn.set_sensitive(True)
            _, _, err = p.communicate_utf8_finish(res)
            code = p.get_exit_status()
            if code == 0:
                self.refresh_perms()
            elif code == 126:
                self.toast("已取消")
            elif code == 127:
                # No polkit agent (common on bare compositors) or not authorized.
                self.toast("没能获得授权：请在终端里运行下面的命令")
            else:
                self.toast(f"失败：{(err or '').strip() or code}")

        proc.communicate_utf8_async(None, None, done)

    def do_shutdown(self):
        if self.tray:
            self.tray.close()
        if self.engine:
            self.engine.stop()
        Adw.Application.do_shutdown(self)

    # ---- tray / background ----
    def _setup_tray(self):
        if not tray.available():
            return  # no tray host: closing the window quits, so nothing runs invisibly
        self.tray = tray.Tray(APP_ID, "key2touch", APP_ID, str(DATA_DIR) if DATA_DIR.is_dir() else "", [
            {"label": "显示主窗口", "action": self.show_window},
            {"label": "启用映射", "action": lambda: self.enable_row.set_active(not self.enable_row.get_active()),
             "checked": lambda: self.enable_row.get_active()},
            {"label": "编辑布局", "action": self.on_edit},
            {"label": "重置触摸（按键没反应时）", "action": lambda: self.engine.send(cmd="reset-touch")},
            None,
            {"label": "退出", "action": self.quit},
        ], on_activate=self.toggle_window)
        self.hold()  # keep running with the window hidden

    def show_window(self):
        self.win.set_visible(True)
        self.win.present()

    def toggle_window(self):
        if self.win.get_visible():
            self.win.set_visible(False)
        else:
            self.show_window()

    def _on_close(self, win):
        if self.tray:
            win.set_visible(False)  # keep mapping in the background; quit from the tray menu
            return True
        self.quit()
        return False

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=2))

    def _build_header(self):
        hb = Adw.HeaderBar()
        self.prof_model = Gtk.StringList.new(prof.list_profiles() + [NEW_PROFILE_ITEM])
        self.prof_dd = Gtk.DropDown(model=self.prof_model, tooltip_text="切换配置")
        self.prof_dd.connect("notify::selected", self._on_profile_dd)
        hb.set_title_widget(self.prof_dd)

        menu = Gio.Menu()
        menu.append("新建配置…", "app.new-profile")
        menu.append("复制当前配置…", "app.dup-profile")
        menu.append("重命名…", "app.rename-profile")
        menu.append("删除当前配置…", "app.delete-profile")
        self.menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="配置管理")
        hb.pack_end(self.menu_btn)

        return hb

    def _build_footer(self):
        box = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        gear = Gtk.Button(child=Adw.ButtonContent(icon_name="emblem-system-symbolic", label="高级设置"))
        gear.add_css_class("pill")
        gear.connect("clicked", self.show_prefs)
        box.append(gear)

        folders = Gio.Menu()
        folders.append("打开配置目录", "app.open-config")
        folders.append("打开缓存目录", "app.open-cache")
        fb = Gtk.MenuButton(menu_model=folders, always_show_arrow=True,
                            child=Adw.ButtonContent(icon_name="folder-symbolic", label="文件夹"))
        fb.add_css_class("pill")
        box.append(fb)
        return box

    def _build_layout_page(self):
        # Doubles as the empty state: with no points it tells a new user what to do.
        self.layout_page = Adw.StatusPage(icon_name=APP_ID)
        self.layout_page.add_css_class("compact")
        btn = Gtk.Button(label="编辑布局", halign=Gtk.Align.CENTER)
        btn.add_css_class("pill")
        btn.add_css_class("suggested-action")
        btn.add_css_class("key2touch-hero")
        btn.connect("clicked", self.on_edit)
        self.edit_btn = btn
        self.layout_page.set_child(btn)
        return self.layout_page

    def _build_run_group(self):
        g = Adw.PreferencesGroup()
        self.enable_row = Adw.SwitchRow(title="启用映射", active=True)
        self.enable_row.connect("notify::active", self._on_enable)
        g.add(self.enable_row)
        self.hk_rows = {}
        for kind, (title, sub) in HOTKEYS.items():
            row = Adw.ActionRow(title=title, subtitle=sub)
            btn = Gtk.Button(valign=Gtk.Align.CENTER, tooltip_text="点击后按下新的按键或组合键")
            btn.connect("clicked", lambda _b, k=kind: self._on_hotkey_btn(k))
            row.add_suffix(btn)
            row.set_activatable_widget(btn)
            g.add(row)
            self.hk_rows[kind] = (row, btn)
        return g

    def _build_output_group(self):
        g = Adw.PreferencesGroup()
        # Keep entries short (they ellipsize); the model goes in the subtitle.
        labels = [f"{n} · {self.outputs[n]['width']}×{self.outputs[n]['height']}" for n in self.out_names]
        self.out_row = Adw.ComboRow(title="应用于显示器", model=Gtk.StringList.new(labels))
        self.out_row.connect("notify::selected", self._on_output)
        g.add(self.out_row)
        return g

    def refresh_layout_page(self):
        n = len(self.profile["points"])
        if n == 0:
            self.layout_page.set_title("还没有按键点")
            self.layout_page.set_description("打开编辑器，在游戏画面上点击要触摸的位置，再按下对应的键。")
            self.edit_btn.set_label("开始编辑布局")
        else:
            joys = sum(p["type"] == "joystick" for p in self.profile["points"])
            parts = [f"{n - joys} 个按键"] + ([f"{joys} 个摇杆"] if joys else [])
            self.layout_page.set_title(self.name)
            self.layout_page.set_description("，".join(parts))
            self.edit_btn.set_label("编辑布局")

    def refresh_banner(self):
        notice = self.backend.notice(self.profile["output"], len(self.outputs))
        if notice:
            text, button = notice
            self.banner.set_title(text)
            self.banner.set_button_label(button)
        self.banner.set_revealed(bool(notice))

    def _on_banner_button(self, *_):
        dlg = Adw.AlertDialog(heading=f"设置 {self.backend.label}",
                              body=self.backend.setup_text(self.profile["output"]))
        dlg.add_response("cancel", "取消")
        dlg.add_response("ok", "写入配置")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def resp(d, r):
            if r != "ok":
                return
            try:
                self.backend.setup(self.profile["output"])
                self.toast("已写入，配置会自动重载")
            except OSError as ex:
                self.toast(f"写入失败：{ex}")
            self.refresh_banner()

        dlg.connect("response", resp)
        dlg.present(self.win)

    def _setup_actions(self):
        for name, cb in (("new-profile", lambda *_: self._ask_name("新建配置", "", self._do_new)),
                         ("dup-profile", lambda *_: self._ask_name("复制配置", f"{self.name} 副本", self._do_dup)),
                         ("rename-profile", lambda *_: self._ask_name("重命名配置", self.name, self._do_rename)),
                         ("delete-profile", self._ask_delete),
                         ("open-config", lambda *_: open_dir(prof.CONFIG_DIR)),
                         ("open-cache", lambda *_: open_dir(prof.CACHE_DIR))):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            self.add_action(a)

        # Actions the CLI drives over D-Bus (org.gtk.Actions), see __main__.py.
        for name, cb in (("show", lambda *_: self.show_window()),
                         ("reload", lambda *_: self._reload_from_disk()),
                         ("reset-touch", lambda *_: self.engine.send(cmd="reset-touch")),
                         ("edit", lambda *_: self.on_edit()),
                         ("quit", lambda *_: self.quit())):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            self.add_action(a)
        self.enabled_action = Gio.SimpleAction.new_stateful("enabled", None, GLib.Variant("b", True))
        self.enabled_action.connect("change-state", lambda a, v: self.enable_row.set_active(v.get_boolean()))
        self.add_action(self.enabled_action)
        self.profile_action = Gio.SimpleAction.new_stateful("profile", None, GLib.Variant("s", self.name))
        self.profile_action.connect("change-state", self._on_profile_action)
        self.add_action(self.profile_action)
        # Read-only JSON snapshot for `key2touch status`.
        self.state_action = Gio.SimpleAction.new_stateful("state", None, GLib.Variant("s", "{}"))
        self.state_action.set_enabled(False)
        self.add_action(self.state_action)

    def _watch_profiles(self):
        """Profiles may change behind our back (CLI, an AI agent, a text
        editor): watch the directory instead of relying on being told."""
        prof.PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self.prof_monitor = Gio.File.new_for_path(str(prof.PROFILE_DIR)).monitor_directory(
            Gio.FileMonitorFlags.WATCH_MOVES, None)
        self.prof_monitor.connect("changed", lambda *_: self._schedule_reload())
        self._reload_pending = False

    def _schedule_reload(self):
        # Saves arrive as several events (temp file + rename): coalesce them.
        if not self._reload_pending:
            self._reload_pending = True
            GLib.timeout_add(250, self._reload_from_disk)

    def _reload_from_disk(self, *_):
        self._reload_pending = False
        names = prof.list_profiles()
        if self.name not in names:
            if self.editor:
                return False  # the editor still holds it; it is saved back on exit
            if not names:
                prof.save("默认", prof.normalize({"output": self.profile["output"]}))
                names = ["默认"]
            self.switch_profile(names[0])
            return False
        if not self.editor:
            try:
                disk = prof.load(self.name)
            except (OSError, ValueError):
                disk = self.profile  # half-written or broken file: keep what we have
            # Our own saves land here too; only reload real outside changes
            # (comparing content also prevents a save → reload → save loop).
            if disk != self.profile:
                self.profile = disk
                self.load_into_ui()
                return False
        self.reload_profile_list()
        return False

    def _on_profile_action(self, action, value):
        name = value.get_string()
        if name != self.name and prof.exists(name):
            if self.editor:
                return  # don't swap the profile out from under the editor
            self.save_current()
            self.switch_profile(name)

    def publish_state(self):
        if not getattr(self, "state_action", None):
            return
        st = self.engine_state
        self.enabled_action.set_state(GLib.Variant("b", self.enable_row.get_active()))
        self.profile_action.set_state(GLib.Variant("s", self.name))
        self.state_action.set_state(GLib.Variant("s", json.dumps({
            "enabled": self.enable_row.get_active(), "active": bool(st.get("active")),
            "editing": self.editor is not None, "profile": self.name,
            "output": self.profile["output"], "points": len(self.profile["points"]),
            "focus": st.get("focus"), "desktop": self.backend.name,
            "hotkey": self.hotkey_label(), "edit_hotkey": self.hotkey_label("edit"),
            "priority": st.get("priority"),
        }, ensure_ascii=False)))

    # ================================================================ advanced settings
    def show_prefs(self, *_):
        dlg = Adw.PreferencesDialog(title="高级设置", search_enabled=False)
        page = Adw.PreferencesPage(title="高级", icon_name="emblem-system-symbolic")
        dlg.add(page)

        g = Adw.PreferencesGroup(title="键盘")
        grab = Adw.SwitchRow(title="已映射的键不再传给应用",
                             subtitle="映射生效时，这些键只会变成触摸，游戏收不到按键本身",
                             active=self.profile.get("grab", True))
        grab.connect("notify::active", lambda r, _: self.set_opt("grab", r.get_active()))
        g.add(grab)
        page.add(g)

        for grp in self._build_window_rules():
            page.add(grp)

        g = Adw.PreferencesGroup(title="外观")
        hud = Adw.SwitchRow(title="显示 HUD",
                            subtitle=("在屏幕上显示按键位置，按下时高亮" if self.layer_shell and not self.overlay_broken
                                      else "当前桌面合成器不支持透明 HUD 覆盖层，已禁用"),
                            active=self.settings.get("hud", True),
                            sensitive=self.layer_shell and not self.overlay_broken)
        row = Adw.ActionRow(title="HUD 不透明度", sensitive=self.layer_shell)
        opacity = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.15, 1.0, 0.05)
        opacity.set_value(self.settings.get("hud_opacity", 0.6))
        opacity.set_draw_value(False)
        opacity.set_hexpand(True)
        opacity.set_size_request(180, -1)
        row.add_suffix(opacity)

        def hud_changed(*_):
            self.settings["hud"] = hud.get_active()
            self.settings["hud_opacity"] = round(opacity.get_value(), 2)
            prof.save_settings(self.settings)
            self.update_hud()

        hud.connect("notify::active", hud_changed)
        opacity.connect("value-changed", hud_changed)
        g.add(hud)
        g.add(row)
        page.add(g)

        g = Adw.PreferencesGroup(title="运行状态")
        self.diag_rows = {}
        for key, title in (("desktop", "桌面"), ("focus", "当前焦点窗口"), ("keyboards", "键盘"),
                           ("priority", "引擎调度")):
            r = Adw.ActionRow(title=title, subtitle_selectable=True)
            r.add_css_class("property")
            self.diag_rows[key] = r
            g.add(r)
        page.add(g)
        self.prefs = dlg
        self.refresh_diag()

        dlg.connect("closed", lambda *_: setattr(self, "prefs", None))
        dlg.present(self.win)

    def _build_window_rules(self):
        g = Adw.PreferencesGroup(title="窗口")
        only = Adw.SwitchRow(title="仅聚焦指定软件窗口时生效",
                             active=self.profile.get("only_focused", False) and self.backend.supports_focus,
                             sensitive=self.backend.supports_focus)
        only.set_subtitle("焦点切到其他窗口时，键盘照常使用" if self.backend.supports_focus
                          else f"当前桌面（{self.backend.label}）无法获取窗口焦点，请用开关热键手动开关")
        g.add(only)

        rules = Adw.PreferencesGroup(title="窗口匹配规则",
                                     description="填写窗口的 app_id，可用 * 通配，不区分大小写；任意一条匹配即生效。")
        rules.set_sensitive(only.get_active())
        only.connect("notify::active", lambda r, _: (rules.set_sensitive(r.get_active()),
                                                      self.set_opt("only_focused", r.get_active())))
        rows = []

        def rebuild():
            for r in rows:
                rules.remove(r)
            rows.clear()
            for i, pat in enumerate(self.profile["windows"]):
                r = Adw.ActionRow(title=pat)
                rm = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, tooltip_text="删除")
                rm.add_css_class("flat")
                rm.connect("clicked", lambda _b, i=i: remove(i))
                r.add_suffix(rm)
                rules.add(r)
                rows.append(r)
            rules.add(add_row)
            rows.append(add_row)

        def remove(i):
            del self.profile["windows"][i]
            self.apply()
            rebuild()

        def add(pat):
            pat = pat.strip()
            if pat and pat not in self.profile["windows"]:
                self.profile["windows"].append(pat)
                self.apply()
            add_row.set_text("")
            rebuild()

        add_row = Adw.EntryRow(title="添加规则，例如 waydroid.*", show_apply_button=True)
        add_row.connect("apply", lambda r: add(r.get_text()))
        add_row.connect("entry-activated", lambda r: add(r.get_text()))
        pick = Gtk.MenuButton(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER,
                              tooltip_text="从当前打开的窗口添加")
        pick.add_css_class("flat")
        pick.set_create_popup_func(lambda btn: self._fill_window_picker(btn, add))
        add_row.add_suffix(pick)
        rebuild()
        return g, rules

    def _fill_window_picker(self, btn, add):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6,
                      margin_bottom=6, margin_start=6, margin_end=6)
        seen = set()
        for w in self.backend.windows():
            app = w.get("app_id")
            if not app or app in seen or app == self.get_application_id():
                continue
            seen.add(app)
            b = Gtk.Button(label=f"{app}  —  {(w.get('title') or '')[:30]}")
            b.add_css_class("flat")
            b.get_child().set_xalign(0)

            def pick(_b, app=app):
                btn.popdown()
                add(app)

            b.connect("clicked", pick)
            box.append(b)
        if not seen:
            box.append(Gtk.Label(label="没有其他窗口"))
        btn.set_popover(Gtk.Popover(child=box))

    def refresh_diag(self):
        if not self.prefs:
            return
        st = self.engine_state
        values = {
            "desktop": self.backend.label + ("" if self.backend.supports_focus else "（无焦点信息）"),
            "focus": st.get("focus") or "-",
            "keyboards": f"{st.get('keyboards', 0)} 个",
            "priority": st.get("priority", "-"),
        }
        for k, r in self.diag_rows.items():
            r.set_subtitle(str(values[k]))

    # ================================================================ profiles
    def _ask_name(self, heading, initial, done):
        dlg = Adw.AlertDialog(heading=heading)
        entry = Gtk.Entry(text=initial, activates_default=True)
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "取消")
        dlg.add_response("ok", "确定")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("ok")
        dlg.connect("response", lambda d, r: r == "ok" and done(entry.get_text().strip()))
        dlg.present(self.win)

    def _do_new(self, name):
        if self.create_profile(name, empty=True):
            self.switch_profile(name)
        else:
            self.toast("名称无效或已存在")

    def _do_dup(self, name):
        self.save_current()
        if self.create_profile(name, empty=False):
            self.switch_profile(name)
        else:
            self.toast("名称无效或已存在")

    def _do_rename(self, name):
        if name == self.name:
            return
        if not prof.valid_name(name) or prof.exists(name):
            self.toast("名称无效或已存在")
            return
        self.save_current()
        prof.rename(self.name, name)
        self.switch_profile(name)

    def _ask_delete(self, *_):
        dlg = Adw.AlertDialog(heading=f"删除配置「{self.name}」？", body="配置文件会被删除，无法恢复。")
        dlg.add_response("cancel", "取消")
        dlg.add_response("delete", "删除")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def resp(d, r):
            if r != "delete":
                return
            prof.delete(self.name)
            rest = prof.list_profiles()
            if not rest:
                prof.save("默认", prof.normalize({"output": self.profile["output"]}))
                rest = ["默认"]
            self.switch_profile(rest[0])

        dlg.connect("response", resp)
        dlg.present(self.win)

    def reload_profile_list(self):
        was = self._syncing
        self._syncing = True
        names = prof.list_profiles()
        self.prof_model.splice(0, self.prof_model.get_n_items(), names + [NEW_PROFILE_ITEM])
        if self.name in names:
            self.prof_dd.set_selected(names.index(self.name))
        self._syncing = was

    def _on_profile_dd(self, dd, _):
        if self._syncing:
            return
        if dd.get_selected() == self.prof_model.get_n_items() - 1:
            # The trailing "new profile" entry: snap back, then ask for a name.
            self.reload_profile_list()
            self.activate_action("new-profile")
            return
        name = self.prof_model.get_string(dd.get_selected())
        if name and name != self.name:
            self.save_current()
            self.switch_profile(name)

    def load_into_ui(self):
        self._syncing = True
        self.reload_profile_list()
        self.out_row.set_selected(self.out_names.index(self.profile["output"]))
        self._update_output_subtitle()
        self._refresh_hotkey_rows()
        self._syncing = False
        if self.prefs:
            self.prefs.close()  # its widgets show the previous profile
        self.apply()

    def _on_output(self, row, _):
        self._update_output_subtitle()
        self.set_opt("output", self.out_names[row.get_selected()])

    def _update_output_subtitle(self):
        o = self.outputs[self.out_names[self.out_row.get_selected()]]
        self.out_row.set_subtitle("游戏窗口所在的显示器" + (f"（{o['model']}）" if o["model"] else ""))

    # ================================================================ hotkeys
    def hotkey_label(self, kind="toggle"):
        p = self.profile
        key = p.get(f"{kind}_key")
        return combo_label(key, p.get(f"{kind}_mods")) if key else None

    def _refresh_hotkey_rows(self):
        for kind, (row, btn) in self.hk_rows.items():
            btn.set_label(self.hotkey_label(kind) or "未设置")
            row.set_subtitle(HOTKEYS[kind][1])

    def _on_hotkey_btn(self, kind):
        self.capturing = kind
        self.hk_mods.clear()
        self.hk_solo = False
        row, btn = self.hk_rows[kind]
        btn.set_label("按下按键或组合键…")
        row.set_subtitle("Esc 取消，Backspace 清除")
        # So the old hotkeys can't fire while the new one is being pressed.
        self.engine.send(cmd="hotkeys", on=False)

    def _finish_hotkey(self, name=None, mods=(), clear=False):
        kind, self.capturing = self.capturing, None
        self.hk_mods.clear()
        self.engine.send(cmd="hotkeys", on=True)
        if clear:
            self.profile[f"{kind}_mods"] = []
            self.set_opt(f"{kind}_key", None)
        elif name:
            mods = [m for m in MOD_ORDER if m in mods]
            other = "edit" if kind == "toggle" else "toggle"
            if name == self.profile.get(f"{other}_key") and mods == (self.profile.get(f"{other}_mods") or []):
                self.toast(f"{combo_label(name, mods)} 已经是{HOTKEYS[other][0]}")
            else:
                self.profile[f"{kind}_mods"] = mods
                self.set_opt(f"{kind}_key", name)
        self._refresh_hotkey_rows()

    def _on_window_key(self, c, keyval, keycode, state):
        if not self.capturing:
            return False
        if keyval == Gdk.KEY_Escape:
            self._finish_hotkey()
            return True
        if keyval == Gdk.KEY_BackSpace and not self.hk_mods:
            self._finish_hotkey(clear=True)
            return True
        name = key_name(keycode - 8)
        if not name:
            return True
        if name in MOD_NAME_GROUP:
            # Wait: a key may follow (combo), or the modifier is released alone.
            if name not in self.hk_mods:
                self.hk_mods.append(name)
            self.hk_solo = True
            held = "+".join(MOD_LABEL[MOD_NAME_GROUP[m]] for m in self.hk_mods)
            self.hk_rows[self.capturing][1].set_label(f"{held}+…")
            return True
        self._finish_hotkey(name, {MOD_NAME_GROUP[m] for m in self.hk_mods})
        return True

    def _on_window_key_released(self, c, keyval, keycode, state):
        if not self.capturing:
            return
        name = key_name(keycode - 8)
        if name not in self.hk_mods:
            return
        if self.hk_solo:
            self._finish_hotkey(name, {MOD_NAME_GROUP[m] for m in self.hk_mods if m != name})
        else:
            self.hk_mods.remove(name)

    # ================================================================ engine
    def _on_enable(self, row, _):
        # While editing the engine stays off; editor_done() applies this switch.
        if not self._syncing and not self.editor:
            self.engine.send(cmd="enable", on=row.get_active(), why="switch / tray / CLI")
        self.publish_state()

    def on_engine(self, msg):
        ev = msg.get("ev")
        if ev == "key":
            if self.hud:
                self.hud.key_event(msg["code"], msg.get("idx"), msg["down"])
        elif ev == "toggled":
            # Toggled by the hotkey, maybe by accident in-game: say so.
            n = Gio.Notification.new("key2touch：映射已" + ("开启" if msg["enabled"] else "关闭"))
            hk = self.hotkey_label()
            if hk:
                n.set_body(f"按 {hk} 可以再次{'关闭' if msg['enabled'] else '开启'}")
            self.send_notification("toggled", n)
        elif ev == "hotkey" and msg.get("action") == "edit":
            if self.editor:
                self.editor.finish(True)  # second press: save and leave the editor
            else:
                self.on_edit()
        elif ev == "state":
            self.engine_state = msg
            if not self.synced_after_start:
                # The virtual touchscreen exists now: (re)apply per-device mapping.
                self.synced_after_start = True
                self.backend.touch_sync(self.profile["output"])
                self.refresh_banner()
            if not self.editor and self.enable_row.get_active() != msg["enabled"]:
                self._syncing = True
                self.enable_row.set_active(msg["enabled"])
                self._syncing = False
            if not self.editor and self.settings.get("enabled", True) != msg["enabled"]:
                self.settings["enabled"] = msg["enabled"]  # remembered for the next start
                prof.save_settings(self.settings)
            if self.tray:
                self.tray.refresh()
            self.refresh_status()
            self.refresh_diag()
            self.update_hud()
        elif ev == "exit":
            self.enable_row.set_subtitle(f"引擎已退出（code {msg.get('code')}），日志在缓存目录的 engine.log")

    def refresh_status(self):
        st = self.engine_state
        if "keyboards" not in st:
            return
        hotkey = self.hotkey_label()
        if self.editor:
            text = "编辑布局中"
        elif st["active"]:
            text = "生效中"
        elif not st["enabled"]:
            text = f"已关闭，按 {hotkey} 开启" if hotkey else "已关闭"
        elif not self.profile["points"]:
            text = "还没有按键点，先编辑布局"
        else:
            text = "等待匹配的窗口获得焦点"
        self.enable_row.set_subtitle(text)
        self.publish_state()

    # ================================================================ HUD / editor
    def rebuild_hud(self):
        if self.editor:
            return
        if self.hud and self.hud.output == self.profile["output"]:
            self.update_hud()  # the HUD reads app.profile, so a redraw is enough
            return
        if self.hud:
            self.hud.close()
            self.hud = None
        if not self.layer_shell:
            return  # HUD needs wlr-layer-shell (not available on e.g. GNOME)
        # Only create the overlay when there is something to draw. An empty
        # overlay is a fullscreen transparent window; on some GTK4 + KWin
        # (NVIDIA GL) compositors that alpha is dropped and the window renders
        # as an opaque black canvas the user cannot dismiss.
        if not self.profile["points"]:
            return
        if self.overlay_broken:
            return  # KWin + NVIDIA GL: GTK4 transparent overlay = black canvas
        self.hud = Overlay(self, edit=False)
        self.hud.present()
        self.update_hud()

    def update_hud(self):
        show = (self.settings.get("hud", True) and self.engine_state.get("enabled", True)
                and bool(self.profile["points"]))
        if not show:
            if self.hud:
                self.hud.close()
                self.hud = None
            return
        if not self.hud or self.hud.output != self.profile["output"]:
            self.rebuild_hud()
            return
        self.hud.present()
        # Shown whenever mapping is on; dimmed while waiting for the game window.
        self.hud.set_hud_visible(True, dim=not self.engine_state.get("active", False))

    def on_edit(self, *_):
        if self.editor:
            return
        self.engine.send(cmd="enable", on=False, why="editor opened")
        if self.hud:
            self.hud.close()
            self.hud = None
        self.editor = Overlay(self, edit=True)
        self.editor.present()
        self.refresh_status()

    def editor_done(self, save, backup):
        """Called by the editor after it closed. `backup` is the current profile
        as it was when the editor opened (or last switched profiles)."""
        self.editor = None
        if not save:
            self.profile = backup
        self.engine.send(cmd="enable", on=self.enable_row.get_active(), why="editor closed")
        try:
            self.load_into_ui()
        except OSError as ex:
            self.toast(f"保存失败：{ex}")
            return
        if save:
            self.toast(f"已保存「{self.name}」")
