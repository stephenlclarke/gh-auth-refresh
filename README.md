# gh-auth-refresh

Headlessly mint and install a short-lived GitHub App installation token for GitHub CLI and HTTPS Git operations.

The utility uses a GitHub App's private key to request a new installation token, then atomically writes it to `~/.secrets/GITHUB_TOKEN` with owner-only permissions. It never prints the token. It uses Python and runs on macOS and Linux.

## Before the first refresh

You will need a GitHub App that you own, installed on the account or repositories it should access, and its generated private-key PEM file. GitHub requires the owner to confirm App creation and installation once. The numeric App ID and installation ID are discovered automatically from the App slug and key; you do not need to look up or guess those numbers.

## Install

Clone the repository and run the installer:

```sh
git clone https://github.com/stephenlclarke/gh-auth-refresh.git
cd gh-auth-refresh
./install.sh
```

The installer creates an isolated Python virtual environment under `~/.local/share/gh-auth-refresh`, installs the command and its dependencies, and links `gh-auth-refresh` into `~/bin`. Set `GH_AUTH_REFRESH_BIN_DIR` before running the installer to choose another bin directory.

## One-time GitHub App setup

GitHub requires the account owner to create and install an app and grant its permissions. That approval cannot be automated away. After setup, token refreshes run without a browser.

1. Create a GitHub App under [Settings → Developer settings → GitHub Apps](https://github.com/settings/apps/new). GitHub requires the account owner to confirm this one-time app registration and installation.
2. Grant only the permissions you need. For GitHub issues and Git over HTTPS, `Issues: write` and `Contents: write` are a typical starting point. Install the app on the repositories it should access.
3. Generate a private key for the app and save it somewhere private, for example `~/.secrets/gh-auth-refresh-app.pem`.
4. Copy the **App slug** from the settings page URL (`https://github.com/settings/apps/APP_SLUG`) and configure it with the private key:

```sh
gh-auth-refresh configure \
  --app-slug APP_SLUG \
  --installation-account YOUR_GITHUB_LOGIN \
  --private-key ~/.secrets/gh-auth-refresh-app.pem
```

Replace `APP_SLUG` with the part after `/settings/apps/` and `YOUR_GITHUB_LOGIN` with the account where you installed the app. The command discovers the numeric App ID and installation ID through GitHub's [App lookup](https://docs.github.com/en/rest/apps/apps#get-an-app) and [installation listing](https://docs.github.com/en/rest/apps/apps#list-installations-for-the-authenticated-app) endpoints, then saves them in `~/.config/gh-auth-refresh/config.json` with owner-only permissions. If the app is installed only once, `--installation-account` can be omitted. If automatic App lookup is unavailable, use `--app-id` and `--installation-id` with the numbers shown in the app settings and installation URL.

The configure command itself is headless. The default requested token permissions are `contents:write` and `issues:write`; use `--permissions '{"issues":"write"}'` to request a narrower token when you do not need Git access. The private-key file is restricted to owner access.

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
