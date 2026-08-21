import hashlib
import hmac
import ipaddress
import json
import os
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional, Tuple

from clash_sub.models import (
    Request,
    Response,
    Settings,
    SubscriptionUserinfo,
    UserSpec,
    VARIANTS,
)
from clash_sub.releases import (
    MANIFEST_NAME,
    SAFE_SLUG_RE,
    SIDECAR_SUFFIX,
    VARIANT_SUFFIX,
    BuildError,
    _load_manifest,
    _require_manifest_identity,
    _require_user_releases_root,
    _validate_manifest,
)
from clash_sub.traffic import TrafficClient
from clash_sub.validation import sha256_bytes


LOOPBACK_LISTEN = "127.0.0.1"
TRAFFIC_CACHE_SECONDS = 600.0
MAX_YAML_BYTES = 5 * 1024 * 1024
RATE_LIMIT_REQUESTS = 30
RATE_LIMIT_WINDOW = 60.0
RATE_LIMIT_BURST = 10
LRU_MAX_ENTRIES = 4096
CONNECTION_TIMEOUT_SECONDS = 15
MAX_TARGET_BYTES = 2048

_NOT_FOUND_BODY = b"not found\n"
_NOT_FOUND_HEADERS = {
    "Content-Type": "text/plain; charset=utf-8",
    "Content-Length": str(len(_NOT_FOUND_BODY)),
    "Cache-Control": "no-store",
}
_METHOD_NOT_ALLOWED_BODY = b"method not allowed\n"
_RATE_LIMITED_BODY = b"rate limited\n"
_HEALTH_BODY = b'{"status": "ok"}\n'
_RATE_RETRY_AFTER_SECONDS = str(int(RATE_LIMIT_WINDOW / RATE_LIMIT_REQUESTS))
_LOG_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class _Authorization:
    user: UserSpec
    variant: str
    private_root: Path

    @property
    def token_hash(self) -> str:
        return self.user.token_sha256


@dataclass
class _TrafficRecord:
    value: Optional[SubscriptionUserinfo]
    expires_at: float


@dataclass
class _TokenBucket:
    tokens: float
    updated: float


def _is_loopback_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def resolve_client_ip(peer_ip: str, headers: Mapping[str, str]) -> str:
    """Trust X-Real-IP only when the socket peer itself is loopback."""
    forwarded = None
    for name, value in headers.items():
        if str(name).lower() == "x-real-ip":
            forwarded = str(value).strip()
            break
    if forwarded is None or not _is_loopback_ip(peer_ip):
        return peer_ip
    try:
        ipaddress.ip_address(forwarded)
    except ValueError:
        return peer_ip
    return forwarded


def settings_file_revision(paths: Iterable[Path]) -> Tuple[Tuple[int, int], ...]:
    """Return a comparable (mtime_ns, size) fingerprint for settings files."""
    revisions = []
    for path in paths:
        stat_result = os.stat(path)
        revisions.append((stat_result.st_mtime_ns, stat_result.st_size))
    return tuple(revisions)


def _default_log_sink(line: str) -> None:
    """Emit the sanitized access log to stderr (no handler setup needed)."""
    sys.stderr.write(line + "\n")


