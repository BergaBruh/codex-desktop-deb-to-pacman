#!/bin/sh
script_dir=$(CDPATH= cd "$(dirname "$0")" && pwd) || exit 1
repo_dir=$(CDPATH= cd "$script_dir/.." && pwd) || exit 1
cd "$repo_dir" || exit 1

if makepkg -si; then
  systemctl --user enable --now chatgpt-bin-update-check.timer
else
  status=$?
  exit "$status"
fi
