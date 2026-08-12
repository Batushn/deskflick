# Maintainer: Batuhan Sahin <mrbatuhansahin@gmail.com>
pkgname=deskflick
pkgver=0.8.0
pkgrel=1
pkgdesc="Hold a mouse button and flick to switch KDE Plasma virtual desktops (Wayland & X11)"
arch=('any')
url="https://github.com/Batushn/deskflick"
license=('MIT')
depends=('python' 'python-evdev' 'glib2')
optdepends=('pyside6: deskflick-ui settings window')
source=("$pkgname-$pkgver.tar.gz::$url/archive/v$pkgver.tar.gz")
sha256sums=('SKIP')

package() {
    cd "$pkgname-$pkgver"
    install -Dm755 deskflick.py "$pkgdir/usr/bin/deskflick"
    install -Dm755 deskflick-ui.py "$pkgdir/usr/bin/deskflick-ui"
    install -Dm644 deskflick.desktop "$pkgdir/usr/share/applications/deskflick.desktop"
    install -Dm644 71-deskflick.rules "$pkgdir/usr/lib/udev/rules.d/71-deskflick.rules"
    install -Dm644 deskflick.service "$pkgdir/usr/lib/systemd/user/deskflick.service"
    install -Dm644 config.example.toml "$pkgdir/usr/share/doc/$pkgname/config.example.toml"
    install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
