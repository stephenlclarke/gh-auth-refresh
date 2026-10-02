"""Command-line interface for refreshing GitHub App installation tokens."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote
from typing import Any

import jwt

DEFAULT_CONFIG = Path("~/.config/gh-auth-refresh/config.json").expanduser()
DEFAULT_TOKEN_FILE = Path("~/.secrets/GITHUB_TOKEN").expanduser()
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
    if path.parent == Path.home() / ".config" / "gh-auth-refresh":
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

    token, expires_at = _mint_token(
        str(app_id),
        str(installation_id),
        Path(str(private_key_value)).expanduser(),
        permissions,
    )
    token_file = Path(token_file_value).expanduser()
    if token_file == DEFAULT_TOKEN_FILE:
        token_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        token_file.parent.chmod(0o700)
    _write_private_file(token_file, token + "\n")
    print(f"GitHub App token written to {token_file} (expires {expires_at}).")


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

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "configure":
            configure(args)
        else:
            refresh(args)
    except RefreshError as exc:
        print(f"gh-auth-refresh: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
