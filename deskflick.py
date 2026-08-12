#!/usr/bin/env python3
"""deskflick — flick your mouse to glide between KDE Plasma virtual desktops.

Hold a mouse button (default: BTN_SIDE, a.k.a. "back" / button 4) and push the
mouse left/right/up/down to switch virtual desktops. A plain tap of the button
does whatever you configured: replay the original click, show all windows, or
nothing.

Works on Wayland and X11: it reads evdev devices directly and re-emits events
through uinput clones, so the compositor never sees the trigger while a
gesture is in progress. Desktop switching goes through KGlobalAccel D-Bus
(KWin's own shortcuts), so KWin's animations and OSD work as usual.

Gaming mice often split their buttons across several HID interfaces: the
pointer on one, macro keys on another. deskflick therefore grabs *every*
interface of the mouse it needs -- the trigger may live on a different device
node than the motion.
"""

import argparse
import asyncio
import os
import signal
import sys
import time
import tomllib

from evdev import InputDevice, UInput, ecodes, list_devices

VIRTUAL_SUFFIX = " [deskflick]"

RESCUE_BUTTONS = ("BTN_LEFT", "BTN_RIGHT", "BTN_MIDDLE", "BTN_SIDE",
                  "BTN_EXTRA", "BTN_FORWARD", "BTN_BACK", "BTN_TASK")

TAP_MODES = ("passthrough", "overview", "none")

DEFAULT_CONFIG = {
    "trigger": {
        "button": "BTN_SIDE",
        "tap": "passthrough",
        "tap_timeout_ms": 350,
    },
    "gesture": {
        "threshold": 150,
        "repeat": True,
        "cooldown_ms": 180,
        "lock_pointer": True,
        "invert_x": False,
        "invert_y": False,
    },
    "actions": {
        "left": "Switch One Desktop to the Left",
        "right": "Switch One Desktop to the Right",
        "up": "Switch One Desktop Up",
        "down": "Switch One Desktop Down",
    },
    "overview": {
        "shortcut": "ExposeAll",
    },
}


def log(*args):
    print(*args, flush=True)


def key_name(code: int) -> str:
    name = ecodes.bytype[ecodes.EV_KEY].get(code, code)
    if isinstance(name, (list, tuple)):
        for n in name:
            if n.startswith("BTN_"):
                return n
        return name[0]
    return str(name)


def key_code(name) -> int | None:
    if isinstance(name, int):
        return name
    return ecodes.ecodes.get(str(name).strip().upper())


def phys_base(dev: InputDevice) -> str:
    """USB path shared by every interface of one physical device."""
    return (dev.phys or "").split("/")[0]


# ---------------------------------------------------------------- config


class Config:
    def __init__(self, data: dict):
        def get(section, key):
            return data.get(section, {}).get(key, DEFAULT_CONFIG[section][key])

        name = get("trigger", "button")
        code = key_code(name)
        if code is None:
            sys.exit(f"deskflick: unknown trigger button/key: {name!r}")
        self.button = code
        self.button_name = key_name(code)
        self.trigger_is_button = self.button_name.startswith("BTN_")

        tap = str(get("trigger", "tap")).lower()
        # legacy keys from <= 0.3.0
        if "tap" not in data.get("trigger", {}):
            if data.get("overview", {}).get("enabled"):
                tap = "overview"
            elif data.get("trigger", {}).get("tap_passthrough") is False:
                tap = "none"
            else:
                tap = "passthrough"
        self.tap = tap if tap in TAP_MODES else "passthrough"
        self.tap_timeout = float(get("trigger", "tap_timeout_ms")) / 1000.0

        self.threshold = max(10, int(get("gesture", "threshold")))
        self.repeat = bool(get("gesture", "repeat"))
        self.cooldown = float(get("gesture", "cooldown_ms")) / 1000.0
        self.lock_pointer = bool(get("gesture", "lock_pointer"))
        self.invert_x = bool(get("gesture", "invert_x"))
        self.invert_y = bool(get("gesture", "invert_y"))

        self.actions = {
            d: data.get("actions", {}).get(d, DEFAULT_CONFIG["actions"][d])
            for d in ("left", "right", "up", "down")
        }
        self.overview_shortcut = str(get("overview", "shortcut"))

    @classmethod
    def load(cls, path: str) -> "Config":
        if os.path.exists(path):
            with open(path, "rb") as f:
                return cls(tomllib.load(f))
        return cls({})


# ---------------------------------------------------------------- actions


