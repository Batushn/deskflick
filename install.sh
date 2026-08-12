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
sudo install -Dm644 99-deskflick-uinput.rules /etc/udev/rules.d/99-deskflick-uinput.rules

if ! python3 -c "import PySide6" 2>/dev/null; then
    echo "note: PySide6 not found — the deskflick-ui settings window needs it."
    echo "  Arch: sudo pacman -S pyside6"
fi
sudo udevadm control --reload-rules
sudo udevadm trigger --name-match=uinput || true

if ! id -nG "$USER" | grep -qw input; then
    echo ":: Adding $USER to the input group (takes effect after re-login)"
    sudo usermod -aG input "$USER"
    NEED_RELOGIN=1
fi

echo ":: Installing user config and systemd service"
mkdir -p ~/.config/deskflick
[ -f ~/.config/deskflick/config.toml ] || cp config.example.toml ~/.config/deskflick/config.toml
mkdir -p ~/.config/systemd/user
cp deskflick.service ~/.config/systemd/user/deskflick.service
systemctl --user daemon-reload
systemctl --user enable deskflick.service

if [ "${NEED_RELOGIN:-0}" = 1 ]; then
    echo
    echo "!! You were added to the 'input' group."
    echo "!! Log out and back in, then run: systemctl --user start deskflick"
else
    systemctl --user restart deskflick.service
    echo
    echo ":: deskflick is running. Hold mouse button 4 and flick!"
    echo ":: Config: ~/.config/deskflick/config.toml"
fi
