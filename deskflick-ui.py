#!/usr/bin/env python3
"""deskflick-ui — small settings window for deskflick.

Edits ~/.config/deskflick/config.toml and restarts the deskflick user
service on save. Can capture a trigger button directly from your mouse
("Detect" button).
"""

import os
import subprocess
import sys
import tomllib

from PySide6 import QtCore, QtWidgets

CONFIG_PATH = os.path.expanduser("~/.config/deskflick/config.toml")

DEFAULTS = {
    "trigger": {"button": "BTN_SIDE", "tap_passthrough": True, "tap_timeout_ms": 350},
    "gesture": {
        "threshold": 150, "repeat": True, "cooldown_ms": 180,
        "lock_pointer": True, "invert_x": False, "invert_y": False,
    },
    "actions": {
        "left": "Switch One Desktop to the Left",
        "right": "Switch One Desktop to the Right",
        "up": "Switch One Desktop Up",
        "down": "Switch One Desktop Down",
    },
    "overview": {"enabled": False, "shortcut": "ExposeAll"},
}

BUTTON_CHOICES = [
    ("BTN_SIDE", "Button 4 — back (BTN_SIDE)"),
    ("BTN_EXTRA", "Button 5 — forward (BTN_EXTRA)"),
    ("BTN_MIDDLE", "Middle button (BTN_MIDDLE)"),
    ("BTN_FORWARD", "BTN_FORWARD"),
    ("BTN_BACK", "BTN_BACK"),
    ("BTN_TASK", "BTN_TASK"),
]


def load_config() -> dict:
    cfg = {s: dict(v) for s, v in DEFAULTS.items()}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "rb") as f:
                user = tomllib.load(f)
            for section, values in user.items():
                if section in cfg and isinstance(values, dict):
                    cfg[section].update(values)
        except Exception as e:
            print(f"warning: could not parse {CONFIG_PATH}: {e}", file=sys.stderr)
    return cfg


