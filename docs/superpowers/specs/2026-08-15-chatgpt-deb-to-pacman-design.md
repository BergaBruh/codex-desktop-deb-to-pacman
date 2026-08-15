# ChatGPT Debian-to-Pacman Package Design

## Status and goal

This approved design defines one `x86_64` Arch Linux binary package, named
`chatgpt-bin`, by repackaging the official ChatGPT Linux `.deb`. The upstream
desktop app includes ChatGPT Work and Codex. It is not an Arch-supported
release: OpenAI documents the Linux app as a preview and formally supports
only Ubuntu, Debian, and Fedora.

The package must install the upstream application as `chatgpt` without
building, patching, stripping, or otherwise modifying its executable or
resources. It must be installable and removable through Pacman without adding
an APT repository or making unrequested system-policy changes.

## Scope and non-goals

In scope:

- A single prebuilt `chatgpt-bin` package for `x86_64`.
- Repackaging only the verified upstream Debian data payload.
- Native Arch dependency metadata, desktop integration, and the upstream
  AppArmor profile treatment described below.
- Repeatable source verification and a manual, reviewable release-update
  process.

Out of scope:

- Building ChatGPT/Codex from source, repackaging the ARM64 `.deb` or either
  RPM, or supporting non-`x86_64` architectures.
- Changing application behavior, its update channel, Electron sandboxing, or
  its bundled libraries.
- Enabling or configuring APT, adding OpenAI keys or repository files, creating
  an Arch repository, publishing to the AUR, or signing/releasing packages.
- Generating a replacement application icon. Preserve the supplied desktop
  entry and its upstream pixmap.

## Authoritative source snapshot

OpenAI's [Linux desktop-app guide](https://learn.chatgpt.com/docs/linux/linux-app)
links the x64 Debian artifact at this exact URL:

```text
https://persistent.oaistatic.com/codex-app-prod/linux/deb/latest/chatgpt_amd64.deb
```

This is a mutable `latest` endpoint, not a versioned immutable URL. Therefore
the source URL alone is not a release identifier; the pinned SHA-256 is
mandatory.

Snapshot verified on 2026-08-15 from the locally reviewed downloaded artifact
`chatgpt_amd64.deb`:

| Field | Value |
| --- | --- |
| Upstream Debian package | `chatgpt` |
| Debian control `Version` | `26.810.52044` |
| Debian `Architecture` | `amd64` |
| Artifact size | `391801406` bytes |
| SHA-256 | `708a15a1bb76e2bb7f0e376e5145391fa277ad3a64057c1d32537bdc2a1b4e6e` |
| Arch package version | `pkgver=26.810.52044`, `pkgrel=1` |

The implementation must use that URL and checksum literally for this release.
A mismatch must fail before extraction or packaging; `SKIP`, an unchecked
cached download, and a rolling `pkgver` are prohibited.

### Update behavior

An update is a deliberate source-review operation, not an automatic build
step. Download the endpoint into a fresh source cache, calculate its SHA-256,
read the Debian control `Version`, and review its data-file allowlist, desktop
entry, AppArmor profile, ELF dependencies, and licenses. Only if the reviewed
control version and artifact are acceptable, change `pkgver`, reset `pkgrel` to
`1`, and replace the SHA-256 together in one review. A changed `latest`
artifact that has not received all three updates must fail checksum validation.

No detached checksum or detached signature is published with this direct
artifact URL. HTTPS plus the reviewed pinned hash protects the build input from
silent replacement after review, but does not create a stronger independent
upstream signature chain; the package must not claim otherwise.

## Archive and installation data flow

```text
OpenAI HTTPS .deb
  -> SHA-256 verification by makepkg
  -> extract ar container
  -> extract data.tar.* only
  -> validate source-path allowlist
  -> stage approved files in $pkgdir unchanged
  -> build chatgpt-bin-x86_64.pkg.tar.*
```

The `.deb` has separate `control.tar.*` and `data.tar.*` archives. Extract only
`data.tar.*`; never run, translate, or copy its Debian maintainer scripts. In
this snapshot, `postinst` installs an OpenAI APT signing key and writes
`/etc/apt/sources.list.d/chatgpt.sources`; those actions are explicitly outside
the Arch package.

Accept only these upstream payload roots and files, rejecting any newly added
top-level payload path for manual review:

- `/usr/lib/chatgpt/**` — the upstream executable, launcher, Chromium/Electron
  resources, bundled libraries, locales, and license material.
- `/usr/bin/chatgpt` — the upstream symlink to
  `/usr/lib/chatgpt/codex-launcher`; preserve it rather than adding a wrapper.
- `/usr/share/applications/chatgpt.desktop` and
  `/usr/share/doc/chatgpt/copyright`.
- `/usr/share/pixmaps/chatgpt.png` — the icon named by the upstream desktop
  entry.
- `/etc/apparmor.d/chatgpt` as described in the AppArmor section.

The package may additionally copy the supplied copyright notice into
`/usr/share/licenses/chatgpt-bin/` to meet Arch license-location conventions.
It must not add unrelated launchers, files under `/etc/apt`, keyrings, package
manager configuration, or generated source code.

The snapshot contains no `chrome-sandbox` helper. Do not add a setuid sandbox
binary or change file capabilities; preserve the upstream Electron sandbox
model exactly.

## Arch metadata and dependency strategy

Use `arch=('x86_64')`, `pkgname=chatgpt-bin`, `pkgver=26.810.52044`,
`pkgrel=1`, and a `custom` license classification unless a later reviewed
upstream license statement establishes a more accurate package license. Keep
the supplied Electron copyright and Chromium license material installed.