async def run_action(action: str, verbose: bool):
    """Run an action: a KGlobalAccel shortcut name, or `cmd:<shell>`."""
    if verbose:
        log(f"  -> {action}")
    if action.startswith("cmd:"):
        proc = await asyncio.create_subprocess_shell(action[4:])
    else:
        proc = await asyncio.create_subprocess_exec(
            "gdbus", "call", "--session",
            "--dest", "org.kde.kglobalaccel",
            "--object-path", "/component/kwin",
            "--method", "org.kde.kglobalaccel.Component.invokeShortcut",
            action,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    await proc.wait()


# ---------------------------------------------------------------- state


class Gesture:
    """Gesture state shared by every device of one physical mouse."""

    def __init__(self, cfg: Config, verbose: bool):
        self.cfg = cfg
        self.verbose = verbose
        self.held = False
        self.fired = False
        self.press_time = 0.0
        self.last_fire = 0.0
        self.acc_x = 0
        self.acc_y = 0
        # key events swallowed on a macro/keyboard interface during a hold,
        # replayed verbatim if the hold turns out to be a tap
        self.swallowed: list[tuple[int, int]] = []

    def press(self):
        self.held = True
        self.fired = False
        self.acc_x = self.acc_y = 0
        self.swallowed.clear()
        self.press_time = time.monotonic()

    def release(self) -> bool:
        """End a hold. Returns True if it counts as a tap."""
        was_held, self.held = self.held, False
        return (was_held and not self.fired
                and time.monotonic() - self.press_time <= self.cfg.tap_timeout)

    def expire(self):
        """Safety valve: a release we never saw must not freeze the pointer."""
        if self.held and time.monotonic() - self.press_time > 10:
            self.held = False
            self.swallowed.clear()

    def add_motion(self, code: int, value: int) -> str | None:
        cfg = self.cfg
        if code == ecodes.REL_X:
            self.acc_x += value
        elif code == ecodes.REL_Y:
            self.acc_y += value
        else:
            return None

        now = time.monotonic()
        if now - self.last_fire < cfg.cooldown:
            return None
        if self.fired and not cfg.repeat:
            return None

        x = -self.acc_x if cfg.invert_x else self.acc_x
        y = -self.acc_y if cfg.invert_y else self.acc_y
        if abs(x) >= cfg.threshold and abs(x) >= abs(y):
            direction = "right" if x > 0 else "left"
        elif abs(y) >= cfg.threshold:
            direction = "down" if y > 0 else "up"
        else:
            return None

        self.fired = True
        self.last_fire = now
        self.acc_x = self.acc_y = 0
        return direction


# ---------------------------------------------------------------- worker


class DeviceWorker:
    """Grabs one device and proxies its events through a uinput clone."""

    def __init__(self, dev: InputDevice, cfg: Config, gesture: Gesture,
                 is_pointer: bool, verbose: bool):
        self.dev = dev
        self.cfg = cfg
        self.g = gesture
        self.is_pointer = is_pointer
        self.verbose = verbose
        self.ui = UInput.from_device(dev, name=dev.name + VIRTUAL_SUFFIX)
        self.forwarded: set[int] = set()

    async def run(self):
        dev, ui, cfg, g = self.dev, self.ui, self.cfg, self.g
        # Never grab mid-click: the compositor would have seen the press on the
        # real device and the release on our clone, leaving it stuck forever.
        await self._wait_until_idle()
        dev.grab()
        try:
            async for ev in dev.async_read_loop():
                g.expire()

                if ev.type == ecodes.EV_KEY and ev.code == cfg.button:
                    await self._on_trigger(ev.value)
                    continue

                # On a macro/keyboard interface, suppress the rest of the
                # button's key burst while gesturing (a mouse whose side
                # button sends e.g. Ctrl+Tab would otherwise still switch
                # tabs). Pointer interfaces keep working normally so that
                # clicking during a gesture is never swallowed.
                if (g.held and not self.is_pointer
                        and ev.type == ecodes.EV_KEY and ev.value in (0, 1)):
                    g.swallowed.append((ev.code, ev.value))
                    continue

                if g.held and self.is_pointer and ev.type == ecodes.EV_REL:
                    direction = g.add_motion(ev.code, ev.value)
                    if direction:
                        if self.verbose:
                            log(f"[{dev.name}] flick {direction}")
                        asyncio.ensure_future(
                            run_action(cfg.actions[direction], self.verbose))
                    if ev.code in (ecodes.REL_X, ecodes.REL_Y) and cfg.lock_pointer:
                        continue

                if ev.type == ecodes.EV_KEY:
                    if ev.value == 1:
                        self.forwarded.add(ev.code)
                    elif ev.value == 0:
                        self.forwarded.discard(ev.code)
                ui.write_event(ev)
        finally:
            self.shutdown()

    async def _on_trigger(self, value: int):
        g, cfg = self.g, self.cfg
        if value == 1:
            g.press()
            return
        if value == 2:  # autorepeat
            return
        if not g.release():
            g.swallowed.clear()
            return

        if cfg.tap == "overview":
            asyncio.ensure_future(
                run_action(cfg.overview_shortcut, self.verbose))
        elif cfg.tap == "passthrough":
            self._replay_tap()
        g.swallowed.clear()

    def _replay_tap(self):
        """Re-send the button's original action through the clone."""
        ui = self.ui
        ui.write(ecodes.EV_KEY, self.cfg.button, 1)
        ui.syn()
        for code, value in self.g.swallowed:
            ui.write(ecodes.EV_KEY, code, value)
            ui.syn()
        ui.write(ecodes.EV_KEY, self.cfg.button, 0)
        ui.syn()

    async def _wait_until_idle(self, timeout: float = 5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if not self.dev.active_keys():
                    return
            except OSError:
                return
            await asyncio.sleep(0.05)

    def shutdown(self):
        """Release everything still held, then hand the device back.

        Releases must reach the compositor through the clone *before* it
        disappears, or a button pressed at shutdown stays stuck.
        """
        try:
            held = set(self.dev.active_keys())
        except OSError:
            held = set()
        try:
            for code in sorted(self.forwarded | held):
                self.ui.write(ecodes.EV_KEY, code, 0)
            if self.forwarded or held:
                self.ui.syn()
                time.sleep(0.05)
        except OSError:
            pass
        self.forwarded.clear()
        for close in (self.dev.ungrab, self.ui.close, self.dev.close):
            try:
                close()
            except OSError:
                pass


# ---------------------------------------------------------------- devices


def is_pointer(dev: InputDevice) -> bool:
    caps = dev.capabilities()
    return ecodes.REL_X in caps.get(ecodes.EV_REL, [])


def find_devices(cfg: Config):
    """Devices to grab: pointers, plus whatever carries the trigger key.

    A KEY_* trigger is only honoured on interfaces belonging to the same
    physical device as a pointer, so binding e.g. KEY_TAB (sent by a mouse
    macro button) never swallows Tab from your actual keyboard.
    """
    opened = []
    for path in list_devices():
        try:
            dev = InputDevice(path)
        except OSError:
            continue
        if VIRTUAL_SUFFIX in dev.name:
            dev.close()
            continue
        opened.append(dev)

    pointer_bases = {phys_base(d) for d in opened if is_pointer(d)}

    triggers = []
    for dev in opened:
        if cfg.button not in dev.capabilities().get(ecodes.EV_KEY, []):
            continue
        # A KEY_* trigger is only trusted on hardware that also presents a
        # pointer -- i.e. the mouse itself, never a real keyboard.
        if not cfg.trigger_is_button and phys_base(dev) not in pointer_bases:
            continue
        triggers.append(dev)

    trigger_paths = {d.path for d in triggers}
    trigger_bases = {phys_base(d) for d in triggers}

    # Take motion from the pointer interfaces of the same physical mouse. With
    # no trigger device present we grab nothing at all: an idle deskflick must
    # never sit between you and your mouse.
    selected = {}
    for dev in opened:
        trigger = dev.path in trigger_paths
        pointer = bool(triggers) and is_pointer(dev) and \
            phys_base(dev) in trigger_bases
        if trigger or pointer:
            selected[dev.path] = (dev, pointer, trigger)
        else:
            try:
                dev.close()
            except OSError:
                pass
    return selected


def unstick(quiet: bool = False) -> bool:
    """Clear mouse buttons the compositor may still think are pressed.

    A release with no matching press is a no-op, so this is always safe; if a
    button *was* stuck (unclean shutdown mid-click), this frees it.
    """
    codes = [ecodes.ecodes[n] for n in RESCUE_BUTTONS]
    try:
        ui = UInput({ecodes.EV_KEY: codes,
                     ecodes.EV_REL: [ecodes.REL_X, ecodes.REL_Y]},
                    name="deskflick-unstick")
    except OSError as e:
        if not quiet:
            log(f"deskflick: cannot open /dev/uinput ({e})")
        return False
    try:
        time.sleep(0.8)  # let the compositor notice the new device
        for code in codes:
            ui.write(ecodes.EV_KEY, code, 0)
            ui.syn()
            time.sleep(0.02)
        time.sleep(0.3)
    finally:
        ui.close()
    if not quiet:
        log("deskflick: released all mouse buttons")
    return True


def any_button_held() -> bool:
    for path in list_devices():
        try:
            dev = InputDevice(path)
        except OSError:
            continue
        try:
            if ecodes.BTN_LEFT in dev.capabilities().get(ecodes.EV_KEY, []) \
                    and dev.active_keys():
                return True
        except OSError:
            pass
        finally:
            dev.close()
    return False


# ---------------------------------------------------------------- main loop


async def main_loop(cfg: Config, verbose: bool):
    gesture = Gesture(cfg, verbose)
    workers: dict[str, asyncio.Task] = {}
    warned = False

    while True:
        found = find_devices(cfg)
        has_trigger_dev = any(t for _, _, t in found.values())

        if not has_trigger_dev and not warned:
            warned = True
            log(f"deskflick: no readable device reports {cfg.button_name}. "
                f"Run `deskflick --list-devices`; if your mouse is missing, "
                f"its permissions are not set up (re-run install.sh).")
        elif has_trigger_dev:
            warned = False

        for path, (dev, pointer, trigger) in found.items():
            if path in workers and not workers[path].done():
                dev.close()
                continue
            roles = ", ".join(r for r, on in
                              (("motion", pointer), ("trigger", trigger)) if on)
            log(f"deskflick: attached to {path} ({dev.name}) [{roles}]")
            worker = DeviceWorker(dev, cfg, gesture, pointer, verbose)
            workers[path] = asyncio.ensure_future(worker.run())

        for path, task in list(workers.items()):
            if task.done():
                exc = task.exception()
                if exc and not isinstance(exc, (OSError, asyncio.CancelledError)):
                    log(f"deskflick: worker for {path} died: {exc!r}")
                del workers[path]
        await asyncio.sleep(2)


# ---------------------------------------------------------------- cli


def cmd_list_devices(cfg: Config | None = None):
    paths = list_devices()
    if not paths:
        sys.exit("deskflick: no readable input devices — re-run install.sh")
    trigger = cfg.button if cfg else None
    for path in sorted(paths, key=lambda p: int("".join(filter(str.isdigit, p)))):
        try:
            dev = InputDevice(path)
        except OSError as e:
            print(f"{path}: <no access: {e}>")
            continue
        caps = dev.capabilities()
        keys = caps.get(ecodes.EV_KEY, [])
        buttons = [key_name(k) for k in keys if key_name(k).startswith("BTN_")]
        tags = []
        if is_pointer(dev):
            tags.append("pointer")
        if trigger is not None and trigger in keys:
            tags.append("HAS TRIGGER")
        tag = f" [{', '.join(tags)}]" if tags else ""
        print(f"{path}: {dev.name}{tag}")
        if buttons:
            print(f"    buttons: {', '.join(buttons)}")
        dev.close()


def cmd_watch():
    """Print button/key names as they are pressed, to find a trigger by hand."""
    import select
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
        sys.exit("deskflick: no readable input devices — re-run install.sh")
    print("Press buttons (Ctrl+C to stop). Names printed here are exactly "
          "what belongs in the config's `button =`.")
    try:
        while True:
            for dev in select.select(devices, [], [])[0]:
                try:
                    for ev in dev.read():
                        if ev.type == ecodes.EV_KEY and ev.value == 1:
                            print(f"{key_name(ev.code):<14} {dev.name}")
                except OSError:
                    pass
    except KeyboardInterrupt:
        print()
    finally:
        for dev in devices:
            dev.close()


def main():
    parser = argparse.ArgumentParser(
        prog="deskflick",
        description="Hold a mouse button and flick to switch KDE virtual desktops.",
    )
    parser.add_argument(
        "-c", "--config",
        default=os.path.expanduser("~/.config/deskflick/config.toml"),
        help="path to config file (default: %(default)s)",
    )
    parser.add_argument("-l", "--list-devices", action="store_true",
                        help="list input devices and exit")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="log every gesture")
    parser.add_argument("-w", "--watch", action="store_true",
                        help="print button names as you press them, then exit")
    parser.add_argument("--unstick", action="store_true",
                        help="release all mouse buttons and exit "
                             "(rescue command if a click ever gets stuck)")
    args = parser.parse_args()

    if args.unstick:
        sys.exit(0 if unstick() else 1)

    if args.watch:
        cmd_watch()
        return

    cfg = Config.load(args.config)

    if args.list_devices:
        cmd_list_devices(cfg)
        return

    if not any_button_held():
        unstick(quiet=True)

    loop = asyncio.new_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, loop.stop)
    log(f"deskflick: trigger={cfg.button_name} threshold={cfg.threshold}px "
        f"tap={cfg.tap}")
    loop.create_task(main_loop(cfg, args.verbose))
    try:
        loop.run_forever()
    finally:
        tasks = asyncio.all_tasks(loop)
        for t in tasks:
            t.cancel()
        if tasks:
            loop.run_until_complete(
                asyncio.gather(*tasks, return_exceptions=True))
        loop.close()
        if not any_button_held():
            unstick(quiet=True)


if __name__ == "__main__":
    main()
