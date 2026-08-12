#!/usr/bin/env bash
set -euo pipefail
systemctl --user disable --now deskflick.service 2>/dev/null || true
rm -f ~/.config/systemd/user/deskflick.service
systemctl --user daemon-reload
sudo rm -f /usr/local/bin/deskflick /etc/udev/rules.d/99-deskflick-uinput.rules
sudo udevadm control --reload-rules
echo "deskflick removed. Config kept at ~/.config/deskflick (delete it if you want)."
