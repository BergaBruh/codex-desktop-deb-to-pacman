# ChatGPT Debian-to-Pacman Package Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reviewable `chatgpt-bin` x86_64 Pacman package from the pinned official ChatGPT Debian data payload without executing Debian maintainer scripts or changing the upstream application.

**Architecture:** `PKGBUILD` owns the immutable source URL, version, checksum, package metadata, and Pacman staging. A small Python standard-library helper inspects the `.deb` archive, stages only declared runtime payload paths, discards one named Debian lintian override, and rejects every other unsafe or unallowlisted member before writing `$pkgdir`. Separate review and trusted-update commands derive source metadata from a local `.deb`; the latter is read-only by default and can mutate pinned package metadata only behind explicit expected-value guards. Tests use synthetic Debian archives and no-network fixtures, and enforce static package-contract invariants without downloading or launching ChatGPT.

**Tech Stack:** Bash/PKGBUILD and makepkg, Python 3 standard library, `ar` from `binutils`, `bsdtar` from `libarchive`, `makepkg`, `devtools`, `namcap`, `desktop-file-utils`, Pacman, and an x86_64 Arch Linux test environment.

**Spec:** `docs/superpowers/specs/2026-08-15-chatgpt-deb-to-pacman-design.md`

## Global Constraints

- Ship exactly one binary package: `chatgpt-bin` version `26.810.52044-1` for `x86_64`.
- Use exactly `https://persistent.oaistatic.com/codex-app-prod/linux/deb/latest/chatgpt_amd64.deb` and SHA-256 `708a15a1bb76e2bb7f0e376e5145391fa277ad3a64057c1d32537bdc2a1b4e6e` for this release; never use `SKIP` or a rolling `pkgver`.
- Extract only `data.tar.*`; never execute, install, translate, or copy `control.tar.*` or any Debian maintainer script.
- Stage only `/usr/lib/chatgpt/**`, `/usr/bin/chatgpt`, `/usr/share/applications/chatgpt.desktop`, `/usr/share/doc/chatgpt/copyright`, `/usr/share/pixmaps/chatgpt.png`, and `/etc/apparmor.d/chatgpt`; add only the copied Arch license notice under `/usr/share/licenses/chatgpt-bin/`. Accept but discard exactly `usr/share/lintian/overrides/chatgpt` as non-runtime Debian metadata; reject every other unallowlisted member.
- Reject absolute or traversal member names, duplicate members, setuid/setgid modes, `security.capability` xattrs, disallowed member types, and links that escape the permitted payload; reject the entire payload before staging any member.
- Preserve accepted upstream bytes, modes, timestamps, symlinks, desktop entry, pixmap, and AppArmor profile unchanged. Do not run `strip`, `upx`, `patchelf`, application code, or `chmod u+s`; do not create a `.INSTALL` hook or package-manager configuration.
- Mark `etc/apparmor.d/chatgpt` as a Pacman backup file. It remains upstream `flags=(unconfined)` compatibility policy, is not loaded by this package, and has no service or kernel-policy side effect.
- Declare only the reviewed native dependency providers: `alsa-lib at-spi2-core cairo cups dbus expat gcc-libs gdk-pixbuf2 glib2 glibc gtk3 libdrm libnotify libx11 libxcb libxcomposite libxdamage libxext libxfixes libxkbcommon libxrandr mesa nspr nss pango systemd-libs xdg-utils`; use `apparmor` only as an optional dependency.
- Every update is a manual binary-security review: download to a fresh cache, inspect the Debian control version, hash, data allowlist, desktop entry, AppArmor profile, ELF and native `.node` requirements, and licenses; then change `pkgver`, reset `pkgrel=1`, and change the checksum together.
- `scripts/update_source.py --deb PATH` is read-only and makes no network request. It may rewrite `PKGBUILD` `pkgver`, `pkgrel`, checksum, and `SOURCE_DATE_EPOCH` and regenerate `.SRCINFO` only with `--apply --expected-version VERSION --expected-sha256 HASH`, where both expected values exactly match the locally reviewed artifact.

---

## Planned File Structure

