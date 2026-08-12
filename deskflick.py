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

DIRECTIONS = ("left", "right", "up", "down",
              "up_left", "up_right", "down_left", "down_right")

RESCUE_BUTTONS = ("BTN_LEFT", "BTN_RIGHT", "BTN_MIDDLE", "BTN_SIDE",
                  "BTN_EXTRA", "BTN_FORWARD", "BTN_BACK", "BTN_TASK")

# Besides these keywords, every press action also accepts a KWin shortcut
# name or "cmd:<shell command>".
#   passthrough -- replay the button's own action    overview -- [overview].shortcut
#   none        -- do nothing

DEFAULT_CONFIG = {
    "trigger": {
        "button": "BTN_SIDE",
        "tap": "Show Desktop",
        "double": "Window Maximize",
        "hold": "overview",
        "tap_timeout_ms": 350,
        "double_ms": 250,
        "hold_ms": 200,
    },
    "gesture": {
        "threshold": 150,
        "repeat": True,
        "cooldown_ms": 180,
        # Recognise diagonal flicks. A flick counts as diagonal when the
        # smaller axis reaches this fraction of the larger one -- 0.5 gives a
        # roughly 27°-63° cone around each diagonal.
        "diagonals": True,
        "diagonal_ratio": 0.5,
        "lock_pointer": False,
        # Pushing the mouse feels like dragging the desktop under it, so the
        # view goes the other way. Flip either axis if you disagree.
        "invert_x": True,
        "invert_y": True,
    },
    "actions": {
        "left": "Switch One Desktop to the Left",
        "right": "Switch One Desktop to the Right",
        "up": "Switch One Desktop Up",
        "down": "Switch One Desktop Down",
        # Unbound by default: an unbound diagonal falls back to its dominant
        # axis, so switching desktops never stops working on a sloppy flick.
        "up_left": "none",
        "up_right": "none",
        "down_left": "none",
        "down_right": "none",
    },
    "overview": {
        "shortcut": "ExposeAll",
    },
    "modifier": {
        "enabled": True,
        "key": "KEY_LEFTMETA",
        "left": "Window Quick Tile Left",
        "right": "Window Quick Tile Right",
        "up": "Window Quick Tile Top",
        "down": "Window Quick Tile Bottom",
        "up_left": "Window Quick Tile Top Left",
        "up_right": "Window Quick Tile Top Right",
        "down_left": "Window Quick Tile Bottom Left",
        "down_right": "Window Quick Tile Bottom Right",
        # Snapping follows the hand rather than the desktop metaphor, so it
        # gets its own inversion instead of inheriting [gesture].
        "invert_x": False,
        "invert_y": False,
        # While the modifier is down the button belongs to window management
        # alone: no tap, double tap or long press.
        "suppress_press": True,
        "defuse_launcher": True,
    },
}

