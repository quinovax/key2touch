"""Layer-shell overlay: layout editor (takes input) or HUD (click-through).

Both read the live profile from the app (`app.profile`), so the HUD only
needs a redraw when the profile changes.
"""

import copy
import math
import tempfile

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Gtk4LayerShell as LS  # noqa: E402
from evdev import ecodes as e  # noqa: E402

from .mapper import MOD_GROUP, MOD_ORDER  # noqa: E402
from .profile import JOY_DIRS, KEYED, LOOK_MIN_R, SKILL_BTN, SKILL_R, TAP_R, new_joystick, new_look, new_skill, new_tap  # noqa: E402

ARROWS = {"KEY_UP": "↑", "KEY_DOWN": "↓", "KEY_LEFT": "←", "KEY_RIGHT": "→", "KEY_SPACE": "Space"}
DIR_ANGLE = {"up": -90, "right": 0, "down": 90, "left": 180}
DIR_ARROW = {"up": "↑", "left": "←", "down": "↓", "right": "→"}
MOD_NAME_GROUP = {e.KEY[c]: g for c, g in MOD_GROUP.items()}
MOD_LABEL = {"CTRL": "Ctrl", "ALT": "Alt", "SHIFT": "Shift", "META": "Super"}
SIZE_RANGE = {"tap": (0.008, 0.1), "joystick": (0.03, 0.45), "look": (LOOK_MIN_R, 0.45), "skill": (0.04, 0.3)}
TYPE_TITLE = {"joystick": "摇杆", "look": "视角", "skill": "技能键"}
MOUSE_LABELS = {"BTN_LEFT": "左键", "BTN_RIGHT": "右键", "BTN_MIDDLE": "中键",
                "BTN_SIDE": "侧键1", "BTN_EXTRA": "侧键2"}
# GDK button number -> evdev code, for binding mouse buttons by clicking.
GDK_BUTTONS = {1: "BTN_LEFT", 2: "BTN_MIDDLE", 3: "BTN_RIGHT", 8: "BTN_SIDE", 9: "BTN_EXTRA"}
WASD = {"up": "KEY_W", "left": "KEY_A", "down": "KEY_S", "right": "KEY_D"}


def key_label(name):
    if not name:
        return "?"
    if name in ARROWS:
        return ARROWS[name]
    if name in MOUSE_LABELS:
        return MOUSE_LABELS[name]
    s = name.removeprefix("KEY_")
    for a, b in (("LEFT", "L"), ("RIGHT", "R")):
        if s.startswith(a) and len(s) > len(a):
            return b + s[len(a):].capitalize()  # LShift, RAlt
    return s.capitalize() if len(s) > 1 else s


def combo_label(key, mods=()):
    parts = [MOD_LABEL[m] for m in MOD_ORDER if m in (mods or ())]
    return "+".join(parts + [key_label(key)])


def key_name(evcode):
    n = e.KEY.get(evcode)
    if isinstance(n, list):
        n = n[0]
    return n


def find_monitor(connector):
    mons = Gdk.Display.get_default().get_monitors()
    for i in range(mons.get_n_items()):
        m = mons.get_item(i)
        if m.get_connector() == connector:
            return m
    return None


def clamp(v, lo, hi):
    return min(max(v, lo), hi)


def capture_desktop_snapshot():
    # KWin + NVIDIA GL drops the alpha channel of GTK4 transparent layer-shell
    # windows, so the editor renders as an opaque black canvas instead of
    # dimming the desktop behind it.  Painting a snapshot of the desktop
    # (captured before this window is mapped) as an opaque background below
    # the 45% scrim restores the intended look without relying on alpha.
    # Returns (surface, buffer, w, h, scale) or (None, None, 0, 0, 1.0).
    # The buffer MUST be kept alive for as long as the surface is used.
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        proxy = Gio.DBusProxy.new_sync(
            bus, Gio.DBusProxyFlags.NONE, None,
            "org.kde.KWin", "/org/kde/KWin/ScreenShot2",
            "org.kde.KWin.ScreenShot2", None)
        with tempfile.TemporaryFile() as f:
            fds = Gio.UnixFDList.new()
            idx = fds.append(f.fileno())
            opts = {"include-cursor": GLib.Variant("b", False)}
            args = GLib.Variant("(a{sv}h)", (opts, idx))
            res = proxy.call_with_unix_fd_list_sync(
                "CaptureWorkspace", args, Gio.DBusCallFlags.NONE, 3000, fds, None)
            meta = res[0].unpack()[0]
            if meta.get("type") != "raw":
                return (None, None, 0, 0, 1.0)
            w, h = int(meta["width"]), int(meta["height"])
            stride = int(meta["stride"])
            scale = float(meta.get("scale", 1.0)) or 1.0
            f.seek(0)
            raw = f.read(stride * h)
        if len(raw) < stride * h:
            return (None, None, 0, 0, 1.0)
        # KWin raw is R,G,B,X; cairo FORMAT_ARGB32 is B,G,R,A on little-endian.
        buf = bytearray(raw)
        buf[0::4], buf[2::4] = buf[2::4], buf[0::4]
        surf = cairo.ImageSurface.create_for_data(buf, cairo.FORMAT_ARGB32, w, h, stride)
        return (surf, buf, w, h, scale)
    except Exception:
        return (None, None, 0, 0, 1.0)


def joystick_keys_free(points, keep):
    used = {q.get("key") for q in points if q is not keep and q["type"] == "tap" and not q.get("mods")}
    used |= {k for q in points if q is not keep and q["type"] == "joystick" for k in q["keys"].values()}
    return not (used & set(WASD.values()))