- `PKGBUILD` — immutable source declaration, Arch metadata, exact dependency arrays, safe helper invocation, untouched upstream staging, AppArmor backup handling, and license copy.
- `.SRCINFO` — generated metadata committed from `makepkg --printsrcinfo`; never manually edited.
- `scripts/deb_payload.py` — Python command-line helper and importable validation interface for inspecting `.deb` members and extracting the approved data payload only after a complete preflight.
- `scripts/review_source.py` — read-only release-review command that reads control metadata and calls `deb_payload.py` to report the accepted package version, architecture, SHA-256, allowed file manifest, and deterministic source-date epoch; it never edits `PKGBUILD`.
- `scripts/update_source.py` — trusted, no-network source-metadata editor that defaults to the same read-only review output and rewrites only reviewed `PKGBUILD` metadata and generated `.SRCINFO` when all explicit apply guards match.
- `tests/test_deb_payload.py` — synthetic archive tests for accepted and rejected data payloads, including no-partial-stage assertions.
- `tests/test_review_source.py` — synthetic control/archive metadata tests for the review interface and version/architecture checks.
- `tests/test_update_source.py` — no-network fixture tests for the trusted-update command's read-only default, required guards, mismatch rejection, and exact mutation path.
- `tests/test_pkgbuild_contract.py` — static tests for required `PKGBUILD`, `.SRCINFO`, desktop/AppArmor, source-pinning, dependency, and forbidden-hook/command contracts.
- `README.md` — build prerequisites, immutable-source verification, manual update review, static validation, clean-chroot procedure, and explicitly manual graphical smoke-test/reproducibility boundaries.

### Task 1: Source metadata and manual update-review interface

**Files:**

- Create: `scripts/deb_payload.py`
- Create: `scripts/review_source.py`
- Create: `tests/test_review_source.py`

**Interfaces:**

- Produces: `read_deb_control(deb_path: pathlib.Path) -> dict[str, str]`, which returns Debian control fields from `control.tar.*` without extracting it to disk or executing scripts.
- Produces: `inspect_payload(deb_path: pathlib.Path) -> list[PayloadMember]`, which reads `data.tar.*`, returns its validated member metadata without staging it, accepts but marks only `usr/share/lintian/overrides/chatgpt` as discarded, and rejects every other unsafe or unallowlisted member.
- Produces: `review_source(deb_path: pathlib.Path) -> dict[str, object]`, which returns `sha256`, `debian_version`, `architecture`, `allowed_members`, and `source_date_epoch`.
- Produces: `python scripts/review_source.py --deb PATH --format json`, which exits `0` only for a syntactically valid `.deb` whose control `Package` is `chatgpt`, whose `Architecture` is `amd64`, and whose data payload passes `deb_payload.py` preflight.
- Consumes: `inspect_payload(deb_path: pathlib.Path) -> list[PayloadMember]` from this task; `PayloadMember` has `path: str`, `kind: str`, `mode: int`, `link_target: str | None`, `mtime: int`, and `discarded: bool`.

- [ ] **Step 1: Write failing source-review tests with a synthetic `.deb` fixture**

  Build the fixture in `tests/test_review_source.py` from a minimal `ar` archive containing `debian-binary`, `control.tar.gz`, and `data.tar.gz`. Assert that a control file with `Package: chatgpt`, `Version: 26.810.52044`, and `Architecture: amd64` yields exactly those fields, a lowercase SHA-256 of the fixture bytes, an integer `source_date_epoch` equal to the maximum accepted data-member mtime, and a sorted manifest. Add separate assertions that `Architecture: arm64`, a wrong package name, missing `control.tar.*`, and duplicate control archives raise `ValueError` and write nothing outside `tmp_path`.

- [ ] **Step 2: Run the new tests and confirm the interface is absent**

  Run: `python -m unittest tests.test_review_source -v`

  Expected: failure because `scripts.review_source` and `scripts.deb_payload` do not yet exist.

