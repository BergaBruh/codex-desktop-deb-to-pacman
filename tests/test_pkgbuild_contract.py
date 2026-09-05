from __future__ import annotations

from pathlib import Path
import re
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PKGBUILD_PATH = REPOSITORY_ROOT / "PKGBUILD"
SRCINFO_PATH = REPOSITORY_ROOT / ".SRCINFO"
README_PATH = REPOSITORY_ROOT / "README.md"

_URL = "https://persistent.oaistatic.com/codex-app-prod/linux/deb/latest/chatgpt_amd64.deb"
_SHA256 = "708a15a1bb76e2bb7f0e376e5145391fa277ad3a64057c1d32537bdc2a1b4e6e"
_DEPENDS = {
    "alsa-lib",
    "at-spi2-core",
    "cairo",
    "cups",
    "dbus",
    "expat",
    "gcc-libs",
    "gdk-pixbuf2",
    "glib2",
    "glibc",
    "gtk3",
    "libdrm",
    "libx11",
    "libxcb",
    "libxcomposite",
    "libxdamage",
    "libxext",
    "libxfixes",
    "libxkbcommon",
    "libxrandr",
    "libarchive",
    "mesa",
    "nspr",
    "nss",
    "pango",
    "polkit",
    "python",
    "systemd-libs",
    "xdg-utils",
}
_MAKEDEPENDS = {"python", "binutils", "libarchive"}
_OPTDEPENDS = {
    "apparmor: optional support for loading the upstream AppArmor profile",
    "libnotify: desktop notifications and update install action prompts; stdout fallback is used when unavailable",
}


def _srcinfo_value(contents: str, field: str) -> str:
    match = re.search(rf"^\s*{re.escape(field)} = (.+)$", contents, re.MULTILINE)
    if match is None:
        raise AssertionError(f"missing {field} in .SRCINFO")
    return match.group(1)


def _srcinfo_values(contents: str, field: str) -> set[str]:
    return set(re.findall(rf"^\s*{re.escape(field)} = (.+)$", contents, re.MULTILINE))


def _pkgbuild_array(contents: str, name: str) -> set[str]:
    match = re.search(rf"(?ms)^{re.escape(name)}=\((.*?)\)", contents)
    if match is None:
        raise AssertionError(f"missing {name} array in PKGBUILD")
    return set(re.findall(r"'([^']+)'", match.group(1)))


def _pkgbuild_word_array(contents: str, name: str) -> set[str]:
    match = re.search(rf"(?ms)^{re.escape(name)}=\((.*?)\)", contents)
    if match is None:
        raise AssertionError(f"missing {name} array in PKGBUILD")
    return set(match.group(1).split())