class Overlay(Gtk.Window):
    def __init__(self, app, edit):
        super().__init__(application=app)
        self.app = app
        self.edit = edit
        self.output = app.profile["output"]
        self.pressed = set()  # key names held (HUD joystick labels)
        self.pressed_idx = set()  # control indices held (HUD)
        self.visible_hud = False
        self.dim = False

        # On KWin + NVIDIA GL, GTK4 transparent layer-shell windows lose their
        # alpha and render as an opaque black canvas.  Capture the compositor's
        # framebuffer now (before this window is mapped) and paint it as the
        # editor's opaque background below the 45% dim scrim.
        self.snapshot = None       # (surface, buffer, w, h, scale)
        self.snapshot_rect = None  # (x, y, w, h) in snapshot pixels
        if edit and getattr(app, "overlay_broken", False):
            self.snapshot = capture_desktop_snapshot()
            if self.snapshot[0] is not None:
                self.snapshot_rect = self._monitor_snapshot_rect()

        self.add_css_class("key2touch-overlay")
        mon = find_monitor(self.output)
        if LS.is_supported():
            LS.init_for_window(self)
            LS.set_namespace(self, "key2touch-edit" if edit else "key2touch-hud")
            LS.set_layer(self, LS.Layer.OVERLAY)
            for edge in (LS.Edge.LEFT, LS.Edge.RIGHT, LS.Edge.TOP, LS.Edge.BOTTOM):
                LS.set_anchor(self, edge, True)
            LS.set_exclusive_zone(self, -1)
            LS.set_keyboard_mode(self, LS.KeyboardMode.EXCLUSIVE if edit else LS.KeyboardMode.NONE)
            if mon:
                LS.set_monitor(self, mon)
        else:
            # No wlr-layer-shell (GNOME): the editor falls back to a borderless
            # fullscreen window on the target monitor, same logical size.
            self.set_decorated(False)
            if mon:
                self.fullscreen_on_monitor(mon)
            else:
                self.fullscreen()

        self.area = Gtk.DrawingArea(hexpand=True, vexpand=True)
        self.area.set_draw_func(self.draw)

        if edit:
            self._init_editor()
        else:
            self.binding = None
            self.set_child(self.area)
            self.set_can_target(False)
            self.connect("realize", self._passthrough)
            self.connect("map", self._passthrough)

    def _monitor_snapshot_rect(self):
        # CaptureWorkspace covers the whole workspace; the editor is fullscreen
        # on one monitor, so paint only that monitor's slice of the snapshot.
        _, _, sw, sh, scale = self.snapshot
        try:
            mon = find_monitor(self.output)
            if mon is not None:
                g = mon.get_geometry()
                x = max(0, int(g.x * scale))
                y = max(0, int(g.y * scale))
                w = min(int(g.width * scale), sw - x)
                h = min(int(g.height * scale), sh - y)
                if w > 0 and h > 0:
                    return (x, y, w, h)
        except Exception:
            pass
        return (0, 0, sw, sh)

    @property
    def points(self):
        return self.app.profile["points"]

    # ================================================================ HUD
    @staticmethod
    def _passthrough(win):
        surf = win.get_surface()
        if surf:
            surf.set_input_region(cairo.Region())

    def set_hud_visible(self, v, dim=False):
        if (v, dim) != (self.visible_hud, self.dim):
            self.visible_hud, self.dim = v, dim
            if not v or dim:
                self.pressed.clear()
                self.pressed_idx.clear()
        self.area.queue_draw()

    def key_event(self, code, idx, down):
        n = key_name(code)
        (self.pressed.add if down else self.pressed.discard)(n)
        if idx is not None:
            (self.pressed_idx.add if down else self.pressed_idx.discard)(idx)
        self.area.queue_draw()

    # ============================================================= editor
    def _init_editor(self):
        self.sel = None
        self.binding = None  # (idx, dir or None, sequence: bool)
        self.binding_new = False  # the point being bound was just created
        self.session_points = []  # [(point, original_dict_or_None)] from this session
        self.mods_held = []
        self.mod_solo = False
        self.drag_orig = None
        self.placing_cancel = None  # skill index whose cancel position the next click sets
        self.pointer = (0, 0)
        self.popovers_open = 0
        self.backup = copy.deepcopy(self.app.profile)
        self._syncing = False

        ov = Gtk.Overlay()
        ov.set_child(self.area)
        ov.add_overlay(self._build_toolbar())
        self.panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                             valign=Gtk.Align.CENTER, margin_start=32, margin_end=32,
                             width_request=280, visible=False)
        self.panel.add_css_class("key2touch-panel")
        ov.add_overlay(self.panel)
        self.toast = Gtk.Label(halign=Gtk.Align.CENTER, valign=Gtk.Align.START,
                               margin_top=84, visible=False)
        self.toast.add_css_class("key2touch-toast")
        ov.add_overlay(self.toast)
        self.set_child(ov)

        mclick = Gtk.GestureClick(button=0, propagation_phase=Gtk.PropagationPhase.CAPTURE)
        mclick.connect("pressed", self._mouse_bind)
        self.area.add_controller(mclick)
        drag = Gtk.GestureDrag(button=1)
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.area.add_controller(drag)
        rclick = Gtk.GestureClick(button=3)
        rclick.connect("pressed", self._right_click)
        self.area.add_controller(rclick)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda c, x, y: setattr(self, "pointer", (x, y)))
        self.area.add_controller(motion)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._scroll)
        self.area.add_controller(scroll)
        # Capture phase: key binding must win over focused buttons (Space/Enter).
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._key)
        keys.connect("key-released", self._key_released)
        self.add_controller(keys)

    def _popover(self, child):
        pop = Gtk.Popover(child=child)
        pop.connect("show", lambda *_: setattr(self, "popovers_open", self.popovers_open + 1))
        pop.connect("hide", lambda *_: setattr(self, "popovers_open", max(0, self.popovers_open - 1)))
        return pop

    def _build_toolbar(self):
        bar = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER, valign=Gtk.Align.START, margin_top=20)
        bar.add_css_class("key2touch-bar")

        self.prof_model = Gtk.StringList.new(self.app.profile_names())
        self.prof_dd = Gtk.DropDown(model=self.prof_model, tooltip_text="切换配置（当前修改会先保存）")
        self._select_profile_in_dd()
        self.prof_dd.connect("notify::selected", self._on_profile_dd)
        bar.append(self.prof_dd)

        # New profile: inline popover (xdg dialogs would open *below* this overlay).
        box = Gtk.Box(spacing=6, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        entry = Gtk.Entry(placeholder_text="新配置名称", width_chars=16)
        create = Gtk.Button(label="创建")
        create.add_css_class("suggested-action")
        box.append(entry)
        box.append(create)
        new_pop = self._popover(box)

        def do_create(*_):
            name = entry.get_text().strip()
            if not self.app.create_profile(name):
                self.show_toast("名称无效或已存在")
                return
            new_pop.popdown()
            entry.set_text("")
            self._commit_and_switch(name)

        create.connect("clicked", do_create)
        entry.connect("activate", do_create)
        bar.append(Gtk.MenuButton(icon_name="list-add-symbolic", popover=new_pop, tooltip_text="新建配置"))

        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        joy = Gtk.Button(label="＋ 摇杆")
        joy.connect("clicked", self.add_joystick)
        bar.append(joy)
        look = Gtk.Button(label="＋ 视角", tooltip_text="用鼠标拖动视角（每个配置一个）")
        look.connect("clicked", self.add_look)
        bar.append(look)

        cbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=8,
                       margin_bottom=8, margin_start=8, margin_end=8)
        cbox.append(Gtk.Label(label="清空当前配置的所有按键点？"))
        really = Gtk.Button(label="清空")
        really.add_css_class("destructive-action")
        cbox.append(really)
        clear_pop = self._popover(cbox)

        def do_clear(*_):
            clear_pop.popdown()
            self.cancel_binding()
            self.points.clear()
            self.select(None)

        really.connect("clicked", do_clear)
        bar.append(Gtk.MenuButton(label="清空", popover=clear_pop))

        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        discard = Gtk.Button(label="放弃修改")
        discard.connect("clicked", lambda *_: self.finish(False))
        bar.append(discard)
        save = Gtk.Button(label="保存并退出")
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda *_: self.finish(True))
        bar.append(save)
        return bar

    def _select_profile_in_dd(self):
        names = [self.prof_model.get_string(i) for i in range(self.prof_model.get_n_items())]
        self._syncing = True
        if self.app.name in names:
            self.prof_dd.set_selected(names.index(self.app.name))
        self._syncing = False

    def _on_profile_dd(self, dd, _):
        if self._syncing:
            return
        name = self.prof_model.get_string(dd.get_selected())
        if name and name != self.app.name:
            self._commit_and_switch(name)

    def _commit_and_switch(self, name):
        self.cancel_binding()
        self.app.save_current()
        self.app.switch_profile(name)
        self.backup = copy.deepcopy(self.app.profile)
        self.session_points = []  # points belong to the previous profile
        self._syncing = True
        self.prof_model.splice(0, self.prof_model.get_n_items(), self.app.profile_names())
        self._syncing = False
        self._select_profile_in_dd()
        self.select(None)
        self.show_toast(f"已切换到「{name}」")

    def show_toast(self, text):
        self.toast.set_text(text)
        self.toast.set_visible(True)
        GLib.timeout_add(1800, lambda: self.toast.set_visible(False) or False)

    # ---- selection / properties panel ----
    def select(self, i):
        self.sel = i
        self.refresh_panel()
        self.area.queue_draw()

    def refresh_panel(self):
        while (c := self.panel.get_first_child()) is not None:
            self.panel.remove(c)
        if self.sel is None or self.sel >= len(self.points):
            self.panel.set_visible(False)
            return
        p = self.points[self.sel]
        self.panel.set_visible(True)
        # Keep the panel on the side away from the point.
        self.panel.set_halign(Gtk.Align.START if p["x"] > 0.55 else Gtk.Align.END)

        title = Gtk.Label(label=TYPE_TITLE.get(p["type"], "按键点"), xalign=0)
        title.add_css_class("title-3")
        self.panel.append(title)
        if p["type"] == "look":
            self._look_panel(p)
            return

        kind = Gtk.Box(homogeneous=True)
        kind.add_css_class("linked")
        tb_tap = Gtk.ToggleButton(label="点击", active=p["type"] == "tap")
        tb_skill = Gtk.ToggleButton(label="技能", active=p["type"] == "skill", group=tb_tap,
                                    tooltip_text="按住选方向、松开释放（MOBA 的技能键）")
        tb_joy = Gtk.ToggleButton(label="摇杆", active=p["type"] == "joystick", group=tb_tap)
        for b, k in ((tb_tap, "tap"), (tb_skill, "skill"), (tb_joy, "joystick")):
            b.connect("toggled", lambda b, k=k: b.get_active() and GLib.idle_add(self.convert, k))
            kind.append(b)
        self.panel.append(kind)

        self.panel.append(self._caption("绑定"))
        if p["type"] in KEYED:
            binding = self.binding is not None and self.binding[0] == self.sel
            lbl = "按下按键…" if binding else (combo_label(p.get("key"), p.get("mods")) if p.get("key") else "未绑定")
            b = Gtk.Button(label=lbl)
            if binding:
                b.add_css_class("suggested-action")
            b.connect("clicked", lambda *_: self.start_binding(self.sel, None))
            self.panel.append(b)
        else:
            grid = Gtk.Grid(row_spacing=4, column_spacing=4, halign=Gtk.Align.CENTER)
            for d, (col, row) in {"up": (1, 0), "left": (0, 1), "down": (1, 1), "right": (2, 1)}.items():
                binding = self.binding is not None and self.binding[0] == self.sel and self.binding[1] == d
                k = p["keys"].get(d)
                b = Gtk.Button(label="…" if binding else f"{DIR_ARROW[d]} {key_label(k) if k else '?'}",
                               width_request=76)
                if binding:
                    b.add_css_class("suggested-action")
                b.connect("clicked", lambda _b, d=d: self.start_binding(self.sel, d))
                grid.attach(b, col, row, 1, 1)
            self.panel.append(grid)
            seq = Gtk.Button(label="依次绑定 ↑ ← ↓ →")
            seq.add_css_class("flat")
            seq.connect("clicked", lambda *_: self.start_binding(self.sel, JOY_DIRS[0], seq=True))
            self.panel.append(seq)

        if p["type"] == "skill":
            self._skill_panel(p)
            return
        self._size_and_footer(p)

    def _skill_panel(self, p):
        self.panel.append(self._caption("按住按键时移动鼠标选择方向，松开释放；\n不动鼠标直接按就是快速施法"))
        self.panel.append(self._caption("灵敏度"))
        sens = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.2, 5.0, 0.05)
        sens.set_value(p.get("sens", 1.0))
        sens.add_mark(1.0, Gtk.PositionType.BOTTOM, None)
        sens.set_draw_value(True)
        sens.set_digits(2)
        sens.connect("value-changed", lambda sc: p.__setitem__("sens", round(sc.get_value(), 2)))
        self.panel.append(sens)
        self.panel.append(self._caption("取消施法：按住时点鼠标右键（所有技能共用）"))
        placing = self.placing_cancel == self.sel
        c = p.get("cancel")
        row = Gtk.Box(spacing=6)
        b = Gtk.Button(label="点击画面上的取消位置…" if placing else
                       (f"取消位置 {c[0] * 100:.0f}% , {c[1] * 100:.0f}%" if c else "设置取消位置"), hexpand=True)
        if placing:
            b.add_css_class("suggested-action")
        b.connect("clicked", lambda *_: self.start_cancel_pick(self.sel))
        row.append(b)
        if c and not placing:
            clear = Gtk.Button(icon_name="edit-clear-symbolic", tooltip_text="不使用取消")
            clear.connect("clicked", lambda *_: (self._set_all_cancels(None), self.refresh_panel(), self.area.queue_draw()))
            row.append(clear)
        self.panel.append(row)
        self._size_and_footer(p, "拖动范围（也可在画面上滚动滚轮）")

    def _set_all_cancels(self, c):
        """The cancel area belongs to the game and there is one: all skills share it."""
        for q in self.points:
            if q["type"] == "skill":
                q["cancel"] = c

    def start_cancel_pick(self, i):
        self.cancel_binding()
        self.placing_cancel = i
        self.select(i)

    def _set_cancel(self, x, y):
        i, self.placing_cancel = self.placing_cancel, None
        if i is None or i >= len(self.points):
            return
        self._set_all_cancels([round(x, 4), round(y, 4)])
        self.show_toast("已设置取消位置")
        self.refresh_panel()
        self.area.queue_draw()

    def _look_panel(self, p):
        self.panel.append(self._caption("游戏里移动鼠标即拖动视角；松开一会儿手指会抬起"))
        self.panel.append(self._caption("显示鼠标（点界面用）的按键"))
        binding = self.binding is not None and self.binding[0] == self.sel
        b = Gtk.Button(label="按下按键…" if binding else key_label(p.get("cursor_key")))
        if binding:
            b.add_css_class("suggested-action")
        b.connect("clicked", lambda *_: self.start_binding(self.sel, "cursor"))
        self.panel.append(b)
        mode = Gtk.Box(homogeneous=True)
        mode.add_css_class("linked")
        tg = Gtk.ToggleButton(label="单击切换", active=p.get("cursor_mode", "toggle") == "toggle")
        hd = Gtk.ToggleButton(label="按住显示", active=p.get("cursor_mode") == "hold", group=tg)

        def set_mode(btn, m):
            if btn.get_active():
                p["cursor_mode"] = m
                self.area.queue_draw()

        tg.connect("toggled", set_mode, "toggle")
        hd.connect("toggled", set_mode, "hold")
        mode.append(tg)
        mode.append(hd)
        self.panel.append(mode)
        self.panel.append(self._caption("灵敏度"))
        sens = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.2, 5.0, 0.05)
        sens.set_value(p.get("sens", 1.0))
        sens.add_mark(1.0, Gtk.PositionType.BOTTOM, None)
        sens.set_draw_value(True)
        sens.set_digits(2)
        sens.connect("value-changed", lambda sc: p.__setitem__("sens", round(sc.get_value(), 2)))
        self.panel.append(sens)
        self._size_and_footer(p, "活动范围（手指移出范围会回到中心重新按下）")

    def _size_and_footer(self, p, size_caption="大小（也可在画面上滚动滚轮）"):
        self.panel.append(self._caption(size_caption))
        lo, hi = SIZE_RANGE[p["type"]]
        self.size_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, (hi - lo) / 200)
        self.size_scale.set_draw_value(False)
        self._syncing = True
        self.size_scale.set_value(p.get("r", TAP_R))
        self._syncing = False
        self.size_scale.connect("value-changed", self._on_size)
        self.panel.append(self.size_scale)

        self.pos_label = self._caption("")
        self.panel.append(self.pos_label)
        self._update_pos_label()

        delete = Gtk.Button(label="删除")
        delete.add_css_class("destructive-action")
        delete.connect("clicked", lambda *_: self.delete_selected())
        self.panel.append(delete)

    @staticmethod
    def _caption(text):
        lab = Gtk.Label(label=text, xalign=0)
        lab.add_css_class("caption")
        lab.add_css_class("dim-label")
        return lab

    def _update_pos_label(self):
        if self.sel is not None and self.sel < len(self.points) and self.panel.get_visible():
            p = self.points[self.sel]
            self.pos_label.set_text(f"位置 {p['x'] * 100:.1f}% , {p['y'] * 100:.1f}%")

    def _on_size(self, scale):
        if self._syncing or self.sel is None:
            return
        self.points[self.sel]["r"] = scale.get_value()
        self.area.queue_draw()

    def convert(self, kind):
        if self.sel is None:
            return False
        p = self.points[self.sel]
        if p["type"] == kind:
            return False
        # Choosing a type keeps the point: don't drop it as an abandoned new one.
        self.binding_new = False
        self.cancel_binding()
        x, y = p["x"], p["y"]
        key, mods = (p.get("key"), p.get("mods") or []) if p["type"] in KEYED else (None, [])
        cancel = next((q["cancel"] for q in self.points if q["type"] == "skill" and q.get("cancel")), None)
        p.clear()
        if kind in KEYED:
            # Tap <-> skill keeps the binding: same button, different behaviour.
            p.update(new_tap(x, y) if kind == "tap" else new_skill(x, y))
            p["key"], p["mods"] = key, list(mods)
            if kind == "skill":
                p["cancel"] = cancel  # the game's cancel area is shared by all skills
            if key:
                self.refresh_panel()
            else:
                self.start_binding(self.sel, None)
        else:
            orig = dict(p)
            p.update(new_joystick(x, y))
            if joystick_keys_free(self.points, p):
                self.refresh_panel()
            else:
                p["keys"] = {d: None for d in JOY_DIRS}
                self.session_points.append((p, orig))
                self.start_binding(self.sel, JOY_DIRS[0], seq=True)
                self.binding_new = True  # abandon -> restore the original point
        self.area.queue_draw()
        return False

    def delete_selected(self):
        if self.sel is None:
            return
        self.cancel_binding()
        self.placing_cancel = None
        if self.sel is not None and self.sel < len(self.points):
            del self.points[self.sel]
        self.select(None)

    # ---- canvas input ----
    def _size(self):
        return self.area.get_width() or 1, self.area.get_height() or 1

    @staticmethod
    def _radius_px(p, h):
        if p["type"] == "skill":
            return SKILL_BTN * h  # r is the drag range; only the button itself is grabbable
        return max(p.get("r", TAP_R) * h, 10)

    def hit(self, x, y):
        w, h = self._size()
        best = None
        for i, p in enumerate(self.points):
            d = math.hypot(x - p["x"] * w, y - p["y"] * h)
            if d <= self._radius_px(p, h) + 4 and (best is None or d < best[1]):
                best = (i, d)
        return None if best is None else best[0]

    def _drag_begin(self, g, x, y):
        w, h = self._size()
        if self.placing_cancel is not None:
            self.drag_orig = None
            self._set_cancel(x / w, y / h)
            return
        self.cancel_binding()  # may delete an unbound new point, so hit-test after
        i = self.hit(x, y)
        if i is None:
            self.points.append(new_tap(x / w, y / h))
            i = len(self.points) - 1
            self.drag_orig = None
            self.sel = i
            self.start_binding(i, None, new=True)
        else:
            self.drag_orig = (self.points[i]["x"], self.points[i]["y"])
            if i != self.sel:
                self.select(i)

    def _drag_update(self, g, dx, dy):
        if self.drag_orig is None or self.sel is None:
            return
        w, h = self._size()
        p = self.points[self.sel]
        p["x"] = clamp(self.drag_orig[0] + dx / w, 0, 1)
        p["y"] = clamp(self.drag_orig[1] + dy / h, 0, 1)
        self._update_pos_label()
        self.area.queue_draw()

    def _drag_end(self, g, dx, dy):
        if self.drag_orig is not None and self.sel is not None:
            if math.hypot(dx, dy) < 4:
                # Click without movement: rebind.
                kind = self.points[self.sel]["type"]
                if kind == "joystick":
                    self.start_binding(self.sel, JOY_DIRS[0], seq=True)
                elif kind in KEYED:
                    self.start_binding(self.sel, None)
            else:
                self.refresh_panel()  # panel may need to change sides
        self.drag_orig = None
        self.area.queue_draw()

    def _right_click(self, g, n, x, y):
        if self.placing_cancel is not None:
            self.placing_cancel = None
            self.refresh_panel()
            self.area.queue_draw()
            return
        self.cancel_binding()
        i = self.hit(x, y)
        if i is not None:
            del self.points[i]
            self.select(None)

    def _scroll(self, c, dx, dy):
        i = self.hit(*self.pointer)
        if i is None:
            i = self.sel
        if i is None:
            return False
        p = self.points[i]
        lo, hi = SIZE_RANGE[p["type"]]
        p["r"] = clamp(p.get("r", TAP_R) * (0.92 if dy > 0 else 1.08), lo, hi)
        if i == self.sel and self.panel.get_visible():
            self._syncing = True
            self.size_scale.set_value(p["r"])
            self._syncing = False
        self.area.queue_draw()
        return True

    # ---- key binding ----
    def start_binding(self, i, d, seq=False, new=False):
        self.binding = (i, d, seq)
        self.binding_new = new
        if new and not any(q is self.points[i] for q, _ in self.session_points):
            self.session_points.append((self.points[i], None))
        self.mods_held.clear()
        self.mod_solo = False
        self.sel = i
        self.refresh_panel()
        self.area.queue_draw()

    @staticmethod
    def _incomplete(p):
        """True when a point cannot drive any input: a joystick missing one of
        its four directions, or a tap/skill with no key. A look needs no key."""
        if p["type"] == "joystick":
            return not all(p["keys"].get(k) for k in JOY_DIRS)
        if p["type"] in KEYED:
            return not p.get("key")
        return False

    def _rollback_incomplete(self, p):
        """Discard a half-bound point created this session, or restore the
        original dict when it came from converting an existing point."""
        for q, orig in self.session_points:
            if q is not p:
                continue
            if orig is None:
                j = next((k for k, r in enumerate(self.points) if r is p), None)
                if j is not None:
                    del self.points[j]
                self.sel = None
            else:
                p.clear()
                p.update(orig)
            return

    def cancel_binding(self):
        self.mods_held.clear()
        self.mod_solo = False
        if self.binding is None:
            return
        i = self.binding[0]
        self.binding = None
        if self.binding_new and i < len(self.points) and self._incomplete(self.points[i]):
            self._rollback_incomplete(self.points[i])
        self.binding_new = False
        self.refresh_panel()

    def _steal(self, name, mods, keep):
        """A key combo may only drive one control: unbind it elsewhere."""
        mods = sorted(mods)
        for p in self.points:
            if p is keep:
                continue
            if p["type"] in KEYED and p.get("key") == name and sorted(p.get("mods") or []) == mods:
                p["key"] = None
            elif p["type"] == "joystick" and not mods:
                for d, k in p["keys"].items():
                    if k == name:
                        p["keys"][d] = None

    def _bind(self, name, mods):
        i, d, seq = self.binding
        p = self.points[i]
        if d == "cursor":
            p["cursor_key"] = name
            self.binding = None
            self.mods_held.clear()
            self.mod_solo = False
            self.refresh_panel()
            self.area.queue_draw()
            return
        if d is not None:
            mods = []  # joystick directions are single keys
        self._steal(name, mods, p)
        if d is None:
            p["key"] = name
            p["mods"] = [m for m in MOD_ORDER if m in mods]
            self.binding = None
            self.binding_new = False
        else:
            p["keys"][d] = name
            nxt = JOY_DIRS.index(d) + 1
            if seq and nxt < len(JOY_DIRS):
                # Keep binding_new set until every direction is bound, so
                # abandoning a half-bound sequence drops the new point.
                self.binding = (i, JOY_DIRS[nxt], True)
            else:
                self.binding = None
                self.binding_new = False
        self.mods_held.clear()
        self.mod_solo = False
        self.refresh_panel()
        self.area.queue_draw()

    def _key(self, c, keyval, keycode, state):
        name = key_name(keycode - 8)  # Wayland keycodes are evdev + 8
        if self.binding is None:
            if self.popovers_open:
                return False  # typing in a popover entry
            if keyval == Gdk.KEY_Escape and self.placing_cancel is not None:
                self.placing_cancel = None
                self.refresh_panel()
                self.area.queue_draw()
                return True
            if keyval == Gdk.KEY_Escape:
                self.finish(True)
                return True
            if keyval in (Gdk.KEY_Delete, Gdk.KEY_BackSpace) and self.sel is not None:
                self.delete_selected()
                return True
            return False
        if keyval == Gdk.KEY_Escape:
            self.cancel_binding()
            self.area.queue_draw()
            return True
        if not name:
            return True
        if name in MOD_NAME_GROUP:
            # Wait: either another key follows (combo) or it is released alone.
            if name not in self.mods_held:
                self.mods_held.append(name)
            self.mod_solo = True
            self.area.queue_draw()
            return True
        mods = {MOD_NAME_GROUP[m] for m in self.mods_held}
        if self.is_hotkey(name, mods):
            self.show_toast(f"{combo_label(name, mods)} 是热键，不能绑定")
            return True
        self._bind(name, mods)
        return True

    def is_hotkey(self, name, mods):
        p = self.app.profile
        return any(name == p.get(f"{k}_key") and set(mods) == set(p.get(f"{k}_mods") or ())
                   for k in ("toggle", "edit"))

    def _key_released(self, c, keyval, keycode, state):
        name = key_name(keycode - 8)
        if name not in MOD_NAME_GROUP:
            return
        if self.binding is not None and self.mod_solo and name in self.mods_held:
            # Modifier pressed and released on its own: bind the modifier itself.
            others = {MOD_NAME_GROUP[m] for m in self.mods_held if m != name}
            if self.is_hotkey(name, others):
                self.show_toast(f"{combo_label(name, others)} 是热键，不能绑定")
                self.mods_held.remove(name)
                return
            self._bind(name, others)
            return
        if name in self.mods_held:
            self.mods_held.remove(name)

    def add_look(self, *_):
        self.cancel_binding()
        existing = next((i for i, p in enumerate(self.points) if p["type"] == "look"), None)
        if existing is not None:
            self.select(existing)
            self.show_toast("每个配置只有一个视角区域")
            return
        self.points.append(new_look(0.62, 0.42))
        self.select(len(self.points) - 1)
        if not self.app.profile.get("only_focused"):
            self.show_toast("提示：视角会占用鼠标。建议在高级设置里开启「仅聚焦指定软件窗口时生效」")

    def _mouse_bind(self, g, n, x, y):
        """While binding a tap, a click binds that mouse button (attack on LMB...)."""
        if not self.binding or self.binding[1] is not None:
            return
        name = GDK_BUTTONS.get(g.get_current_button())
        if not name:
            return
        g.set_state(Gtk.EventSequenceState.CLAIMED)  # don't also start a drag / delete
        self._bind(name, {MOD_NAME_GROUP[m] for m in self.mods_held})

    def add_joystick(self, *_):
        self.cancel_binding()
        p = new_joystick(0.18, 0.72)
        self.points.append(p)
        i = len(self.points) - 1
        if joystick_keys_free(self.points, p):
            self.select(i)
        else:
            p["keys"] = {d: None for d in JOY_DIRS}
            self.start_binding(i, JOY_DIRS[0], seq=True, new=True)

    def finish(self, save):
        self.cancel_binding()
        if save:
            # Any point created/converted in this session that is still
            # half-bound is invalid: drop it (or restore its original form)
            # so key2touch check never reports an unbound joystick.
            for p, _ in list(self.session_points):
                if self._incomplete(p):
                    self._rollback_incomplete(p)
        # Close first: if saving fails, a full-screen overlay holding the
        # keyboard exclusively must never be left behind.
        self.close()
        self.app.editor_done(save, self.backup)

    # ============================================================ drawing
    def draw(self, area, cr, w, h):
        if self.edit:
            if self.snapshot is not None and self.snapshot[0] is not None:
                # Opaque desktop copy first, then the dim scrim.  This is what
                # a working alpha channel would have produced visually.
                surf, _buf, _sw, _sh, _scale = self.snapshot
                sx, sy, srw, srh = self.snapshot_rect
                cr.save()
                cr.scale(w / srw, h / srh)
                cr.translate(-sx, -sy)
                cr.set_source_surface(surf, 0, 0)
                cr.paint()
                cr.restore()
                cr.set_source_rgba(0, 0, 0, 0.45)
                cr.paint()
            else:
                # Compositors where window alpha works: transparent + scrim.
                cr.set_source_rgba(0, 0, 0, 0.45)
                cr.paint()
            alpha = 1.0
        else:
            if not self.visible_hud:
                return
            alpha = self.app.settings.get("hud_opacity", 0.6) * (0.5 if self.dim else 1)
        cr.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        for i, p in enumerate(self.points):
            selected = self.edit and i == self.sel
            bind = self.binding if self.binding and self.binding[0] == i else None
            if p["type"] == "look":
                self._draw_look(cr, p, w, h, alpha, selected, bind is not None)
            elif p["type"] == "skill":
                self._draw_skill(cr, p, w, h, alpha, selected, bind is not None, i in self.pressed_idx)
            elif p["type"] == "joystick":
                self._draw_joy(cr, p, w, h, alpha, selected, bind[1] if bind else None)
            else:
                self._draw_tap(cr, p, w, h, alpha, selected, bind is not None, i in self.pressed_idx)
        if self.edit:
            self._draw_cancels(cr, w, h)
            self._draw_help(cr, w, h)

    def _label(self, cr, x, y, text, size, rgba=(1, 1, 1, 1)):
        cr.set_font_size(size)
        ext = cr.text_extents(text)
        cr.move_to(x - ext.width / 2 - ext.x_bearing, y - ext.height / 2 - ext.y_bearing)
        cr.set_source_rgba(*rgba)
        cr.show_text(text)

    def _hint(self, cr, x, y, text):
        cr.set_font_size(14)
        ext = cr.text_extents(text)
        pad = 10
        cr.new_path()
        _rounded(cr, x - ext.width / 2 - pad, y - 14, ext.width + 2 * pad, 28, 8)
        cr.set_source_rgba(0.1, 0.1, 0.12, 0.9)
        cr.fill()
        self._label(cr, x, y, text, 14, (1, 0.82, 0.35, 1))

    def _draw_tap(self, cr, p, w, h, a, selected, binding, down):
        x, y = p["x"] * w, p["y"] * h
        r = self._radius_px(p, h)
        cr.new_path()
        cr.arc(x, y, r, 0, 2 * math.pi)
        if down:
            cr.set_source_rgba(0.21, 0.52, 0.89, 0.85)
        else:
            cr.set_source_rgba(0.08, 0.08, 0.1, 0.55 * a)
        cr.fill_preserve()
        cr.set_line_width(3 if selected or binding else 2)
        if binding:
            cr.set_source_rgba(1, 0.75, 0.2, 1)
        elif selected:
            cr.set_source_rgba(0.35, 0.7, 1, 1)
        else:
            cr.set_source_rgba(1, 1, 1, 0.8 * a)
        cr.stroke()
        if binding:
            held = [MOD_LABEL[MOD_NAME_GROUP[m]] for m in self.mods_held]
            text = "+".join(held + ["…"])
        else:
            text = combo_label(p.get("key"), p.get("mods"))
        # Fit the label inside the circle.
        size = clamp(min(r * 0.8, 2.6 * r / max(len(text), 1)), 9, 30)
        self._label(cr, x, y, text, size, (1, 1, 1, max(a, 0.35)))
        if binding:
            self._hint(cr, x, y - r - 24, "按下要绑定的键或鼠标按键，可配合 Ctrl/Alt/Shift（Esc 取消）")

    def _draw_joy(self, cr, p, w, h, a, selected, bind_dir):
        x, y, r = p["x"] * w, p["y"] * h, p["r"] * h
        cr.new_path()
        cr.arc(x, y, r, 0, 2 * math.pi)
        cr.set_source_rgba(0.08, 0.08, 0.1, 0.35 * a)
        cr.fill_preserve()
        cr.set_line_width(3 if selected else 2)
        if selected:
            cr.set_source_rgba(0.35, 0.7, 1, 1)
        else:
            cr.set_source_rgba(1, 1, 1, 0.7 * a)
        cr.stroke()
        cr.new_path()
        cr.arc(x, y, 6, 0, 2 * math.pi)
        cr.set_source_rgba(1, 1, 1, 0.8 * a)
        cr.fill()
        for d in JOY_DIRS:
            ang = math.radians(DIR_ANGLE[d])
            lx, ly = x + math.cos(ang) * r * 0.62, y + math.sin(ang) * r * 0.62
            k = p["keys"].get(d)
            if bind_dir == d:
                text, col = "…", (1, 0.8, 0.3, 1)
            elif k in self.pressed:
                text, col = key_label(k), (0.4, 0.75, 1, 1)
            else:
                text, col = key_label(k), (1, 1, 1, max(a, 0.35))
            self._label(cr, lx, ly, text, clamp(r * 0.16, 11, 22), col)
        if bind_dir:
            names = {"up": "上", "left": "左", "down": "下", "right": "右"}
            self._hint(cr, x, y - r - 24, f"按下「{names[bind_dir]}」方向的键（Esc 停止）")

    def _draw_look(self, cr, p, w, h, a, selected, binding):
        x, y, r = p["x"] * w, p["y"] * h, max(p["r"], LOOK_MIN_R) * h
        cr.new_path()
        cr.set_dash([10, 8])
        cr.arc(x, y, r, 0, 2 * math.pi)
        cr.set_source_rgba(0.08, 0.08, 0.1, 0.2 * a)
        cr.fill_preserve()
        cr.set_line_width(3 if selected else 2)
        cr.set_source_rgba(*((0.35, 0.7, 1, 1) if selected else (1, 1, 1, 0.6 * a)))
        cr.stroke()
        cr.set_dash([])
        # crosshair
        cr.set_line_width(2)
        cr.set_source_rgba(1, 1, 1, 0.8 * a)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            cr.move_to(x + dx * 8, y + dy * 8)
            cr.line_to(x + dx * 22, y + dy * 22)
        cr.stroke()
        self._label(cr, x, y + 40, "视角", 16, (1, 1, 1, max(a, 0.35)))
        verb = "按住" if p.get("cursor_mode") == "hold" else "单击"
        hint = "按下按键…" if binding else f"{verb} {key_label(p.get('cursor_key'))} 显示鼠标"
        self._label(cr, x, y + 62, hint, 12, (1, 0.82, 0.35, 1) if binding else (1, 1, 1, max(a * 0.8, 0.3)))

    def _draw_skill(self, cr, p, w, h, a, selected, binding, down):
        x, y = p["x"] * w, p["y"] * h
        if selected:
            # Drag range: how far the finger can travel while aiming. Only for
            # the selected skill; skills sit close together and rings would pile up.
            cr.new_path()
            cr.set_dash([6, 6])
            cr.arc(x, y, p.get("r", SKILL_R) * h, 0, 2 * math.pi)
            cr.set_line_width(2)
            cr.set_source_rgba(0.35, 0.7, 1, 0.9)
            cr.stroke()
            cr.set_dash([])
        # The button itself looks like a tap with a ring, so it reads as "skill".
        self._draw_tap(cr, {**p, "r": SKILL_BTN}, w, h, a, selected, binding, down)
        r = SKILL_BTN * h
        cr.new_path()
        cr.arc(x, y, r + 5, 0, 2 * math.pi)
        cr.set_line_width(2)
        cr.set_source_rgba(1, 0.82, 0.35, 0.8 * max(a, 0.4))
        cr.stroke()

    def _draw_cancels(self, cr, w, h):
        seen = set()
        sel = self.points[self.sel] if self.sel is not None and self.sel < len(self.points) else None
        for q in self.points:
            c = q.get("cancel") if q["type"] == "skill" else None
            if not c or tuple(c) in seen:
                continue
            seen.add(tuple(c))
            x, y, r = c[0] * w, c[1] * h, 0.03 * h
            hot = sel is q
            cr.new_path()
            cr.arc(x, y, r, 0, 2 * math.pi)
            cr.set_source_rgba(0.75, 0.15, 0.2, 0.75 if hot else 0.45)
            cr.fill_preserve()
            cr.set_line_width(2)
            cr.set_source_rgba(1, 1, 1, 0.9 if hot else 0.5)
            cr.stroke()
            d = r * 0.4  # drawn, not a glyph: the cairo toy font has no ✕
            cr.set_line_width(3)
            cr.set_source_rgba(1, 1, 1, 1)
            cr.move_to(x - d, y - d)
            cr.line_to(x + d, y + d)
            cr.move_to(x + d, y - d)
            cr.line_to(x - d, y + d)
            cr.stroke()
            self._label(cr, x, y + r + 14, "取消施法", 12, (1, 1, 1, 0.9 if hot else 0.6))
        if self.placing_cancel is not None:
            self._hint(cr, w / 2, 110, "点击游戏里「取消施法」的位置（右键或 Esc 放弃）")

    def _draw_help(self, cr, w, h):
        text = ("左键空白处 添加按键  ·  点击按键 重新绑定  ·  拖动 移动  ·  滚轮 调整大小  ·  "
                "右键 / Delete 删除  ·  Esc 保存并退出")
        self._hint(cr, w / 2, h - 36, text)


def _rounded(cr, x, y, w, h, r):
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()
