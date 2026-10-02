#!/bin/sh
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install_dir="${GH_AUTH_REFRESH_INSTALL_DIR:-$HOME/.local/share/gh-auth-refresh}"
bin_dir="${GH_AUTH_REFRESH_BIN_DIR:-$HOME/bin}"
venv_dir="$install_dir/venv"
command_link="$bin_dir/gh-auth-refresh"
gh_wrapper="$bin_dir/gh"
install_gh_wrapper="${GH_AUTH_REFRESH_INSTALL_GH_WRAPPER:-0}"

case "$install_gh_wrapper" in
  0|1) ;;
  *)
    echo "GH_AUTH_REFRESH_INSTALL_GH_WRAPPER must be 0 or 1." >&2
    exit 1
    ;;
esac

if [ "$install_gh_wrapper" = 1 ] && { [ -e "$gh_wrapper" ] || [ -L "$gh_wrapper" ]; }; then
  if [ ! -f "$gh_wrapper" ] || ! grep -q "Managed by gh-auth-refresh" "$gh_wrapper"; then
    echo "$gh_wrapper already exists and is not a gh-auth-refresh wrapper; leaving it unchanged." >&2
    exit 1
  fi
fi

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

if [ "$install_gh_wrapper" = 1 ]; then
  wrapper_tmp=$(mktemp "$bin_dir/.gh-wrapper.XXXXXX")
  cat > "$wrapper_tmp" <<'EOF'
#!/bin/sh
# Managed by gh-auth-refresh. Refresh GitHub credentials before invoking gh.
set -eu
case "$0" in
  /*) GH_AUTH_REFRESH_WRAPPER_PATH=$0 ;;
  */*) GH_AUTH_REFRESH_WRAPPER_PATH=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/$(basename -- "$0") ;;
  *) GH_AUTH_REFRESH_WRAPPER_PATH=$(command -v "$0") ;;
esac
export GH_AUTH_REFRESH_WRAPPER_PATH
exec "$(dirname "$GH_AUTH_REFRESH_WRAPPER_PATH")/gh-auth-refresh" run-gh -- "$@"
EOF
  chmod 755 "$wrapper_tmp"
  mv -f "$wrapper_tmp" "$gh_wrapper"
  printf 'Installed refreshing gh wrapper at %s\n' "$gh_wrapper"

  if command -v git >/dev/null 2>&1; then
    github_helpers=$(git config --global --get-all credential.https://github.com.helper || true)
    case "$github_helpers" in
      *"gh auth git-credential"*)
        git config --global --replace-all credential.https://github.com.helper \
          "!$gh_wrapper auth git-credential" '^.*gh auth git-credential$'
        printf 'Updated the GitHub HTTPS credential helper to use %s\n' "$gh_wrapper"
        ;;
      *)
        printf 'GitHub HTTPS helper was not changed; it does not currently use gh auth git-credential.\n'
        ;;
    esac
  fi
fi
