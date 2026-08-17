pkgname=chatgpt-bin
pkgver=26.810.52044
pkgrel=1
pkgdesc='OpenAI ChatGPT desktop application'
arch=('x86_64')
url='https://openai.com/codex/'
license=('custom')
depends=(
  'alsa-lib'
  'at-spi2-core'
  'cairo'
  'cups'
  'dbus'
  'expat'
  'gcc-libs'
  'gdk-pixbuf2'
  'glib2'
  'glibc'
  'gtk3'
  'libdrm'
  'libx11'
  'libxcb'
  'libxcomposite'
  'libxdamage'
  'libxext'
  'libxfixes'
  'libxkbcommon'
  'libxrandr'
  'libarchive'
  'mesa'
  'nspr'
  'nss'
  'pango'
  'polkit'
  'python'
  'systemd-libs'
  'xdg-utils'
)
makedepends=('python' 'binutils' 'libarchive')
optdepends=(
  'apparmor: optional support for loading the upstream AppArmor profile'
  'libnotify: desktop notifications for update checks; stdout fallback is used when unavailable'
)
backup=('etc/apparmor.d/chatgpt')
options=(!strip)
source=('chatgpt_amd64.deb::https://persistent.oaistatic.com/codex-app-prod/linux/deb/latest/chatgpt_amd64.deb')
sha256sums=('708a15a1bb76e2bb7f0e376e5145391fa277ad3a64057c1d32537bdc2a1b4e6e')
SOURCE_DATE_EPOCH=1786770000

package() {
  python "$startdir/scripts/deb_payload.py" --deb "$srcdir/chatgpt_amd64.deb" --destination "$pkgdir"
  install -Dm644 "$pkgdir/usr/share/doc/chatgpt/copyright" "$pkgdir/usr/share/licenses/$pkgname/copyright"
  install -Dm644 "$startdir/updater/common.py" "$pkgdir/usr/lib/chatgpt-bin/updater/common.py"
  install -Dm644 "$startdir/updater/check_update.py" "$pkgdir/usr/lib/chatgpt-bin/updater/check_update.py"
  install -Dm644 "$startdir/updater/build_update.py" "$pkgdir/usr/lib/chatgpt-bin/updater/build_update.py"
  install -Dm644 "$startdir/updater/install_package.py" "$pkgdir/usr/lib/chatgpt-bin/updater/install_package.py"
  install -Dm644 "$startdir/scripts/review_source.py" "$pkgdir/usr/lib/chatgpt-bin/updater/review_source.py"
  install -Dm644 "$startdir/scripts/deb_payload.py" "$pkgdir/usr/lib/chatgpt-bin/updater/deb_payload.py"
  install -Dm644 "$startdir/PKGBUILD" "$pkgdir/usr/lib/chatgpt-bin/updater/recipe/PKGBUILD"
  install -Dm644 "$startdir/.SRCINFO" "$pkgdir/usr/lib/chatgpt-bin/updater/recipe/.SRCINFO"
  install -Dm644 "$startdir/scripts/update_source.py" "$pkgdir/usr/lib/chatgpt-bin/updater/recipe/scripts/update_source.py"
  install -Dm644 "$startdir/scripts/review_source.py" "$pkgdir/usr/lib/chatgpt-bin/updater/recipe/scripts/review_source.py"
  install -Dm644 "$startdir/scripts/deb_payload.py" "$pkgdir/usr/lib/chatgpt-bin/updater/recipe/scripts/deb_payload.py"
  install -Dm644 "$startdir/updater/common.py" "$pkgdir/usr/lib/chatgpt-bin/updater/recipe/updater/common.py"
  install -Dm644 "$startdir/updater/check_update.py" "$pkgdir/usr/lib/chatgpt-bin/updater/recipe/updater/check_update.py"
  install -Dm644 "$startdir/updater/build_update.py" "$pkgdir/usr/lib/chatgpt-bin/updater/recipe/updater/build_update.py"
  install -Dm644 "$startdir/updater/install_package.py" "$pkgdir/usr/lib/chatgpt-bin/updater/recipe/updater/install_package.py"
  install -Dm755 "$startdir/updater/chatgpt-bin-check-update" "$pkgdir/usr/lib/chatgpt-bin/updater/recipe/updater/chatgpt-bin-check-update"
  install -Dm755 "$startdir/updater/chatgpt-bin-install-package" "$pkgdir/usr/lib/chatgpt-bin/updater/recipe/updater/chatgpt-bin-install-package"
  install -Dm755 "$startdir/updater/chatgpt-bin-update" "$pkgdir/usr/lib/chatgpt-bin/updater/recipe/updater/chatgpt-bin-update"
  install -Dm644 "$startdir/updater/polkit/org.chatgpt-bin.install-package.policy" "$pkgdir/usr/lib/chatgpt-bin/updater/recipe/updater/polkit/org.chatgpt-bin.install-package.policy"
  install -Dm644 "$startdir/updater/systemd/chatgpt-bin-update-check.service" "$pkgdir/usr/lib/chatgpt-bin/updater/recipe/updater/systemd/chatgpt-bin-update-check.service"
  install -Dm644 "$startdir/updater/systemd/chatgpt-bin-update-check.timer" "$pkgdir/usr/lib/chatgpt-bin/updater/recipe/updater/systemd/chatgpt-bin-update-check.timer"
  install -Dm755 "$startdir/updater/chatgpt-bin-check-update" "$pkgdir/usr/bin/chatgpt-bin-check-update"
  install -Dm755 "$startdir/updater/chatgpt-bin-update" "$pkgdir/usr/bin/chatgpt-bin-update"
  install -Dm755 "$startdir/updater/chatgpt-bin-install-package" "$pkgdir/usr/libexec/chatgpt-bin/install-package"
  install -Dm644 "$startdir/updater/polkit/org.chatgpt-bin.install-package.policy" "$pkgdir/usr/share/polkit-1/actions/org.chatgpt-bin.install-package.policy"
  install -Dm644 "$startdir/updater/systemd/chatgpt-bin-update-check.service" "$pkgdir/usr/lib/systemd/user/chatgpt-bin-update-check.service"
  install -Dm644 "$startdir/updater/systemd/chatgpt-bin-update-check.timer" "$pkgdir/usr/lib/systemd/user/chatgpt-bin-update-check.timer"
}
