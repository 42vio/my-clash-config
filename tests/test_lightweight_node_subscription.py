import base64
import unittest
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import yaml

from clash_sub.domain import XuiCdn
from clash_sub.generator import render_subscription_bundle
from clash_sub.metadata_server import resolve_target
from clash_sub.node_subscription import render_node_subscription
from clash_sub.release_store import _validate_bundle
from clash_sub.sources import SourceError, normalize_xui_endpoints
from tests.test_lightweight_generator import reality_proxy
from tests.test_lightweight_end_to_end import AcceptanceHarness


class NodeSubscriptionTests(unittest.TestCase):
    def decode(self, nodes):
        return base64.b64decode(render_node_subscription(nodes), validate=False).decode().splitlines()

    def test_reality_parameters_and_unicode_name_round_trip(self):
        node = reality_proxy("自建 #1 & +")
        node["server"] = "2001:db8::1"
        uri = urlsplit(self.decode([node])[0])
        self.assertEqual(uri.scheme, "vless")
        self.assertEqual(uri.hostname, "2001:db8::1")
        self.assertEqual(uri.port, 443)
        self.assertEqual(uri.username, node["uuid"])
        self.assertEqual(unquote(uri.fragment), node["name"])
        self.assertEqual(parse_qs(uri.query), {
            "type": ["tcp"], "encryption": ["none"], "security": ["reality"],
            "pbk": [node["reality-opts"]["public-key"]],
            "sid": [node["reality-opts"]["short-id"]], "fp": ["chrome"],
            "sni": ["www.example.com"], "flow": ["xtls-rprx-vision"],
        })

    def test_ss_and_trojan_password_escaping(self):
        common = {"server": "example.test", "port": 443, "name": "节点"}
        password = "a:/@+#?中文"
        ss, trojan = self.decode([
            dict(common, type="ss", cipher="aes-128-gcm", password=password),
            dict(common, type="trojan", password=password),
        ])
        userinfo = urlsplit(ss).username
        self.assertEqual(base64.urlsafe_b64decode(userinfo + "=" * (-len(userinfo) % 4)).decode(), "aes-128-gcm:" + password)
        self.assertEqual(unquote(urlsplit(trojan).username), password)
        self.assertEqual(parse_qs(urlsplit(trojan).query)["security"], ["tls"])

    def test_local_cdn_inbound_publishes_with_reality_and_public_tls(self):
        cdn = XuiCdn("cdn.example.com", "/vless-ws")
        reality = dict(reality_proxy("REALITY"), port=10443)
        websocket = {
            "name": "Cloudflare", "type": "vless", "uuid": reality["uuid"],
            "server": "127.0.0.1", "port": 11443, "tls": False, "network": "ws",
            "ws-opts": {"path": "/vless-ws"},
        }
        templates = Path(__file__).resolve().parents[1] / "templates"
        for source in ([reality, websocket], [reality]):
            nodes = normalize_xui_endpoints(source, "node.example.com:443", cdn)
            bundle = render_subscription_bundle(False, nodes, None, templates)
            document = yaml.safe_load(bundle["compat"])
            group = next(item for item in document["proxy-groups"] if item["name"] == "🏴‍☠️ 自建节点")
            self.assertEqual(group["proxies"], [node["name"] for node in source])
        nodes = normalize_xui_endpoints([websocket], "node.example.com:443", cdn)
        self.assertEqual(nodes[0]["ws-opts"]["headers"], {"Host": cdn.hostname})
        uri = urlsplit(self.decode(nodes)[0])
        self.assertEqual((uri.hostname, uri.port), (cdn.hostname, 443))
        self.assertEqual(parse_qs(uri.query), {
            "type": ["ws"], "encryption": ["none"], "security": ["tls"],
            "path": ["/vless-ws"], "host": [cdn.hostname], "sni": [cdn.hostname],
        })
        self.assertIs(websocket["tls"], False)
        with self.assertRaises(SourceError):
            normalize_xui_endpoints(
                [dict(websocket, **{"ws-opts": {"path": "/wrong"}})],
                "node.example.com:443",
                cdn,
            )

    def test_trojan_inbound_publishes_with_reality_and_uses_public_sni(self):
        hostname = "site.example.com"
        reality = dict(reality_proxy("REALITY"), port=10443)
        trojan = {
            "name": "洛杉矶-Trojan", "type": "trojan", "server": "127.0.0.1",
            "port": 20443, "network": "tcp", "password": "test-password",
            "tls": True, "sni": hostname, "servername": hostname, "alpn": ["http/1.1"],
        }
        nodes = normalize_xui_endpoints(
            [reality, trojan], "node.example.com:443", trojan_hostname=hostname
        )
        templates = Path(__file__).resolve().parents[1] / "templates"
        bundle = render_subscription_bundle(False, nodes, None, templates)
        groups = yaml.safe_load(bundle["compat"])["proxy-groups"]
        group = next(item for item in groups if item["name"] == "🏴‍☠️ 自建节点")
        self.assertEqual(group["proxies"], ["REALITY", "洛杉矶-Trojan"])
        uri = urlsplit(self.decode(nodes)[1])
        self.assertEqual((uri.scheme, uri.hostname, uri.port), ("trojan", hostname, 443))
        self.assertEqual(parse_qs(uri.query), {
            "type": ["tcp"], "security": ["tls"], "sni": [hostname], "alpn": ["http/1.1"],
        })
        with self.assertRaises(SourceError):
            normalize_xui_endpoints(
                [dict(trojan, sni="other.example.com")],
                "node.example.com:443",
                trojan_hostname=hostname,
            )

    def test_reality_mlkem_flag_is_preserved_in_share_uri(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                node = reality_proxy("REALITY with ML-KEM")
                node["reality-opts"]["support-x25519mlkem768"] = enabled
                query = parse_qs(urlsplit(self.decode([node])[0]).query)
                self.assertEqual(query["support-x25519mlkem768"], [str(enabled).lower()])
                self.assertEqual(query["flow"], ["xtls-rprx-vision"])

    def test_reality_mlkem_flag_requires_a_boolean(self):
        node = reality_proxy("REALITY")
        node["reality-opts"]["support-x25519mlkem768"] = "true"
        with self.assertRaises(ValueError):
            render_node_subscription([node])

    def test_unsupported_options_fail_without_echoing_secrets(self):
        for changes in ({"network": "grpc"}, {"plugin": "secret-plugin"}, {"type": "unknown"}):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, "^node subscription contains unsupported options$"):
                    render_node_subscription([dict(reality_proxy("secret-name"), **changes)])

    def test_legacy_and_extended_release_shapes_are_accepted(self):
        for variants in (("compat",), ("compat", "balance", "airport")):
            self.assertEqual(_validate_bundle(dict.fromkeys(variants, "body"))[0], variants)
            extended = variants + ("nodes",)
            self.assertEqual(_validate_bundle(dict.fromkeys(extended, "body"))[0], extended)

    def test_metadata_uses_only_self_hosted_traffic(self):
        self.assertEqual(resolve_target("/profile/8/Nodes.txt"), ("/accel/8/Nodes.txt", "profile", 8))