class PublicationService:
    """Token-gated read-only publisher for verified current releases."""

    def __init__(
        self,
        settings_loader: Callable[[], Settings],
        traffic_client: TrafficClient,
        clock: Callable[[], float] = time.monotonic,
        *,
        settings_revision: Optional[Callable[[], object]] = None,
        log_sink: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._settings_loader = settings_loader
        self._traffic_client = traffic_client
        self._clock = clock
        self._settings_revision = settings_revision
        self._log_sink = log_sink or _default_log_sink
        self._lock = threading.Lock()
        self._settings = settings_loader()
        self._settings_revision_value = self._probe_settings_revision()
        self._traffic_cache: "OrderedDict[str, _TrafficRecord]" = OrderedDict()
        self._anonymous_buckets: "OrderedDict[str, _TokenBucket]" = OrderedDict()
        self._authorized_buckets: "OrderedDict[Tuple[str, str], _TokenBucket]" = (
            OrderedDict()
        )

    def handle(self, request: Request) -> Response:
        method = request.method.upper()
        if method not in {"GET", "HEAD"}:
            return self._finish(
                method, request, self._method_not_allowed(), "method", "method_not_allowed"
            )
        if request.path == "/healthz":
            return self._finish(
                method, request, self._health_response(request), "health"
            )
        self._maybe_reload_settings()
        authorization = self._authorize_subscription_path(request.path)
        if authorization is None:
            if not self._consume_rate_limit(self._anonymous_buckets, request.client_ip):
                return self._finish(
                    method, request, self._rate_limited(), "subscription", "rate_limited"
                )
            return self._finish(
                method, request, self._not_found(), "subscription", "not_found"
            )
        if not self._consume_rate_limit(
            self._authorized_buckets, (request.client_ip, authorization.token_hash)
        ):
            return self._finish(
                method, request, self._rate_limited(), "subscription", "rate_limited"
            )
        return self._finish(
            method, request, self._serve_current(authorization), "subscription"
        )

    def _finish(
        self,
        method: str,
        request: Request,
        response: Response,
        route_class: str,
        error_code: str = "ok",
    ) -> Response:
        if method == "HEAD":
            response = Response(response.status, response.headers, b"")
        self._emit_log(request, response, route_class, error_code)
        return response

    def _probe_settings_revision(self):
        if self._settings_revision is None:
            return None
        try:
            return self._settings_revision()
        except Exception:
            return None

    def _maybe_reload_settings(self) -> None:
        if self._settings_revision is None:
            return
        try:
            revision = self._settings_revision()
        except Exception:
            return
        with self._lock:
            unchanged = revision == self._settings_revision_value
        if unchanged:
            return
        try:
            settings = self._settings_loader()
        except Exception:
            return
        with self._lock:
            self._settings = settings
            self._settings_revision_value = revision

    def _authorize_subscription_path(self, path: str) -> Optional[_Authorization]:
        if not path.startswith("/") or path.startswith("//"):
            return None
        if "%" in path or "\\" in path or "?" in path or "#" in path:
            return None
        segments = path.split("/")
        if len(segments) != 4 or segments[0] != "" or segments[1] != "s":
            return None
        token, file_name = segments[2], segments[3]
        if not token or "." in token:
            return None
        if not file_name.endswith(VARIANT_SUFFIX):
            return None
        variant = file_name[: -len(VARIANT_SUFFIX)]
        if not variant or variant not in VARIANTS:
            return None
        presented_hash = sha256_bytes(token.encode("utf-8"))
        with self._lock:
            settings = self._settings
        matched = None
        for user in settings.users.values():
            if hmac.compare_digest(presented_hash, user.token_sha256):
                matched = user
        if matched is None or variant not in matched.variants:
            return None
        if not SAFE_SLUG_RE.fullmatch(matched.user_id):
            return None
        return _Authorization(
            user=matched,
            variant=variant,
            private_root=settings.service.private_root,
        )

    def _serve_current(self, authorization: _Authorization) -> Response:
        try:
            release_dir, payload, expected_hash = self._load_verified_payload(authorization)
        except (BuildError, OSError):
            return self._not_found()
        userinfo = self._traffic_metadata(authorization, release_dir, expected_hash)
        variant = authorization.variant
        headers = {
            "Content-Type": "text/yaml; charset=utf-8",
            "Content-Disposition": 'attachment; filename="%s%s"' % (variant, VARIANT_SUFFIX),
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "Content-Length": str(len(payload)),
        }
        if userinfo is not None:
            headers["Subscription-Userinfo"] = userinfo.header_value
        return Response(200, headers, payload)

    def _load_verified_payload(
        self, authorization: _Authorization
    ) -> Tuple[Path, bytes, str]:
        user = authorization.user
        release_roots = _require_user_releases_root(authorization.private_root, user.user_id)
        if release_roots is None:
            raise BuildError("current release is missing")
        _releases_root, canonical_user_root = release_roots
        current_link = authorization.private_root / "current" / user.user_id
        if not current_link.is_symlink():
            raise BuildError("current link is missing")
        release_dir = current_link.resolve(strict=True)
        if release_dir.parent != canonical_user_root or not release_dir.is_dir():
            raise BuildError("current link escapes the user release root")
        release_id = release_dir.name
        if not SAFE_SLUG_RE.fullmatch(release_id):
            raise BuildError("current release id is invalid")
        manifest_path = release_dir / MANIFEST_NAME
        _require_manifest_identity(release_dir, manifest_path, "release manifest path")
        manifest = _load_manifest(manifest_path)
        manifest_variants = _validate_manifest(manifest, release_id, user.user_id)
        variant = authorization.variant
        if variant not in manifest_variants:
            raise BuildError("variant is not part of the current release")
        expected_hash = manifest["output_hashes"][variant]
        yaml_path = release_dir / (variant + VARIANT_SUFFIX)
        if yaml_path.is_symlink() or not yaml_path.is_file():
            raise BuildError("release is incomplete")
        if yaml_path.stat().st_size > MAX_YAML_BYTES:
            raise BuildError("release file exceeds the size limit")
        payload = yaml_path.read_bytes()
        if len(payload) > MAX_YAML_BYTES:
            raise BuildError("release file exceeds the size limit")
        if sha256_bytes(payload) != expected_hash:
            raise BuildError("release hash mismatch")
        return release_dir, payload, expected_hash

    def _traffic_metadata(
        self, authorization: _Authorization, release_dir: Path, expected_hash: str
    ) -> Optional[SubscriptionUserinfo]:
        user = authorization.user
        now = self._clock()
        with self._lock:
            record = self._traffic_cache.get(user.user_id)
            if record is not None and record.value is not None:
                if now < record.expires_at:
                    self._traffic_cache.move_to_end(user.user_id)
                    return record.value
        try:
            fetched = self._traffic_client.fetch(user.xui_source.url)
        except Exception:
            fetched = None
        if fetched is not None:
            with self._lock:
                self._traffic_cache[user.user_id] = _TrafficRecord(
                    value=fetched, expires_at=now + TRAFFIC_CACHE_SECONDS
                )
                self._traffic_cache.move_to_end(user.user_id)
                while len(self._traffic_cache) > LRU_MAX_ENTRIES:
                    self._traffic_cache.popitem(last=False)
            return fetched
        with self._lock:
            record = self._traffic_cache.get(user.user_id)
            if record is not None and record.value is not None:
                return record.value
        return self._sidecar_traffic(release_dir, authorization.variant, expected_hash)

    def _sidecar_traffic(
        self, release_dir: Path, variant: str, expected_hash: str
    ) -> Optional[SubscriptionUserinfo]:
        sidecar_path = release_dir / (variant + SIDECAR_SUFFIX)
        try:
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(sidecar, dict):
            return None
        if sidecar.get("yaml_sha256") != expected_hash:
            return None
        traffic = sidecar.get("traffic")
        if not isinstance(traffic, dict):
            return None
        values = []
        for field_name in ("upload", "download", "total", "expire"):
            value = traffic.get(field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None
            values.append(value)
        return SubscriptionUserinfo(*values)

    def _consume_rate_limit(self, store: "OrderedDict", key) -> bool:
        now = self._clock()
        with self._lock:
            bucket = store.get(key)
            if bucket is None:
                bucket = _TokenBucket(tokens=float(RATE_LIMIT_BURST), updated=now)
            elapsed = now - bucket.updated
            if elapsed > 0:
                bucket.tokens = min(
                    float(RATE_LIMIT_BURST),
                    bucket.tokens + elapsed * (RATE_LIMIT_REQUESTS / RATE_LIMIT_WINDOW),
                )
            bucket.updated = now
            allowed = bucket.tokens >= 1.0
            if allowed:
                bucket.tokens -= 1.0
            store[key] = bucket
            store.move_to_end(key)
            while len(store) > LRU_MAX_ENTRIES:
                store.popitem(last=False)
        return allowed

    def _health_response(self, request: Request) -> Response:
        if _is_loopback_ip(request.client_ip):
            return Response(
                200,
                {
                    "Content-Type": "application/json",
                    "Content-Length": str(len(_HEALTH_BODY)),
                    "Cache-Control": "no-store",
                },
                _HEALTH_BODY,
            )
        return self._not_found()

    def _method_not_allowed(self) -> Response:
        return Response(
            405,
            {
                "Allow": "GET, HEAD",
                "Content-Type": "text/plain; charset=utf-8",
                "Content-Length": str(len(_METHOD_NOT_ALLOWED_BODY)),
                "Cache-Control": "no-store",
            },
            _METHOD_NOT_ALLOWED_BODY,
        )

    def _rate_limited(self) -> Response:
        return Response(
            429,
            {
                "Content-Type": "text/plain; charset=utf-8",
                "Content-Length": str(len(_RATE_LIMITED_BODY)),
                "Retry-After": _RATE_RETRY_AFTER_SECONDS,
                "Cache-Control": "no-store",
            },
            _RATE_LIMITED_BODY,
        )

    @staticmethod
    def _not_found() -> Response:
        return Response(404, dict(_NOT_FOUND_HEADERS), _NOT_FOUND_BODY)

    def _emit_log(
        self, request: Request, response: Response, route_class: str, error_code: str
    ) -> None:
        timestamp = datetime.now(timezone.utc).strftime(_LOG_TIMESTAMP_FORMAT)
        route_hash = hashlib.sha256(route_class.encode("utf-8")).hexdigest()[:16]
        line = "%s %s %d route=%s bytes=%d error=%s" % (
            timestamp,
            request.method.upper(),
            response.status,
            route_hash,
            len(response.body),
            error_code,
        )
        try:
            self._log_sink(line)
        except Exception:
            pass


class PublisherRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "clash-sub-publisher"
    sys_version = ""
    timeout = CONNECTION_TIMEOUT_SECONDS
    service: PublicationService = None  # bound by create_publication_server

    def do_GET(self):
        self._dispatch()

    def do_HEAD(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_PUT(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    def do_PATCH(self):
        self._dispatch()

    def do_OPTIONS(self):
        self._dispatch()

    def _dispatch(self):
        if len(self.path.encode("utf-8", "surrogateescape")) > MAX_TARGET_BYTES:
            self.close_connection = True
            self._write(
                Response(
                    414,
                    {
                        "Content-Type": "text/plain; charset=utf-8",
                        "Content-Length": str(len(b"uri too long\n")),
                        "Cache-Control": "no-store",
                    },
                    b"uri too long\n",
                )
            )
            return
        peer_ip = str(self.client_address[0])
        headers = {
            str(name).lower(): str(value) for name, value in self.headers.items()
        }
        request = Request(
            method=self.command,
            path=self.path,
            client_ip=resolve_client_ip(peer_ip, headers),
            peer_ip=peer_ip,
            headers=headers,
        )
        self._write(self.service.handle(request))

    def _write(self, response: Response) -> None:
        body = response.body
        if self.command == "HEAD":
            body = b""
        self.send_response(response.status)
        for name, value in response.headers.items():
            self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def version_string(self):
        return self.server_version

    def log_message(self, format, *args):
        """Never let the standard library write tokenized paths."""
        return


def create_publication_server(
    listen: str, port: int, service: PublicationService
) -> ThreadingHTTPServer:
    """Bind the read-only publisher; only 127.0.0.1 is ever accepted."""
    if listen != LOOPBACK_LISTEN:
        raise ValueError("publisher listen address must be 127.0.0.1")
    handler = type(
        "BoundPublisherRequestHandler",
        (PublisherRequestHandler,),
        {"service": service},
    )
    return ThreadingHTTPServer((listen, port), handler)
