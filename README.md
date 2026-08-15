# chatgpt-bin

An Arch Linux `chatgpt-bin` package made from the checked data payload of
OpenAI's official x86_64 Debian desktop artifact. It preserves the upstream
application, `chatgpt` launcher symlink, desktop entry, pixmap, and AppArmor
profile, but never runs or copies Debian maintainer scripts. In particular, it
does not add an APT source, signing key, or AppArmor load/unload action.

## Build prerequisites

Install `base-devel`, `python`, `binutils`, `libarchive`, `devtools`, `namcap`,
and `desktop-file-utils` in an Arch x86_64 environment. The declared runtime
dependencies are installed by Pacman when the package is installed.

The pinned source is a mutable `latest` URL. The recorded SHA-256 is mandatory:

```sh
makepkg --verifysource
makepkg -s
```

Never replace `sha256sums` with `SKIP`. HTTPS plus a pinned hash protects this
reviewed input from silent replacement, but it is not an upstream signature
chain.

## Review and update a new source

Download a candidate `.deb` into a fresh local cache. The review command does
not fetch data, mutate package files, run application code, or invoke
`makepkg`:

```sh
python scripts/review_source.py --deb /fresh/cache/chatgpt_amd64.deb --format json
```

Before accepting a new artifact, manually inspect its Debian control version,
SHA-256, allowlisted data manifest, desktop file, AppArmor profile, executable
ELF and native `.node` dependencies, and licenses. Review every changed file;
the package deliberately supports only the expected paths and drops Debian
Lintian metadata.

After that human review, the guarded editor can change the four coupled
metadata fields and regenerate `.SRCINFO`:

```sh
python scripts/update_source.py \
  --deb /fresh/cache/chatgpt_amd64.deb \
  --apply \
  --expected-version VERSION \
  --expected-sha256 LOWERCASE_SHA256
```

Run it from the repository root (or pass `--repo-root PATH`). It rejects a
missing or mismatched expected value. Do not update only a subset: `pkgver`,
`pkgrel=1`, `sha256sums`, and `SOURCE_DATE_EPOCH` must change together after
review.

## Offline static gate

```sh
python -m unittest discover -s tests -v
bash -n PKGBUILD
makepkg --printsrcinfo | diff -u .SRCINFO -
```

These checks do not download, extract a production artifact, execute the
application, or alter package-manager configuration.

## Package inspection before installation

After a local build, replace `PACKAGE` with the generated package path and run:

```sh
pacman -Qip PACKAGE
pacman -Qlp PACKAGE | tee package-file-list.txt
mkdir -p package-check
bsdtar -xf PACKAGE -C package-check
desktop-file-validate package-check/usr/share/applications/chatgpt.desktop
find package-check -type f \( -perm -0100 -o -name '*.node' \) -print0 \
  | xargs -0r readelf -d | tee elf-needed.txt
namcap PACKAGE | tee namcap.txt
```

Use `pacman -Qo` on every observed required SONAME provider to reconcile the
declared dependencies. Review every `namcap` finding; do not suppress findings
without evidence. Confirm that `Icon=chatgpt` resolves to the shipped
`/usr/share/pixmaps/chatgpt.png` and that `/usr/bin/chatgpt` resolves to the
upstream `/usr/lib/chatgpt/codex-launcher`.

`apparmor` is optional and the packaged profile is a Pacman backup file only.
It is not a sandbox, and this package does not load it.

## Clean-Arch release boundary

Use a fresh verified source cache and two independent x86_64 clean chroots:

```sh
makepkg --verifysource | tee verifysource.log
SOURCE_DATE_EPOCH="$(python scripts/review_source.py --deb /fresh/cache/chatgpt_amd64.deb --format json | jq -r .source_date_epoch)" \
  extra-x86_64-build -- -I /fresh/cache/chatgpt_amd64.deb
```

Run the same command twice with the same reviewed artifact, tool version, and
epoch. Compare the results with:

```sh
diffoscope --text reproducibility-diff.txt first/PACKAGE second/PACKAGE
```

If archive signatures or tool metadata differ, compare normalized extracted
payload and package metadata; any remaining content difference is a release
blocker. Keep `verifysource.log`, `package-file-list.txt`, `elf-needed.txt`,
`namcap.txt`, `pacman-install-remove.log`, `reproducibility-diff.txt`, and
`gui-smoke-test.md` as release evidence.

In a disposable Arch environment, install only the reviewed package with
`sudo pacman -U PACKAGE`, then run `pacman -Qkk chatgpt-bin`,
`readlink -f /usr/bin/chatgpt`, and `desktop-file-validate` on its installed
desktop file. Remove it with `sudo pacman -Rns chatgpt-bin` and record the
Pacman result. Confirm that no APT configuration, keyring, or AppArmor
load/unload action occurred.

The GUI smoke test is manual and uses no account or credentials: as an
unprivileged user in a graphical session, launch `chatgpt` and confirm only the
sign-in window appears. When Wayland is available, separately record the
result of:

```sh
chatgpt --enable-features=UseOzonePlatform --ozone-platform=wayland
```

Do not publish, sign, upload, or create a package repository as part of these
checks.