- [ ] **Step 3: Implement read-only metadata review**

  Implement `read_deb_control()` with `ar` listing/extraction and a temporary directory created by `tempfile.TemporaryDirectory`; permit exactly one `control.tar.*`, read only its `./control` regular file, parse Debian control continuations, and reject malformed or duplicate required fields. In `scripts/deb_payload.py`, define `PayloadMember` and a read-only `inspect_payload()` that identifies exactly one `data.tar.*`, normalizes every path, permits only regular files, directories, and symlinks in the approved locations, marks exactly `usr/share/lintian/overrides/chatgpt` as `discarded=True`, and rejects all other disallowed or unsafe members before returning metadata. Implement `review_source()` so it computes `hashlib.sha256` from the original `.deb` bytes, verifies `Package == "chatgpt"` and `Architecture == "amd64"`, calls `inspect_payload()`, sorts member paths, and sets `source_date_epoch` to `max(member.mtime for member in members if not member.discarded)`. The CLI serializes that dictionary with `json.dumps(..., sort_keys=True)` and makes no filesystem mutation except its automatic temporary-directory cleanup.

- [ ] **Step 4: Run the tests and exercise the CLI output**

  Run: `python -m unittest tests.test_review_source -v`

  Expected: all tests pass, including rejected package/architecture/control cases and deterministic metadata output.

  Run: `python scripts/review_source.py --help`

  Expected: exit `0` and describe the required `--deb` argument and JSON output; it must not offer an option that updates package files.

- [ ] **Step 5: Commit the review interface and its tests**

  ```bash
  git add scripts/deb_payload.py scripts/review_source.py tests/test_review_source.py
  git commit -m "feat: add read-only Debian source review"
  ```

### Task 2: Guarded trusted source update

**Files:**

- Create: `scripts/update_source.py`
- Create: `tests/test_update_source.py`

**Interfaces:**

- Consumes: `review_source(deb_path: pathlib.Path) -> dict[str, object]` from
  Task 1 and the repository-root `PKGBUILD` and `.SRCINFO` files.
- Produces: `update_source(deb_path: pathlib.Path, repo_root: pathlib.Path,
  *, apply: bool = False, expected_version: str | None = None,
  expected_sha256: str | None = None) -> dict[str, object]`.
- Produces: `python scripts/update_source.py --deb PATH [--repo-root PATH]
  [--apply --expected-version VERSION --expected-sha256 HASH]`. Without
  `--apply`, it prints sorted JSON review metadata and leaves every repository
  file unchanged. With `--apply`, both expected flags are mandatory and must
  match the local artifact's Debian control version and lowercase SHA-256
  exactly before the first write.

- [ ] **Step 1: Write failing no-network trusted-update tests with local fixtures**

  In `tests/test_update_source.py`, build each `.deb` fixture locally from
  `debian-binary`, `control.tar.gz`, and `data.tar.gz` under `tmp_path`; do not
  use a URL, subprocess downloader, or a real ChatGPT artifact. Give the data
  fixture an allowlisted member and the exact discarded
  `usr/share/lintian/overrides/chatgpt` member. Create a disposable repository
  fixture containing a `PKGBUILD` with distinct `pkgver`, `pkgrel`,
  `sha256sums`, and `SOURCE_DATE_EPOCH` assignments and a stale `.SRCINFO`.
  Put a fake `makepkg` executable first in `PATH`; it accepts only
  `--printsrcinfo`, emits fixed fixture `.SRCINFO` text, and records each
  invocation.

  Patch `urllib.request.urlopen`, `socket.create_connection`, and
  `http.client.HTTPConnection.connect` to raise `AssertionError("network is
  forbidden")`. Assert all of the following using only that fixture:

  ```python
  result = update_source(deb, repo_root)
  assert result["debian_version"] == "26.810.52044"
  assert pkgbuild.read_bytes() == before_pkgbuild
  assert srcinfo.read_bytes() == before_srcinfo
  assert fake_makepkg_calls == []
  ```

  Add separate tests that `--apply` without either expected flag, with only one
  expected flag, or with a mismatched version or SHA-256 raises `ValueError`
  and preserves both original byte strings. Add a successful apply test using
  the exact fixture version and computed lowercase SHA-256: it asserts that
  only `pkgver`, `pkgrel=1`, `sha256sums`, and `SOURCE_DATE_EPOCH` change in
  `PKGBUILD`, that `.SRCINFO` equals the fake `makepkg --printsrcinfo` output,
  that the epoch equals the reviewed maximum accepted-member mtime, and that
  the fake tool was called exactly once with `--printsrcinfo`. Every test must
  assert that none of the patched network functions was called.