Do not mechanically copy Debian package names. The Arch `depends` array must
be derived from the extracted ELF `DT_NEEDED` entries, dynamic runtime loads,
and Arch repository ownership of the required SONAMEs. For this snapshot that
includes these native package providers:

```text
alsa-lib at-spi2-core cairo cups dbus expat gcc-libs gdk-pixbuf2 glib2 glibc
gtk3 libdrm libnotify libx11 libxcb libxcomposite libxdamage libxext libxfixes
libxkbcommon libxrandr mesa nspr nss pango systemd-libs xdg-utils
```

Before release, validate every listed provider with Arch package metadata and
scan every packaged ELF object, including unpacked native Node modules. Add a
dependency only when an observed library load or documented launcher behavior
requires it; remove no entry solely because the main executable did not list it
directly. `apparmor` is an optional dependency, not a hard runtime dependency.
Do not declare `provides` or `conflicts` speculatively; check current ownership
of `/usr/bin/chatgpt` before publishing and declare a conflict only for a
verified file collision.

## Desktop and AppArmor treatment

Install the upstream desktop file unchanged at
`/usr/share/applications/chatgpt.desktop`. It uses `Exec=chatgpt %U`, declares
the `codex` URI scheme and the supplied document MIME types, and relies on the
upstream `/usr/bin/chatgpt` symlink. The package must not replace it with a
  shell wrapper or generate a non-upstream icon. The installed
  `/usr/share/pixmaps/chatgpt.png` must resolve the desktop entry's
  `Icon=chatgpt` value.

Install the upstream AppArmor profile at `/etc/apparmor.d/chatgpt` and mark it
as a Pacman backup file. The profile is intentionally `flags=(unconfined)` and
allows `userns`; it is compatibility policy for Electron on AppArmor-enabled
systems, **not** a security sandbox. Preserve it verbatim, including its
`include if exists <local/chatgpt>` extension point.

Do not ship an install script that invokes `apparmor_parser`, enables an
AppArmor service, removes another profile, or changes kernel user-namespace
policy. On an AppArmor-enabled host, the administrator may load or reload the
profile through the host's normal AppArmor management after reviewing it. This
avoids both a misleading sandbox claim and unrequested privileged policy
changes during `pacman -U`.

## Reproducibility and security constraints

- The build must use only the verified `.deb`; no network access, package
  downloads, or application execution may occur after source acquisition.
- Use a clean Arch build chroot and a fixed `SOURCE_DATE_EPOCH` derived from
  the reviewed source artifact's recorded data-file timestamps. Do not alter
  upstream payload timestamps, modes, symlinks, executable bytes, or embedded
  resources.
- Do not run `strip`, `upx`, `patchelf`, `chmod u+s`, or application code during
  `build()` or `package()`.
- Fail if extraction yields a path outside the allowlist, an absolute path, a
  traversal path, a setuid/setgid file, or file capabilities. Files must be
  owned by root in the final Pacman archive through normal fakeroot packaging.
- The package must have no `.INSTALL` hook. Installing and removing it may only
  add or remove its declared files and Pacman's normal backup-file handling.
- Treat every upstream release as a fresh binary-security review. A checksum
  update proves only that the downloaded bytes match the reviewed value; it
  does not establish that the proprietary binary is safe.

## Concrete verification criteria

The implementation is acceptable only when all of these are true:

1. `makepkg --verifysource` succeeds for the snapshot URL and exact SHA-256;
   replacing the source with one byte changed fails verification.
2. The extracted Debian control metadata reports exactly version `26.810.52044`
   and architecture `amd64` for this release, while the produced Pacman package
   reports `chatgpt-bin`, `26.810.52044-1`, and `x86_64`.
3. A file-list comparison shows only the allowlisted payload, the Arch license
   copy, and no `/etc/apt`, `/usr/share/keyrings`, APT source file, Debian
   maintainer script, setuid/setgid file, or file capability.
4. `desktop-file-validate` accepts the installed desktop entry; its `Exec`
   target resolves through `/usr/bin/chatgpt` to the upstream launcher, and
   `Icon=chatgpt` resolves to `/usr/share/pixmaps/chatgpt.png`.
5. Static dependency inspection of all packaged ELF and native `.node` objects
   resolves every required SONAME to an installed Arch dependency; `namcap`
   findings are reviewed rather than blindly suppressed.
6. In a clean `x86_64` Arch test environment, `pacman -U` installs the package
   without package-manager configuration side effects, and removal leaves no
   OpenAI APT repository, keyring, or AppArmor load/unload side effect.
7. In a logged-in graphical test session, launch `chatgpt` as an unprivileged
   user and verify that the sign-in window opens. Do not use a real account or
   enter credentials for this packaging check. Test the documented Wayland
   invocation separately when Wayland is available.
8. Two clean builds from the same downloaded artifact, chroot, package-tool
   version, and `SOURCE_DATE_EPOCH` have identical packaged file content and
   metadata. If archive-level bytes differ because of package signing or build
   metadata, compare the unsigned normalized package payload and metadata;
   record any remaining difference as a release blocker.

## Spec self-review

This design intentionally makes the mutable source endpoint, the supplied
upstream pixmap, and the unconfined AppArmor profile explicit. It contains no
placeholder values or automatic-update path: the version, checksum, package
name, architecture, source URL, extraction boundary, privileged side-effect
boundary, and acceptance checks are all concrete.
