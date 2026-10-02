"""Command-line interface for refreshing GitHub App installation tokens."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import secrets
import shlex
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
from typing import Any

import jwt

DEFAULT_CONFIG = Path("~/.config/gh-auth-refresh/config.json").expanduser()
DEFAULT_TOKEN_FILE = Path("~/.secrets/GITHUB_TOKEN").expanduser()
DEFAULT_PRIVATE_KEY = Path("~/.secrets/gh-auth-refresh-app.pem").expanduser()
APP_HOME = "https://github.com/stephenlclarke/gh-auth-refresh"
API_VERSION = "2026-03-10"
DEFAULT_PERMISSIONS = {"contents": "write", "issues": "write"}


class RefreshError(Exception):
    """An expected, safe-to-display configuration or GitHub API error."""


def _json_object(value: str, label: str) -> dict[str, str]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RefreshError(f"{label} must be valid JSON.") from exc
    if not isinstance(parsed, dict) or not parsed:
        raise RefreshError(f"{label} must be a non-empty JSON object.")
    for key, permission in parsed.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z_]+", key):
            raise RefreshError(f"{label} contains an invalid permission name.")
        if permission not in ("read", "write"):
            raise RefreshError(f"{label} values must be 'read' or 'write'.")
    return parsed


def _write_private_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent in {
        Path.home() / ".config" / "gh-auth-refresh",
        Path.home() / ".secrets",
    }:
        path.parent.chmod(0o700)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temp_path.unlink(missing_ok=True)
        raise


def _load_config(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RefreshError(
            f"Configuration not found at {path}. Run 'gh-auth-refresh configure' first."
        ) from exc
    except json.JSONDecodeError as exc:
        raise RefreshError(f"Configuration at {path} is not valid JSON.") from exc
    if not isinstance(data, dict):
        raise RefreshError(f"Configuration at {path} must be a JSON object.")
    return data


def configure(args: argparse.Namespace) -> None:
    private_key = Path(args.private_key).expanduser().resolve()
    if not private_key.is_file():
        raise RefreshError(f"Private key file not found: {private_key}")
    private_key.chmod(stat.S_IRUSR | stat.S_IWUSR)

    app_id = str(args.app_id) if args.app_id else _lookup_app_id(args.app_slug)
    if not app_id.isdigit():
        raise RefreshError("App ID must contain digits only.")

    installation_id = str(args.installation_id) if args.installation_id else _lookup_installation_id(
        app_id, private_key, args.installation_account
    )
    if not installation_id.isdigit():
        raise RefreshError("Installation ID must contain digits only.")

    permissions = _json_object(args.permissions, "Permissions")
    config_path = Path(args.config).expanduser()
    token_file = Path(args.token_file).expanduser()
    config = {
        "app_id": app_id,
        "installation_id": installation_id,
        "private_key_file": str(private_key),
        "permissions": permissions,
        "token_file": str(token_file),
    }
    _write_private_file(config_path, json.dumps(config, indent=2) + "\n")
    print(f"Configuration saved to {config_path}")
    print(f"Discovered App ID {app_id} and installation ID {installation_id}.")
    print("Run 'gh-auth-refresh' to mint and save an installation token.")


def _start_manifest_callback() -> tuple[HTTPServer, threading.Thread, threading.Event, dict[str, str]]:
    received = threading.Event()
    result: dict[str, str] = {}
    state = secrets.token_urlsafe(32)

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/start":
                page = result.get("setup_page")
                if not page:
                    self.send_error(503, "Setup page is not ready")
                    return
                body = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
                return
            query = parse_qs(parsed.query)
            returned_state = query.get("state", [""])[0]
            if parsed.path != "/callback" or not secrets.compare_digest(returned_state, state):
                self.send_error(400, "Invalid GitHub setup callback")
                return
            if query.get("error"):
                result["error"] = query["error"][0]
            elif query.get("code"):
                result["code"] = query["code"][0]
            else:
                result["error"] = "GitHub did not return a registration code"
            received.set()
            body = b"GitHub App registration received. Return to the terminal."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    result["state"] = state
    result["redirect_url"] = f"http://127.0.0.1:{server.server_port}/callback"
    return server, thread, received, result


def _render_manifest_form(state: str, redirect_url: str, permissions: dict[str, str]) -> str:
    manifest = {
        "name": f"gh-auth-refresh-{secrets.token_hex(4)}",
        "url": APP_HOME,
        "description": "Locally mint short-lived GitHub App tokens for Git and GitHub CLI.",
        "redirect_url": redirect_url,
        "public": False,
        "default_permissions": permissions,
        "default_events": [],
    }
    manifest_value = html.escape(json.dumps(manifest, separators=(",", ":")), quote=True)
    state_value = html.escape(state, quote=True)
    page = (
        "<!doctype html><meta charset=utf-8><title>Set up gh-auth-refresh</title>"
        "<p>Continue to GitHub to review and approve this pre-filled private App registration.</p>"
        f'<form action="https://github.com/settings/apps/new?state={state_value}" method="post">'
        f'<input type="hidden" name="manifest" value="{manifest_value}">'
        '<button type="submit">Continue to GitHub</button></form>'
    )
    return page


def _complete_manifest_registration(code: str) -> dict[str, Any]:
    response = _request_json(
        f"https://api.github.com/app-manifests/{quote(code, safe='')}/conversions",
        method="POST",
        payload={},
        purpose="App registration completion",
    )
    if not isinstance(response, dict):
        raise RefreshError("GitHub returned an invalid App registration response.")
    return response


def _wait_for_installation(
    app_id: str,
    private_key: Path,
    slug: str,
    account: str | None,
) -> str:
    install_url = f"https://github.com/apps/{quote(slug, safe='')}/installations/new"
    print("Opening GitHub so you can approve installing the App and select repositories.")
    if not webbrowser.open(install_url):
        print(f"Open this URL to continue: {install_url}")
    print("Waiting for the installation to appear...")
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        assertion = _create_app_assertion(app_id, private_key)
        installations = _request_json(
            "https://api.github.com/app/installations?per_page=100",
            bearer=assertion,
            purpose="installation lookup",
        )
        if not isinstance(installations, list):
            raise RefreshError("GitHub's installation lookup response was not a list.")
        app_installations = [
            item for item in installations if str(item.get("app_id")) == app_id
        ]
        matches = app_installations
        if account:
            matches = [
                item for item in matches
                if str(item.get("account", {}).get("login", "")).casefold() == account.casefold()
            ]
        if len(matches) == 1 and isinstance(matches[0].get("id"), int):
            return str(matches[0]["id"])
        if len(matches) > 1:
            accounts = ", ".join(
                str(item.get("account", {}).get("login", "unknown")) for item in matches
            )
            raise RefreshError(
                "The App is installed on multiple accounts. Re-run configure with "
                f"--app-slug {slug} --installation-account LOGIN --private-key "
                f"{shlex.quote(str(private_key))}. Accounts: {accounts}"
            )
        if app_installations and account and not matches:
            accounts = ", ".join(
                str(item.get("account", {}).get("login", "unknown"))
                for item in app_installations
            )
            raise RefreshError(
                f"The App is installed on {accounts}, not '{account}'. Re-run configure with "
                "the account where it was installed."
            )
        time.sleep(3)
    raise RefreshError(
        "Timed out waiting for installation approval. The App and private key were created; "
        f"finish at {install_url}, then run configure with --app-slug {slug} "
        f"--private-key {shlex.quote(str(private_key))}."
    )


def setup(args: argparse.Namespace) -> None:
    permissions = _json_object(args.permissions, "Permissions")
    private_key = Path(args.private_key).expanduser()
    config_path = Path(args.config).expanduser()
    if private_key.exists():
        raise RefreshError(
            f"Refusing to overwrite existing private key {private_key}. Choose another path with --private-key."
        )
    if config_path.exists():
        raise RefreshError(
            f"Configuration already exists at {config_path}. Use configure to update it, or choose another --config path."
        )
    private_key.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if private_key.parent == Path.home() / ".secrets":
        private_key.parent.chmod(0o700)
    server, thread, received, callback = _start_manifest_callback()
    callback["setup_page"] = _render_manifest_form(
        callback["state"], callback["redirect_url"], permissions
    )
    setup_url = f"http://127.0.0.1:{server.server_port}/start"
    try:
        print("Opening a pre-filled GitHub App registration. Review and approve it in GitHub.")
        if not webbrowser.open(setup_url):
            print(f"Open this local setup page in a browser: {setup_url}")
        if not received.wait(timeout=600):
            raise RefreshError("Timed out waiting for GitHub App registration approval.")
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    if callback.get("error"):
        raise RefreshError(f"GitHub App registration was not completed: {callback['error']}.")
    app = _complete_manifest_registration(callback["code"])
    app_id = app.get("id")
    slug = app.get("slug")
    pem = app.get("pem")
    if not isinstance(app_id, int) or not isinstance(slug, str) or not isinstance(pem, str):
        raise RefreshError("GitHub did not return the App ID, slug, and private key.")

    _write_private_file(private_key, pem)
    installation_id = _wait_for_installation(
        str(app_id), private_key, slug, args.installation_account
    )
    config = {
        "app_id": str(app_id),
        "installation_id": installation_id,
        "private_key_file": str(private_key.resolve()),
        "permissions": permissions,
        "token_file": str(Path(args.token_file).expanduser()),
    }
    _write_private_file(config_path, json.dumps(config, indent=2) + "\n")
    print(f"GitHub App '{slug}' configured (App ID {app_id}, installation ID {installation_id}).")
    print(f"Private key saved to {private_key}; configuration saved to {config_path}.")
    refresh(argparse.Namespace(config=str(config_path)))


def _config_value(config: dict[str, Any], env_name: str, key: str) -> Any:
    return os.environ.get(env_name) or config.get(key)


def _create_app_assertion(app_id: str, private_key_path: Path) -> str:
    try:
        private_key = private_key_path.read_bytes()
    except OSError as exc:
        raise RefreshError(f"Cannot read GitHub App private key at {private_key_path}.") from exc

    now = int(time.time())
    try:
        return jwt.encode(
            {"iat": now - 60, "exp": now + 8 * 60, "iss": app_id},
            private_key,
            algorithm="RS256",
        )
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        raise RefreshError("Could not sign a GitHub App assertion; check the private key.") from exc


def _request_json(
    url: str,
    *,
    bearer: str | None = None,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    purpose: str,
) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "gh-auth-refresh",
        "X-GitHub-Api-Version": API_VERSION,
    }
    data = None
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RefreshError(f"GitHub {purpose} failed with HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RefreshError("Could not reach api.github.com. Check the network and try again.") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RefreshError("GitHub returned an unreadable response.") from exc


def _lookup_app_id(app_slug: str) -> str:
    result = _request_json(
        f"https://api.github.com/apps/{quote(app_slug, safe='')}",
        purpose="App lookup",
    )
    app_id = result.get("id") if isinstance(result, dict) else None
    if not isinstance(app_id, int):
        raise RefreshError("GitHub's App lookup response did not include an App ID.")
    return str(app_id)


def _lookup_installation_id(
    app_id: str,
    private_key_path: Path,
    account: str | None,
) -> str:
    assertion = _create_app_assertion(app_id, private_key_path)
    installations = _request_json(
        "https://api.github.com/app/installations?per_page=100",
        bearer=assertion,
        purpose="installation lookup",
    )
    if not isinstance(installations, list):
        raise RefreshError("GitHub's installation lookup response was not a list.")

    matches = [item for item in installations if str(item.get("app_id")) == app_id]
    if account:
        matches = [
            item for item in matches
            if str(item.get("account", {}).get("login", "")).casefold() == account.casefold()
        ]
    if len(matches) == 1 and isinstance(matches[0].get("id"), int):
        return str(matches[0]["id"])
    if not matches:
        suffix = f" for account '{account}'" if account else ""
        raise RefreshError(
            f"No installation of this app was found{suffix}. Install the app on your account first."
        )

    candidates = ", ".join(
        f"{item.get('account', {}).get('login', 'unknown')} (ID {item.get('id')})"
        for item in matches
    )
    raise RefreshError(
        "This app has multiple matching installations. Re-run configure with "
        f"--installation-account to select one: {candidates}"
    )


def _mint_token(
    app_id: str,
    installation_id: str,
    private_key_path: Path,
    permissions: dict[str, str],
) -> tuple[str, str]:
    assertion = _create_app_assertion(app_id, private_key_path)
    result = _request_json(
        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        bearer=assertion,
        method="POST",
        payload={"permissions": permissions},
        purpose="installation-token request",
    )
    token = result.get("token") if isinstance(result, dict) else None
    expires_at = result.get("expires_at") if isinstance(result, dict) else None
    if not isinstance(token, str) or not token or not isinstance(expires_at, str):
        raise RefreshError("GitHub's response did not include a token and expiry time.")
    return token, expires_at


def refresh(args: argparse.Namespace) -> None:
    config_path = Path(args.config).expanduser()
    config = _load_config(config_path)
    settings = _refresh_settings(config)
    app_id = settings["app_id"]
    installation_id = settings["installation_id"]
    private_key = settings["private_key"]
    permissions = settings["permissions"]
    token_file = settings["token_file"]

    token, expires_at = _mint_token(app_id, installation_id, private_key, permissions)
    if token_file == DEFAULT_TOKEN_FILE:
        token_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        token_file.parent.chmod(0o700)
    _write_private_file(token_file, token + "\n")
    config["token_state"] = {
        "expires_at": expires_at,
        "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "app_id": app_id,
        "installation_id": installation_id,
        "private_key_file": str(private_key.resolve()),
        "permissions": permissions,
        "token_file": str(token_file.resolve()),
    }
    _write_private_file(config_path, json.dumps(config, indent=2) + "\n")
    if not getattr(args, "quiet", False):
        print(f"GitHub App token written to {token_file} (expires {expires_at}).")


def _refresh_settings(config: dict[str, Any]) -> dict[str, Any]:
    app_id = _config_value(config, "GITHUB_APP_ID", "app_id")
    installation_id = _config_value(config, "GITHUB_APP_INSTALLATION_ID", "installation_id")
    private_key_value = _config_value(config, "GITHUB_APP_PRIVATE_KEY_FILE", "private_key_file")
    permissions_value = os.environ.get("GITHUB_APP_PERMISSIONS")
    permissions = (
        _json_object(permissions_value, "GITHUB_APP_PERMISSIONS")
        if permissions_value
        else config.get("permissions")
    )
    token_file_value = os.environ.get("GITHUB_TOKEN_FILE") or config.get("token_file")

    if not app_id or not installation_id or not private_key_value:
        raise RefreshError("Config must include app_id, installation_id, and private_key_file.")
    if not isinstance(permissions, dict) or not permissions:
        raise RefreshError("Config must include a non-empty permissions object.")
    permissions = _json_object(json.dumps(permissions), "Configured permissions")
    if not isinstance(token_file_value, str) or not token_file_value:
        token_file_value = str(DEFAULT_TOKEN_FILE)
    if not str(app_id).isdigit() or not str(installation_id).isdigit():
        raise RefreshError("App ID and installation ID must contain digits only.")

    return {
        "app_id": str(app_id),
        "installation_id": str(installation_id),
        "private_key": Path(str(private_key_value)).expanduser(),
        "permissions": permissions,
        "token_file": Path(token_file_value).expanduser(),
    }


def ensure_valid_token(args: argparse.Namespace) -> bool:
    config_path = Path(args.config).expanduser()
    config = _load_config(config_path)
    settings = _refresh_settings(config)
    state = config.get("token_state")
    token_file = settings["token_file"]
    try:
        token = token_file.read_text(encoding="utf-8").strip()
        expires_at = datetime.fromisoformat(
            str(state["expires_at"]).replace("Z", "+00:00")
        ).timestamp()
        state_matches = (
            isinstance(state, dict)
            and state.get("app_id") == settings["app_id"]
            and state.get("installation_id") == settings["installation_id"]
            and state.get("private_key_file") == str(settings["private_key"].resolve())
            and state.get("permissions") == settings["permissions"]
            and state.get("token_file") == str(token_file.resolve())
            and state.get("token_sha256") == hashlib.sha256(token.encode("utf-8")).hexdigest()
        )
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        state_matches = False
        expires_at = 0

    if state_matches and expires_at > time.time() + 300:
        return False
    refresh(argparse.Namespace(config=str(config_path), quiet=True))
    return True


def _real_gh_path() -> Path:
    wrapper_value = os.environ.get(
        "GH_AUTH_REFRESH_WRAPPER_PATH", str(Path.home() / "bin" / "gh")
    )
    wrapper_path = Path(wrapper_value).resolve()
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory or ".") / "gh"
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        try:
            if candidate.resolve() == wrapper_path:
                continue
        except OSError:
            continue
        return candidate
    raise RefreshError("Could not find the original GitHub CLI after the gh-auth-refresh wrapper.")


def run_gh(args: argparse.Namespace) -> None:
    gh_args = list(args.gh_args)
    if gh_args and gh_args[0] == "--":
        gh_args.pop(0)
    gh_path = _real_gh_path()

    local_commands = {"--help", "-h", "--version", "version", "help", "completion"}
    needs_auth = bool(gh_args) and gh_args[0] not in local_commands
    if needs_auth and Path(args.config).expanduser().is_file():
        ensure_valid_token(args)
        config = _load_config(Path(args.config).expanduser())
        settings = _refresh_settings(config)
        try:
            token = settings["token_file"].read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RefreshError(f"Cannot read the refreshed token file {settings['token_file']}.") from exc
        if not token:
            raise RefreshError(f"The refreshed token file {settings['token_file']} is empty.")
        os.environ["GH_TOKEN"] = token
        os.environ["GITHUB_TOKEN"] = token

    try:
        os.execv(str(gh_path), [str(gh_path), *gh_args])
    except OSError as exc:
        raise RefreshError(f"Could not start GitHub CLI at {gh_path}.") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gh-auth-refresh",
        description="Mint and securely store a short-lived GitHub App installation token.",
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="config file path")
    subparsers = parser.add_subparsers(dest="command")

    configure_parser = subparsers.add_parser("configure", help="save GitHub App settings")
    app_identifier = configure_parser.add_mutually_exclusive_group(required=True)
    app_identifier.add_argument("--app-id", help="numeric GitHub App ID")
    app_identifier.add_argument("--app-slug", help="slug from the GitHub App settings URL; App ID is looked up")
    configure_parser.add_argument(
        "--installation-id",
        help="numeric installation ID; discovered automatically when omitted",
    )
    configure_parser.add_argument(
        "--installation-account",
        help="account login to select when the app has multiple installations",
    )
    configure_parser.add_argument("--private-key", required=True, help="path to the App PEM private key")
    configure_parser.add_argument(
        "--permissions",
        default=json.dumps(DEFAULT_PERMISSIONS),
        help='JSON map of requested permissions, e.g. \'{"issues":"write"}\'',
    )
    configure_parser.add_argument(
        "--token-file", default=str(DEFAULT_TOKEN_FILE), help="where to save the installation token"
    )

    setup_parser = subparsers.add_parser(
        "setup", help="create and install a pre-filled GitHub App with your approval"
    )
    setup_parser.add_argument(
        "--installation-account", help="account login to select when installing the App"
    )
    setup_parser.add_argument(
        "--private-key", default=str(DEFAULT_PRIVATE_KEY), help="where to save the generated App PEM key"
    )
    setup_parser.add_argument(
        "--permissions",
        default=json.dumps(DEFAULT_PERMISSIONS),
        help='JSON map of requested permissions, e.g. \'{"issues":"write"}\'',
    )
    setup_parser.add_argument(
        "--token-file", default=str(DEFAULT_TOKEN_FILE), help="where to save the installation token"
    )

    gh_parser = subparsers.add_parser(
        "run-gh", add_help=False, help="run GitHub CLI with a refreshed token"
    )
    gh_parser.add_argument("gh_args", nargs=argparse.REMAINDER)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "configure":
            configure(args)
        elif args.command == "setup":
            setup(args)
        elif args.command == "run-gh":
            run_gh(args)
        else:
            refresh(args)
    except RefreshError as exc:
        print(f"gh-auth-refresh: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
