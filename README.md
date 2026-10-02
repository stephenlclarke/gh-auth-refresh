# gh-auth-refresh

Headlessly mint and install a short-lived GitHub App installation token for GitHub CLI and HTTPS Git operations.

The utility uses a GitHub App's private key to request a new installation token, then atomically writes it to `~/.secrets/GITHUB_TOKEN` with owner-only permissions. It never prints the token. It uses Python and runs on macOS and Linux.

## Install

Clone the repository and run the installer:

```sh
git clone https://github.com/stephenlclarke/gh-auth-refresh.git
cd gh-auth-refresh
./install.sh
```

The installer creates an isolated Python virtual environment under `~/.local/share/gh-auth-refresh`, installs the command and its dependencies, and links `gh-auth-refresh` into `~/bin`. Set `GH_AUTH_REFRESH_BIN_DIR` before running the installer to choose another bin directory.

## Set up the GitHub App once

Run the guided setup:

```sh
gh-auth-refresh setup
```

The command prepares a private GitHub App registration with the required settings, then opens GitHub for your approval. After registration, it opens the installation page; choose the account and repositories the App may access and approve installation. The command then detects the App and installation IDs, saves the generated private key to `~/.secrets/gh-auth-refresh-app.pem`, writes the configuration file, and mints the first token. You do not need to enter App details, find an App slug, or copy numeric IDs.

Setup uses the browser only for these two GitHub approvals. Refreshing tokens and using Git afterward do not open a browser.

GitHub requires your approval for registration and installation. The setup command creates a private App owned by your account, with a unique name and `Contents: write` and `Issues: write` permissions. During installation, limit it to the repositories where you need Git or issue access. The generated PEM and configuration are saved with owner-only permissions. Use `--permissions '{"issues":"write"}'` if the App only needs to create and update issues.

This is why setup differs from installing Codex or SonarQube: those publishers register and operate their Apps centrally, while this local utility needs its own private key to mint tokens on your laptop. The key is returned by GitHub directly to the local setup process; it is not sent to this repository or a hosted service.

If setup cannot detect the installation before timing out, it keeps the generated key and prints a `configure` command to finish later. To use an App you already own, `gh-auth-refresh configure` remains available; it discovers the numeric IDs from the App slug and private key.

## Refresh

Run this before GitHub work:

```sh
gh-auth-refresh
```

It requests a fresh installation token and atomically replaces `~/.secrets/GITHUB_TOKEN`. The token is short-lived (typically one hour). If your shell or Codex was already running with an older `GITHUB_TOKEN`, start a new shell or restart Codex so it loads the updated file. A child process cannot change its parent process's environment.

The App must be installed on the target repository and have the permissions required by the operation. GitHub Apps cannot act on repositories where they are not installed.

## Configuration

The command also accepts `--config PATH` before its subcommand to use another config file. The token output path can be changed with `--token-file PATH` during configuration.

All token material stays local. Do not commit the App private key, generated token, or config file. The repository's `.gitignore` excludes local config, private keys, virtual environments, and build output.

## License

MIT. See [LICENSE](LICENSE).
