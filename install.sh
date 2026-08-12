#!/usr/bin/env bash
# deskflick installer — run from the repo root: ./install.sh
set -euo pipefail
cd "$(dirname "$0")"

if ! python3 -c "import evdev" 2>/dev/null; then
    echo "python-evdev is required. Install it first:"
    echo "  Arch:   sudo pacman -S python-evdev"
    echo "  Debian: sudo apt install python3-evdev"
    echo "  Fedora: sudo dnf install python3-evdev"
    exit 1
fi

echo ":: Installing binaries (sudo needed for /usr/local/bin and udev rule)"
sudo install -Dm755 deskflick.py /usr/local/bin/deskflick
sudo install -Dm755 deskflick-ui.py /usr/local/bin/deskflick-ui
sudo install -Dm644 deskflick.desktop /usr/local/share/applications/deskflick.desktop
# Earlier names sorted wrong and never applied: 99- ran after uaccess,
# 60-deskflick ran before ID_INPUT_MOUSE was set.
sudo rm -f /etc/udev/rules.d/99-deskflick-uinput.rules /etc/udev/rules.d/60-deskflick.rules
sudo install -Dm644 71-deskflick.rules /etc/udev/rules.d/71-deskflick.rules

if ! python3 -c "import PySide6" 2>/dev/null; then
    echo "note: PySide6 not found — the deskflick-ui settings window needs it."
    echo "  Arch: sudo pacman -S pyside6"
fi
sudo udevadm control --reload-rules
sudo udevadm trigger --name-match=uinput 2>/dev/null || true
# Apply the mouse ACL to devices that are already plugged in
sudo udevadm trigger --subsystem-match=input --action=change 2>/dev/null || true
sudo udevadm settle 2>/dev/null || true

# Mice that send keyboard macros expose them on a second HID interface, which
# the mouse-only ACL above does not cover. `input` group membership does; it
# needs one logout to take effect.
if ! id -nG "$USER" | grep -qw input; then
    echo ":: Adding $USER to the input group (needed for macro buttons;"
    echo "   takes effect after you log out and back in once)"
    sudo usermod -aG input "$USER"
fi

echo ":: Installing user config and systemd service"
mkdir -p ~/.config/deskflick
[ -f ~/.config/deskflick/config.toml ] || cp config.example.toml ~/.config/deskflick/config.toml
mkdir -p ~/.config/systemd/user
cp deskflick.service ~/.config/systemd/user/deskflick.service
systemctl --user daemon-reload
systemctl --user enable deskflick.service

echo ":: Checking mouse access"
BLOCKED=$(deskflick --list-devices 2>/dev/null | grep "no access" || true)
if [ -n "$BLOCKED" ]; then
    echo "   !! These devices are still unreadable:"
    echo "$BLOCKED" | sed 's/^/      /'
    echo "   !! If your mouse is in that list, unplug and replug it (or log"
    echo "   !! out and back in), then: systemctl --user restart deskflick"
else
    echo "   ok — every input device is readable"
fi

systemctl --user restart deskflick.service
sleep 1
if [ "$(systemctl --user is-active deskflick)" = active ]; then
    echo
    echo ":: deskflick is running. Hold mouse button 4 and flick!"
    echo ":: Config: ~/.config/deskflick/config.toml  (GUI: deskflick-ui)"
    echo ":: If a click ever gets stuck: deskflick --unstick"
else
    echo
    echo "!! Service did not start. Logs:"
    journalctl --user -u deskflick -n 10 --no-pager
fi