class PkgbuildContractTests(unittest.TestCase):
    def test_pinned_metadata_dependencies_and_safe_staging_contract(self) -> None:
        contents = PKGBUILD_PATH.read_text(encoding="utf-8")
        srcinfo = SRCINFO_PATH.read_text(encoding="utf-8")

        self.assertIn("pkgname=chatgpt-bin", contents)
        self.assertIn("pkgver=26.810.52044", contents)
        self.assertIn("pkgrel=1", contents)
        self.assertIn("arch=('x86_64')", contents)
        self.assertIn("license=('custom')", contents)
        self.assertIn(_URL, contents)
        self.assertIn(_SHA256, contents)
        self.assertIn("options=(!strip !debug)", contents)
        self.assertIn("backup=('etc/apparmor.d/chatgpt')", contents)
        self.assertIn("SOURCE_DATE_EPOCH=1786770000", contents)
        self.assertIn("apparmor: optional support for loading the upstream AppArmor profile", contents)
        self.assertIn("desktop notifications and update install action prompts", contents)
        self.assertIn('python "$startdir/scripts/deb_payload.py"', contents)
        self.assertIn('install -Dm644 "$pkgdir/usr/share/doc/chatgpt/copyright"', contents)

        self.assertEqual(_pkgbuild_array(contents, "depends"), _DEPENDS)
        self.assertEqual(_pkgbuild_array(contents, "makedepends"), _MAKEDEPENDS)
        self.assertEqual(_pkgbuild_array(contents, "optdepends"), _OPTDEPENDS)
        self.assertEqual(_srcinfo_values(srcinfo, "depends"), _DEPENDS)
        self.assertEqual(_srcinfo_values(srcinfo, "makedepends"), _MAKEDEPENDS)
        self.assertEqual(_srcinfo_values(srcinfo, "optdepends"), _OPTDEPENDS)
        self.assertNotIn("apt", contents)
        self.assertNotRegex(contents, r"(?m)^(?:build|prepare)\(\)")
        package_body = contents.split("package() {", 1)[1]
        self.assertNotRegex(package_body, r"(?m)^\s*strip(?:\s|$)")
        for forbidden in (
            "SKIP",
            ".INSTALL",
            "post_install",
            "apparmor_parser",
            "/etc/apt",
            "keyring",
            "chmod u+s",
            "patchelf",
            "upx",
            "strip -",
            "install -Dm755 \"$pkgdir/usr/bin/chatgpt\"",
        ):
            self.assertNotIn(forbidden, contents)

    def test_desktop_icon_and_payload_paths_are_preserved(self) -> None:
        payload_helper = (REPOSITORY_ROOT / "scripts" / "deb_payload.py").read_text(encoding="utf-8")

        self.assertIn('"usr/share/applications/chatgpt.desktop"', payload_helper)
        self.assertIn('"usr/share/pixmaps/chatgpt.png"', payload_helper)
        self.assertIn('"etc/apparmor.d/chatgpt"', payload_helper)
        self.assertIn('_LAUNCHER_PATH = "usr/bin/chatgpt"', payload_helper)
        self.assertIn('target != "../lib/chatgpt/codex-launcher"', payload_helper)

    def test_generated_srcinfo_matches_the_package_identity(self) -> None:
        contents = SRCINFO_PATH.read_text(encoding="utf-8")

        self.assertEqual(_srcinfo_value(contents, "pkgbase"), "chatgpt-bin")
        self.assertEqual(_srcinfo_value(contents, "pkgver"), "26.810.52044")
        self.assertEqual(_srcinfo_value(contents, "pkgrel"), "1")
        self.assertEqual(_srcinfo_value(contents, "arch"), "x86_64")

    def test_debug_source_artifacts_are_disabled_in_pkgbuild_and_srcinfo(self) -> None:
        pkgbuild_options = _pkgbuild_word_array(PKGBUILD_PATH.read_text(encoding="utf-8"), "options")
        srcinfo_options = _srcinfo_values(SRCINFO_PATH.read_text(encoding="utf-8"), "options")

        self.assertIn("!strip", pkgbuild_options)
        self.assertIn("!debug", pkgbuild_options)
        self.assertNotIn("debug", pkgbuild_options)
        self.assertIn("!strip", srcinfo_options)
        self.assertIn("!debug", srcinfo_options)
        self.assertNotIn("debug", srcinfo_options)

    def test_readme_exists_for_the_release_procedure(self) -> None:
        contents = README_PATH.read_text(encoding="utf-8")

        self.assertIn('SRCDEST="$SOURCE_CACHE" makepkg --verifysource', contents)
        self.assertIn('SRCDEST="$SOURCE_CACHE" extra-x86_64-build -D "$SOURCE_CACHE"', contents)
        self.assertNotIn(" -- -I ", contents)

    def test_readme_documents_action_gated_update_workflow(self) -> None:
        contents = README_PATH.read_text(encoding="utf-8")

        self.assertIn("chatgpt-bin-update workflow", contents)
        self.assertIn("chatgpt-bin-update-check.service", contents)
        self.assertIn("chatgpt-bin-update-workflow.service", contents)
        self.assertIn("chatgpt-bin-update install", contents)
        self.assertIn("only after clicking the Install notification action", contents)


if __name__ == "__main__":
    unittest.main()
