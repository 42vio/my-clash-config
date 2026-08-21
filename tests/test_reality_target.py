"""Read-only REALITY dest verification tests.

`scripts/check_reality_target.py` judges a prospective REALITY dest
from one full-mode `openssl s_client` observation (no ``-brief``:
quiet mode routes the ALPN summary through a discarded BIO, so the
ALPN line never appears there).  The passing, TLS 1.2, and
connection-refused constants below are real captures made with
OpenSSL 3.6.3 against www.example.com (the only committed domain);
the environment-specific ``Connecting to ...`` resolver line was
removed and the OpenSSL 3.0 ``Server Temp Key:`` label variant is a
documented hand adjustment (OpenSSL >= 3.5 renamed that line to
``Peer Temp Key:``).  The tests prove that no certificate material
or other raw target body ever reaches a serialized report.
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


# Real capture: passing TLS 1.3 + h2 + X25519 handshake, SNI and
# -verify_hostname www.example.com, OpenSSL 3.6.3 (Peer Temp Key era).
VALID_OUTPUT = """depth=3 C=US, O=SSL Corporation, CN=SSL.com TLS ECC Root CA 2022
verify return:1
depth=2 C=US, O=SSL Corporation, CN=SSL.com TLS Transit ECC CA R2
verify return:1
depth=1 C=US, O=SSL Corporation, CN=Cloudflare TLS Issuing ECC CA 3
verify return:1
depth=0 CN=example.com
verify return:1
CONNECTED(00000005)
---
Certificate chain
 0 s:CN=example.com
   i:C=US, O=SSL Corporation, CN=Cloudflare TLS Issuing ECC CA 3
   a:PKEY: EC, (prime256v1); sigalg: ecdsa-with-SHA256
   v:NotBefore: Jul 29 22:10:08 2026 GMT; NotAfter: Oct 27 22:17:21 2026 GMT
 1 s:C=US, O=SSL Corporation, CN=Cloudflare TLS Issuing ECC CA 3
   i:C=US, O=SSL Corporation, CN=SSL.com TLS Transit ECC CA R2
   a:PKEY: EC, (prime256v1); sigalg: ecdsa-with-SHA384
   v:NotBefore: May 29 19:49:45 2025 GMT; NotAfter: May 27 19:49:44 2035 GMT
 2 s:C=US, O=SSL Corporation, CN=SSL.com TLS Transit ECC CA R2
   i:C=US, O=SSL Corporation, CN=SSL.com TLS ECC Root CA 2022
   a:PKEY: EC, (secp384r1); sigalg: ecdsa-with-SHA384
   v:NotBefore: Oct 21 17:02:23 2022 GMT; NotAfter: Oct 17 17:02:22 2037 GMT
 3 s:C=US, O=SSL Corporation, CN=SSL.com TLS ECC Root CA 2022
   i:C=GB, ST=Greater Manchester, L=Salford, O=Comodo CA Limited, CN=AAA Certificate Services
   a:PKEY: EC, (secp384r1); sigalg: sha256WithRSAEncryption
   v:NotBefore: Aug  1 00:00:00 2025 GMT; NotAfter: Dec 31 23:59:59 2028 GMT
