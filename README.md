# chatgpt-bin for Arch Linux

An unofficial Arch Linux `x86_64` package for the ChatGPT desktop app. It takes
the verified payload from OpenAI's official Debian artifact and installs it
through Pacman. This is not an OpenAI package, an AUR recipe supported by
OpenAI, or a guarantee that the client itself can update on Linux.

The package does not add an APT repository or key, run Debian maintainer
scripts, or load/unload an AppArmor profile. Updates use a separate
user-controlled mechanism: it checks a new Debian artifact, builds a new Pacman
package, and offers to install it.

## Requirements

- Arch Linux `x86_64`;
- build tools: `base-devel`, `python`, `binutils`, `libarchive`;
- an active per-user systemd session for automatic checks;
- optional: `libnotify` for the **Install** button in the notification. Without
  it, the update is reported only in the journal/standard output.

```sh
sudo pacman -S --needed base-devel python binutils libarchive
```

## First build and installation

Clone this repository, then run the following from its root:

```sh
makepkg --verifysource
makepkg -si
```

`makepkg --verifysource` checks the Debian file against the pinned SHA-256
checksum. Do not replace the checksum with `SKIP`.

Before installation, remove any other Pacman package that already owns ChatGPT
files (for example, `/usr/bin/chatgpt` or `/usr/lib/chatgpt`). Pacman will stop
on a conflict; read its output and remove only the package that actually causes
the conflict. Do not bypass the conflict with `--overwrite`.

You can verify the installed package and launcher with:

```sh
pacman -Q chatgpt-bin
pacman -Qkk chatgpt-bin
readlink -f /usr/bin/chatgpt
chatgpt
```

## Automatic updates

Enable the per-user timer after installation:

```sh
systemctl --user enable --now chatgpt-bin-update-check.timer
systemctl --user list-timers chatgpt-bin-update-check.timer
```

The timer runs daily with a randomized delay of up to one hour and starts
`chatgpt-bin-update-workflow.service`, not just the check service. The flow is:

1. A candidate is checked and validated locally.
2. If a new version is found, the package is built as the current user in a
   private cache.
3. A notification with an **Install** button appears.
4. Only clicking **Install** invokes Polkit (`pkexec`) to install the verified
   ready package through Pacman.

Neither the timer nor the build runs an automatic root installation. If
notifications are unavailable, the button is not clicked, or a different
action is selected, the package is not installed: run
`chatgpt-bin-update install` yourself when you are ready to authenticate with
Polkit.

Installation is possible only after clicking the **Install** notification
action (`only after clicking the Install notification action`).

If there is no saved candidate or source validator, the check performs a normal
HTTP GET. ETag or Last-Modified is saved only after a new candidate is accepted
and written, or when the downloaded candidate matches the one already saved. A
conditional HEAD request and `304 Not Modified` response are used only when
both a saved candidate and its validator exist; in that case the `.deb` is not
downloaded again. An unsupported HEAD request or a preflight error falls back
to a normal GET.

## Manual control

Run all commands below as a regular user:

```sh
# Check the source; exit code 10 means that a new candidate was found.
chatgpt-bin-update check

# Show the saved, verified candidate.
chatgpt-bin-update status

# Build the saved candidate without sudo and without installing it.
chatgpt-bin-update build

# Install a compatible package that has already been built; Polkit opens.
chatgpt-bin-update install

# Run check -> build -> notification with Install in one invocation.
chatgpt-bin-update workflow
```

Timer/service status and logs:

```sh
systemctl --user status chatgpt-bin-update-check.timer
systemctl --user status chatgpt-bin-update-workflow.service
journalctl --user -u chatgpt-bin-update-workflow.service --since today
journalctl --user -u chatgpt-bin-update-check.service --since today
```

## Troubleshooting

### `403`, DNS, or download errors

This usually means an unavailable CDN, DNS, captive portal, proxy, or network.
Check connectivity and DNS, then retry later:

```sh
getent hosts persistent.oaistatic.com
chatgpt-bin-update check
```

Do not disable SHA-256 verification or replace the URL with an arbitrary mirror.

### `no-compatible-current-build` or “no compatible current build”

The recorded build report is intentionally tied to the currently installed
recipe and the hash of the ready archive. After the wrapper package is updated,
an old report may be rejected. Build again, then install:

```sh
chatgpt-bin-update build
chatgpt-bin-update install
```

### An old report or missing archive

Do not edit the JSON report or try to install the archive manually. Run
`chatgpt-bin-update build`; it creates a new archive and report in the user
cache. Installation accepts only a matching report/archive pair.

### “A ready package already exists”

The current wrapper uses `makepkg -f` and isolates the old expected archive
before starting, so a newly built package should not be mistaken for the old
one. If this message comes from an earlier installation, update the wrapper
package using the instructions below and run `chatgpt-bin-update build` again.

### `usr/src/debug` appears in the archive or an error

The current recipe explicitly disables the debug package (`options=(!strip
!debug)`). Such an artifact indicates an old recipe, an old build, or an
external `makepkg` configuration. Reinstall the current wrapper, rebuild the
candidate, and do not install a suspicious old archive.

## Updating the wrapper

The wrapper is `chatgpt-bin` itself: along with the app, it installs the update
scripts, recipe, unit files, and Polkit rule. After obtaining a new repository
version, rebuild and reinstall it from the repository root:

```sh
makepkg --verifysource
makepkg -si
systemctl --user daemon-reload
systemctl --user enable --now chatgpt-bin-update-check.timer
```

Then run `chatgpt-bin-update build` again if a build had already been created:
the old report is intentionally incompatible with an updated recipe.

## Recipe checks and development

These commands do not publish the package or change Pacman settings:

```sh
python -m unittest discover -s tests -v
bash -n PKGBUILD
makepkg --printsrcinfo | diff -u .SRCINFO -
git diff --check
```

To inspect an already built archive manually, replace `PACKAGE` with its path:

```sh
pacman -Qip PACKAGE
pacman -Qlp PACKAGE
```

Releasing from a clean Arch environment also requires `devtools`. Pass the
already verified `.deb` through a separate source cache mounted read-only in the
chroot:

```sh
SOURCE_CACHE=/fresh/cache
test -f "$SOURCE_CACHE/chatgpt_amd64.deb"
SRCDEST="$SOURCE_CACHE" makepkg --verifysource
SRCDEST="$SOURCE_CACHE" extra-x86_64-build -D "$SOURCE_CACHE"
```

## Security boundaries

This is a local repackaging, not a trusted OpenAI signature chain for Arch. The
pinned SHA-256 protects the currently verified download from silent
substitution, but it does not replace an independent audit of a new upstream
artifact. The automatic flow does not promise that OpenAI will maintain the
URL, ETag, Debian builds, or Linux client self-updating. Do not run update
commands with `sudo`; root is requested only during the final, explicit
installation step through Polkit.