# Left/right halves of the same modifier are always watched together.
MODIFIER_SIBLINGS = {
    "KEY_LEFTMETA": "KEY_RIGHTMETA", "KEY_RIGHTMETA": "KEY_LEFTMETA",
    "KEY_LEFTCTRL": "KEY_RIGHTCTRL", "KEY_RIGHTCTRL": "KEY_LEFTCTRL",
    "KEY_LEFTALT": "KEY_RIGHTALT", "KEY_RIGHTALT": "KEY_LEFTALT",
    "KEY_LEFTSHIFT": "KEY_RIGHTSHIFT", "KEY_RIGHTSHIFT": "KEY_LEFTSHIFT",
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

        # Never case-fold these: KWin shortcut names are case-sensitive, and
        # lowercase "overview" (our keyword) must stay distinct from KWin's
        # own "Overview" shortcut.
        tap = str(get("trigger", "tap"))
        # Migrate <= 0.3.0 layout, but only when those keys are really there:
        # an absent [trigger] must fall through to the default, not to this.
        legacy = ("tap_passthrough" in data.get("trigger", {})
                  or "enabled" in data.get("overview", {}))
        if legacy and "tap" not in data.get("trigger", {}):
            if data.get("overview", {}).get("enabled"):
                tap = "overview"
            elif data.get("trigger", {}).get("tap_passthrough") is False:
                tap = "none"
            else:
                tap = "passthrough"
        self.tap = tap or "passthrough"
        self.double = str(get("trigger", "double")) or "none"
        self.hold = str(get("trigger", "hold")) or "none"
        self.tap_timeout = float(get("trigger", "tap_timeout_ms")) / 1000.0
        self.double_window = float(get("trigger", "double_ms")) / 1000.0
        self.hold_delay = float(get("trigger", "hold_ms")) / 1000.0

        self.threshold = max(10, int(get("gesture", "threshold")))
        self.repeat = bool(get("gesture", "repeat"))
        self.cooldown = float(get("gesture", "cooldown_ms")) / 1000.0
        self.lock_pointer = bool(get("gesture", "lock_pointer"))
        self.invert_x = bool(get("gesture", "invert_x"))
        self.invert_y = bool(get("gesture", "invert_y"))

        self.diagonals = bool(get("gesture", "diagonals"))
        self.diagonal_ratio = min(1.0, max(0.1, float(
            get("gesture", "diagonal_ratio"))))

        self.actions = {
            d: data.get("actions", {}).get(d, DEFAULT_CONFIG["actions"][d])
            for d in DIRECTIONS
        }
        self.overview_shortcut = str(get("overview", "shortcut"))

        self.mod_enabled = bool(get("modifier", "enabled"))
        mod_name = str(get("modifier", "key")).upper()
        mod_code = key_code(mod_name)
        if mod_code is None:
            sys.exit(f"deskflick: unknown modifier key: {mod_name!r}")
        self.mod_name = mod_name
        sibling = key_code(MODIFIER_SIBLINGS.get(mod_name, ""))
        self.mod_codes = {mod_code} | ({sibling} if sibling else set())
        self.mod_actions = {
            d: data.get("modifier", {}).get(d, DEFAULT_CONFIG["modifier"][d])
            for d in DIRECTIONS
        }
        self.mod_invert_x = bool(get("modifier", "invert_x"))
        self.mod_invert_y = bool(get("modifier", "invert_y"))
        self.mod_suppress = bool(get("modifier", "suppress_press"))
        self.mod_defuse = bool(get("modifier", "defuse_launcher"))

    def action_for(self, direction: str, with_mod: bool) -> str | None:
        table = self.mod_actions if with_mod else self.actions
        return self.resolve(table.get(direction, "none"))

    def resolve(self, action: str) -> str | None:
        """Config value -> shortcut/command, or None for 'do nothing'."""
        if not action or action == "none":
            return None
        if action == "overview":
            return self.overview_shortcut
        return action

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


# ---------------------------------------------------------------- modifier


class ModifierWatcher:
    """Tells whether a modifier is held, without grabbing the keyboard.

    Keyboards are opened read-only and only ever queried for the state of the
    one modifier deskflick was told to watch (EVIOCGKEY), never read as a
    stream. Nothing else about your typing is looked at, and nothing is opened
    at all unless [modifier] is enabled.
    """

    def __init__(self, cfg: Config):
        self.codes = cfg.mod_codes
        self.devices: list[InputDevice] = []
        self.readable = False

    def refresh(self):
        """Reopen the watched keyboards. Costs ~100ms — call it only when the
        set of device nodes actually changed, never on a timer."""
        for dev in self.devices:
            try:
                dev.close()
            except OSError:
                pass
        self.devices = []
        blocked = False
        for path in list_devices():
            try:
                dev = InputDevice(path)
            except OSError:
                blocked = True
                continue
            if VIRTUAL_SUFFIX in dev.name or "deskflick" in dev.name:
                dev.close()
                continue
            if self.codes & set(dev.capabilities().get(ecodes.EV_KEY, [])):
                self.devices.append(dev)
            else:
                dev.close()
        self.readable = bool(self.devices)
        return blocked

    def held(self) -> bool:
        for dev in self.devices:
            try:
                if self.codes & set(dev.active_keys()):
                    return True
            except OSError:
                continue
        return False

    def close(self):
        for dev in self.devices:
            try:
                dev.close()
            except OSError:
                pass
        self.devices = []


class LauncherDefuser:
    """Keeps a bare Meta press from opening the application launcher.

    KWin opens the launcher when Meta is released without anything else being
    pressed -- and deskflick swallows the very button that would otherwise
    count. Tapping Ctrl through a virtual keyboard marks the modifier as used;
    Ctrl on its own does nothing.
    """

    def __init__(self):
        self.ui = None
        try:
            self.ui = UInput({ecodes.EV_KEY: [ecodes.KEY_LEFTCTRL]},
                             name="deskflick-modifier")
        except OSError as e:
            log(f"deskflick: no launcher defuser ({e})")

    def tap(self):
        if not self.ui:
            return
        try:
            self.ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL, 1)
            self.ui.syn()
            self.ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL, 0)
            self.ui.syn()
        except OSError:
            pass

    def close(self):
        if self.ui:
            try:
                self.ui.close()
            except OSError:
                pass
            self.ui = None


