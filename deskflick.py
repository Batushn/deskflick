#!/usr/bin/env python3
"""deskflick — flick your mouse to glide between KDE Plasma virtual desktops.

Hold a mouse button (default: BTN_SIDE, a.k.a. "back" / button 4) and push the
mouse left/right/up/down to switch virtual desktops. A plain tap of the button
still works as a normal click (passthrough), so you don't lose "back" in your
browser.

Works on Wayland and X11: it reads evdev devices directly and re-emits events
through a uinput clone, so the compositor never sees the trigger button while
a gesture is in progress. Desktop switching is done via KGlobalAccel D-Bus
(KWin's own shortcuts), so KWin's animations and OSD work as usual.
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

DEFAULT_CONFIG = {
    "trigger": {
        "button": "BTN_SIDE",
        "tap_passthrough": True,
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
        "enabled": False,
        "shortcut": "ExposeAll",
    },
}


def log(*args):
    print(*args, flush=True)


class Config:
    def __init__(self, data: dict):
        def get(section, key):
            return data.get(section, {}).get(key, DEFAULT_CONFIG[section][key])

        btn = get("trigger", "button")
        if isinstance(btn, int):
            self.button = btn
        else:
            code = ecodes.ecodes.get(str(btn).upper())
            if code is None:
                sys.exit(f"deskflick: unknown button name in config: {btn!r}")
            self.button = code
        self.tap_passthrough = bool(get("trigger", "tap_passthrough"))
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

        self.overview_enabled = bool(get("overview", "enabled"))
        self.overview_shortcut = str(get("overview", "shortcut"))

    @classmethod
    def load(cls, path: str) -> "Config":
        if os.path.exists(path):
            with open(path, "rb") as f:
                return cls(tomllib.load(f))
        return cls({})


async def run_action(action: str, verbose: bool):
    """Run a gesture action: a KGlobalAccel shortcut name, or `cmd:<shell>`."""
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


class DeviceWorker:
    """Grabs one pointer device and proxies its events through a uinput clone."""

    def __init__(self, dev: InputDevice, cfg: Config, verbose: bool):
        self.dev = dev
        self.cfg = cfg
        self.verbose = verbose
        self.ui = UInput.from_device(dev, name=dev.name + VIRTUAL_SUFFIX)
        self.held = False
        self.gestured = False
        self.press_time = 0.0
        self.last_fire = 0.0
        self.acc_x = 0
        self.acc_y = 0

    async def run(self):
        dev, ui, cfg = self.dev, self.ui, self.cfg
        dev.grab()
        try:
            async for ev in dev.async_read_loop():
                if ev.type == ecodes.EV_KEY and ev.code == cfg.button:
                    await self._on_trigger(ev.value)
                    continue
                # safety: never stay in gesture mode longer than 10s, so a
                # missed release event can't freeze the pointer forever
                if self.held and time.monotonic() - self.press_time > 10:
                    self.held = False
                if self.held and ev.type == ecodes.EV_REL:
                    if ev.code == ecodes.REL_X:
                        self.acc_x += ev.value
                        await self._maybe_fire()
                        if cfg.lock_pointer:
                            continue
                    elif ev.code == ecodes.REL_Y:
                        self.acc_y += ev.value
                        await self._maybe_fire()
                        if cfg.lock_pointer:
                            continue
                ui.write_event(ev)
        finally:
            try:
                dev.ungrab()
            except OSError:
                pass
            ui.close()
            dev.close()

    async def _on_trigger(self, value: int):
        if value == 1:  # press: swallow, start tracking
            self.held = True
            self.gestured = False
            self.acc_x = self.acc_y = 0
            self.press_time = time.monotonic()
        elif value == 0 and self.held:  # release
            self.held = False
            tap = (
                not self.gestured
                and (self.cfg.tap_passthrough or self.cfg.overview_enabled)
                and time.monotonic() - self.press_time <= self.cfg.tap_timeout
            )
            if tap:
                if self.cfg.overview_enabled:
                    # tap shows/hides all windows instead of clicking
                    asyncio.ensure_future(
                        run_action(self.cfg.overview_shortcut, self.verbose))
                else:  # replay the click so "back" etc. still works
                    ui = self.ui
                    ui.write(ecodes.EV_KEY, self.cfg.button, 1)
                    ui.syn()
                    ui.write(ecodes.EV_KEY, self.cfg.button, 0)
                    ui.syn()
        # value == 2 (autorepeat): swallow

    async def _maybe_fire(self):
        cfg = self.cfg
        now = time.monotonic()
        if now - self.last_fire < cfg.cooldown:
            return
        if self.gestured and not cfg.repeat:
            return

        x, y = self.acc_x, self.acc_y
        if cfg.invert_x:
            x = -x
        if cfg.invert_y:
            y = -y

        direction = None
        if abs(x) >= cfg.threshold and abs(x) >= abs(y):
            direction = "right" if x > 0 else "left"
        elif abs(y) >= cfg.threshold:
            direction = "down" if y > 0 else "up"
        if direction is None:
            return

        self.gestured = True
        self.last_fire = now
        self.acc_x = self.acc_y = 0
        if self.verbose:
            log(f"[{self.dev.name}] flick {direction}")
        asyncio.ensure_future(run_action(cfg.actions[direction], self.verbose))


def find_pointer_devices(cfg: Config):
    """Devices that have relative X motion and the trigger button."""
    found = []
    for path in list_devices():
        try:
            dev = InputDevice(path)
        except OSError:
            continue
        if VIRTUAL_SUFFIX in dev.name:
            dev.close()
            continue
        caps = dev.capabilities()
        rel = caps.get(ecodes.EV_REL, [])
        keys = caps.get(ecodes.EV_KEY, [])
        if ecodes.REL_X in rel and cfg.button in keys:
            found.append(dev)
        else:
            dev.close()
    return found


async def main_loop(cfg: Config, verbose: bool):
    workers: dict[str, asyncio.Task] = {}

    while True:
        active = {p for p, t in workers.items() if not t.done()}
        for dev in find_pointer_devices(cfg):
            if dev.path in active:
                dev.close()
                continue
            log(f"deskflick: attached to {dev.path} ({dev.name})")
            worker = DeviceWorker(dev, cfg, verbose)
            workers[dev.path] = asyncio.ensure_future(worker.run())
        for path, task in list(workers.items()):
            if task.done():
                exc = task.exception()
                if exc and not isinstance(exc, OSError):
                    log(f"deskflick: worker for {path} died: {exc!r}")
                del workers[path]
        await asyncio.sleep(2)


def cmd_list_devices():
    paths = list_devices()
    if not paths:
        sys.exit(
            "deskflick: no readable input devices.\n"
            "Are you in the `input` group? (sudo usermod -aG input $USER, then re-login)"
        )
    for path in paths:
        try:
            dev = InputDevice(path)
        except OSError as e:
            print(f"{path}: <no access: {e}>")
            continue
        caps = dev.capabilities()
        keys = caps.get(ecodes.EV_KEY, [])
        extras = [
            n for n in ("BTN_SIDE", "BTN_EXTRA", "BTN_FORWARD", "BTN_BACK", "BTN_TASK")
            if ecodes.ecodes[n] in keys
        ]
        pointer = ecodes.EV_REL in caps
        tag = " [pointer]" if pointer else ""
        btns = f" side-buttons: {', '.join(extras)}" if extras else ""
        print(f"{path}: {dev.name}{tag}{btns}")
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
    args = parser.parse_args()

    if args.list_devices:
        cmd_list_devices()
        return

    cfg = Config.load(args.config)
    devices = find_pointer_devices(cfg)
    if not devices:
        sys.exit(
            "deskflick: no pointer device with the trigger button found.\n"
            "Check permissions (input group) or your [trigger] button setting.\n"
            "Try: deskflick --list-devices"
        )
    for d in devices:
        d.close()

    loop = asyncio.new_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, loop.stop)
    log(f"deskflick: trigger={ecodes.KEY.get(cfg.button) or ecodes.BTN.get(cfg.button, cfg.button)}"
        f" threshold={cfg.threshold}px")
    loop.create_task(main_loop(cfg, args.verbose))
    try:
        loop.run_forever()
    finally:
        # cancel workers so their finally blocks ungrab devices cleanly
        tasks = asyncio.all_tasks(loop)
        for t in tasks:
            t.cancel()
        if tasks:
            loop.run_until_complete(
                asyncio.gather(*tasks, return_exceptions=True))
        loop.close()


if __name__ == "__main__":
    main()
