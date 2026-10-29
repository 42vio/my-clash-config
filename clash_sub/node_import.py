"""Strict share-link import. Errors identify a line without exposing credentials."""
import base64
import re
import uuid
from urllib.parse import parse_qsl, unquote, urlsplit

from clash_sub.operation import ServiceError


def _decode(value):
    return base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True).decode('utf-8')


def _port_list(value):
    if not re.fullmatch(r'\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*', value):
        raise ValueError
    for item in value.split(','):
        bounds=[int(v) for v in item.split('-')]
        if not all(1 <= v <= 65535 for v in bounds) or bounds[0] > bounds[-1]:
            raise ValueError
    return int(value.split(',')[0].split('-')[0])


def _parse_tls_proxy(url, node, query):
    """Map AnyTLS / Hysteria 2 share parameters without dropping extensions."""
    hysteria=node['type']=='hysteria2'
    allowed={'sni','insecure','allowInsecure','alpn','security','type'}
    allowed |= {'obfs','obfs-password','pinSHA256','mport','hop-interval','up','down'} if hysteria else {'fp'}
    if set(query)-allowed or query.get('security','tls')!='tls' or query.get('type','tcp' if not hysteria else 'udp')!=('udp' if hysteria else 'tcp'):
        raise ValueError
    if url.username is None or (not hysteria and url.password is not None):
        raise ValueError
    auth=unquote(url.netloc.rsplit('@',1)[0],errors='strict')
    if not auth or any(ord(c)<32 for c in auth):
        raise ValueError
    node['password']=auth
    if query.get('sni'):node['sni']=query['sni']
    flags=[query[k] for k in ('insecure','allowInsecure') if k in query]
    if flags:
        if any(value not in ('0','1','false','true') for value in flags):raise ValueError
        values=[value in ('1','true') for value in flags]
        if len(set(values))!=1:raise ValueError
        node['skip-cert-verify']=values[0]
    if 'alpn' in query:
        node['alpn']=query['alpn'].split(',')
        if not all(node['alpn']):raise ValueError
    if not hysteria:
        if query.get('fp'):node['client-fingerprint']=query['fp']
        return node
    if 'obfs' in query:
        if query['obfs'] not in ('salamander','gecko') or not query.get('obfs-password'):raise ValueError
        node.update(obfs=query['obfs'],**{'obfs-password':query['obfs-password']})
    elif 'obfs-password' in query:raise ValueError
    if 'pinSHA256' in query:
        fingerprint=query['pinSHA256'].replace(':','')
        if not re.fullmatch(r'[0-9a-fA-F]{64}',fingerprint):raise ValueError
        node['fingerprint']=fingerprint.lower()
    if 'mport' in query:
        if 'ports' in node:raise ValueError
        _port_list(query['mport'])
        node['ports']=query['mport']
    if 'hop-interval' in query:
        if not query['hop-interval'].isascii() or not query['hop-interval'].isdigit() or int(query['hop-interval'])<=0:raise ValueError
        node['hop-interval']=int(query['hop-interval'])
    for key in ('up','down'):
        if key in query:
            if not re.fullmatch(r'\d+(?:\.\d+)?',query[key]) or float(query[key])<=0:raise ValueError
            node[key]=query[key]+' Mbps'
    return node


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
    if url.scheme not in ('vless', 'trojan', 'ss', 'anytls', 'hysteria2', 'hy2') or url.path not in ('', '/'):
        raise ValueError
    kind='hysteria2' if url.scheme=='hy2' else url.scheme
    host=url.hostname
    ports=None
    if kind=='hysteria2':
        address=url.netloc.rsplit('@',1)[-1]
        ports=address.split(']',1)[1].removeprefix(':') if address.startswith('[') else address.partition(':')[2]
        port=_port_list(ports) if ports else 443
    else:
        port=url.port
        if kind=='anytls' and port is None:port=443
    if not host or not port or not 1 <= port <= 65535 or any(c in host for c in '/@?#[] \\'):
        raise ValueError
    name = unquote(url.fragment, errors='strict') or '%s %s:%s' % (url.scheme.upper(), host, port)
    if len(name) > 200 or any(ord(c) < 32 for c in name):
        raise ValueError
    pairs = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True, errors='strict')
    query = dict(pairs)
    if len(query) != len(pairs) or any(any(ord(c)<32 for c in value) for value in query.values()):
        raise ValueError
    node = {'name': name, 'type': kind, 'server': host, 'port': port}
    if kind in ('anytls','hysteria2'):
        if ports and (',' in ports or '-' in ports):node['ports']=ports
        return _parse_tls_proxy(url,node,query)
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
