import base64
import unittest
from urllib.parse import parse_qs, unquote, urlsplit

from clash_sub.generator import render_subscription_bundle
from clash_sub.metadata_server import resolve_target
from clash_sub.node_subscription import render_node_subscription
from clash_sub.release_store import _validate_bundle
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
