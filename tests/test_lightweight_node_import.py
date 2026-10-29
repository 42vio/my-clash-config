import base64
import unittest
from urllib.parse import urlunsplit

from clash_sub.node_import import parse_links
from clash_sub.node_subscription import _share_uri
from clash_sub.operation import ServiceError
from tests.test_lightweight_generator import reality_proxy


class NodeImportTests(unittest.TestCase):
    def test_anytls_default_port_encoded_password_and_tls_parameters(self):
        link=urlunsplit(('anytls','placeholder%3Apass%40word@[2001:db8::1]','/','sni=example.com&insecure=0&fp=chrome&alpn=h2,http%2F1.1','AnyTLS%20Example'))
        node=parse_links(link)[0]
        self.assertEqual(node,{'name':'AnyTLS Example','type':'anytls','server':'2001:db8::1','port':443,'password':'placeholder:pass@word','sni':'example.com','skip-cert-verify':False,'client-fingerprint':'chrome','alpn':['h2','http/1.1']})

    def test_hysteria_alias_userpass_and_default_port(self):
        links=['hy2://placeholder:password@example.com/?sni=example.org&insecure=1#HY2',
               'hysteria2://placeholder%3Apassword@example.com/?sni=example.org&insecure=1#HY2']
        nodes=parse_links('\n'.join(links))
        self.assertEqual(nodes[0],nodes[1])
        self.assertEqual(nodes[0]['type'],'hysteria2')
        self.assertEqual(nodes[0]['password'],'placeholder:password')
        self.assertEqual(nodes[0]['port'],443)
        self.assertTrue(nodes[0]['skip-cert-verify'])

    def test_hysteria_obfuscation_pin_and_port_hopping(self):
        pin='AB'*32
        link='hysteria2://placeholder@example.com:443,8443-8445/?obfs=salamander&obfs-password=placeholder-obfs&pinSHA256='+pin+'&hop-interval=30&up=20&down=100#HY2'
        node=parse_links(link)[0]
        self.assertEqual(node['ports'],'443,8443-8445')
        self.assertEqual(node['port'],443)
        self.assertEqual(node['obfs-password'],'placeholder-obfs')
        self.assertEqual(node['fingerprint'],pin.lower())
        self.assertEqual(node['hop-interval'],30)
        self.assertEqual(node['up'],'20 Mbps')
        ipv6=urlunsplit(('hy2','placeholder@[2001:db8::1]:443','/','mport=1000-2000','HY2'))
        self.assertEqual(parse_links(ipv6)[0]['ports'],'1000-2000')

    def test_anytls_and_hysteria_reject_unsupported_or_conflicting_parameters(self):
        for link in ('anytls://placeholder@example.com?security=none',
                     'anytls://placeholder@example.com?insecure=1&allowInsecure=0',
                     'anytls://placeholder@example.com?unknown=secret',
                     'hy2://placeholder@example.com?obfs-password=placeholder',
                     'hy2://placeholder@example.com?obfs=unknown&obfs-password=placeholder',
                     'hy2://placeholder@example.com?insecure=yes',
                     'hy2://placeholder@example.com?pinSHA256=invalid',
                     'hy2://placeholder@example.com:70000/',
                     'hy2://placeholder@example.com:8443-443/',
                     'hy2://placeholder@example.com:443,8443?mport=9000',
                     'hy2://placeholder@example.com?hop-interval=0',
                     'hy2://placeholder@example.com?ech=unsupported'):
            with self.subTest(link=link),self.assertRaises(ServiceError) as context:
                parse_links(link)
            self.assertEqual(context.exception.code,'backup_node_line_1_invalid_or_unsupported')

    def test_round_trip_reality_trojan_and_shadowsocks(self):
        nodes=[reality_proxy('Reality 测试'),
               {'name':'Trojan','type':'trojan','server':'192.0.2.1','port':443,'password':'synthetic:password@value','sni':'example.com'},
               {'name':'SS','type':'ss','server':'2001:db8::1','port':443,'cipher':'aes-128-gcm','password':'synthetic:password'}]
        result=parse_links('\n'.join(_share_uri(node) for node in nodes))
        self.assertEqual([n['name'] for n in result],[n['name'] for n in nodes])
        self.assertEqual(result[0]['reality-opts'],nodes[0]['reality-opts'])
        self.assertEqual(result[1]['password'],nodes[1]['password'])
        self.assertEqual(result[2]['password'],nodes[2]['password'])
        self.assertEqual(result[2]['server'],'2001:db8::1')
        legacy=base64.b64encode(b'aes-128-gcm:placeholder-password@192.0.2.1:443').decode()
        self.assertEqual(parse_links('ss://'+legacy+'#Legacy')[0]['password'],'placeholder-password')

    def test_websocket_grpc_and_ss_2022_parameters(self):
        secret='11111111-1111-1111-1111-111111111111'
        ws=parse_links('vless://'+secret+'@example.com:443?security=tls&type=ws&host=example.org&path=%2Fws&sni=example.com#WebSocket')[0]
        self.assertEqual(ws['ws-opts'],{'path':'/ws','headers':{'Host':'example.org'}})
        grpc=parse_links('trojan://placeholder-password@example.com:443?type=grpc&serviceName=service&sni=example.com')[0]
        self.assertEqual(grpc['grpc-opts'],{'grpc-service-name':'service'})
        ss=parse_links(_share_uri({'type':'ss','name':'SS','cipher':'2022-blake3-aes-128-gcm','password':'synthetic/base64==','server':'example.com','port':443}))[0]
        self.assertEqual(ss['password'],'synthetic/base64==')

    def test_unsupported_and_invalid_lines_fail_without_exposing_input(self):
        good='trojan://placeholder-password@example.com:443#Good'
        for bad in ('vmess://placeholder@example.com:443',good+'?unknown=1',
                    'vless://placeholder@example.com:443',
                    'trojan://placeholder-password@example.com:443?type=ws&type=tcp',
                    'trojan://placeholder-password@example.com:443?type=tcp&path=%2F',
                    'trojan://placeholder-password@example.com:443?type=tcp&unsupported=secret',
                    'trojan://placeholder-password@example.com:443?security=none',
                    'trojan://placeholder-password@example.com:443?fp=%00'):
            # Fragment text can contain '?', so put the unknown parameter before '#'.
            if bad==good+'?unknown=1':bad=good.replace('#Good','?unknown=1#Good')
            with self.subTest(bad=bad),self.assertRaises(ServiceError) as context:
                parse_links(good+'\n'+bad)
            self.assertEqual(context.exception.code,'backup_node_line_2_invalid_or_unsupported')
            self.assertNotIn('placeholder-password',str(context.exception))
