import base64
import unittest

from clash_sub.node_import import parse_links
from clash_sub.node_subscription import _share_uri
from clash_sub.operation import ServiceError
from tests.test_lightweight_generator import reality_proxy


class NodeImportTests(unittest.TestCase):
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
