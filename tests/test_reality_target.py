"""Read-only REALITY dest verification tests.

`scripts/check_reality_target.py` judges a prospective REALITY dest
from one `openssl s_client -brief` observation.  The synthetic
outputs below mirror the real `-brief` shape (CONNECTION ESTABLISHED,
Protocol version, Ciphers, Peer certificate, Verification, Server
Temp Key, ALPN protocol) and prove that no certificate material or
other raw target body ever reaches a serialized report.
"""

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    module_name = "scripts_%s" % name
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / "scripts" / ("%s.py" % name)
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


check_reality_target = load_script("check_reality_target")

parse_s_client_output = check_reality_target.parse_s_client_output
evaluate_target = check_reality_target.evaluate_target
probe_target = check_reality_target.probe_target
build_command = check_reality_target.build_command
InvalidTargetError = check_reality_target.InvalidTargetError


VALID_OUTPUT = """CONNECTION ESTABLISHED
Protocol version: TLSv1.3
Ciphers: TLS_AES_256_GCM_SHA384
Peer certificate: CN=www.example.com
Hash Algorithm: sha256
Signature Algorithm: rsaPSSSHA256
Verification: OK
Server Temp Key: X25519, 253 bits
ALPN protocol: h2
"""

# Fails every check and embeds certificate material plus a synthetic
# marker that must never leak into a report.
INVALID_OUTPUT = """private-marker: synthetic invalid observation
CONNECTION ESTABLISHED
Protocol version: TLSv1.2
Ciphers: TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256
Peer certificate: CN=other.example.net
-----BEGIN CERTIFICATE-----
MIIBszANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAsynthetic
-----END CERTIFICATE-----
Server Temp Key: P-256, 256 bits
Verification error: unable to get local issuer certificate
ALPN protocol: http/1.1
"""

TLS12_OUTPUT = """CONNECTION ESTABLISHED
Protocol version: TLSv1.2
Ciphers: TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256
Peer certificate: CN=www.example.com
Verification: OK
Server Temp Key: X25519, 253 bits
ALPN protocol: h2
"""

NO_ALPN_OUTPUT = """CONNECTION ESTABLISHED
Protocol version: TLSv1.3
Ciphers: TLS_AES_256_GCM_SHA384
Peer certificate: CN=www.example.com
Verification: OK
Server Temp Key: X25519, 253 bits
"""

HTTP1_ALPN_OUTPUT = TLS12_OUTPUT.replace(
    "ALPN protocol: h2", "ALPN protocol: http/1.1"
).replace("Protocol version: TLSv1.2", "Protocol version: TLSv1.3")

NO_X25519_OUTPUT = TLS12_OUTPUT.replace(
    "Protocol version: TLSv1.2", "Protocol version: TLSv1.3"
).replace("Server Temp Key: X25519, 253 bits", "Server Temp Key: P-256, 256 bits")

NAME_MISMATCH_OUTPUT = """CONNECTION ESTABLISHED
Protocol version: TLSv1.3
Ciphers: TLS_AES_256_GCM_SHA384
Peer certificate: CN=other.example.net
Verification error: unable to get local issuer certificate
Server Temp Key: X25519, 253 bits
ALPN protocol: h2
"""

REFUSED_OUTPUT = (
    "00F10000:error:0200206F:system library:connect:Connection refused:"
    "call to connect() failed\n"
)

MALFORMED_OUTPUT = "nothing recognizable here\njust noise\n"


