"""Regression tests for abandoned editor bindings.

An unfinished joystick sequence used to leave a point with all four
directions unset in the saved profile; key2touch check then reported
"第 1 个摇杆有方向没有绑定按键".  These tests exercise the editor's
binding state machine without a display (Overlay.__new__ + stubbed GTK
calls).
"""

import copy
import unittest
from types import SimpleNamespace

from key2touch.overlay import Overlay
from key2touch.profile import JOY_DIRS, new_joystick, new_tap


def blank_joystick():
    p = new_joystick(0.4, 0.4)
    p["keys"] = {d: None for d in JOY_DIRS}
    return p


class AbandonedBindingTests(unittest.TestCase):
    def make(self, points=None):
        if points is None:
            points = [new_joystick(0.2, 0.7)]  # WASD already taken
        ov = Overlay.__new__(Overlay)
        ov.app = SimpleNamespace(profile={"points": points})
        ov.backup = copy.deepcopy(ov.app.profile)
        ov.binding = None
        ov.binding_new = False
        ov.session_points = []
        ov.mods_held = []
        ov.mod_solo = False
        ov.sel = None
        ov.refresh_panel = lambda: None
        ov.area = SimpleNamespace(queue_draw=lambda: None)
        ov.close = lambda: None
        ov.app.editor_done = lambda save, backup: None
        return ov

    def test_incomplete(self):
        self.assertTrue(Overlay._incomplete(blank_joystick()))
        self.assertTrue(Overlay._incomplete(new_tap(0.5, 0.5)))
        self.assertFalse(Overlay._incomplete(new_joystick(0.2, 0.7)))

    def test_cancel_drops_blank_new_joystick(self):
        ov = self.make()
        ov.points.append(blank_joystick())
        ov.start_binding(1, JOY_DIRS[0], seq=True, new=True)
        ov.cancel_binding()
        self.assertEqual(len(ov.points), 1)
        self.assertIsNone(ov.binding)

    def test_cancel_drops_partially_bound_sequence(self):
        ov = self.make()
        ov.points.append(blank_joystick())
        ov.start_binding(1, JOY_DIRS[0], seq=True, new=True)
        ov._bind("KEY_I", set())
        self.assertEqual(ov.points[1]["keys"]["up"], "KEY_I")
        self.assertEqual(ov.binding[1], "left")
        ov.cancel_binding()
        self.assertEqual(len(ov.points), 1)

    def test_complete_sequence_is_kept(self):
        ov = self.make()
        ov.points.append(blank_joystick())
        ov.start_binding(1, JOY_DIRS[0], seq=True, new=True)
        for k in ("KEY_I", "KEY_J", "KEY_K", "KEY_L"):
            ov._bind(k, set())
        self.assertIsNone(ov.binding)
        self.assertFalse(ov.binding_new)
        self.assertEqual(ov.points[1]["keys"],
                         dict(zip(JOY_DIRS, ("KEY_I", "KEY_J", "KEY_K", "KEY_L"))))
        self.assertEqual(len(ov.points), 2)

    def test_finish_prunes_abandoned_session_point(self):
        ov = self.make()
        ov.points.append(blank_joystick())
        ov.start_binding(1, JOY_DIRS[0], seq=True, new=True)
        ov.binding = None  # sequence interrupted without a cancel
        ov.binding_new = False
        ov.finish(True)
        self.assertEqual(len(ov.points), 1)

    def test_finish_keeps_complete_session_point(self):
        ov = self.make()
        ov.points.append(blank_joystick())
        ov.start_binding(1, JOY_DIRS[0], seq=True, new=True)
        for k in ("KEY_I", "KEY_J", "KEY_K", "KEY_L"):
            ov._bind(k, set())
        ov.finish(True)
        self.assertEqual(len(ov.points), 2)

    def test_converted_point_is_restored(self):
        ov = self.make([new_tap(0.5, 0.5, "KEY_Q")])
        p = ov.points[0]
        ov.session_points.append((p, dict(p)))
        p.clear()
        p.update(new_joystick(0.5, 0.5))
        p["keys"] = {d: None for d in JOY_DIRS}
        ov._rollback_incomplete(p)
        self.assertEqual(p["type"], "tap")
        self.assertEqual(p["key"], "KEY_Q")
        self.assertEqual(len(ov.points), 1)


if __name__ == "__main__":
    unittest.main()
