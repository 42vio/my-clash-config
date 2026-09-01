"""Internal metadata HTTP service bound to the systemd activation socket.

Nginx (running as www-data) is the only intended client.  Exactly two GET
targets are recognised — ``/profile/<client_id>/<filename>`` and
``/airport/AmyTelecom.yaml`` — and every other request collapses into the
same fixed 404 body: the stdlib error paths would echo the request line,
so they are all overridden.  No response or log line ever contains any
part of a request.  The service reads only the traffic metadata store;
it never opens profiles, YAML documents, or the network.
"""

import http.server
import os
import re
import socket
import socketserver
from types import MappingProxyType

from clash_sub.domain import AIRPORT_FILENAME, PROFILE_FILENAMES
from clash_sub.metadata import render_subscription_userinfo

_NOT_FOUND_BODY = b"not found\n"
_MAX_TARGET_LENGTH = 256
_CLIENT_ID = re.compile(r"[1-9][0-9]{0,18}")
_PROFILE_INTERNAL = MappingProxyType(
    {filename: "/protected/" + filename for filename in PROFILE_FILENAMES.values()}
)
_AIRPORT_INTERNAL = "/protected/provider/%s" % AIRPORT_FILENAME


class MetadataServerError(RuntimeError):
    """Stable error codes for socket-activation failures."""


def listener_from_environment(environ=None, *, pid=None, fromfd=None):
    """Return the AF_UNIX listener systemd passed as descriptor 3."""
    environ = os.environ if environ is None else environ
    pid = os.getpid if pid is None else pid
    fromfd = socket.fromfd if fromfd is None else fromfd
    if environ.get("LISTEN_FDS") != "1" or environ.get("LISTEN_PID") != str(pid()):
        raise MetadataServerError("socket_activation_invalid")
    try:
        return fromfd(3, socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError as error:
        raise MetadataServerError("socket_activation_invalid") from error


def resolve_target(path):
    """Map a request target onto the fixed internal whitelist, or None.

    The whitelist is derived only from the parsed shape: a canonical
    decimal client id and one of the fixed profile filenames, or the one
    airport filename.  Query strings, extra segments, encoded control
    characters, absolute-URI forms and over-long targets never match.
    """
    if len(path) > _MAX_TARGET_LENGTH or not path.startswith("/"):
        return None
    segments = path.split("/")
    if len(segments) >= 2 and segments[1] == "airport":
        if len(segments) == 3 and segments[2] == AIRPORT_FILENAME:
            return (_AIRPORT_INTERNAL, "airport", None)
        return None
    if len(segments) == 4 and segments[1] == "profile":
        client_id_text, filename = segments[2], segments[3]
        if _CLIENT_ID.fullmatch(client_id_text) and filename in _PROFILE_INTERNAL:
            return (_PROFILE_INTERNAL[filename], "profile", int(client_id_text))
    return None


class MetadataRequestHandler(http.server.BaseHTTPRequestHandler):
    """Serve the two metadata targets; reject everything else with 404."""

    # Nginx talks to this socket one request per connection; slow peers
    # must not pin a thread longer than a few seconds.
    timeout = 5

    def do_GET(self):
        if self.request_version == "HTTP/0.9":
            # Truncated or ancient request lines never reach the store.
            self._reject()
            return
        resolved = resolve_target(self.path)
        if resolved is None:
            self._reject()
            return
        internal, kind, argument = resolved
        headers = [
            ("Content-Length", "0"),
            ("X-Accel-Redirect", internal),
        ]
        userinfo = self._userinfo(kind, argument)
        if userinfo is not None:
            headers.append(("Subscription-Userinfo", userinfo))
        self._respond(200, headers)

    def send_error(self, code, message=None, explain=None):
        # Every stdlib failure path (bad syntax, oversized request line,
        # unknown method) must collapse into the same fixed 404: the
        # defaults would echo the request line back to the client.
        self._reject()

    def log_message(self, format, *args):
        pass

    def log_request(self, code="-", size="-"):
        pass

    def log_error(self, format, *args):
        pass

    def _userinfo(self, kind, argument):
        # Metadata absence must never block the file: any store or render
        # failure degrades to a redirect without the traffic header.
        try:
            if kind == "airport":
                traffic = self.server.store.airport_traffic()
            else:
                traffic = self.server.store.traffic_for(argument)
            if traffic is None:
                return None
            return render_subscription_userinfo(traffic)
        except Exception:
            return None

    def _respond(self, status, headers=()):
        # The status line and every header are written here directly: the
        # stdlib helpers would omit the status line for HTTP/0.9 requests
        # and attach build-specific Server/Date headers.
        lines = ["HTTP/1.0 %d %s" % (status, "OK" if status == 200 else "Not Found")]
        for name, value in headers:
            lines.append("%s: %s" % (name, value))
        self.wfile.write(("\r\n".join(lines) + "\r\n\r\n").encode("iso-8859-1"))

    def _reject(self):
        self.close_connection = True
        self._respond(
            404,
            (
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Content-Length", str(len(_NOT_FOUND_BODY))),
            ),
        )
        if self.command != "HEAD":
            self.wfile.write(_NOT_FOUND_BODY)


class MetadataSocketServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Threaded HTTP server adopting an already-listening AF_UNIX socket."""

    address_family = socket.AF_UNIX
    daemon_threads = True
    # An orderly shutdown closes the listener without waiting on any
    # still-running handler thread.
    block_on_close = False

    def handle_error(self, request, client_address):
        # Transport failures (timeouts, resets, truncated requests) carry
        # no request data; the default traceback printer stays unused.
        pass

    def __init__(self, store, listener):
        try:
            address = listener.getsockname()
        except OSError:
            address = ""
        # Skip HTTPServer binding entirely: systemd or the caller already
        # owns the listening socket, and the server must adopt it as-is.
        socketserver.BaseServer.__init__(self, address, MetadataRequestHandler)
        self.socket = listener
        self.server_name = "clash-sub-metadata"
        self.server_port = 0
        self.store = store


def serve(store, listener):
    """Serve metadata requests on the given listener until shutdown."""
    server = MetadataSocketServer(store, listener)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
