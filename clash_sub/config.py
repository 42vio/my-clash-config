import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from clash_sub.domain import ServiceConfig


class ConfigError(ValueError):
    pass


_CONFIG_KEYS = {
    "schema-version",
    "owner-email",
    "subscription-authority",
    "xui-public-endpoint",
    "xui-database",
    "private-root",
    "public-root",
    "nginx-routes",
    "mihomo-binary",
    "nginx-binary",
    "systemctl-binary",
    "max-source-bytes",
}

_PATH_KEYS = {
    "xui-database": "xui_database",
    "private-root": "private_root",
    "public-root": "public_root",
    "nginx-routes": "nginx_routes",
    "mihomo-binary": "mihomo_binary",
    "nginx-binary": "nginx_binary",
    "systemctl-binary": "systemctl_binary",
}


def load_config(path: Path, repo_root: Path) -> ServiceConfig:
    config_path = _private_config_path(path, repo_root)
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ConfigError("service configuration could not be read") from error

    if not isinstance(data, dict):
        raise ConfigError("service configuration must be a mapping")
    unknown = set(data) - _CONFIG_KEYS
    if unknown:
        raise ConfigError("unsupported configuration key")
    if data.get("schema-version") != 2 or isinstance(data.get("schema-version"), bool):
        raise ConfigError("unsupported configuration schema")
    missing = _CONFIG_KEYS - set(data)
    if missing:
        raise ConfigError("missing required configuration")

    owner_email = _nonempty_string(data["owner-email"], "owner email")
    subscription_authority = _subscription_authority(data["subscription-authority"])
    xui_public_endpoint = _xui_public_endpoint(data["xui-public-endpoint"])
    configured_paths = {
        field: _absolute_path(data[key]) for key, field in _PATH_KEYS.items()
    }
    max_source_bytes = data["max-source-bytes"]
    if (
        not isinstance(max_source_bytes, int)
        or isinstance(max_source_bytes, bool)
        or max_source_bytes <= 0
    ):
        raise ConfigError("max source bytes must be a positive integer")

    return ServiceConfig(
        owner_email=owner_email,
        subscription_authority=subscription_authority,
        xui_public_endpoint=xui_public_endpoint,
        template_root=Path(repo_root) / "templates",
        max_source_bytes=max_source_bytes,
        **configured_paths,
    )


def _private_config_path(path: Path, repo_root: Path) -> Path:
    config_path = Path(path)
    root = Path(repo_root)
    if not config_path.is_absolute() or not root.is_absolute():
        raise ConfigError("configuration path and repository root must be absolute")
    try:
        resolved_path = config_path.resolve(strict=True)
        resolved_path.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise ConfigError("configuration must be within repository root") from error
    if not resolved_path.is_file():
        raise ConfigError("configuration must be a regular file")
    try:
        mode = resolved_path.stat().st_mode & 0o777
    except OSError as error:
        raise ConfigError("service configuration could not be read") from error
    if mode != 0o600:
        raise ConfigError("service configuration mode must be 0600")
    if os.geteuid() == 0 and resolved_path.stat().st_uid != 0:
        raise ConfigError("service configuration must be root-owned")
    return resolved_path


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("%s must not be empty" % label)
    return value


def _subscription_authority(value: Any) -> str:
    authority = _nonempty_string(value, "subscription authority")
    if "://" in authority or any(character.isspace() for character in authority):
        raise ConfigError("invalid subscription authority")
    try:
        parsed = urlsplit("//" + authority)
        valid = (
            parsed.hostname is not None
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise ConfigError("subscription authority must use port 443")
    # 443 is the https default: normalize it away so every rendered
    # subscription URL stays free of a redundant ":443".
    return authority[:-4] if authority.endswith(":443") else authority


def _xui_public_endpoint(value: Any) -> str:
    endpoint = _nonempty_string(value, "xui public endpoint")
    if "://" in endpoint or any(character.isspace() for character in endpoint):
        raise ConfigError("invalid xui public endpoint")
    try:
        parsed = urlsplit("//" + endpoint)
        valid = (
            parsed.hostname is not None
            and parsed.port == 443
            and parsed.username is None
            and parsed.password is None
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise ConfigError("xui public endpoint must use port 443")
    return endpoint


def _absolute_path(value: Any) -> Path:
    path = Path(_nonempty_string(value, "configured path"))
    if not path.is_absolute():
        raise ConfigError("configured path must be an absolute path")
    return path