---
Server certificate
-----BEGIN CERTIFICATE-----
MIID5jCCA42gAwIBAgIQBiTQqzEVWHgLfVITuWMYMTAKBggqhkjOPQQDAjBRMQsw
CQYDVQQGEwJVUzEYMBYGA1UECgwPU1NMIENvcnBvcmF0aW9uMSgwJgYDVQQDDB9D
bG91ZGZsYXJlIFRMUyBJc3N1aW5nIEVDQyBDQSAzMB4XDTI2MDcyOTIyMTAwOFoX
DTI2MTAyNzIyMTcyMVowFjEUMBIGA1UEAwwLZXhhbXBsZS5jb20wWTATBgcqhkjO
PQIBBggqhkjOPQMBBwNCAAR2Tgmj3bLPRaVN0Vud8FEAUiMz3Z2Bd5lti39uhuvB
ARyn+R6JJkBCv54dlTizzaUBzLnriaPVW9uysYIJXTVio4ICgDCCAnwwDAYDVR0T
AQH/BAIwADAfBgNVHSMEGDAWgBSDA/3n9vVKTRVB9O0iFtMyCj7KZjBsBggrBgEF
BQcBAQRgMF4wOQYIKwYBBQUHMAKGLWh0dHA6Ly9pLmNmLWkuc3NsLmNvbS9DbG91
ZGZsYXJlLVRMUy1JLUUzLmNlcjAhBggrBgEFBQcwAYYVaHR0cDovL28uY2YtaS5z
c2wuY29tMCUGA1UdEQQeMByCC2V4YW1wbGUuY29tgg0qLmV4YW1wbGUuY29tMCMG
A1UdIAQcMBowCAYGZ4EMAQIBMA4GDCsGAQQBgqkwAQMBATATBgNVHSUEDDAKBggr
BgEFBQcDATBTBgNVHR8ETDBKMEigRqBEhkJodHRwOi8vYy5jZi1pLnNzbC5jb20v
YWU4MDFlZDFjNTViYjU3OWQ3OTIwOGIwZDc3MmFjZmI4Y2MzYTIwOC5jcmwwDgYD
VR0PAQH/BAQDAgeAMA8GCSsGAQQBgtpLLAQCBQAwggEEBgorBgEEAdZ5AgQCBIH1
BIHyAPAAdwCUTkOH+uzB74HzGSQmqBhlAcfTXzgCAT9yZ31VNy4Z2AAAAZ+v9sM2
AAAEAwBIMEYCIQD9WFotRGzWRjLUpKu5UgFVEIW2JB7MtvZe+tocSNgcyQIhAJCF
dDoCWE99JjFKSmzjeRhbiH0M3Aw+h414y9bGxT+PAHUAyKPEf8ezrbk1awE/anoS
beM6TkOlxkb5l605dZkdz5oAAAGfr/bDTAAABAMARjBEAiAKprPtjMQLlLrSks4e
CDoJZ6WqekRLH6AWHSHco9LXtQIgMsRhNtbw0Gp9Q0ItZB5D/0qTzrPKMBDbJZor
+NZkce4wCgYIKoZIzj0EAwIDRwAwRAIgELh9REqDsIBMBAkADWsc3iuhbkwHyfcv
6w+HsjhdPcwCIDzda23fZzKA2+qG5L/k1ti5g4rk3WiJU0UbvpUGLKKv
-----END CERTIFICATE-----
subject=CN=example.com
issuer=C=US, O=SSL Corporation, CN=Cloudflare TLS Issuing ECC CA 3
---
No client certificate CA names sent
Peer signing digest: SHA256
Peer signature type: ecdsa_secp256r1_sha256
Peer Temp Key: X25519, 253 bits
---
SSL handshake has read 3991 bytes and written 328 bytes
Verification: OK
Verified peername: *.example.com
---
New, TLSv1.3, Cipher is TLS_AES_256_GCM_SHA384
Protocol: TLSv1.3
Server public key is 256 bit
This TLS version forbids renegotiation.
Compression: NONE
Expansion: NONE
ALPN protocol: h2
Early data was not sent
Verify return code: 0 (ok)
---
DONE
"""

# Real capture: the same endpoint negotiating TLS 1.2 (tls13 False,
# everything else true).  Captured with -tls1_2.
TLS12_OUTPUT = """depth=3 C=US, O=SSL Corporation, CN=SSL.com TLS ECC Root CA 2022
verify return:1
depth=2 C=US, O=SSL Corporation, CN=SSL.com TLS Transit ECC CA R2
verify return:1
depth=1 C=US, O=SSL Corporation, CN=Cloudflare TLS Issuing ECC CA 3
verify return:1
depth=0 CN=example.com
verify return:1
80D1A849F87F0000:error:0A00017A:SSL routines:tls12_check_peer_sigalg:wrong curve:ssl/t1_lib.c:2820:
CONNECTED(00000005)
---
Certificate chain
 0 s:CN=example.com
   i:C=US, O=SSL Corporation, CN=Cloudflare TLS Issuing ECC CA 3
   a:PKEY: EC, (prime256v1); sigalg: ecdsa-with-SHA256
   v:NotBefore: Jul 29 22:10:08 2026 GMT; NotAfter: Oct 27 22:17:21 2026 GMT
 1 s:C=US, O=SSL Corporation, CN=Cloudflare TLS Issuing ECC CA 3
   i:C=US, O=SSL Corporation, CN=SSL.com TLS Transit ECC CA R2
   a:PKEY: EC, (prime256v1); sigalg: ecdsa-with-SHA384
   v:NotBefore: May 29 19:49:45 2025 GMT; NotAfter: May 27 19:49:44 2035 GMT
 2 s:C=US, O=SSL Corporation, CN=SSL.com TLS Transit ECC CA R2
   i:C=US, O=SSL Corporation, CN=SSL.com TLS ECC Root CA 2022
   a:PKEY: EC, (secp384r1); sigalg: ecdsa-with-SHA384
   v:NotBefore: Oct 21 17:02:23 2022 GMT; NotAfter: Oct 17 17:02:22 2037 GMT
 3 s:C=US, O=SSL Corporation, CN=SSL.com TLS ECC Root CA 2022
   i:C=GB, ST=Greater Manchester, L=Salford, O=Comodo CA Limited, CN=AAA Certificate Services
   a:PKEY: EC, (secp384r1); sigalg: sha256WithRSAEncryption
   v:NotBefore: Aug  1 00:00:00 2025 GMT; NotAfter: Dec 31 23:59:59 2028 GMT
