import hashlib
import ipaddress
import os
import re
import secrets
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from clash_sub.models import (
    LOCAL_SOURCE_KINDS,
    VARIANTS,
    CertificateSettings,
    PublicationSettings,
    RealitySettings,
    ServiceSettings,
    Settings,
    SourceSpec,
    TokenRotation,
    UserSpec,
    XuiSettings,
)


class SettingsError(ValueError):
    """Raised when private service or user settings are unsafe or invalid."""


_TOKEN_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ACME_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_ARGV_FORBIDDEN_CHARS = set(";|&`$<>\"'\\\n\r\t")
_LOOPBACK_HOSTS = {"127.0.0.1", "::1"}
# Short-lived IP certificates leave little runway: alert at least two
# full days ahead when publication relies on one.
IP_MODE_MIN_ALERT_SECONDS = 172800


def load_settings(service_path: Path, users_path: Path) -> Settings:
    """Load strict YAML settings and return only resolved immutable models."""
    _ensure_private_file_mode(service_path)
    _ensure_private_file_mode(users_path)

    service_doc = _load_yaml_mapping(service_path, "service")
    users_doc = _load_yaml_mapping(users_path, "users")

    service = _parse_service_settings(service_doc)
    users = _parse_user_settings(users_doc, service.private_root)
    return Settings(service=service, users=users)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def rotate_user_token(
    users_path: Path,
    settings: Settings,
    user_id: str,
) -> TokenRotation:
    """Atomically store only a SHA-256 hash and return plaintext once."""
    if user_id not in settings.users:
        raise SettingsError("unknown user: %s" % user_id)

    users_doc = _load_yaml_mapping(users_path, "users")
    users_section = users_doc.get("users")
    if not isinstance(users_section, dict) or user_id not in users_section:
        raise SettingsError("unknown user: %s" % user_id)

    token = secrets.token_urlsafe(32)
    users_section[user_id]["token-sha256"] = hash_token(token)
    dumped = yaml.safe_dump(users_doc, sort_keys=False)
    _atomic_write_private_yaml(users_path, dumped)

    urls = {}
    for variant in settings.users[user_id].variants:
        urls[variant] = "https://%s/s/%s/%s.yaml" % (
            settings.service.publication.subscription_authority,
            token,
            variant,
        )
    return TokenRotation(user_id=user_id, token=token, urls=urls)


def _load_yaml_mapping(path: Path, label: str):
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise SettingsError("invalid %s YAML: %s" % (label, exc))
    if not isinstance(loaded, dict):
        raise SettingsError("%s must contain a mapping" % label)
    return loaded


def _parse_service_settings(service_doc):
    _require_mapping_keys(
        service_doc,
        (
            "schema-version",
            "private-root",
            "converter-base-url",
            "publication",
            "reality",
            "xui",
            "certificate",
        ),
        "service",
    )
    _require_int(service_doc["schema-version"], "service schema-version")
    private_root = Path(_require_str(service_doc["private-root"], "private-root")).resolve()
    publication = _parse_publication_settings(_require_dict(service_doc["publication"], "publication"))
    reality = _parse_reality_settings(_require_dict(service_doc["reality"], "reality"))
    xui = _parse_xui_settings(_require_dict(service_doc["xui"], "xui"))
    converter_base_url = _validate_loopback_http_url(
        _require_str(service_doc["converter-base-url"], "converter-base-url"),
        "converter-base-url",
    )
    if publication.mode == "ip":
        subscription_host = _authority_host(publication.subscription_authority)
        panel_host = _authority_host(publication.panel_authority)
        if subscription_host != panel_host:
            raise SettingsError(
                "publication authorities must use the same public IP in ip mode"
            )
        if subscription_host != reality.public_address:
            raise SettingsError(
                "publication authorities must use the reality public-address in ip mode"
            )
    certificate = _parse_certificate_settings(
        _require_dict(service_doc["certificate"], "certificate"),
        publication_mode=publication.mode,
        public_ip=reality.public_address,
    )
    _validate_distinct_ports(
        (
            ("xui.panel-port", xui.panel_port),
            ("xui.subscription-port", xui.subscription_port),
            ("publication.publisher-port", publication.publisher_port),
        )
    )
    return ServiceSettings(
        private_root=private_root,
        converter_base_url=converter_base_url,
        publication=publication,
        reality=reality,
        xui=xui,
        certificate=certificate,
    )