- [ ] **Step 2: Run the update tests and confirm the interface is absent**

  Run: `python -m unittest tests.test_update_source -v`

  Expected: failure because `scripts.update_source` does not yet exist.

- [ ] **Step 3: Implement the guarded local editor**

  Implement `update_source()` by calling Task 1's `review_source()` on the
  supplied local path; do not import or invoke `urllib`, `requests`, `curl`,
  `wget`, or a package download command. In read-only mode, serialize the
  result with `json.dumps(..., sort_keys=True)` and do not call `makepkg`.
  Refuse `apply=True` unless both expected values are nonempty, the expected
  version equals `debian_version`, and the expected SHA-256 equals `sha256`.
  Before any write, require exactly one editable assignment each for `pkgver`,
  `pkgrel`, `sha256sums`, and `SOURCE_DATE_EPOCH` in `PKGBUILD`; reject any
  unexpected layout and leave `PKGBUILD` and `.SRCINFO` unchanged. On a valid
  guarded apply, replace only those assignments, force `pkgrel=1`, set the
  reviewed epoch, run `makepkg --printsrcinfo` from `repo_root`, and atomically
  replace `.SRCINFO` with its stdout. Do not alter `source`, dependencies, or
  any other file.

- [ ] **Step 4: Run the no-network update suite**

  Run: `python -m unittest tests.test_update_source -v`

  Expected: all read-only, missing-guard, mismatch, and successful-apply
  fixture tests pass with no network call and no real `makepkg` invocation.

- [ ] **Step 5: Commit the guarded update command and tests**

  ```bash
  git add scripts/update_source.py tests/test_update_source.py
  git commit -m "feat: add guarded ChatGPT source update"
  ```

### Task 3: Safe data-only extraction and PKGBUILD

**Files:**

- Modify: `scripts/deb_payload.py`
- Create: `PKGBUILD`
- Create: `tests/test_deb_payload.py`

**Interfaces:**

- Consumes: `inspect_payload(deb_path: pathlib.Path) -> list[PayloadMember]` from Task 1 and produces `stage_payload(deb_path: pathlib.Path, destination: pathlib.Path) -> list[PayloadMember]`.
- `stage_payload()` re-runs the complete Task 1 preflight before creating the destination, then writes only non-discarded accepted `data.tar.*` members; `control.tar.*` and the exact discarded Lintian member are never staged.
- Produces: `python scripts/deb_payload.py --deb PATH --destination PATH`, which exits nonzero and leaves `PATH` absent or empty for every rejected archive.
- Consumes: `review_source()` from Task 1 and is called by `PKGBUILD` as `python "$startdir/scripts/deb_payload.py" --deb "$srcdir/chatgpt_amd64.deb" --destination "$pkgdir"`.

- [ ] **Step 1: Write failing data-only extraction tests**

  In `tests/test_deb_payload.py`, create synthetic `.deb` files with a valid `data.tar.gz`. Assert that staging preserves a regular file, directory mode, and the exact `/usr/bin/chatgpt` symlink; permits the seven exact upstream paths and recursive `/usr/lib/chatgpt/**`; accepts but does not stage exactly `usr/share/lintian/overrides/chatgpt`; and does not stage a `control.tar.*` member. Add one test each for an absolute member name, `../` traversal, a path outside the allowlist other than that one exact Lintian path, duplicate data member, setuid mode `0o4755`, setgid mode `0o2755`, `SCHILY.xattr.security.capability`, a FIFO/device/hard link, and a symlink whose resolved target escapes its permitted subtree. Each rejection must leave the requested destination absent or empty.

- [ ] **Step 2: Run extraction tests and confirm they fail**

  Run: `python -m unittest tests.test_deb_payload -v`

  Expected: failure because `stage_payload()` is not implemented.

- [ ] **Step 3: Implement complete preflight and safe staging**

  Complete the Task 1 preflight so it rejects absolute paths, `..` components, duplicate paths, mode bits `0o6000`, PAX/xattr keys ending in `security.capability`, hard links and special files, and links that do not resolve within `/usr/lib/chatgpt` or the exact `/usr/bin/chatgpt -> /usr/lib/chatgpt/codex-launcher` launcher link. Then implement `stage_payload()` with `ar` and `bsdtar` only; never use `dpkg`, `apt`, shell hooks, or archive `--to-command` processing. It must re-run the complete preflight, refuse a pre-existing nonempty destination, create no destination before the full member list passes validation, and stage only non-discarded members. Preserve every staged member's bytes, modes, mtimes, and symlink target unchanged.

