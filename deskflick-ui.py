#!/usr/bin/env python3
"""deskflick-ui — settings window for deskflick.

Edits ~/.config/deskflick/config.toml and restarts the deskflick user service
on save. The trigger can be captured straight from the mouse, including
buttons that send keyboard macros from the mouse's onboard profile.
"""

import os
import re
import subprocess
import sys
import tomllib

from PySide6 import QtCore, QtGui, QtWidgets

CONFIG_PATH = os.path.expanduser("~/.config/deskflick/config.toml")

DEFAULTS = {
    "trigger": {"button": "BTN_SIDE", "tap": "Show Desktop",
                "double": "Window Maximize", "hold": "overview",
                "tap_timeout_ms": 350, "double_ms": 250, "hold_ms": 200},
    "gesture": {
        "threshold": 150, "repeat": True, "cooldown_ms": 180,
        "lock_pointer": False, "invert_x": True, "invert_y": True,
        "diagonals": True, "diagonal_ratio": 0.5,
    },
    "actions": {
        "left": "Switch One Desktop to the Left",
        "right": "Switch One Desktop to the Right",
        "up": "Switch One Desktop Up",
        "down": "Switch One Desktop Down",
        "up_left": "none", "up_right": "none",
        "down_left": "none", "down_right": "none",
    },
    "overview": {"shortcut": "ExposeAll"},
    "modifier": {
        "enabled": True, "key": "KEY_LEFTMETA", "defuse_launcher": True,
        "left": "Window Quick Tile Left", "right": "Window Quick Tile Right",
        "up": "Window Quick Tile Top", "down": "Window Quick Tile Bottom",
        "up_left": "Window Quick Tile Top Left",
        "up_right": "Window Quick Tile Top Right",
        "down_left": "Window Quick Tile Bottom Left",
        "down_right": "Window Quick Tile Bottom Right",
        "invert_x": False, "invert_y": False, "suppress_press": True,
    },
}

FLICK_DIRECTIONS = [
    ("left", "←"), ("right", "→"), ("up", "↑"), ("down", "↓"),
    ("up_left", "↖"), ("up_right", "↗"),
    ("down_left", "↙"), ("down_right", "↘"),
]

MODIFIER_KEYS = [
    ("KEY_LEFTMETA", "Meta / Super"),
    ("KEY_LEFTCTRL", "Ctrl"),
    ("KEY_LEFTALT", "Alt"),
    ("KEY_LEFTSHIFT", "Shift"),
]

BUTTON_CHOICES = [
    ("BTN_SIDE", "Button 4 — back (BTN_SIDE)"),
    ("BTN_EXTRA", "Button 5 — forward (BTN_EXTRA)"),
    ("BTN_MIDDLE", "Middle button (BTN_MIDDLE)"),
    ("BTN_FORWARD", "BTN_FORWARD"),
    ("BTN_BACK", "BTN_BACK"),
    ("BTN_TASK", "BTN_TASK"),
]

# Offered at the top of every press-action box; anything else typed or picked
# is passed to KWin as a shortcut name (or run as cmd:<command>).
PRESET_ACTIONS = [
    ("none", "— nothing —"),
    ("passthrough", "Its original action (click / macro)"),
    ("overview", "Show all windows (Present Windows)"),
]


def load_config() -> dict:
    cfg = {s: dict(v) for s, v in DEFAULTS.items()}
    if not os.path.exists(CONFIG_PATH):
        return cfg
    try:
        with open(CONFIG_PATH, "rb") as f:
            user = tomllib.load(f)
    except Exception as e:
        print(f"warning: could not parse {CONFIG_PATH}: {e}", file=sys.stderr)
        return cfg
    for section, values in user.items():
        if section in cfg and isinstance(values, dict):
            cfg[section].update(values)
    # migrate <= 0.3.0 layout
    trig = user.get("trigger", {})
    if "tap" not in trig:
        if user.get("overview", {}).get("enabled"):
            cfg["trigger"]["tap"] = "overview"
        elif trig.get("tap_passthrough") is False:
            cfg["trigger"]["tap"] = "none"
        else:
            cfg["trigger"]["tap"] = "passthrough"
    cfg["trigger"].pop("tap_passthrough", None)
    cfg["overview"].pop("enabled", None)
    return cfg