---
Server certificate
-----BEGIN CERTIFICATE-----
MIID5jCCA42gAwIBAgIQBiTQqzEVWHgLfVITuWMYMTAKBggqhkjOPQQDAjBRMQsw
CQYDVQQGEwJVUzEYMBYGA1UECgwPU1NMIENvcnBvcmF0aW9uMSgwJgYDVQQDDB9D
bG91ZGZsYXJlIFRMUyBJc3N1aW5nIEVDQyBDQSAzMB4XDTI2MDcyOTIyMTAwOFoX
DTI2MTAyNzIyMTcyMVowFjEUMBIGA1UEAwwLZXhhbXBsZS5jb20wWTATBgcqhkjO
PQIBBggqhkjOPQMBBwNCAAR2Tgmj3bLPRaVN0Vud8FEAUiMz3Z2Bd5lti39uhuvB
ARyn+R6JJkBCv54dlTizzaUBzLnriaPVW9uysYIJXTVio4ICgDCCAnwwDAYDVR0T
AQH/BAIwADAfBgNVHSMEGDAWgBSDA/3n9vVKTRVB9O0iFtMyCj7KZjBsBggrBgEF
BQcBAQRgMF4wOQYIKwYBBQUHMAKGLWh0dHA6Ly9pLmNmLWkuc3NsLmNvbS9DbG91
ZGZsYXJlLVRMUy1JLUUzLmNlcjAhBggrBgEFBQcwAYYVaHR0cDovL28uY2YtaS5z
c2wuY29tMCUGA1UdEQQeMByCC2V4YW1wbGUuY29tgg0qLmV4YW1wbGUuY29tMCMG
A1UdIAQcMBowCAYGZ4EMAQIBMA4GDCsGAQQBgqkwAQMBATATBgNVHSUEDDAKBggr
BgEFBQcDATBTBgNVHR8ETDBKMEigRqBEhkJodHRwOi8vYy5jZi1pLnNzbC5jb20v
YWU4MDFlZDFjNTViYjU3OWQ3OTIwOGIwZDc3MmFjZmI4Y2MzYTIwOC5jcmwwDgYD
VR0PAQH/BAQDAgeAMA8GCSsGAQQBgtpLLAQCBQAwggEEBgorBgEEAdZ5AgQCBIH1
BIHyAPAAdwCUTkOH+uzB74HzGSQmqBhlAcfTXzgCAT9yZ31VNy4Z2AAAAZ+v9sM2
AAAEAwBIMEYCIQD9WFotRGzWRjLUpKu5UgFVEIW2JB7MtvZe+tocSNgcyQIhAJCF
dDoCWE99JjFKSmzjeRhbiH0M3Aw+h414y9bGxT+PAHUAyKPEf8ezrbk1awE/anoS
beM6TkOlxkb5l605dZkdz5oAAAGfr/bDTAAABAMARjBEAiAKprPtjMQLlLrSks4e
CDoJZ6WqekRLH6AWHSHco9LXtQIgMsRhNtbw0Gp9Q0ItZB5D/0qTzrPKMBDbJZor
+NZkce4wCgYIKoZIzj0EAwIDRwAwRAIgELh9REqDsIBMBAkADWsc3iuhbkwHyfcv
6w+HsjhdPcwCIDzda23fZzKA2+qG5L/k1ti5g4rk3WiJU0UbvpUGLKKv
-----END CERTIFICATE-----
subject=CN=example.com
issuer=C=US, O=SSL Corporation, CN=Cloudflare TLS Issuing ECC CA 3
---
No client certificate CA names sent
Peer Temp Key: X25519, 253 bits
---
SSL handshake has read 3883 bytes and written 217 bytes
Verification: OK
Verified peername: example.com
---
New, (NONE), Cipher is (NONE)
Protocol: TLSv1.2
Server public key is 256 bit
Secure Renegotiation IS supported
Compression: NONE
Expansion: NONE
ALPN protocol: h2
SSL-Session:
    Protocol  : TLSv1.2
    Cipher    : 0000
    Session-ID: 
    Session-ID-ctx: 
    Master-Key: 
    PSK identity: None
    PSK identity hint: None
    SRP username: None
    Start Time: 1787336528
    Timeout   : 7200 (sec)
    Verify return code: 0 (ok)
    Extended master secret: yes
