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
  'libnotify'
  'libx11'
  'libxcb'
  'libxcomposite'
  'libxdamage'
  'libxext'
  'libxfixes'
  'libxkbcommon'
  'libxrandr'
  'mesa'
  'nspr'
  'nss'
  'pango'
  'systemd-libs'
  'xdg-utils'
)
makedepends=('python' 'binutils' 'libarchive')
optdepends=('apparmor: optional support for loading the upstream AppArmor profile')
backup=('etc/apparmor.d/chatgpt')
options=(!strip)
source=('chatgpt_amd64.deb::https://persistent.oaistatic.com/codex-app-prod/linux/deb/latest/chatgpt_amd64.deb')
sha256sums=('708a15a1bb76e2bb7f0e376e5145391fa277ad3a64057c1d32537bdc2a1b4e6e')
SOURCE_DATE_EPOCH=1786770000

package() {
  python "$startdir/scripts/deb_payload.py" --deb "$srcdir/chatgpt_amd64.deb" --destination "$pkgdir"
  install -Dm644 "$pkgdir/usr/share/doc/chatgpt/copyright" "$pkgdir/usr/share/licenses/$pkgname/copyright"
}