class RealityTargetTests(unittest.TestCase):
    def test_accepts_tls13_h2_x25519_and_matching_san(self):
        observation = parse_s_client_output(VALID_OUTPUT)
        result = evaluate_target(observation, expected_server_name="www.example.com")
        self.assertTrue(result.ok)
        self.assertEqual(
            result.checks,
            {
                "reachable": True,
                "tls13": True,
                "alpn_h2": True,
                "x25519": True,
                "certificate_name": True,
            },
        )

    def test_report_never_contains_certificate_or_target_body(self):
        result = evaluate_target(
            parse_s_client_output(INVALID_OUTPUT),
            expected_server_name="www.example.com",
        )
        self.assertFalse(result.ok)
        self.assertFalse(result.checks["tls13"])
        self.assertFalse(result.checks["alpn_h2"])
        self.assertFalse(result.checks["x25519"])
        self.assertFalse(result.checks["certificate_name"])
        serialized = json.dumps(result.to_json())
        self.assertNotIn("BEGIN CERTIFICATE", serialized)
        self.assertNotIn("private-marker", serialized)

    def test_json_payload_is_limited_to_stable_fields(self):
        result = evaluate_target(
            parse_s_client_output(VALID_OUTPUT), expected_server_name="www.example.com"
        )
        payload = result.to_json()
        self.assertEqual(
            set(payload), {"ok", "checks", "elapsed_ms", "error_code", "connect_address_family"}
        )
        self.assertEqual(
            set(payload["checks"]),
            {"reachable", "tls13", "alpn_h2", "x25519", "certificate_name"},
        )

    def test_tls12_negotiation_fails_only_the_tls13_check(self):
        result = evaluate_target(
            parse_s_client_output(TLS12_OUTPUT), expected_server_name="www.example.com"
        )
        self.assertFalse(result.ok)
        self.assertFalse(result.checks["tls13"])
        self.assertTrue(result.checks["alpn_h2"])
        self.assertTrue(result.checks["x25519"])
        self.assertTrue(result.checks["certificate_name"])

    def test_missing_alpn_fails_the_h2_check(self):
        result = evaluate_target(
            parse_s_client_output(NO_ALPN_OUTPUT), expected_server_name="www.example.com"
        )
        self.assertFalse(result.checks["alpn_h2"])
        self.assertTrue(result.checks["tls13"])

    def test_http1_alpn_fails_the_h2_check(self):
        result = evaluate_target(
            parse_s_client_output(HTTP1_ALPN_OUTPUT), expected_server_name="www.example.com"
        )
        self.assertFalse(result.checks["alpn_h2"])
        self.assertTrue(result.checks["tls13"])

    def test_non_x25519_temp_key_fails_the_group_check(self):
        result = evaluate_target(
            parse_s_client_output(NO_X25519_OUTPUT), expected_server_name="www.example.com"
        )
        self.assertFalse(result.checks["x25519"])
        self.assertTrue(result.checks["tls13"])

    def test_server_name_mismatch_fails_the_certificate_check(self):
        result = evaluate_target(
            parse_s_client_output(NAME_MISMATCH_OUTPUT),
            expected_server_name="www.example.com",
        )
        self.assertFalse(result.checks["certificate_name"])
        self.assertTrue(result.checks["reachable"])

    def test_connection_refused_reports_a_stable_error_code(self):
        result = evaluate_target(
            parse_s_client_output(REFUSED_OUTPUT), expected_server_name="www.example.com"
        )
        self.assertFalse(result.ok)
        self.assertFalse(result.checks["reachable"])
        self.assertEqual(result.error_code, "connection_refused")

    def test_malformed_output_reports_a_stable_error_code(self):
        result = evaluate_target(
            parse_s_client_output(MALFORMED_OUTPUT), expected_server_name="www.example.com"
        )
        self.assertFalse(result.ok)
        self.assertFalse(result.checks["reachable"])
        self.assertEqual(result.error_code, "malformed_output")

    def test_probe_timeout_reports_timeout_without_any_output(self):
        def raising_executor(argv, timeout):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

        result = probe_target(
            "192.0.2.10",
            443,
            "www.example.com",
            timeout=1,
            executor=raising_executor,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "timeout")
        self.assertTrue(all(value is False for value in result.checks.values()))
        serialized = json.dumps(result.to_json())
        self.assertNotIn("openssl", serialized)

    def test_probe_uses_the_injected_executor_and_reports_address_family(self):
        def recording_executor(argv, timeout):
            self.assertEqual(argv[0], "openssl")
            self.assertEqual(argv[1], "s_client")
            return 0, VALID_OUTPUT

        result = probe_target(
            "2001:db8::1",
            443,
            "www.example.com",
            timeout=5,
            executor=recording_executor,
        )
        self.assertTrue(result.ok)
        self.assertIsNone(result.error_code)
        self.assertEqual(result.address_family, "ipv6")

    def test_ipv4_and_ipv6_connect_addresses_use_a_separate_sni(self):
        self.assertEqual(
            build_command("192.0.2.10", 443, "www.example.com"),
            [
                "openssl",
                "s_client",
                "-connect",
                "192.0.2.10:443",
                "-servername",
                "www.example.com",
                "-tls1_3",
                "-alpn",
                "h2",
                "-groups",
                "X25519",
                "-brief",
            ],
        )
        ipv6 = build_command("2001:db8::1", 443, "www.example.com")
        self.assertEqual(ipv6[3], "[2001:db8::1]:443")
        self.assertEqual(ipv6[5], "www.example.com")

    def test_argument_validation_rejects_metacharacters_and_bad_shapes(self):
        def quiet_executor(argv, timeout):
            return 0, b""

        for bad_address in (
            "192.0.2.10; touch /tmp/evil",
            "8.8.8.8\n",
            "",
            "exam ple.com",
            "-injected",
            "host`id`",
            "host$(id)",
        ):
            with self.assertRaises(InvalidTargetError):
                probe_target(bad_address, 443, "www.example.com", executor=quiet_executor)
        for bad_port in (0, -1, 65536, 70000):
            with self.assertRaises(InvalidTargetError):
                probe_target("192.0.2.10", bad_port, "www.example.com", executor=quiet_executor)
        for bad_name in (
            "www.example.com:443",
            "www.example.com/path",
            "www.exa mple.com",
            "-evil",
            "under_score.example.com",
            "",
        ):
            with self.assertRaises(InvalidTargetError):
                probe_target("192.0.2.10", 443, bad_name, executor=quiet_executor)


if __name__ == "__main__":
    unittest.main()