---
80D1A849F87F0000:error:0A000197:SSL routines:SSL_shutdown:shutdown while in init:ssl/ssl_lib.c:2804:
"""

# Real capture: connection refused against a closed loopback port.
REFUSED_OUTPUT = """80D1A849F87F0000:error:8000003D:system library:BIO_connect:Connection refused:crypto/bio/bio_sock2.c:183:calling connect()
80D1A849F87F0000:error:10000067:BIO routines:BIO_connect:connect error:crypto/bio/bio_sock2.c:185:
connect:errno=61
"""

# Real capture: TCP reachable but the TLS handshake fails with NO
# peer certificate (alert 40).  OpenSSL then prints vacuous success
# ("Verification: OK", "Verify return code: 0 (ok)") and a
# "Protocol: TLSv1.3" line that reflects only the attempted version
# ("New, (NONE), Cipher is (NONE)") — neither may produce a true
# tls13 or certificate_name boolean.
NO_CERT_OUTPUT = """80D1A849F87F0000:error:0A000410:SSL routines:ssl3_read_bytes:ssl/tls alert handshake failure:ssl/record/rec_layer_s3.c:918:SSL alert number 40
CONNECTED(00000005)
---
no peer certificate available
---
No client certificate CA names sent
Negotiated TLS1.3 group: <NULL>
---
SSL handshake has read 7 bytes and written 248 bytes
Verification: OK
---
New, (NONE), Cipher is (NONE)
Protocol: TLSv1.3
This TLS version forbids renegotiation.
Compression: NONE
Expansion: NONE
No ALPN negotiated
Early data was not sent
Verify return code: 0 (ok)
---
80D1A849F87F0000:error:0A000197:SSL routines:SSL_shutdown:shutdown while in init:ssl/ssl_lib.c:2804:
"""

# Hand adjustment: OpenSSL 3.0 (the Debian 12 target) still labels the
# key-exchange line "Server Temp Key:"; OpenSSL >= 3.5 prints "Peer
# Temp Key:".  The parser must accept both labels.
SERVER_TEMP_KEY_OUTPUT = VALID_OUTPUT.replace(
    "Peer Temp Key: X25519, 253 bits", "Server Temp Key: X25519, 253 bits"
)

NO_ALPN_OUTPUT = VALID_OUTPUT.replace("ALPN protocol: h2", "No ALPN negotiated")

HTTP1_ALPN_OUTPUT = VALID_OUTPUT.replace(
    "ALPN protocol: h2", "ALPN protocol: http/1.1"
)

NO_X25519_OUTPUT = VALID_OUTPUT.replace(
    "Peer Temp Key: X25519, 253 bits", "Peer Temp Key: P-256, 256 bits"
)

# Hostname verification fails: the -verify_hostname result is nonzero,
# so certificate_name must be false even though the chain itself was
# verifiable in the original capture.
NAME_MISMATCH_OUTPUT = VALID_OUTPUT.replace(
    "Verify return code: 0 (ok)", "Verify return code: 10 (certificate hostname mismatch)"
).replace("Verified peername: *.example.com\n", "")

# Fails every check and embeds a synthetic marker next to the real
# (public) certificate block: neither may leak into a report.
INVALID_OUTPUT = (
    "private-marker: synthetic invalid observation\n"
    + VALID_OUTPUT.replace(
        "New, TLSv1.3, Cipher is TLS_AES_256_GCM_SHA384",
        "New, (NONE), Cipher is (NONE)",
    )
    .replace("Protocol: TLSv1.3", "Protocol: TLSv1.2")
    .replace("Peer Temp Key: X25519, 253 bits", "Peer Temp Key: P-256, 256 bits")
    .replace("ALPN protocol: h2", "ALPN protocol: http/1.1")
    .replace("Verify return code: 0 (ok)", "Verify return code: 10 (certificate hostname mismatch)")
    .replace("Verification: OK\n", "")
    .replace("Verified peername: *.example.com\n", "")
)

# Retained parser compatibility for -brief-style text piped in by a
# human: brief mode prints these labels (and never an ALPN line).
BRIEF_COMPAT_OUTPUT = """CONNECTION ESTABLISHED
Protocol version: TLSv1.3
Ciphersuite: TLS_AES_256_GCM_SHA384
Peer certificate: CN=www.example.com
Verification: OK
Server Temp Key: X25519, 253 bits
"""

MALFORMED_OUTPUT = "nothing recognizable here\njust noise\n"


def strip_verification_evidence(text):
    """Remove every verification signal to exercise the name fallbacks."""
    return "".join(
        line + "\n"
        for line in text.splitlines()
        if not line.startswith(("Verification: OK", "Verified peername:", "Verify return code"))
    )


FALLBACK_OUTPUT = strip_verification_evidence(VALID_OUTPUT)

FALLBACK_SUBJECT_MISMATCH = FALLBACK_OUTPUT.replace(
    "subject=CN=example.com", "subject=CN=other.example.net"
)

FALLBACK_SAN_OUTPUT = FALLBACK_OUTPUT + (
    "X509v3 Subject Alternative Name: DNS:www.example.com\n"
)

FALLBACK_WILDCARD_OUTPUT = FALLBACK_OUTPUT + "Verified peername: *.example.com\n"


class RealityTargetTests(unittest.TestCase):
    def test_accepts_tls13_h2_x25519_and_verified_hostname(self):
        observation = parse_s_client_output(VALID_OUTPUT)
        result = evaluate_target(observation, expected_server_name="www.example.com")
        self.assertTrue(result.ok, result.to_json())
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

    def test_openssl_3_0_server_temp_key_label_is_accepted(self):
        result = evaluate_target(
            parse_s_client_output(SERVER_TEMP_KEY_OUTPUT),
            expected_server_name="www.example.com",
        )
        self.assertTrue(result.checks["x25519"])
        self.assertTrue(result.ok, result.to_json())

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
            parse_s_client_output(TLS12_OUTPUT), expected_server_name="example.com"
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

    def test_hostname_verification_failure_fails_the_certificate_check(self):
        result = evaluate_target(
            parse_s_client_output(NAME_MISMATCH_OUTPUT),
            expected_server_name="www.example.com",
        )
        self.assertFalse(result.checks["certificate_name"])
        self.assertTrue(result.checks["reachable"])

    def test_brief_style_text_still_parses_without_alpn(self):
        result = evaluate_target(
            parse_s_client_output(BRIEF_COMPAT_OUTPUT),
            expected_server_name="www.example.com",
        )
        self.assertTrue(result.checks["reachable"])
        self.assertTrue(result.checks["tls13"])
        self.assertTrue(result.checks["x25519"])
        self.assertTrue(result.checks["certificate_name"])
        self.assertFalse(result.checks["alpn_h2"])

    def test_certificate_name_falls_back_to_san_and_subject(self):
        # No verification evidence at all: the SAN entry decides...
        result = evaluate_target(
            parse_s_client_output(FALLBACK_SAN_OUTPUT),
            expected_server_name="www.example.com",
        )
        self.assertTrue(result.checks["certificate_name"])
        # ...and an unrelated subject CN fails.
        result = evaluate_target(
            parse_s_client_output(FALLBACK_SUBJECT_MISMATCH),
            expected_server_name="www.example.com",
        )
        self.assertFalse(result.checks["certificate_name"])
        # Exact subject match passes the fallback.
        result = evaluate_target(
            parse_s_client_output(FALLBACK_OUTPUT),
            expected_server_name="example.com",
        )
        self.assertTrue(result.checks["certificate_name"])

    def test_wildcard_covers_exactly_one_extra_label(self):
        result = evaluate_target(
            parse_s_client_output(FALLBACK_WILDCARD_OUTPUT),
            expected_server_name="www.example.com",
        )
        self.assertTrue(result.checks["certificate_name"])
        result = evaluate_target(
            parse_s_client_output(FALLBACK_WILDCARD_OUTPUT),
            expected_server_name="a.b.example.com",
        )
        self.assertFalse(result.checks["certificate_name"])

    def test_connection_refused_reports_a_stable_error_code(self):
        result = evaluate_target(
            parse_s_client_output(REFUSED_OUTPUT), expected_server_name="www.example.com"
        )
        self.assertFalse(result.ok)
        self.assertFalse(result.checks["reachable"])
        self.assertEqual(result.error_code, "connection_refused")

    def test_no_certificate_handshake_yields_no_vacuous_booleans(self):
        result = evaluate_target(
            parse_s_client_output(NO_CERT_OUTPUT),
            expected_server_name="a.b.example.com",
        )
        self.assertTrue(result.checks["reachable"])
        self.assertFalse(result.checks["tls13"])
        self.assertFalse(result.checks["alpn_h2"])
        self.assertFalse(result.checks["x25519"])
        self.assertFalse(result.checks["certificate_name"])
        self.assertFalse(result.ok)

    def test_no_certificate_handshake_still_exits_rejected(self):
        result = probe_target(
            "www.example.com",
            443,
            "a.b.example.com",
            executor=lambda argv, timeout: (1, NO_CERT_OUTPUT),
        )
        self.assertFalse(result.ok)
        self.assertEqual(
            result.checks,
            {
                "reachable": True,
                "tls13": False,
                "alpn_h2": False,
                "x25519": False,
                "certificate_name": False,
            },
        )

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

    def test_probe_reports_missing_openssl_binary(self):
        result = probe_target(
            "192.0.2.10",
            443,
            "www.example.com",
            executor=lambda argv, timeout: (127, ""),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "openssl_unavailable")
        self.assertTrue(all(value is False for value in result.checks.values()))

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
        self.assertTrue(result.ok, result.to_json())
        self.assertIsNone(result.error_code)
        self.assertEqual(result.address_family, "ipv6")

    def test_command_drops_brief_and_verifies_the_hostname(self):
        self.assertEqual(
            build_command("192.0.2.10", 443, "www.example.com"),
            [
                "openssl",
                "s_client",
                "-connect",
                "192.0.2.10:443",
                "-servername",
                "www.example.com",
                "-verify_hostname",
                "www.example.com",
                "-tls1_3",
                "-alpn",
                "h2",
                "-groups",
                "X25519",
            ],
        )
        ipv6 = build_command("2001:db8::1", 443, "www.example.com")
        self.assertEqual(ipv6[3], "[2001:db8::1]:443")
        self.assertEqual(ipv6[5], "www.example.com")
        self.assertEqual(ipv6[7], "www.example.com")
        self.assertNotIn("-brief", ipv6)

    def test_argument_validation_rejects_metacharacters_and_bad_shapes(self):
        def quiet_executor(argv, timeout):
            return 0, ""

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