- [ ] **Step 4: Add the minimal immutable PKGBUILD after the helper passes**

  Create `PKGBUILD` with `pkgname=chatgpt-bin`, `pkgver=26.810.52044`, `pkgrel=1`, `arch=('x86_64')`, `license=('custom')`, the exact `source` URL/name and SHA-256 from Global Constraints, `makedepends=('python' 'binutils' 'libarchive')`, `options=(!strip)`, the required `depends` list, `optdepends=('apparmor: optional support for loading the upstream AppArmor profile')`, and `backup=('etc/apparmor.d/chatgpt')`. Bootstrap `SOURCE_DATE_EPOCH=0` only long enough to run `makepkg --verifysource` and `python scripts/review_source.py --deb "${SRCDEST:-$PWD}/chatgpt_amd64.deb" --format json`; before committing the PKGBUILD, replace `0` with its reported `source_date_epoch`. Task 2 updates that same field alongside `pkgver`, `pkgrel=1`, and the checksum for each reviewed release. Do not define `build()`, `prepare()`, or `package()` behavior that runs application code. In `package()`, call `stage_payload()` once into `$pkgdir`, verify the expected copyright source exists, and copy it with `install -Dm644` to `/usr/share/licenses/chatgpt-bin/copyright`. Do not add `.INSTALL`, `provides`, `conflicts`, a wrapper, a generated icon, source-list/keyring files, or a sandbox helper.

- [ ] **Step 5: Run unit and PKGBUILD syntax checks**

  Run: `python -m unittest tests.test_deb_payload tests.test_review_source -v && bash -n PKGBUILD`

  Expected: all archive-safety tests pass; `bash -n` exits `0` without downloading a source or creating a package.

- [ ] **Step 6: Commit safe staging and package metadata**

  ```bash
  git add PKGBUILD scripts/deb_payload.py tests/test_deb_payload.py
  git commit -m "feat: package verified ChatGPT Debian payload"
  ```

### Task 4: Static contract validation, source metadata, and release documentation

**Files:**

- Create: `tests/test_pkgbuild_contract.py`
- Create: `.SRCINFO`
- Create: `README.md`

**Interfaces:**

- Consumes: the exact `PKGBUILD` fields and helper CLIs from Tasks 1-3.
- Produces: committed `.SRCINFO` generated only by `makepkg --printsrcinfo`.
- Produces: a reproducible static gate: `python -m unittest discover -s tests -v && makepkg --printsrcinfo | diff -u .SRCINFO -`.

- [ ] **Step 1: Write failing static contract tests**

  In `tests/test_pkgbuild_contract.py`, read files as text and assert the exact pinned URL/hash, package name/version/release/architecture, `options=(!strip)`, AppArmor backup path, all required dependency names, and the `apparmor` optdepend. Assert the absence of `SKIP`, `.INSTALL`, `post_install`, `apparmor_parser`, `apt`, `/etc/apt`, `keyring`, `chmod u+s`, `patchelf`, `upx`, `strip`, and a shell-wrapper `install -Dm755` for `/usr/bin/chatgpt`. Add static fixture checks that the planned desktop path is untouched, the pixmap path matches `Icon=chatgpt`, and a generated `.SRCINFO` parses as the same `pkgbase`, `pkgver`, `pkgrel`, and `arch` as `PKGBUILD`.

- [ ] **Step 2: Run the static tests and capture the first failure**

  Run: `python -m unittest tests.test_pkgbuild_contract -v`

  Expected: failure until `.SRCINFO` and `README.md` exist and the contract reflects the final Task 3 metadata.