def _parse_publication_settings(publication_doc):
    _require_mapping_keys(
        publication_doc,
        (
            "mode",
            "subscription-authority",
            "panel-authority",
            "publisher-listen",
            "publisher-port",
        ),
        "publication",
    )
    mode = _require_str(publication_doc["mode"], "publication.mode")
    if mode not in ("domain", "ip"):
        raise SettingsError("publication.mode must be domain or ip")
    subscription_authority = _validate_public_authority(
        _require_str(publication_doc["subscription-authority"], "subscription-authority"),
        "subscription-authority",
        mode,
    )
    panel_authority = _validate_public_authority(
        _require_str(publication_doc["panel-authority"], "panel-authority"),
        "panel-authority",
        mode,
    )
    publisher_listen = _require_str(publication_doc["publisher-listen"], "publisher-listen")
    if publisher_listen != "127.0.0.1":
        raise SettingsError("publisher-listen must be 127.0.0.1")
    _validate_ip_literal(publisher_listen, "publisher-listen")
    publisher_port = _require_port(
        publication_doc["publisher-port"],
        "publisher-port",
    )
    return PublicationSettings(
        mode=mode,
        subscription_authority=subscription_authority,
        panel_authority=panel_authority,
        publisher_listen=publisher_listen,
        publisher_port=publisher_port,
    )


def _parse_reality_settings(reality_doc):
    _require_mapping_keys(
        reality_doc,
        ("public-address", "public-port", "required-flow"),
        "reality",
    )
    public_address = _require_str(reality_doc["public-address"], "public-address")
    _validate_ip_literal(public_address, "public-address")
    return RealitySettings(
        public_address=public_address,
        public_port=_require_port(reality_doc["public-port"], "public-port"),
        required_flow=_require_str(reality_doc["required-flow"], "required-flow"),
    )


def _parse_xui_settings(xui_doc):
    _require_mapping_keys(
        xui_doc,
        (
            "panel-listen",
            "panel-port",
            "panel-base-path",
            "subscription-listen",
            "subscription-port",
            "xray-config-path",
            "xray-binary-path",
            "expected-panel-version",
            "expected-xray-version",
        ),
        "xui",
    )
    panel_listen = _require_str(xui_doc["panel-listen"], "panel-listen")
    _validate_loopback_literal(panel_listen, "panel-listen")
    subscription_listen = _require_str(
        xui_doc["subscription-listen"],
        "subscription-listen",
    )
    _validate_loopback_literal(subscription_listen, "subscription-listen")
    panel_base_path = _require_str(xui_doc["panel-base-path"], "panel-base-path")
    _validate_panel_base_path(panel_base_path)
    return XuiSettings(
        panel_listen=panel_listen,
        panel_port=_require_port(xui_doc["panel-port"], "panel-port"),
        panel_base_path=panel_base_path,
        subscription_listen=subscription_listen,
        subscription_port=_require_port(
            xui_doc["subscription-port"],
            "subscription-port",
        ),
        xray_config_path=Path(
            _require_str(xui_doc["xray-config-path"], "xray-config-path")
        ).resolve(),
        xray_binary_path=Path(
            _require_str(xui_doc["xray-binary-path"], "xray-binary-path")
        ).resolve(),
        expected_panel_version=_require_str(
            xui_doc["expected-panel-version"],
            "expected-panel-version",
        ),
        expected_xray_version=_require_str(
            xui_doc["expected-xray-version"],
            "expected-xray-version",
        ),
    )