# ---------------------------------------------------------------- state


class Gesture:
    """Gesture state shared by every device of one physical mouse."""

    def __init__(self, cfg: Config, verbose: bool):
        self.cfg = cfg
        self.verbose = verbose
        self.held = False
        self.consumed = False   # a flick or a long press already happened
        self.flicked = False    # used to gate repeats within one hold
        self.press_time = 0.0
        self.last_fire = 0.0
        self.acc_x = 0
        self.acc_y = 0
        # key events swallowed on a macro/keyboard interface during a hold,
        # replayed verbatim if the hold turns out to be a tap
        self.swallowed: list[tuple[int, int]] = []
        # pending single-tap (waiting to see if a double follows) and the
        # long-press countdown
        self.tap_task: asyncio.Task | None = None
        self.hold_task: asyncio.Task | None = None
        self.double_armed = False
        self.last_tap_time = 0.0

    def press(self):
        self.held = True
        self.consumed = False
        self.flicked = False
        self.acc_x = self.acc_y = 0
        self.swallowed.clear()
        self.press_time = time.monotonic()

    def release(self) -> bool:
        """End a hold. Returns True if it counts as a tap."""
        was_held, self.held = self.held, False
        return (was_held and not self.consumed
                and time.monotonic() - self.press_time <= self.cfg.tap_timeout)

    def expire(self):
        """Safety valve: a release we never saw must not freeze the pointer."""
        if self.held and time.monotonic() - self.press_time > 10:
            self.held = False
            self.swallowed.clear()
            self.cancel(self.hold_task)
            self.hold_task = None

    @staticmethod
    def cancel(task: asyncio.Task | None):
        if task and not task.done():
            task.cancel()

    def add_motion(self, code: int, value: int):
        """Accumulate one axis. Deciding happens in evaluate(), on SYN."""
        if code == ecodes.REL_X:
            self.acc_x += value
        elif code == ecodes.REL_Y:
            self.acc_y += value

    def evaluate(self, mod_held=None):
        """Returns (direction, with_modifier), or None if nothing fires yet.

        Called once per SYN_REPORT rather than per motion event: a mouse
        reports X and Y as separate events, so judging after each one compares
        a fresh axis against a stale one and reads diagonals as straight
        flicks.
        """
        cfg = self.cfg
        now = time.monotonic()
        if now - self.last_fire < cfg.cooldown:
            return None
        if self.flicked and not cfg.repeat:
            return None

        # Whether the threshold is crossed does not depend on inversion, so
        # settle that first and only then ask about the modifier -- that keeps
        # the keyboard query to once per flick instead of once per motion.
        ax, ay = abs(self.acc_x), abs(self.acc_y)
        if max(ax, ay) < cfg.threshold:
            return None

        with_mod = bool(mod_held and mod_held())
        invert_x = cfg.mod_invert_x if with_mod else cfg.invert_x
        invert_y = cfg.mod_invert_y if with_mod else cfg.invert_y
        x = -self.acc_x if invert_x else self.acc_x
        y = -self.acc_y if invert_y else self.acc_y

        horizontal = "right" if x > 0 else "left"
        vertical = "down" if y > 0 else "up"
        direction = None
        if cfg.diagonals and min(ax, ay) >= max(ax, ay) * cfg.diagonal_ratio:
            corner = f"{vertical}_{horizontal}"
            # An unbound diagonal falls back to its dominant axis, so a sloppy
            # flick still switches desktops instead of doing nothing.
            if cfg.action_for(corner, with_mod):
                direction = corner
        if direction is None:
            direction = horizontal if ax >= ay else vertical

        self.flicked = True
        self.consumed = True
        self.last_fire = now
        self.acc_x = self.acc_y = 0
        return direction, with_mod


