# deskflick

**Hold a mouse button, flick the mouse, glide between KDE Plasma virtual desktops.**

Hold your mouse's *back* button (button 4 / `BTN_SIDE`) and push the mouse:

- ⬅️ push left → desktop to the left
- ➡️ push right → desktop to the right
- ⬆️ push up → desktop above
- ⬇️ push down → desktop below

Keep holding and keep pushing to fly across your whole grid. A quick **tap**
of the button is passed through untouched, so *back* still works in your
browser.

- 🖥️ **Wayland & X11** — reads evdev directly, re-emits through uinput
- 🎨 **Native feel** — triggers KWin's own shortcuts via D-Bus, so you get
  Plasma's normal switch animation and OSD
- ⚙️ **Configurable** — trigger button, sensitivity, repeat, pointer lock,
  inverted directions, or bind any KWin shortcut / shell command per direction
- 🪶 **Tiny** — one Python file, one dependency (`python-evdev`), runs as a
  systemd user service
- 🖱️ **Settings GUI** — `deskflick-ui` (Qt): pick the trigger button (or
  capture it by pressing it), tune sensitivity, rebind actions
- 🪟 **Present Windows mode** — optional: a quick tap toggles KWin's
  *Present Windows (All desktops)* (the Ctrl+F10 effect) instead of clicking

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
button = "BTN_SIDE"       # "BTN_EXTRA" for button 5, "BTN_MIDDLE", ...
tap_passthrough = true    # tap still acts as a normal click

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
enabled = false           # true: a tap toggles Present Windows (All desktops)
shortcut = "ExposeAll"    # the Ctrl+F10 effect; hold+flick still switches desktops
```

List all bindable KWin shortcut names:

```bash
gdbus call --session --dest org.kde.kglobalaccel --object-path /component/kwin --method org.kde.kglobalaccel.Component.shortcutNames
```

## Troubleshooting

```bash
deskflick --list-devices      # can deskflick see your mouse?
deskflick -v                  # run in foreground, log every gesture
deskflick --unstick           # rescue: release all mouse buttons
journalctl --user -u deskflick -f
```

- **A click feels stuck** — run `deskflick --unstick`. Since 0.3.0 this
  shouldn't happen: deskflick refuses to grab a mouse while a button is
  held, always flushes releases through the clone before letting go, and
  the service runs `--unstick` after every stop, even a kill.
- **"no readable input devices"** — the udev ACL hasn't been applied yet;
  unplug/replug the mouse or re-run `./install.sh`.
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