class NodeSubscriptionAcceptanceTests(unittest.TestCase):
    def test_sync_links_authorization_rotation_and_rollback(self):
        harness = AcceptanceHarness(self)
        harness.service._render = render_subscription_bundle
        harness.import_airport()
        for client_id in (harness.owner_id, harness.member_id):
            node = reality_proxy("Self hosted %d" % client_id)
            node["port"] = 10443
            node["reality-opts"]["support-x25519mlkem768"] = True
            harness.xui_bodies[harness.source_urls[client_id]] = harness._document([node])
        harness.service.sync_all()
        first = harness.release(harness.owner_id)
        for client_id in (harness.owner_id, harness.member_id):
            release = harness.release(client_id)
            text = release.public_paths["nodes"].read_text()
            nodes = base64.b64decode(text).decode().splitlines()
            self.assertEqual(len(nodes), 1)
            self.assertEqual(urlsplit(nodes[0]).hostname, "example.com")
            self.assertEqual(urlsplit(nodes[0]).port, 443)
            self.assertEqual(parse_qs(urlsplit(nodes[0]).query)["security"], ["reality"])
            self.assertEqual(parse_qs(urlsplit(nodes[0]).query)["support-x25519mlkem768"], ["true"])
            self.assertNotIn("AmyTelecom", text)
            user = harness.state().users[client_id]
            url = "https://%s/s/%s/Nodes.txt" % (harness.config.subscription_authority, user.token)
            entry = next(item for item in harness.service.links() if item["client_id"] == client_id)
            self.assertIn(url, entry["urls"])
            self.assertIn("location = /s/%s/Nodes.txt" % user.token, harness.route_text())
        from clash_sub.state import airport_only_token
        owner_token = harness.state().users[harness.owner_id].token
        self.assertNotIn("location = /s/%s/Nodes.txt" % airport_only_token(owner_token), harness.route_text())
        self.assertIn('default_type "text/plain; charset=utf-8"', harness.route_text())
        harness.set_xui_proxy(harness.owner_id, "Changed")
        harness.service.sync_all()
        harness.service.rollback(harness.owner_id, first.release_id)
        self.assertEqual(harness.release(harness.owner_id).public_paths["nodes"].read_text(), first.public_paths["nodes"].read_text())
        rotated = harness.service.rotate_link(harness.owner_id)
        self.assertTrue(any(url.endswith("/Nodes.txt") for url in rotated["urls"]))
        self.assertNotIn("location = /s/%s/Nodes.txt" % owner_token, harness.route_text())

    def test_upgrade_from_legacy_release_and_failed_export_keeps_current(self):
        harness = AcceptanceHarness(self)
        harness.import_airport()
        harness.service.sync_all()
        self.assertFalse(any(url.endswith("/Nodes.txt") for item in harness.service.links() for url in item["urls"]))
        harness.service._render = render_subscription_bundle
        harness.service.sync_all()
        self.assertTrue(all(any(url.endswith("/Nodes.txt") for url in item["urls"]) for item in harness.service.links()))
        before = harness.active_view()
        node = reality_proxy("Unsupported")
        node.update(port=10443, network="grpc")
        harness.xui_bodies[harness.source_urls[harness.member_id]] = harness._document([node])
        result = harness.service.sync_all()
        self.assertEqual(result["errors"], ({"client_id": harness.member_id, "code": "node_subscription_export_failed"},))
        self.assertEqual(harness.active_view(), before)


