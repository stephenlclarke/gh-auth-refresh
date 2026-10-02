#!/bin/sh
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install_dir="${GH_AUTH_REFRESH_INSTALL_DIR:-$HOME/.local/share/gh-auth-refresh}"
bin_dir="${GH_AUTH_REFRESH_BIN_DIR:-$HOME/bin}"
venv_dir="$install_dir/venv"
command_link="$bin_dir/gh-auth-refresh"

command -v python3 >/dev/null 2>&1 || {
  echo "python3 is required." >&2
  exit 1
}

mkdir -p "$install_dir" "$bin_dir"
python3 -m venv "$venv_dir"
"$venv_dir/bin/python" -m pip install --disable-pip-version-check "$repo_dir"

if [ -e "$command_link" ] && [ ! -L "$command_link" ]; then
  echo "$command_link already exists and is not a symlink; leaving it unchanged." >&2
  exit 1
fi
if [ -L "$command_link" ]; then
  current_target=$(readlink "$command_link")
  case "$current_target" in
    "$venv_dir/bin/gh-auth-refresh") ;;
    *)
      echo "$command_link points to another target; leaving it unchanged." >&2
      exit 1
      ;;
  esac
fi
ln -sfn "$venv_dir/bin/gh-auth-refresh" "$command_link"
chmod 700 "$install_dir"
printf 'Installed gh-auth-refresh at %s\n' "$command_link"