def toml_val(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def dump_config(cfg: dict) -> str:
    lines = ["# deskflick configuration (written by deskflick-ui)", ""]
    for section in ("trigger", "gesture", "actions", "overview", "modifier"):
        lines.append(f"[{section}]")
        for key, value in cfg[section].items():
            lines.append(f"{key} = {toml_val(value)}")
        lines.append("")
    return "\n".join(lines)


def kwin_shortcut_names() -> list[str]:
    try:
        out = subprocess.run(
            ["gdbus", "call", "--session",
             "--dest", "org.kde.kglobalaccel",
             "--object-path", "/component/kwin",
             "--method", "org.kde.kglobalaccel.Component.shortcutNames"],
            capture_output=True, text=True, timeout=3,
        ).stdout
        return sorted(set(re.findall(r"'((?:[^'\\]|\\.)*)'", out)))
    except Exception:
        return []


def label_for(direction: str) -> str:
    return direction.replace("_", "-").replace("-", " ").title()


class NoWheel:
    """Mixin: let the wheel scroll the page instead of changing the value.

    Silently rebinding an action because the pointer happened to be over it
    while scrolling is the kind of bug you only notice days later, in a
    config you did not knowingly write.
    """

    def wheelEvent(self, event):
        event.ignore()


class ActionCombo(NoWheel, QtWidgets.QComboBox):
    pass


class SafeSpinBox(NoWheel, QtWidgets.QSpinBox):
    pass


# ------------------------------------------------------------------ capture


# Qt numbers extra buttons in the order the evdev codes appear, starting at
# BTN_SIDE (0x113) -- so ExtraButton1/2/3/4/5 are SIDE, EXTRA, FORWARD, BACK,
# TASK. BackButton/ForwardButton/TaskButton are aliases for the first three.
# Only a fallback: evdev capture below is authoritative.
QT_BUTTON_MAP = {
    QtCore.Qt.MouseButton.MiddleButton: "BTN_MIDDLE",
    QtCore.Qt.MouseButton.BackButton: "BTN_SIDE",       # ExtraButton1, 0x113
    QtCore.Qt.MouseButton.ForwardButton: "BTN_EXTRA",   # ExtraButton2, 0x114
    QtCore.Qt.MouseButton.TaskButton: "BTN_FORWARD",    # ExtraButton3, 0x115
    QtCore.Qt.MouseButton.ExtraButton4: "BTN_BACK",     # 0x116
    QtCore.Qt.MouseButton.ExtraButton5: "BTN_TASK",     # 0x117
}


class EvdevCapture(QtCore.QThread):
    """Reports the first key/button burst seen on any readable device.

    Reports the whole burst, not just one key: mice with onboard profiles
    send macros like Ctrl+Tab, and the user needs to see that is what their
    button does. Devices deskflick itself created are skipped.
    """

    captured = QtCore.Signal(str, str, str)  # first key, full combo, device
    failed = QtCore.Signal(str)

    def run(self):
        try:
            import select
            import time
            from evdev import InputDevice, ecodes, list_devices
        except ImportError:
            self.failed.emit("python-evdev is not installed")
            return

        ignore = {ecodes.BTN_LEFT, ecodes.BTN_RIGHT, ecodes.BTN_TOUCH,
                  ecodes.BTN_TOOL_FINGER, ecodes.BTN_TOOL_DOUBLETAP}
        devices, unreadable = [], 0
        for path in list_devices():
            try:
                dev = InputDevice(path)
            except OSError:
                unreadable += 1
                continue
            # deskflick's own clones are included on purpose: while the daemon
            # holds the real device, the clone is where the true evdev codes
            # still show up.
            if ecodes.EV_KEY in dev.capabilities():
                devices.append(dev)
            else:
                dev.close()
        if not devices:
            self.failed.emit(
                f"no readable input devices ({unreadable} without permission)")
            return

        first, burst, source = "", [], ""
        burst_until = None
        try:
            while not self.isInterruptionRequested():
                if burst_until and time.monotonic() > burst_until:
                    break
                r, _, _ = select.select(devices, [], [], 0.1)
                for dev in r:
                    try:
                        events = list(dev.read())
                    except OSError:
                        continue
                    for ev in events:
                        if ev.type != ecodes.EV_KEY or ev.value != 1:
                            continue
                        if ev.code in ignore:
                            continue
                        name = ecodes.bytype[ecodes.EV_KEY].get(ev.code)
                        if isinstance(name, (list, tuple)):
                            name = next((n for n in name
                                         if n.startswith("BTN_")), name[0])
                        if not name:
                            continue
                        if not first:
                            first, source = name, dev.name
                            burst_until = time.monotonic() + 0.15
                        if name not in burst:
                            burst.append(name)
        finally:
            for dev in devices:
                dev.close()
        if first:
            self.captured.emit(first, " + ".join(burst), source)
        else:
            self.failed.emit("")


class CaptureDialog(QtWidgets.QDialog):
    """Press the wanted button over this dialog.

    Two capture paths run at once: Qt mouse events (always work, no
    permissions needed, but only see real mouse buttons) and evdev (sees
    macro keys and reports which device they came from).
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Detect button")
        self.setModal(True)
        self.result_button = ""
        self.note = ""

        layout = QtWidgets.QVBoxLayout(self)
        self.label = QtWidgets.QLabel(
            "<b>Press the button you want to use</b><br>"
            "with the pointer over this window.<br><br>"
            "<small>Left and right click are ignored.</small>")
        self.label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.label.setWordWrap(True)
        layout.addWidget(self.label)
        cancel = QtWidgets.QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        layout.addWidget(cancel)
        self.setMinimumSize(400, 200)

        self.thread = EvdevCapture()
        self.thread.captured.connect(self._on_evdev)
        self.thread.failed.connect(self._on_failed)
        self.thread.start()
        self.evdev_error = None
        QtCore.QTimer.singleShot(20000, self.reject)

    def mousePressEvent(self, event):
        name = QT_BUTTON_MAP.get(event.button())
        if name:
            # evdev wins if it answers: it reports the exact code and the
            # device, and it sees macro keys Qt never receives.
            QtCore.QTimer.singleShot(
                450, lambda: self._finish(
                    name, "Detected through Qt — install permissions are "
                          "incomplete, so the exact device is unknown."))
        event.accept()

    def _on_evdev(self, first: str, combo: str, device: str):
        note = ""
        if combo and combo != first:
            note = (f"That button sends <b>{combo}</b> from its onboard "
                    f"profile (device: {device}). deskflick will bind "
                    f"<b>{first}</b> and suppress the rest while you gesture.")
        elif device:
            note = f"Detected on: {device}"
        self._finish(first, note)

    def _on_failed(self, message: str):
        self.evdev_error = message
        if message:
            self.label.setText(
                self.label.text() +
                f"<br><br><small style='color:#c0392b'>evdev: {message}. "
                "Only real mouse buttons can be detected; macro buttons "
                "need working permissions (re-run install.sh).</small>")

    def _finish(self, name: str, note: str):
        if self.result_button:
            return
        self.result_button = name
        self.note = note
        self.accept()

    def done(self, r):
        if self.thread.isRunning():
            self.thread.requestInterruption()
            self.thread.wait(2000)
        super().done(r)


# ------------------------------------------------------------------ window


class Window(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("deskflick")
        self.cfg = load_config()
        shortcuts = kwin_shortcut_names()

        # Everything lives inside a scroll area: with eight flick directions
        # in two tables, the form is taller than a lot of screens.
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        page = QtWidgets.QWidget()
        scroll.setWidget(page)
        outer.addWidget(scroll, 1)
        layout = QtWidgets.QVBoxLayout(page)

        # --- Trigger ---------------------------------------------------
        trig_box = QtWidgets.QGroupBox("Trigger")
        trig_form = QtWidgets.QFormLayout(trig_box)

        self.button_combo = ActionCombo()
        self.button_combo.setEditable(True)
        for code, label in BUTTON_CHOICES:
            self.button_combo.addItem(label, code)
        self._select_button(self.cfg["trigger"]["button"])

        self.detect_btn = QtWidgets.QPushButton("Detect…")
        self.detect_btn.setToolTip(
            "Opens a window — press the button you want over it.")
        self.detect_btn.clicked.connect(self.start_capture)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.button_combo, 1)
        row.addWidget(self.detect_btn)
        trig_form.addRow("Hold this button:", row)

        self.trigger_note = QtWidgets.QLabel()
        self.trigger_note.setWordWrap(True)
        self.trigger_note.setStyleSheet("color: palette(mid);")
        trig_form.addRow(self.trigger_note)

        self.press_combos = {}
        for key, label, tip in (
            ("tap", "Tap:", "One quick press and release."),
            ("double", "Double tap:", "Two quick presses. Leave on “nothing” "
                                      "to keep taps instant — otherwise a tap "
                                      "waits to see if a second one follows."),
            ("hold", "Press and hold:",
             "Fires while you keep the button down without flicking. "
             "Flicking afterwards still switches desktops."),
        ):
            combo = self._action_combo(shortcuts, str(self.cfg["trigger"][key]))
            combo.setToolTip(tip)
            self.press_combos[key] = combo
            trig_form.addRow(label, combo)

        self.hold_ms = SafeSpinBox()
        self.hold_ms.setRange(150, 3000)
        self.hold_ms.setSingleStep(50)
        self.hold_ms.setSuffix(" ms")
        self.hold_ms.setValue(int(self.cfg["trigger"]["hold_ms"]))
        self.double_ms = SafeSpinBox()
        self.double_ms.setRange(100, 800)
        self.double_ms.setSingleStep(25)
        self.double_ms.setSuffix(" ms")
        self.double_ms.setValue(int(self.cfg["trigger"]["double_ms"]))
        timing = QtWidgets.QHBoxLayout()
        timing.addWidget(QtWidgets.QLabel("hold after"))
        timing.addWidget(self.hold_ms)
        timing.addSpacing(12)
        timing.addWidget(QtWidgets.QLabel("double within"))
        timing.addWidget(self.double_ms)
        timing.addStretch(1)
        trig_form.addRow("Timing:", timing)

        self.overview_shortcut = ActionCombo()
        self.overview_shortcut.setEditable(True)
        self.overview_shortcut.addItems(shortcuts or ["ExposeAll", "Overview"])
        self.overview_shortcut.setCurrentText(str(self.cfg["overview"]["shortcut"]))
        self.overview_shortcut.setToolTip(
            "Used wherever an action above is set to “Show all windows”.")
        for combo in self.press_combos.values():
            combo.currentIndexChanged.connect(self._sync_overview)
        trig_form.addRow("“Show all windows” is:", self.overview_shortcut)
        self._sync_overview()
        layout.addWidget(trig_box)

        # --- Gesture ---------------------------------------------------
        gest_box = QtWidgets.QGroupBox("Gesture")
        gest_form = QtWidgets.QFormLayout(gest_box)

        class SafeSlider(NoWheel, QtWidgets.QSlider):
            pass
        self.threshold = SafeSlider(QtCore.Qt.Orientation.Horizontal)
        self.threshold.setRange(30, 600)
        self.threshold.setValue(int(self.cfg["gesture"]["threshold"]))
        self.threshold_label = QtWidgets.QLabel()
        self.threshold.valueChanged.connect(
            lambda v: self.threshold_label.setText(f"{v} px"))
        self.threshold_label.setText(f"{self.threshold.value()} px")
        trow = QtWidgets.QHBoxLayout()
        trow.addWidget(self.threshold, 1)
        trow.addWidget(self.threshold_label)
        gest_form.addRow("Sensitivity (lower = twitchier):", trow)

        self.repeat = QtWidgets.QCheckBox("Keep switching while you keep pushing")
        self.repeat.setChecked(bool(self.cfg["gesture"]["repeat"]))
        gest_form.addRow(self.repeat)

        self.lock_pointer = QtWidgets.QCheckBox("Freeze the pointer during a gesture")
        self.lock_pointer.setChecked(bool(self.cfg["gesture"]["lock_pointer"]))
        gest_form.addRow(self.lock_pointer)

        self.diagonals = QtWidgets.QCheckBox("Recognise diagonal flicks")
        self.diagonals.setChecked(bool(self.cfg["gesture"]["diagonals"]))
        self.diagonals.setToolTip(
            "A diagonal with nothing bound to it falls back to its dominant "
            "direction, so this never swallows a sloppy flick.")
        self.diagonal_ratio = SafeSpinBox()
        self.diagonal_ratio.setRange(20, 90)
        self.diagonal_ratio.setSingleStep(5)
        self.diagonal_ratio.setSuffix(" %")
        self.diagonal_ratio.setValue(
            int(round(float(self.cfg["gesture"]["diagonal_ratio"]) * 100)))
        self.diagonal_ratio.setToolTip(
            "How square the movement must be. 50% accepts roughly 27°-63°; "
            "higher means you must aim closer to a true 45°.")
        self.diagonals.toggled.connect(self.diagonal_ratio.setEnabled)
        self.diagonal_ratio.setEnabled(self.diagonals.isChecked())
        drow = QtWidgets.QHBoxLayout()
        drow.addWidget(self.diagonals)
        drow.addWidget(QtWidgets.QLabel("tolerance"))
        drow.addWidget(self.diagonal_ratio)
        drow.addStretch(1)
        gest_form.addRow(drow)

        self.invert_x = QtWidgets.QCheckBox("Invert left/right")
        self.invert_x.setChecked(bool(self.cfg["gesture"]["invert_x"]))
        self.invert_x.setToolTip("Applies to desktop switching only — the "
                                 "modifier gestures have their own.")
        self.invert_y = QtWidgets.QCheckBox("Invert up/down")
        self.invert_y.setChecked(bool(self.cfg["gesture"]["invert_y"]))
        self.invert_y.setToolTip(self.invert_x.toolTip())
        irow = QtWidgets.QHBoxLayout()
        irow.addWidget(self.invert_x)
        irow.addWidget(self.invert_y)
        gest_form.addRow(irow)
        layout.addWidget(gest_box)

        # --- Actions ---------------------------------------------------
        act_box = QtWidgets.QGroupBox("Flick actions (KWin shortcut or cmd:<command>)")
        act_form = QtWidgets.QFormLayout(act_box)
        self.action_combos = {}
        for direction, arrow in FLICK_DIRECTIONS:
            combo = self._action_combo(shortcuts, str(self.cfg["actions"][direction]))
            self.action_combos[direction] = combo
            act_form.addRow(f"{arrow} {label_for(direction)}:", combo)
        layout.addWidget(act_box)

        # --- Modifier --------------------------------------------------
        mod_box = QtWidgets.QGroupBox("With a modifier held")
        mod_form = QtWidgets.QFormLayout(mod_box)

        self.mod_enabled = QtWidgets.QCheckBox(
            "Flicking while this key is held moves the window instead")
        self.mod_enabled.setChecked(bool(self.cfg["modifier"]["enabled"]))
        self.mod_enabled.setToolTip(
            "Meta + trigger button + flick snaps the focused window, the way "
            "Meta + arrow keys does.\n"
            "Reading the keyboard's modifier state needs `input` group "
            "membership — log out and back in once after installing.")
        mod_form.addRow(self.mod_enabled)

        self.mod_key = ActionCombo()
        for code, label in MODIFIER_KEYS:
            self.mod_key.addItem(label, code)
        idx = self.mod_key.findData(str(self.cfg["modifier"]["key"]))
        self.mod_key.setCurrentIndex(max(0, idx))
        mod_form.addRow("Modifier:", self.mod_key)

        self.mod_combos = {}
        for direction, arrow in FLICK_DIRECTIONS:
            combo = self._action_combo(shortcuts, str(self.cfg["modifier"][direction]))
            self.mod_combos[direction] = combo
            mod_form.addRow(f"{arrow} {label_for(direction)}:", combo)

        self.mod_invert_x = QtWidgets.QCheckBox("Invert left/right")
        self.mod_invert_x.setChecked(bool(self.cfg["modifier"]["invert_x"]))
        self.mod_invert_y = QtWidgets.QCheckBox("Invert up/down")
        self.mod_invert_y.setChecked(bool(self.cfg["modifier"]["invert_y"]))
        mrow = QtWidgets.QHBoxLayout()
        mrow.addWidget(self.mod_invert_x)
        mrow.addWidget(self.mod_invert_y)
        mod_form.addRow(mrow)

        self.mod_suppress = QtWidgets.QCheckBox(
            "Ignore tap, double tap and hold while the modifier is down")
        self.mod_suppress.setChecked(bool(self.cfg["modifier"]["suppress_press"]))
        self.mod_suppress.setToolTip(
            "With this on, the button does window management only while the "
            "modifier is held — no click, no launcher, nothing else fires.")
        mod_form.addRow(self.mod_suppress)

        self.mod_defuse = QtWidgets.QCheckBox(
            "Keep a held Meta from opening the application launcher")
        self.mod_defuse.setChecked(bool(self.cfg["modifier"]["defuse_launcher"]))
        mod_form.addRow(self.mod_defuse)

        for widget in (self.mod_key, self.mod_defuse, self.mod_suppress,
                       self.mod_invert_x, self.mod_invert_y,
                       *self.mod_combos.values()):
            self.mod_enabled.toggled.connect(widget.setEnabled)
            widget.setEnabled(self.mod_enabled.isChecked())
        layout.addWidget(mod_box)

        # --- Footer ----------------------------------------------------
        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        save_btn = QtWidgets.QPushButton("Save && restart service")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self.save)
        footer = QtWidgets.QWidget()
        frow = QtWidgets.QHBoxLayout(footer)
        frow.addWidget(self.status, 1)
        frow.addWidget(save_btn)
        outer.addWidget(footer)
        self.refresh_status()

        self.resize(640, min(820, QtGui.QGuiApplication.primaryScreen()
                             .availableGeometry().height() - 80))

    # ------------------------------------------------------------------
    def _action_combo(self, shortcuts, value: str) -> QtWidgets.QComboBox:
        """Editable combo: presets first, then every KWin shortcut name."""
        combo = ActionCombo()
        combo.setEditable(True)
        for code, label in PRESET_ACTIONS:
            combo.addItem(label, code)
        combo.insertSeparator(combo.count())
        for name in shortcuts:
            combo.addItem(name, name)
        idx = combo.findData(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        else:
            combo.setCurrentText(value)
        return combo

    @staticmethod
    def combo_value(combo: QtWidgets.QComboBox) -> str:
        text = combo.currentText().strip()
        for code, label in PRESET_ACTIONS:
            if text == label:
                return code
        return text

    def _sync_overview(self):
        self.overview_shortcut.setEnabled(
            any(self.combo_value(c) == "overview"
                for c in self.press_combos.values()))

    def _select_button(self, value):
        idx = self.button_combo.findData(str(value))
        if idx >= 0:
            self.button_combo.setCurrentIndex(idx)
        else:
            self.button_combo.setCurrentText(str(value))

    def current_button(self) -> str:
        text = self.button_combo.currentText().strip()
        for code, label in BUTTON_CHOICES:
            if text == label:
                return code
        return text

    def start_capture(self):
        dlg = CaptureDialog(self)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted and dlg.result_button:
            self._select_button(dlg.result_button)
            self.trigger_note.setText(dlg.note)
        elif not dlg.result_button and dlg.evdev_error is not None:
            QtWidgets.QMessageBox.information(
                self, "deskflick",
                "No button was detected.\n\n"
                "If it is a macro button, deskflick needs permission to read "
                "your mouse's extra interfaces — re-run install.sh, and log "
                "out and back in once.")

    # ------------------------------------------------------------------
    def deskflick_state(self) -> str:
        active = subprocess.run(["systemctl", "--user", "is-active", "deskflick"],
                                capture_output=True, text=True).stdout.strip()
        if active != "active":
            return "Service: <b style='color:#c0392b'>stopped</b>"
        out = subprocess.run(
            ["journalctl", "--user", "-u", "deskflick", "-n", "40",
             "--no-pager", "-o", "cat"],
            capture_output=True, text=True).stdout
        # only look at the current run
        out = out.rsplit("deskflick: trigger=", 1)[-1]
        grabbed = re.findall(r"attached to \S+ \((.+?)\) \[(.+?)\]", out)
        if "no readable device reports" in out:
            return ("Service: <b style='color:#c0392b'>running, but it cannot "
                    "see that button</b> — re-run install.sh")
        if not grabbed:
            return "Service: <b style='color:#e67e22'>running, no device grabbed</b>"
        names = ", ".join(sorted({n for n, _ in grabbed}))
        return f"Service: <b style='color:#27ae60'>running</b> — {names}"

    def refresh_status(self):
        self.status.setText(self.deskflick_state())

    def available_buttons(self) -> dict[str, str]:
        """Button name -> device that reports it, across readable devices."""
        try:
            from evdev import InputDevice, ecodes, list_devices
        except ImportError:
            return {}
        found = {}
        for path in list_devices():
            try:
                dev = InputDevice(path)
            except OSError:
                continue
            if "[deskflick]" not in dev.name:
                for code in dev.capabilities().get(ecodes.EV_KEY, []):
                    name = ecodes.bytype[ecodes.EV_KEY].get(code)
                    if isinstance(name, (list, tuple)):
                        name = next((n for n in name if n.startswith("BTN_")),
                                    name[0])
                    if name and name.startswith("BTN_"):
                        found.setdefault(name, dev.name)
            dev.close()
        return found

    def confirm_unknown_button(self, button: str) -> bool:
        available = self.available_buttons()
        if not available or button in available:
            return True
        listing = "\n".join(f"  • {n} — {d}" for n, d in sorted(available.items())
                            if n not in ("BTN_LEFT", "BTN_RIGHT"))
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        box.setWindowTitle("deskflick")
        box.setText(f"No device reports <b>{button}</b>.")
        box.setInformativeText(
            "deskflick will not be able to intercept it, so the button will "
            "keep doing whatever it does today.\n\n"
            "Buttons your hardware actually reports:\n" + listing)
        box.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Save |
                               QtWidgets.QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QtWidgets.QMessageBox.StandardButton.Cancel)
        return box.exec() == QtWidgets.QMessageBox.StandardButton.Save

    def save(self):
        button = self.current_button()
        if button.startswith("BTN_") and not self.confirm_unknown_button(button):
            return
        self.cfg["trigger"]["button"] = self.current_button()
        for key, combo in self.press_combos.items():
            self.cfg["trigger"][key] = self.combo_value(combo)
        self.cfg["trigger"]["hold_ms"] = self.hold_ms.value()
        self.cfg["trigger"]["double_ms"] = self.double_ms.value()
        self.cfg["gesture"]["threshold"] = self.threshold.value()
        self.cfg["gesture"]["repeat"] = self.repeat.isChecked()
        self.cfg["gesture"]["lock_pointer"] = self.lock_pointer.isChecked()
        self.cfg["gesture"]["diagonals"] = self.diagonals.isChecked()
        self.cfg["gesture"]["diagonal_ratio"] = self.diagonal_ratio.value() / 100
        self.cfg["gesture"]["invert_x"] = self.invert_x.isChecked()
        self.cfg["gesture"]["invert_y"] = self.invert_y.isChecked()
        for direction, combo in self.action_combos.items():
            self.cfg["actions"][direction] = self.combo_value(combo)
        self.cfg["overview"]["shortcut"] = self.overview_shortcut.currentText().strip()
        self.cfg["modifier"]["enabled"] = self.mod_enabled.isChecked()
        self.cfg["modifier"]["key"] = self.mod_key.currentData()
        self.cfg["modifier"]["defuse_launcher"] = self.mod_defuse.isChecked()
        self.cfg["modifier"]["invert_x"] = self.mod_invert_x.isChecked()
        self.cfg["modifier"]["invert_y"] = self.mod_invert_y.isChecked()
        self.cfg["modifier"]["suppress_press"] = self.mod_suppress.isChecked()
        for direction, combo in self.mod_combos.items():
            self.cfg["modifier"][direction] = self.combo_value(combo)

        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            f.write(dump_config(self.cfg))

        self.status.setText("Restarting…")
        # Small delay so the restart never lands inside this very click.
        QtCore.QTimer.singleShot(250, self._restart_service)

    def _restart_service(self):
        subprocess.run(["systemctl", "--user", "restart", "deskflick"],
                       capture_output=True)
        QtCore.QTimer.singleShot(1500, self.refresh_status)


def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("deskflick")
    app.setDesktopFileName("deskflick")
    win = Window()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
