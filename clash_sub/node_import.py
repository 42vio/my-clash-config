"""Strict share-link import. Errors identify a line without exposing credentials."""
import base64
import re
import uuid
from urllib.parse import parse_qsl, unquote, urlsplit

from clash_sub.operation import ServiceError


def _decode(value):
    return base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True).decode('utf-8')


def parse_links(text):
    if not isinstance(text, str) or len(text.encode()) > 1024 * 1024:
        raise ServiceError('backup_nodes_invalid')
    nodes = []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            nodes.append(_parse(line))
            if len(nodes) > 1000:
                raise ValueError
        except Exception:
            raise ServiceError('backup_node_line_%d_invalid_or_unsupported' % number) from None
    if not nodes:
        raise ServiceError('backup_nodes_invalid')
    return nodes


def _parse(line):
    if any(ord(c) < 33 for c in line) or re.search(r'%(?![0-9a-fA-F]{2})', line):
        raise ValueError
    if line.startswith('ss://') and '@' not in line.split('#', 1)[0]:
        encoded, separator, fragment = line[5:].partition('#')
        line = 'ss://' + _decode(encoded) + (separator + fragment if separator else '')
    url = urlsplit(line)
    if url.scheme not in ('vless', 'trojan', 'ss') or url.path not in ('', '/'):
        raise ValueError
    host, port = url.hostname, url.port
    if not host or not port or not 1 <= port <= 65535 or any(c in host for c in '/@?#[] \\'):
        raise ValueError
    name = unquote(url.fragment, errors='strict') or '%s %s:%s' % (url.scheme.upper(), host, port)
    if len(name) > 200 or any(ord(c) < 32 for c in name):
        raise ValueError
    pairs = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True, errors='strict')
    query = dict(pairs)
    if len(query) != len(pairs) or any(any(ord(c)<32 for c in value) for value in query.values()):
        raise ValueError
    node = {'name': name, 'type': url.scheme, 'server': host, 'port': port}
    if url.scheme == 'ss':
        if query:
            raise ValueError
        userinfo = url.netloc.rsplit('@', 1)[0]
        raw = unquote(userinfo, errors='strict')
        if ':' not in raw:
            raw = _decode(raw)
        cipher, password = raw.split(':', 1)
        if not cipher or not password:
            raise ValueError
        node.update(cipher=cipher, password=password)
        return node
    if url.username is None or url.password is not None:
        raise ValueError
    secret = unquote(url.username, errors='strict')
    if not secret or any(ord(c) < 32 for c in secret):
        raise ValueError
    node['uuid' if url.scheme == 'vless' else 'password'] = str(uuid.UUID(secret)) if url.scheme == 'vless' else secret
    allowed = {'type', 'security', 'encryption', 'flow', 'sni', 'fp', 'pbk', 'sid', 'alpn', 'allowInsecure', 'support-x25519mlkem768', 'host', 'path', 'serviceName'}
    if set(query) - allowed or query.get('encryption', 'none') != 'none':
        raise ValueError
    network = query.get('type', 'tcp')
    security = query.get('security', 'tls' if url.scheme == 'trojan' else 'none')
    if network not in ('tcp', 'ws', 'grpc') or security not in ('none', 'tls', 'reality'):
        raise ValueError
    if url.scheme == 'trojan' and (security != 'tls' or query.get('flow')):
        raise ValueError
    if query.get('flow') and (query['flow'] != 'xtls-rprx-vision' or network != 'tcp' or security == 'none'):
        raise ValueError
    node.update(network=network, tls=security != 'none')
    for source, target in (('sni', 'servername'), ('fp', 'client-fingerprint'), ('flow', 'flow')):
        if query.get(source):
            node[target] = query[source]
    if 'alpn' in query:
        node['alpn'] = query['alpn'].split(',')
        if not all(node['alpn']):
            raise ValueError
    if 'allowInsecure' in query:
        if query['allowInsecure'] not in ('0', '1', 'false', 'true'):
            raise ValueError
        node['skip-cert-verify'] = query['allowInsecure'] in ('1', 'true')
    if security == 'reality':
        if not query.get('pbk') or not re.fullmatch(r'(?:[0-9a-fA-F]{2}){0,8}', query.get('sid', '')):
            raise ValueError
        node['reality-opts'] = {'public-key': query['pbk'], 'short-id': query.get('sid', '')}
        if 'support-x25519mlkem768' in query:
            if query['support-x25519mlkem768'] not in ('true', 'false'):
                raise ValueError
            node['reality-opts']['support-x25519mlkem768'] = query['support-x25519mlkem768'] == 'true'
    elif any(k in query for k in ('pbk', 'sid', 'support-x25519mlkem768')):
        raise ValueError
    if network == 'ws':
        if 'serviceName' in query:
            raise ValueError
        node['ws-opts'] = {'path': query.get('path', '/')}
        if query.get('host'):
            node['ws-opts']['headers'] = {'Host': query['host']}
    elif network == 'grpc':
        if 'host' in query or 'path' in query:
            raise ValueError
        node['grpc-opts'] = {'grpc-service-name': query.get('serviceName', '')}
    elif any(k in query for k in ('host', 'path', 'serviceName')):
        raise ValueError
    return node