def toml_val(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def dump_config(cfg: dict) -> str:
    lines = ["# deskflick configuration (written by deskflick-ui)", ""]
    for section in ("trigger", "gesture", "actions", "overview"):
        lines.append(f"[{section}]")
        for key, value in cfg[section].items():
            lines.append(f"{key} = {toml_val(value)}")
        lines.append("")
    return "\n".join(lines)


def kwin_shortcut_names() -> list[str]:
    """All bindable KWin global shortcut names, via KGlobalAccel."""
    try:
        out = subprocess.run(
            ["gdbus", "call", "--session",
             "--dest", "org.kde.kglobalaccel",
             "--object-path", "/component/kwin",
             "--method", "org.kde.kglobalaccel.Component.shortcutNames"],
            capture_output=True, text=True, timeout=3,
        ).stdout
        import re
        return sorted(set(re.findall(r"'((?:[^'\\]|\\.)*)'", out)))
    except Exception:
        return []


class ButtonCapture(QtCore.QThread):
    """Waits for the next mouse-button press on any input device.

    Also listens on deskflick's own virtual devices, so capture works while
    the daemon has the physical mouse grabbed. The current trigger button is
    swallowed by the daemon, so press a *different* button to capture it.
    """

    captured = QtCore.Signal(str)

    def run(self):
        try:
            import select
            from evdev import InputDevice, ecodes, list_devices
        except ImportError:
            self.captured.emit("")
            return
        ignore = {ecodes.BTN_LEFT, ecodes.BTN_RIGHT, ecodes.BTN_TOUCH,
                  ecodes.BTN_TOOL_FINGER, ecodes.BTN_TOOL_DOUBLETAP}
        devices = []
        for path in list_devices():
            try:
                dev = InputDevice(path)
            except OSError:
                continue
            if ecodes.EV_KEY in dev.capabilities():
                devices.append(dev)
            else:
                dev.close()
        if not devices:
            self.captured.emit("")
            return
        name = ""
        deadline = QtCore.QDeadlineTimer(15000)
        try:
            while (not deadline.hasExpired() and not name
                   and not self.isInterruptionRequested()):
                r, _, _ = select.select(devices, [], [], 0.2)
                for dev in r:
                    try:
                        for ev in dev.read():
                            if (ev.type == ecodes.EV_KEY and ev.value == 1
                                    and ev.code not in ignore):
                                names = ecodes.keys.get(ev.code) or ecodes.BTN.get(ev.code)
                                if isinstance(names, list):
                                    names = next(
                                        (n for n in names if n.startswith("BTN_")),
                                        names[0])
                                if names and str(names).startswith("BTN_"):
                                    name = str(names)
                                    break
                    except OSError:
                        pass
                    if name:
                        break
        finally:
            for dev in devices:
                dev.close()
        self.captured.emit(name)


QT_BUTTON_MAP = {
    QtCore.Qt.MouseButton.BackButton: "BTN_SIDE",      # button 4
    QtCore.Qt.MouseButton.ForwardButton: "BTN_EXTRA",  # button 5
    QtCore.Qt.MouseButton.MiddleButton: "BTN_MIDDLE",
    QtCore.Qt.MouseButton.TaskButton: "BTN_TASK",
    QtCore.Qt.MouseButton.ExtraButton4: "BTN_FORWARD",
    QtCore.Qt.MouseButton.ExtraButton5: "BTN_BACK",
}


class CaptureDialog(QtWidgets.QDialog):
    """Press the wanted mouse button over this dialog.

    Captures via Qt mouse events (works on Wayland without any permissions)
    and, in parallel, via evdev (works for buttons the compositor never
    delivers to apps). Whichever fires first wins.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Detect button")
        self.setModal(True)
        self.result_button = ""
        layout = QtWidgets.QVBoxLayout(self)
        label = QtWidgets.QLabel(
            "<b>Press the mouse button you want to use</b><br>"
            "with the cursor over this window.<br><br>"
            "<small>Left/right click are ignored. If the button is the "
            "current trigger, a quick tap works (it is replayed); with "
            "Present Windows tap mode on, pick it from the list "
            "instead.</small>")
        label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)
        cancel = QtWidgets.QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        layout.addWidget(cancel)
        self.setMinimumSize(340, 180)

        self.evdev_thread = ButtonCapture()
        self.evdev_thread.captured.connect(self._on_evdev)
        self.evdev_thread.start()
        QtCore.QTimer.singleShot(15000, self.reject)

    def mousePressEvent(self, event):
        name = QT_BUTTON_MAP.get(event.button())
        if name:
            self._finish(name)
        event.accept()

    def _on_evdev(self, name: str):
        if name and not self.result_button:
            self._finish(name)

    def _finish(self, name: str):
        self.result_button = name
        self.accept()

    def done(self, r):
        if self.evdev_thread.isRunning():
            self.evdev_thread.requestInterruption()
            self.evdev_thread.wait(2000)
        super().done(r)


class Window(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("deskflick")
        self.cfg = load_config()
        shortcuts = kwin_shortcut_names()

        layout = QtWidgets.QVBoxLayout(self)

        # --- Trigger ---------------------------------------------------
        trig_box = QtWidgets.QGroupBox("Trigger button")
        trig_form = QtWidgets.QFormLayout(trig_box)

        self.button_combo = QtWidgets.QComboBox()
        self.button_combo.setEditable(True)
        for code, label in BUTTON_CHOICES:
            self.button_combo.addItem(label, code)
        self._select_button(self.cfg["trigger"]["button"])

        self.detect_btn = QtWidgets.QPushButton("Detect…")
        self.detect_btn.setToolTip(
            "Opens a window — press the mouse button you want over it.")
        self.detect_btn.clicked.connect(self.start_capture)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.button_combo, 1)
        row.addWidget(self.detect_btn)
        trig_form.addRow("Button:", row)

        self.tap_passthrough = QtWidgets.QCheckBox(
            "A quick tap acts as a normal click (keeps “back” working)")
        self.tap_passthrough.setChecked(bool(self.cfg["trigger"]["tap_passthrough"]))
        trig_form.addRow(self.tap_passthrough)

        self.overview_check = QtWidgets.QCheckBox(
            "A quick tap shows all windows instead (Present Windows, Ctrl+F10)")
        self.overview_check.setChecked(bool(self.cfg["overview"]["enabled"]))
        self.overview_check.setToolTip(
            "When enabled, tapping the trigger button toggles KWin's\n"
            "“Present Windows (All desktops)” effect instead of clicking.\n"
            "Hold + flick still switches desktops.")
        trig_form.addRow(self.overview_check)

        self.overview_shortcut = QtWidgets.QComboBox()
        self.overview_shortcut.setEditable(True)
        self.overview_shortcut.addItems(shortcuts or ["ExposeAll", "Overview"])
        self.overview_shortcut.setCurrentText(str(self.cfg["overview"]["shortcut"]))
        self.overview_check.toggled.connect(self.overview_shortcut.setEnabled)
        self.overview_shortcut.setEnabled(self.overview_check.isChecked())
        trig_form.addRow("Tap shortcut:", self.overview_shortcut)
        layout.addWidget(trig_box)

        # --- Gesture ---------------------------------------------------
        gest_box = QtWidgets.QGroupBox("Gesture")
        gest_form = QtWidgets.QFormLayout(gest_box)

        self.threshold = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
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

        self.invert_x = QtWidgets.QCheckBox("Invert left/right")
        self.invert_x.setChecked(bool(self.cfg["gesture"]["invert_x"]))
        self.invert_y = QtWidgets.QCheckBox("Invert up/down")
        self.invert_y.setChecked(bool(self.cfg["gesture"]["invert_y"]))
        irow = QtWidgets.QHBoxLayout()
        irow.addWidget(self.invert_x)
        irow.addWidget(self.invert_y)
        gest_form.addRow(irow)
        layout.addWidget(gest_box)

        # --- Actions ---------------------------------------------------
        act_box = QtWidgets.QGroupBox("Flick actions (KWin shortcut or cmd:<command>)")
        act_form = QtWidgets.QFormLayout(act_box)
        self.action_combos = {}
        for direction, arrow in (("left", "←"), ("right", "→"),
                                 ("up", "↑"), ("down", "↓")):
            combo = QtWidgets.QComboBox()
            combo.setEditable(True)
            combo.addItems(shortcuts or [DEFAULTS["actions"][direction]])
            combo.setCurrentText(str(self.cfg["actions"][direction]))
            self.action_combos[direction] = combo
            act_form.addRow(f"{arrow} {direction.capitalize()}:", combo)
        layout.addWidget(act_box)

        # --- Footer ----------------------------------------------------
        self.status = QtWidgets.QLabel()
        self.refresh_status()
        save_btn = QtWidgets.QPushButton("Save && restart service")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self.save)
        frow = QtWidgets.QHBoxLayout()
        frow.addWidget(self.status, 1)
        frow.addWidget(save_btn)
        layout.addLayout(frow)

    # ------------------------------------------------------------------
    def _select_button(self, value):
        idx = self.button_combo.findData(str(value))
        if idx >= 0:
            self.button_combo.setCurrentIndex(idx)
        else:
            self.button_combo.setCurrentText(str(value))

    def current_button(self) -> str:
        data = self.button_combo.currentData()
        if data and self.button_combo.currentText() == dict(BUTTON_CHOICES).get(data, ""):
            return data
        text = self.button_combo.currentText().strip()
        for code, label in BUTTON_CHOICES:
            if text == label:
                return code
        return text

    def start_capture(self):
        dlg = CaptureDialog(self)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted and dlg.result_button:
            self._select_button(dlg.result_button)

    def service_active(self) -> bool:
        r = subprocess.run(["systemctl", "--user", "is-active", "deskflick"],
                           capture_output=True, text=True)
        return r.stdout.strip() == "active"

    def refresh_status(self):
        if self.service_active():
            self.status.setText("Service: <b style='color:#27ae60'>running</b>")
        else:
            self.status.setText("Service: <b style='color:#c0392b'>stopped</b>")

    def save(self):
        self.cfg["trigger"]["button"] = self.current_button()
        self.cfg["trigger"]["tap_passthrough"] = self.tap_passthrough.isChecked()
        self.cfg["gesture"]["threshold"] = self.threshold.value()
        self.cfg["gesture"]["repeat"] = self.repeat.isChecked()
        self.cfg["gesture"]["lock_pointer"] = self.lock_pointer.isChecked()
        self.cfg["gesture"]["invert_x"] = self.invert_x.isChecked()
        self.cfg["gesture"]["invert_y"] = self.invert_y.isChecked()
        for direction, combo in self.action_combos.items():
            self.cfg["actions"][direction] = combo.currentText().strip()
        self.cfg["overview"]["enabled"] = self.overview_check.isChecked()
        self.cfg["overview"]["shortcut"] = self.overview_shortcut.currentText().strip()

        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            f.write(dump_config(self.cfg))

        subprocess.run(["systemctl", "--user", "restart", "deskflick"],
                       capture_output=True)
        QtCore.QTimer.singleShot(600, self.refresh_status)


def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("deskflick")
    app.setDesktopFileName("deskflick")
    win = Window()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
