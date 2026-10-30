from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from urllib.parse import quote


VARIANTS = ("compat", "balance")
OWNER_VARIANTS = VARIANTS + ("airport",)
MEMBER_VARIANTS = ("compat",)
NODE_VARIANT = "nodes"
PROFILE_FILENAMES = MappingProxyType({
    NODE_VARIANT: "Nodes.txt",
    "compat": "Clash-Compat.yaml",
    "balance": "Clash-Balance.yaml",
    "airport": "Clash-Airport.yaml",
})
PROFILE_TITLES = MappingProxyType({
    NODE_VARIANT: "Nodes.txt",
    "compat": "Clash-Compat.yaml",
    "balance": "Clash-Balance.yaml",
    "airport": "Clash-Airport.yaml",
})
AIRPORT_FILENAME = "AmyTelecom.yaml"


@dataclass(frozen=True)
class ServiceConfig:
    owner_email: str
    subscription_authority: str
    xui_public_endpoint: str
    xui_database: Path
    private_root: Path
    public_root: Path
    nginx_routes: Path
    mihomo_binary: Path
    nginx_binary: Path
    systemctl_binary: Path
    template_root: Path
    max_source_bytes: int = 5 * 1024 * 1024


@dataclass(frozen=True)
class XuiClient:
    client_id: int
    email: str
    sub_id: str
    enabled: bool
    upload: int
    download: int
    total: int
    expiry_ms: int


@dataclass(frozen=True)
class XuiSnapshot:
    clients: tuple[XuiClient, ...]
    listen: str
    port: int
    clash_path: str

    def source_url(self, client: XuiClient) -> str:
        return "http://%s:%s%s%s" % (
            self.listen,
            self.port,
            self.clash_path,
            quote(client.sub_id, safe=""),
        )


@dataclass(frozen=True)
class UserState:
    client_id: int
    email: str
    token: str
    readable_code: str | None
    active: bool
    current_release: str | None


@dataclass(frozen=True)
class RuntimeState:
    schema_version: int
    owner_client_id: int
    users: Mapping[int, UserState]
    airport_token: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "users", MappingProxyType(dict(self.users)))


@dataclass(frozen=True)
class Traffic:
    upload: int
    download: int
    total: int
    expiry_ms: int


@dataclass(frozen=True)
class AirportDownload:
    """One verbatim airport document and its final response traffic header."""

    document: bytes
    traffic: Traffic | None


@dataclass(frozen=True)
class AirportProvider:
    """The owner-only stable airport source rendered into owner profiles."""

    url: str
    airport_only_url: str | None = None


@dataclass(frozen=True)
class PreparedRelease:
    release_id: str
    public_paths: Mapping[str, Path]
    manifest_path: Path

    def __post_init__(self):
        object.__setattr__(self, "public_paths", MappingProxyType(dict(self.public_paths)))