def _parse_certificate_settings(certificate_doc, publication_mode: str, public_ip: str):
    _require_mapping_keys(
        certificate_doc,
        ("fullchain-path", "acme-email", "alert-before-seconds", "alert-command"),
        "certificate",
    )
    alert_command = certificate_doc["alert-command"]
    if not isinstance(alert_command, list):
        raise SettingsError("alert-command must be a list")
    for item in alert_command:
        _require_str(item, "alert-command item")
        if not item or any(character in item for character in _ARGV_FORBIDDEN_CHARS):
            raise SettingsError(
                "alert-command items must be single non-empty argv tokens"
            )
    acme_email = _require_str(certificate_doc["acme-email"], "acme-email")
    if not _ACME_EMAIL_RE.fullmatch(acme_email):
        raise SettingsError("acme-email must be a valid email address")
    raw_fullchain = _require_str(certificate_doc["fullchain-path"], "fullchain-path")
    fullchain_path = Path(raw_fullchain).resolve()
    if not fullchain_path.is_absolute() or not raw_fullchain.startswith("/"):
        raise SettingsError("fullchain-path must be an absolute path")
    if fullchain_path.name != "fullchain.pem":
        raise SettingsError("fullchain-path must end with fullchain.pem")
    alert_before_seconds = _require_int(
        certificate_doc["alert-before-seconds"],
        "alert-before-seconds",
    )
    if publication_mode == "ip":
        if not alert_command:
            raise SettingsError(
                "alert-command must be configured in ip mode"
            )
        if alert_before_seconds < IP_MODE_MIN_ALERT_SECONDS:
            raise SettingsError(
                "alert-before-seconds must be at least %d in ip mode"
                % IP_MODE_MIN_ALERT_SECONDS
            )
        if public_ip not in str(fullchain_path):
            raise SettingsError(
                "fullchain-path must name the public IP certificate in ip mode"
            )
    return CertificateSettings(
        fullchain_path=fullchain_path,
        acme_email=acme_email,
        alert_before_seconds=alert_before_seconds,
        alert_command=tuple(alert_command),
    )


def _parse_user_settings(users_doc, private_root: Path):
    _require_mapping_keys(users_doc, ("schema-version", "users"), "users")
    _require_int(users_doc["schema-version"], "users schema-version")
    users_section = _require_dict(users_doc["users"], "users.users")
    parsed_users = {}
    owner_count = 0
    for user_id, user_doc in users_section.items():
        if not isinstance(user_id, str):
            raise SettingsError("user ids must be strings")
        parsed_user = _parse_single_user(user_id, _require_dict(user_doc, user_id), private_root)
        if parsed_user.is_owner:
            owner_count += 1
        parsed_users[user_id] = parsed_user
    if owner_count > 1:
        raise SettingsError("users may declare at most one owner")
    return parsed_users


def _parse_single_user(user_id: str, user_doc, private_root: Path):
    _require_mapping_keys(
        user_doc,
        ("role", "token-sha256", "variants", "xui-subscription-url", "local-sources"),
        user_id,
    )
    role = _require_str(user_doc["role"], "%s.role" % user_id)
    if role not in ("owner", "member"):
        raise SettingsError("%s.role must be owner or member" % user_id)
    token_sha256 = _require_str(user_doc["token-sha256"], "%s.token-sha256" % user_id)
    if not _TOKEN_SHA256_RE.fullmatch(token_sha256):
        raise SettingsError("%s token-sha256 must be 64 lowercase hex characters" % user_id)
    variants = _parse_variants(user_id, user_doc["variants"])
    xui_url = _validate_loopback_http_url(
        _require_str(user_doc["xui-subscription-url"], "%s.xui-subscription-url" % user_id),
        "%s.xui-subscription-url" % user_id,
    )
    local_sources_doc = _require_dict(user_doc["local-sources"], "%s.local-sources" % user_id)
    if role != "owner" and local_sources_doc:
        raise SettingsError("%s local-sources are only allowed for the owner" % user_id)
    local_sources = []
    private_root_resolved = private_root.resolve()
    for kind, raw_path in local_sources_doc.items():
        if kind not in LOCAL_SOURCE_KINDS:
            raise SettingsError("%s local-sources contains unknown key %s" % (user_id, kind))
        source_path = Path(_require_str(raw_path, "%s.local-sources.%s" % (user_id, kind)))
        resolved = (private_root_resolved / source_path).resolve()
        try:
            resolved.relative_to(private_root_resolved)
        except ValueError:
            raise SettingsError("%s local-sources path is outside private-root" % user_id)
        if resolved.exists():
            _ensure_private_file_mode(resolved)
        local_sources.append(SourceSpec(kind=kind, label=kind, path=resolved))
    return UserSpec(
        user_id=user_id,
        role=role,
        token_sha256=token_sha256,
        variants=variants,
        xui_source=SourceSpec(kind="xui", label=user_id, url=xui_url),
        local_sources=tuple(local_sources),
    )


def _parse_variants(user_id: str, variants_value):
    if not isinstance(variants_value, list):
        raise SettingsError("%s variants must be a list" % user_id)
    variants = []
    seen = set()
    for variant in variants_value:
        variant_name = _require_str(variant, "%s variant" % user_id)
        if variant_name in seen:
            raise SettingsError("%s variant list contains duplicates" % user_id)
        if variant_name not in VARIANTS:
            raise SettingsError("%s variant %s is unsupported" % (user_id, variant_name))
        seen.add(variant_name)
        variants.append(variant_name)
    return tuple(variants)


