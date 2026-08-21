from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Tuple


VARIANTS = ("balanced", "balanced-win", "privacy")
LOCAL_SOURCE_KINDS = ("airport", "home")


@dataclass(frozen=True)
class PublicationSettings:
    mode: str
    subscription_authority: str
    panel_authority: str
    publisher_listen: str
    publisher_port: int


@dataclass(frozen=True)
class RealitySettings:
    public_address: str
    public_port: int
    required_flow: str


@dataclass(frozen=True)
class XuiSettings:
    panel_listen: str
    panel_port: int
    panel_base_path: str
    subscription_listen: str
    subscription_port: int
    xray_config_path: Path
    xray_binary_path: Path
    expected_panel_version: str
    expected_xray_version: str


@dataclass(frozen=True)
class CertificateSettings:
    fullchain_path: Path
    acme_email: str
    alert_before_seconds: int
    alert_command: Tuple[str, ...]


@dataclass(frozen=True)
class ServiceSettings:
    private_root: Path
    converter_base_url: str
    publication: PublicationSettings
    reality: RealitySettings
    xui: XuiSettings
    certificate: CertificateSettings


@dataclass(frozen=True)
class SourceSpec:
    kind: str
    label: str
    url: Optional[str] = None
    path: Optional[Path] = None


@dataclass(frozen=True)
class UserSpec:
    user_id: str
    role: str
    token_sha256: str
    variants: Tuple[str, ...]
    xui_source: SourceSpec
    local_sources: Tuple[SourceSpec, ...]

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"


@dataclass(frozen=True)
class Settings:
    service: ServiceSettings
    users: Mapping[str, UserSpec]


@dataclass(frozen=True)
class TokenRotation:
    user_id: str
    token: str
    urls: Mapping[str, str]


@dataclass(frozen=True)
class SubscriptionUserinfo:
    upload: int
    download: int
    total: int
    expire: int

    @property
    def remaining(self) -> Optional[int]:
        if self.total == 0:
            return None
        return max(self.total - self.upload - self.download, 0)

    @property
    def header_value(self) -> str:
        return (
            "upload=%d; download=%d; total=%d; expire=%d"
            % (self.upload, self.download, self.total, self.expire)
        )


@dataclass(frozen=True)
class Candidate:
    operation_id: str
    user_id: str
    path: Path
    files: Mapping[str, Path]
    manifest_path: Path


@dataclass(frozen=True)
class Release:
    release_id: str
    user_id: str
    path: Path
    files: Mapping[str, Path]


@dataclass(frozen=True)
class Request:
    """Raw HTTP request as seen by the publisher.

    ``path`` and ``headers`` stay out of the representation: tokenized
    subscription paths must never leak through tracebacks or debug logs.
    """

    method: str
    path: str = field(repr=False)
    client_ip: str
    peer_ip: str
    headers: Mapping[str, str] = field(repr=False)


@dataclass(frozen=True)
class Response:
    """Immutable HTTP response; the body may carry proxy credentials."""

    status: int
    headers: Mapping[str, str]
    body: bytes = field(repr=False)