# ---------------------------------------------------------------- worker


class DeviceWorker:
    """Grabs one device and proxies its events through a uinput clone."""

    def __init__(self, dev: InputDevice, cfg: Config, gesture: Gesture,
                 is_pointer: bool, verbose: bool,
                 mods: "ModifierWatcher | None" = None,
                 defuser: "LauncherDefuser | None" = None):
        self.dev = dev
        self.cfg = cfg
        self.g = gesture
        self.is_pointer = is_pointer
        self.verbose = verbose
        self.mods = mods
        self.defuser = defuser
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
                    g.add_motion(ev.code, ev.value)
                    if ev.code in (ecodes.REL_X, ecodes.REL_Y) and cfg.lock_pointer:
                        continue

                # One report carries X and Y as separate events; judge the
                # gesture only once both have landed.
                if (g.held and self.is_pointer and ev.type == ecodes.EV_SYN
                        and ev.code == ecodes.SYN_REPORT):
                    fired = g.evaluate(self._mod_held)
                    if fired:
                        self._flick(*fired)

                if ev.type == ecodes.EV_KEY:
                    if ev.value == 1:
                        self.forwarded.add(ev.code)
                    elif ev.value == 0:
                        self.forwarded.discard(ev.code)
                ui.write_event(ev)
        finally:
            self.shutdown()

    def _mod_held(self) -> bool:
        return bool(self.cfg.mod_enabled and self.mods and self.mods.held())

    def _flick(self, direction: str, with_mod: bool):
        """Run the flick action, snapping the window if the modifier is held."""
        cfg = self.cfg
        action = cfg.action_for(direction, with_mod)
        if self.verbose:
            log(f"[{self.dev.name}] flick {direction}"
                f"{' + ' + cfg.mod_name if with_mod else ''}"
                f"{'' if action else ' (unbound)'}")
        if not action:
            return
        asyncio.ensure_future(run_action(action, self.verbose))
        if with_mod and cfg.mod_defuse and self.defuser:
            self.defuser.tap()

    async def _on_trigger(self, value: int):
        g, cfg = self.g, self.cfg
        now = time.monotonic()

        if value == 1:
            # A press landing inside the double-tap window turns the pending
            # single tap into a double.
            if (cfg.double != "none" and g.tap_task and not g.tap_task.done()
                    and now - g.last_tap_time <= cfg.double_window):
                g.cancel(g.tap_task)
                g.tap_task = None
                g.double_armed = True
            g.press()
            if cfg.hold != "none":
                g.hold_task = asyncio.ensure_future(self._hold_countdown())
            return

        if value == 2:  # autorepeat
            return

        g.cancel(g.hold_task)
        g.hold_task = None
        tap = g.release()
        swallowed, g.swallowed = list(g.swallowed), []
        if not tap:
            g.double_armed = False
            return

        # With the modifier down the button is for window management only.
        if cfg.mod_suppress and self._mod_held():
            g.double_armed = False
            g.cancel(g.tap_task)
            g.tap_task = None
            if self.verbose:
                log(f"[{self.dev.name}] tap ignored ({cfg.mod_name} held)")
            return

        if g.double_armed:
            g.double_armed = False
            if self.verbose:
                log(f"[{self.dev.name}] double tap")
            await self._do_action(cfg.double, swallowed, repeat=2)
        elif cfg.double == "none":
            await self._do_action(cfg.tap, swallowed)
        else:
            # Wait out the double-tap window before committing to a single.
            g.last_tap_time = now
            g.tap_task = asyncio.ensure_future(self._delayed_tap(swallowed))

    async def _hold_countdown(self):
        """Fire the long-press action if the button stays down and still."""
        try:
            await asyncio.sleep(self.cfg.hold_delay)
        except asyncio.CancelledError:
            return
        g = self.g
        if not g.held or g.consumed:
            return
        if self.cfg.mod_suppress and self._mod_held():
            g.swallowed.clear()
            return
        g.consumed = True
        g.swallowed.clear()
        if self.verbose:
            log(f"[{self.dev.name}] long press")
        await self._do_action(self.cfg.hold, [])

    async def _delayed_tap(self, swallowed):
        try:
            await asyncio.sleep(self.cfg.double_window)
        except asyncio.CancelledError:
            return
        await self._do_action(self.cfg.tap, swallowed)

    async def _do_action(self, action: str, swallowed, repeat: int = 1):
        if action == "passthrough":
            for i in range(repeat):
                if i:
                    await asyncio.sleep(0.06)
                self._replay(swallowed)
            return
        target = self.cfg.resolve(action)
        if target:
            await run_action(target, self.verbose)

    def _replay(self, swallowed):
        """Re-send the button's original action through the clone."""
        ui = self.ui
        ui.write(ecodes.EV_KEY, self.cfg.button, 1)
        ui.syn()
        for code, value in swallowed:
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

    mods = defuser = None
    mod_warned = False
    if cfg.mod_enabled:
        mods = ModifierWatcher(cfg)
        defuser = LauncherDefuser() if cfg.mod_defuse else None

    known_paths: frozenset[str] = frozenset()
    has_trigger_dev = False
    idle_rounds = 0
    # A device we cannot keep (already grabbed by another process, say) would
    # otherwise die and be retried on every pass, and each retry pays for a
    # full rescan. Back off instead.
    failures: dict[str, int] = {}
    retry_at: dict[str, float] = {}
    started_at: dict[str, float] = {}

    while True:
        # Enumerating devices and reading their capabilities takes ~100ms, and
        # this process is the only thing between the mouse and the compositor
        # while it holds a grab -- so a periodic rescan would freeze the
        # pointer on a timer. Detect changes with a cheap listing instead, and
        # do the expensive part off the event loop.
        now = time.monotonic()
        paths = frozenset(list_devices())
        for path, task in list(workers.items()):
            if not task.done():
                continue
            del workers[path]
            exc = task.exception()
            if isinstance(exc, asyncio.CancelledError):
                continue
            if time.monotonic() - started_at.get(path, now) > 10:
                failures.pop(path, None)  # it worked for a while; forgive it
            count = failures[path] = failures.get(path, 0) + 1
            retry_at[path] = now + min(60.0, 2.0 ** min(count, 6))
            if count == 1 and exc is not None:
                log(f"deskflick: lost {path}: {exc}")

        rescan = paths != known_paths or any(
            t <= now for p, t in retry_at.items()
            if p in paths and p not in workers)
        if not has_trigger_dev:
            # Permissions can arrive without any device appearing (a udev ACL
            # being applied), so keep looking, just not every two seconds.
            idle_rounds += 1
            rescan = rescan or idle_rounds % 5 == 0

        if rescan:
            known_paths = paths
            found = await asyncio.to_thread(find_devices, cfg)
            has_trigger_dev = any(t for _, _, t in found.values())
            if has_trigger_dev:
                idle_rounds = 0

            if not has_trigger_dev and not warned:
                warned = True
                log(f"deskflick: no readable device reports {cfg.button_name}. "
                    f"Run `deskflick --list-devices`; if your mouse is missing, "
                    f"its permissions are not set up (re-run install.sh).")
            elif has_trigger_dev:
                warned = False

            for path, (dev, pointer, trigger) in found.items():
                if path in workers or retry_at.get(path, 0) > time.monotonic():
                    dev.close()
                    continue
                roles = ", ".join(r for r, on in
                                  (("motion", pointer), ("trigger", trigger)) if on)
                log(f"deskflick: attached to {path} ({dev.name}) [{roles}]")
                # Creating the uinput clone waits for its device node, which
                # takes up to a second -- off the loop, or the pointer stalls.
                worker = await asyncio.to_thread(
                    DeviceWorker, dev, cfg, gesture, pointer, verbose,
                    mods, defuser)
                retry_at.pop(path, None)
                started_at[path] = time.monotonic()
                workers[path] = asyncio.ensure_future(worker.run())

            if mods is not None:
                await asyncio.to_thread(mods.refresh)
                if not mods.readable and not mod_warned:
                    mod_warned = True
                    log(f"deskflick: cannot read any keyboard, so "
                        f"{cfg.mod_name} gestures are inactive. Log out and "
                        f"back in once so your `input` group membership takes "
                        f"effect.")
                elif mods.readable:
                    mod_warned = False

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
        f"tap={cfg.tap} double={cfg.double} hold={cfg.hold}"
        + (f" modifier={cfg.mod_name}" if cfg.mod_enabled else ""))
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