def _validate_public_authority(authority: str, field_name: str, mode: str) -> str:
    if "://" in authority:
        raise SettingsError("%s must not include a scheme" % field_name)
    parsed = urlsplit("//%s" % authority)
    if not parsed.hostname or parsed.port != 8443:
        raise SettingsError("%s must include port 8443" % field_name)
    if parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise SettingsError("%s must contain only host:port" % field_name)
    host = parsed.hostname
    is_ip = _is_ip_literal(host)
    if mode == "domain" and is_ip:
        raise SettingsError("%s must be a domain in domain mode" % field_name)
    if mode == "ip" and not is_ip:
        raise SettingsError("%s must be an IP in ip mode" % field_name)
    return authority


def _authority_host(authority: str) -> str:
    return authority.rsplit(":", 1)[0]


def _validate_panel_base_path(value: str) -> None:
    """The path lands verbatim in an Nginx location: keep it plain."""
    if not value.startswith("/") or len(value) < 2:
        raise SettingsError("panel-base-path must start with / and name a path")
    if ".." in value:
        raise SettingsError("panel-base-path must not contain ..")
    for character in "{};\n\r\t ":
        if character in value:
            raise SettingsError("panel-base-path contains forbidden characters")


def _validate_loopback_http_url(url: str, field_name: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "http":
        raise SettingsError("%s must use loopback http" % field_name)
    if parsed.hostname not in _LOOPBACK_HOSTS:
        raise SettingsError("%s must use loopback http" % field_name)
    if parsed.port is None:
        raise SettingsError("%s must include a port" % field_name)
    if parsed.username or parsed.password:
        raise SettingsError("%s must not include credentials" % field_name)
    return url


def _validate_loopback_literal(value: str, field_name: str) -> None:
    _validate_ip_literal(value, field_name)
    if value not in _LOOPBACK_HOSTS:
        raise SettingsError("%s must be loopback" % field_name)


def _validate_ip_literal(value: str, field_name: str) -> None:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        raise SettingsError("%s must be a valid IP address" % field_name)


def _validate_distinct_ports(named_ports) -> None:
    seen = {}
    for field_name, port in named_ports:
        if port in seen:
            raise SettingsError("port conflict between %s and %s" % (seen[port], field_name))
        seen[port] = field_name


def _atomic_write_private_yaml(path: Path, contents: str) -> None:
    temp_fd, temp_name = tempfile.mkstemp(
        prefix="%s." % path.name,
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    try:
        os.fchmod(temp_fd, 0o600)
        with os.fdopen(temp_fd, "w", encoding="utf-8") as handle:
            handle.write(contents)
        os.replace(temp_name, path)
    except Exception:
        try:
            os.close(temp_fd)
        except OSError:
            pass
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _ensure_private_file_mode(path: Path) -> None:
    try:
        mode = path.stat().st_mode & 0o777
    except OSError as exc:
        raise SettingsError("cannot stat %s: %s" % (path, exc))
    if mode & 0o077:
        raise SettingsError("%s permissions must not be group/world readable" % path)


def _require_mapping_keys(mapping, allowed_keys, context: str) -> None:
    if not isinstance(mapping, dict):
        raise SettingsError("%s must be a mapping" % context)
    allowed = set(allowed_keys)
    actual = set(mapping.keys())
    unknown = sorted(actual - allowed)
    if unknown:
        raise SettingsError("%s contains unknown keys: %s" % (context, ", ".join(unknown)))
    missing = [key for key in allowed_keys if key not in mapping]
    if missing:
        raise SettingsError("%s is missing keys: %s" % (context, ", ".join(missing)))


def _require_dict(value, field_name: str):
    if not isinstance(value, dict):
        raise SettingsError("%s must be a mapping" % field_name)
    return value


def _require_str(value, field_name: str) -> str:
    if not isinstance(value, str):
        raise SettingsError("%s must be a string" % field_name)
    return value


def _require_int(value, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingsError("%s must be an integer" % field_name)
    return value


def _require_port(value, field_name: str) -> int:
    port = _require_int(value, field_name)
    if port < 1 or port > 65535:
        raise SettingsError("%s must be between 1 and 65535" % field_name)
    return port


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True