- [ ] **Step 3: Generate source metadata and write the operator-facing README**

  Generate metadata exactly once with `makepkg --printsrcinfo > .SRCINFO`; inspect the resulting diff before adding it. In `README.md`, document prerequisites (`base-devel`, `python`, `binutils`, `libarchive`, `devtools`, `namcap`, and `desktop-file-utils`); `makepkg --verifysource`; the deliberate read-only review command `python scripts/review_source.py --deb /fresh/cache/chatgpt_amd64.deb --format json`; the guarded apply command `python scripts/update_source.py --deb /fresh/cache/chatgpt_amd64.deb --apply --expected-version VERSION --expected-sha256 HASH`; the required manual review of version/hash/allowlist/desktop/AppArmor/ELF/native `.node`/licenses; and the rule that `pkgver`, `pkgrel=1`, checksum, and `SOURCE_DATE_EPOCH` change together only after review. State plainly that HTTPS plus a pinned hash is not an upstream signature chain, `apparmor` is not a sandbox, and GUI sign-in testing uses no account or credentials.

- [ ] **Step 4: Add static checks for desktop, ELF, and archive output**

  Add README commands that, after a local package build, run `bsdtar -tf` against the package, `desktop-file-validate usr/share/applications/chatgpt.desktop` after extracting it, `readelf -d` over every executable ELF and `.node` object, and `pacman -Qo` for each observed required SONAME provider. Require review of every `namcap` finding rather than suppression. Include a command that compares `makepkg --printsrcinfo` with the committed `.SRCINFO`; do not make source downloading, ELF scanning, or GUI launching part of unit tests.

- [ ] **Step 5: Run the full offline static gate**

  Run: `python -m unittest discover -s tests -v && bash -n PKGBUILD && makepkg --printsrcinfo | diff -u .SRCINFO -`

  Expected: all tests pass, PKGBUILD syntax is valid, and `diff` emits no output with exit `0`; no network transfer, `.deb` extraction, or application execution occurs.

- [ ] **Step 6: Commit generated metadata, validation, and documentation**

  ```bash
  git add .SRCINFO README.md tests/test_pkgbuild_contract.py
  git commit -m "docs: document ChatGPT package validation and updates"
  ```

### Task 5: Clean-Arch build, installation boundary, reproducibility, and final review

**Files:**

- Modify: `README.md`
- Modify: `tests/test_pkgbuild_contract.py` only if a static assertion is needed to prevent a verified review finding from regressing.

**Interfaces:**

- Consumes: a checksum-verified `.deb`, `PKGBUILD`, `.SRCINFO`, and static gate from Tasks 1-3.
- Produces: a documented release checklist with required evidence files: `verifysource.log`, `package-file-list.txt`, `elf-needed.txt`, `namcap.txt`, `pacman-install-remove.log`, `reproducibility-diff.txt`, and `gui-smoke-test.md`.
- Produces: no automation that creates APT configuration, loads/unloads AppArmor, or launches the application during package build.

- [ ] **Step 1: Record the clean-chroot test commands before executing them**

  Add a README release section that instructs the operator to place the verified `.deb` in a fresh source cache, run `makepkg --verifysource`, then run `SOURCE_DATE_EPOCH="$(python scripts/review_source.py --deb /fresh/cache/chatgpt_amd64.deb --format json | jq -r .source_date_epoch)" extra-x86_64-build -- -I /fresh/cache/chatgpt_amd64.deb`. Explain that `SOURCE_DATE_EPOCH` is the deterministic maximum reviewed data-member mtime from Task 1 and must be identical for both builds. Keep this release-only command outside automated tests because it needs Arch chroot tooling, the reviewed binary, and package dependencies.

- [ ] **Step 2: Run source verification and build in two independent clean x86_64 chroots**

  Run: `makepkg --verifysource`

  Expected: exit `0` only for the exact pinned 391801406-byte artifact and SHA-256; replacing one byte in an isolated copy fails with a checksum-mismatch error before extraction.

  Run: `SOURCE_DATE_EPOCH="$(python scripts/review_source.py --deb /fresh/cache/chatgpt_amd64.deb --format json | jq -r .source_date_epoch)" extra-x86_64-build -- -I /fresh/cache/chatgpt_amd64.deb`

  Expected: an x86_64 `chatgpt-bin-26.810.52044-1-x86_64.pkg.tar.*` built with no application execution, no package-manager configuration, and no maintainer-script processing. Repeat in a second clean chroot with the same cached `.deb`, tool version, and epoch.

