# deskflick

**Hold a mouse button, flick the mouse, glide between KDE Plasma virtual desktops.**

Hold your mouse's *back* button (button 4 / `BTN_SIDE`) and push the mouse:

- ⬅️ push left → desktop to the left
- ➡️ push right → desktop to the right
- ⬆️ push up → desktop above
- ⬇️ push down → desktop below

Keep holding and keep pushing to fly across your whole grid. **Tap**,
**double tap** and **press-and-hold** are separately bindable — by default a
tap is passed through untouched, so *back* still works in your browser.

- 🖥️ **Wayland & X11** — reads evdev directly, re-emits through uinput
- 🎨 **Native feel** — triggers KWin's own shortcuts via D-Bus, so you get
  Plasma's normal switch animation and OSD
- ⚙️ **Configurable** — trigger button, sensitivity, repeat, pointer lock,
  inverted directions, or bind any KWin shortcut / shell command per direction
- 🪶 **Tiny** — one Python file, one dependency (`python-evdev`), runs as a
  systemd user service
- 🖱️ **Settings GUI** — `deskflick-ui` (Qt): pick the trigger button (or
  capture it by pressing it), tune sensitivity, rebind actions
- 🪟 **Tap, double tap and press-and-hold are each bindable** — to the
  button's original action, *Present Windows*, any KWin shortcut, or a shell
  command
- 🪄 **Meta + flick moves the window** — optional: hold Meta with the trigger
  and flick to snap the focused window that way, like Meta + arrow keys
- 🎮 **Handles gaming mice** — buttons that send keyboard macros from an
  onboard profile live on a second HID interface; deskflick grabs those too,
  so the macro no longer leaks through while you gesture

## Install

```bash
git clone https://github.com/Batushn/deskflick
cd deskflick
./install.sh
```

The installer:

1. copies `deskflick` and `deskflick-ui` to `/usr/local/bin`,
2. installs a udev rule granting access to `/dev/uinput` and an ACL on
   **mouse** devices for the logged-in user (keyboards excluded, no group
   change and no re-login needed),
3. enables + starts the `deskflick` systemd **user** service.

Dependency: `python-evdev` (Arch: `sudo pacman -S python-evdev`).

An AUR package is planned; the `PKGBUILD` in this repo is ready for it.

## Configure

**GUI:** launch **deskflick** from your app menu (or run `deskflick-ui`).
Pick a trigger button from the list or click *Detect…* and press the mouse
button you want; adjust sensitivity and per-direction actions; *Save &
restart service* applies everything. Requires `pyside6`
(Arch: `sudo pacman -S pyside6`).

**Or by hand:** edit `~/.config/deskflick/config.toml` (created on install,
fully commented), then `systemctl --user restart deskflick`.

```toml
[trigger]
button = "BTN_SIDE"       # "BTN_EXTRA" for button 5, "BTN_MIDDLE", KEY_* ...
tap = "passthrough"       # passthrough | overview | none | any KWin shortcut
double = "none"           # two quick presses
hold = "none"             # held down without flicking
double_ms = 250
hold_ms = 500

[gesture]
threshold = 150           # lower = more sensitive
repeat = true             # keep pushing = keep switching
lock_pointer = true       # freeze the cursor during a gesture
invert_x = false
invert_y = false

[actions]                 # any KWin shortcut name, or "cmd:<shell command>"
left  = "Switch One Desktop to the Left"
right = "Switch One Desktop to the Right"
up    = "Switch One Desktop Up"
down  = "Switch One Desktop Down"

[overview]
shortcut = "ExposeAll"    # used when tap = "overview"

[modifier]                # hold Meta too -> act on the window, not the desktop
enabled = false
key = "KEY_LEFTMETA"
left = "Window Quick Tile Left"
right = "Window Quick Tile Right"
up = "Window Quick Tile Top"
down = "Window Quick Tile Bottom"
```

With `[modifier]` on, deskflick watches one key's state on your keyboards
(read-only, never grabbed, and only that key). That needs `input` group
membership, which takes effect after one logout; until then it says so in the
log and simply stays inactive.

List all bindable KWin shortcut names:

```bash
gdbus call --session --dest org.kde.kglobalaccel --object-path /component/kwin --method org.kde.kglobalaccel.Component.shortcutNames
```

## Troubleshooting

```bash
deskflick --list-devices      # can deskflick see your mouse?
deskflick --watch             # press a button, see its exact name
deskflick -v                  # run in foreground, log every gesture
deskflick --unstick           # rescue: release all mouse buttons
journalctl --user -u deskflick -f
```

- **A click feels stuck** — run `deskflick --unstick`. Since 0.3.0 this
  shouldn't happen: deskflick refuses to grab a mouse while a button is
  held, always flushes releases through the clone before letting go, and
  the service runs `--unstick` after every stop, even a kill.
- **"no readable input devices"**, or the settings window says *cannot see
  that button* — the udev ACL hasn't been applied; re-run `./install.sh`.
- **The button still does its old thing** — deskflick isn't grabbing it. The
  status line in `deskflick-ui` names every device it grabbed; if your mouse
  isn't there, it's a permission problem. Macro buttons additionally need
  `input` group membership, which needs one logout.
- Desktops don't wrap by default — that's KWin's *Navigation wraps around*
  setting (System Settings → Window Management → Virtual Desktops).

## Uninstall

```bash
./uninstall.sh
```

## How it works

deskflick grabs your mouse at the evdev level and mirrors every event through
a virtual uinput device. While the trigger button is held, its press is
swallowed and relative motion is accumulated; when it crosses the threshold,
deskflick calls `org.kde.kglobalaccel` to invoke the matching KWin shortcut.
On a quick tap the click is replayed, so the button's normal function is
preserved.

## License

[MIT](LICENSE)