class BackupNodeExportTests(unittest.TestCase):
    def test_flat_group_and_node_export_use_same_order_and_collision_names(self):
        from clash_sub.backup_subscription import PRIMARY_GROUP, BACKUP_GROUP
        from clash_sub.node_import import parse_links
        main = reality_proxy('Same')
        backup = {'name': 'Same', 'type': 'ss', 'server': '192.0.2.20', 'port': 443,
                  'cipher': 'aes-128-gcm', 'password': 'placeholder'}
        templates = Path(__file__).resolve().parents[1] / 'templates'
        bundle = render_subscription_bundle(False, [main], None, templates, backup_nodes=[backup])
        document = yaml.safe_load(bundle['compat'])
        primary = next(group for group in document['proxy-groups'] if group['name'] == PRIMARY_GROUP)
        names = [node['name'] for node in document['proxies']]
        self.assertEqual(names, ['Same [3x-ui]', 'Same [backup]'])
        self.assertEqual(primary['proxies'], names)
        self.assertEqual(primary['type'], 'fallback')
        self.assertNotIn(BACKUP_GROUP, [group['name'] for group in document['proxy-groups']])
        self.assertNotIn('Backup', document.get('proxy-providers', {}))
        decoded = base64.b64decode(bundle['nodes']).decode()
        imported = parse_links(decoded)
        self.assertEqual([node['name'] for node in imported], names)

    def test_modern_backup_extensions_survive_portable_export(self):
        from clash_sub.node_import import parse_links
        links = [
            'anytls://placeholder%3A%40@example.com/?sni=example.com&fp=chrome&insecure=1&alpn=h2#AnyTLS',
            'hy2://placeholder%3A%40@192.0.2.2:443?sni=example.com&obfs=gecko&obfs-password=placeholder&pinSHA256='
            + 'ab' * 32 + '&mport=443,8443-8445&hop-interval=30&up=100&down=200&insecure=0&alpn=h3#HY2',
            'vless://11111111-1111-1111-1111-111111111111@example.com:80?type=ws&security=none&path=%2Fws#VLESSWS',
            'trojan://placeholder@example.com:443?type=grpc&serviceName=hello%2Fworld&sni=example.com#Trojan',
            'vless://11111111-1111-1111-1111-111111111111@example.com:443?type=grpc&security=tls&serviceName=hello&sni=example.com#VLESS',
        ]
        for link in links:
            with self.subTest(scheme=urlsplit(link).scheme):
                nodes = parse_links(link)
                if nodes[0]['type'] == 'hysteria2':
                    nodes[0]['server'] = '2001:db8::2'
                exported = base64.b64decode(render_node_subscription(nodes)).decode()
                self.assertEqual(parse_links(exported), nodes)