- [ ] **Step 3: Inspect package content and dependency evidence before installation**

  Run: `pacman -Qip chatgpt-bin-26.810.52044-1-x86_64.pkg.tar.* && pacman -Qlp chatgpt-bin-26.810.52044-1-x86_64.pkg.tar.* | tee package-file-list.txt`

  Expected: `chatgpt-bin`, `26.810.52044-1`, `x86_64`; only the allowlisted upstream payload plus `/usr/share/licenses/chatgpt-bin/copyright`; no `/etc/apt`, keyring, maintainer script, setuid/setgid file, or file capability.

  Run: `namcap chatgpt-bin-26.810.52044-1-x86_64.pkg.tar.* | tee namcap.txt`

  Expected: findings are recorded and individually reconciled with the `readelf`/`pacman -Qo` SONAME evidence; none is blindly ignored.

- [ ] **Step 4: Perform the isolated Pacman and graphical smoke-test boundary**

  In a disposable x86_64 Arch environment, run `sudo pacman -U ./chatgpt-bin-26.810.52044-1-x86_64.pkg.tar.*`, then `pacman -Qkk chatgpt-bin`, `readlink -f /usr/bin/chatgpt`, and `desktop-file-validate /usr/share/applications/chatgpt.desktop`. Confirm the launcher resolves to the upstream `/usr/lib/chatgpt/codex-launcher`, `Icon=chatgpt` resolves to `/usr/share/pixmaps/chatgpt.png`, the desktop validator exits `0`, and no APT source/keyring or AppArmor load/unload action occurred. Remove with `sudo pacman -Rns chatgpt-bin` and record that only package files and Pacman's normal backed-up-profile handling changed.

  In a logged-in graphical session as an unprivileged user, run `chatgpt` and verify only that the sign-in window opens; do not enter credentials or a real account. When Wayland is available, separately run `chatgpt --enable-features=UseOzonePlatform --ozone-platform=wayland` and record whether the same window appears. This is a manual smoke test, not a CI/test-suite command.

- [ ] **Step 5: Compare both builds and complete final package review**

  Run: `diffoscope --text repro-diff.txt first/chatgpt-bin-26.810.52044-1-x86_64.pkg.tar.* second/chatgpt-bin-26.810.52044-1-x86_64.pkg.tar.*`

  Expected: no payload or package-metadata difference. If signed archives or package-tool metadata differ at archive level, extract unsigned normalized package payload and metadata for comparison; any remaining difference is a release blocker recorded in `reproducibility-diff.txt`.

  Re-run the Task 4 static gate, confirm the evidence files listed in this task exist, and check the final `git diff --check` and `git status --short` before review. Do not publish, sign, upload, or create a repository as part of this plan.

- [ ] **Step 6: Commit only documented release-boundary refinements**

  ```bash
  git add README.md tests/test_pkgbuild_contract.py
  git commit -m "docs: add clean Arch release verification"
  ```

## Plan Self-Review

- **Spec coverage:** Task 1 covers immutable-source metadata and read-only source review; Task 2 covers guarded source metadata updates; Task 3 covers data-only extraction, the exact allowlist and single discarded Lintian member, no side effects, PKGBUILD metadata, upstream desktop/icon/profile preservation, license copy, and dependency declaration; Task 4 covers `.SRCINFO`, static validation, desktop/ELF/dependency checks, and README instructions; Task 5 covers checksum behavior, clean Arch builds, install/removal side-effect checks, graphical Wayland/X11 boundary, reproducibility, `namcap`, and final package review. No design requirement is unassigned.
- **Placeholder scan:** Passed. The plan contains no `TBD`, `TODO`, deferred implementation marker, unspecified file, or generic validation instruction; release-only paths such as `/fresh/cache/chatgpt_amd64.deb` are literal operator-selected cache locations, not missing implementation values.
- **Type consistency:** Passed. Tasks 1-3 consistently use `pathlib.Path`, `PayloadMember`, `inspect_payload()`, `stage_payload()`, `review_source()`, and `update_source()`; Tasks 4-5 consume their documented CLI formats without inventing another interface.
- **Scope check:** Passed. The four tasks yield one independently testable binary-package workflow and do not add an AUR upload, repository, signing, ARM/RPM packaging, source build, or application behavior change.
