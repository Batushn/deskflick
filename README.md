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

## Install

```bash
git clone https://github.com/Batushn/deskflick
cd deskflick
./install.sh
```

The installer:

1. copies `deskflick` to `/usr/local/bin`,
2. installs a udev rule so the `input` group can use `/dev/uinput`,
3. adds you to the `input` group (log out & back in if it says so),
4. enables + starts the `deskflick` systemd **user** service.

Dependency: `python-evdev` (Arch: `sudo pacman -S python-evdev`).

An AUR package is planned; the `PKGBUILD` in this repo is ready for it.

## Configure

Edit `~/.config/deskflick/config.toml` (created on install, fully commented),
then `systemctl --user restart deskflick`.

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
```

List all bindable KWin shortcut names:

```bash
gdbus call --session --dest org.kde.kglobalaccel --object-path /component/kwin --method org.kde.kglobalaccel.Component.shortcutNames
```

## Troubleshooting

```bash
deskflick --list-devices      # can deskflick see your mouse?
deskflick -v                  # run in foreground, log every gesture
journalctl --user -u deskflick -f
```

- **"no readable input devices"** — you're not in the `input` group yet;
  log out and back in after installing.
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
