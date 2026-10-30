import json
import queue
import socket
import threading
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from clash_sub.speeds import PanelAuthError, PanelSpeeds, panel_login, parse_traffic


def frame(rows=None, **extra):
    return json.dumps({'type':'traffic','payload':{'clientTraffics':rows or [],**extra}})


class ParseTests(unittest.TestCase):
    def test_same_five_second_divisor_and_aggregation_as_panel(self):
        source, values, ttl = parse_traffic(frame([{'email':'a','up':50,'down':100},{'email':'a','up':10,'down':50}]))
        self.assertEqual((source,ttl),('xray',10))
        self.assertEqual(values,{'a':{'upload':12,'download':30}})
    def test_tuic_interval_is_respected(self):
        source, values, ttl = parse_traffic(frame([{'email':'a','up':20,'down':40}],clientTrafficSource='tuic',clientTrafficIntervalMs=2000))
        self.assertEqual((source,ttl),('tuic',4))
        self.assertEqual(values['a']['download'],20)
    def test_unrelated_frames_and_invalid_samples(self):
        self.assertIsNone(parse_traffic('{"type":"status"}'))
        self.assertIsNone(parse_traffic('{"type":"traffic","payload":{}}'))
        for message in ('[]', frame([{'email':'a','up':-1}]),frame(clientTrafficIntervalMs=0),frame(clientTrafficIntervalMs=float('inf')),'a'*1048577):
            with self.subTest(message=message[:80]),self.assertRaises((ValueError,TypeError)):
                parse_traffic(message)


class FakeConnection:
    def __init__(self):
        self.frames=queue.Queue()
        self.aborted=threading.Event()
        self.closed=threading.Event()
    def recv(self):
        if self.aborted.is_set():raise OSError('closed')
        try:return self.frames.get(timeout=.02)
        except queue.Empty:raise socket.timeout()
    def abort(self):self.aborted.set()
    def close(self, **kwargs):self.closed.set()


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.now=100
        self.state=NS(users={1:NS(active=True,email='member',token='private'),2:NS(active=True,email='idle',token='other')})
        self.connection=FakeConnection()
        self.provider=Mock(return_value=('username','password'))
        self.login=Mock(return_value='session=private-cookie')
        self.connector=Mock(return_value=self.connection)
        self.hub=PanelSpeeds('/database',self.provider,login=self.login,connector=self.connector,
            settings_reader=lambda _:(80,'/panel/',''),clock=lambda:self.now)
        self.addCleanup(self.hub.close)
    def renew(self, viewer='viewer-aaaaaaaaaaaaaaaa',session='one'):
        return self.hub.renew(viewer,session,self.state)
    def wait(self, predicate):
        until=time.monotonic()+2
        while not predicate() and time.monotonic()<until:time.sleep(.005)
        self.assertTrue(predicate())
    def start(self):
        self.renew();self.wait(lambda:self.connector.call_count==1)
    def test_no_connection_until_viewed_and_one_connection_for_multiple_tabs(self):
        self.assertIsNone(self.hub.thread)
        self.connector.assert_not_called();self.provider.assert_not_called()
        self.start();self.renew('viewer-bbbbbbbbbbbbbbbb','two')
        self.connector.assert_called_once_with(80,'/panel/','session=private-cookie')
        self.hub.release('viewer-aaaaaaaaaaaaaaaa','one')
        self.assertFalse(self.connection.aborted.is_set())
        self.hub.release('viewer-bbbbbbbbbbbbbbbb','two')
        self.wait(self.connection.closed.is_set)
        self.assertFalse(self.hub.viewers)
    def test_raw_sources_and_expiry_match_panel_without_disclosing_names(self):
        self.start()
        self.connection.frames.put(frame([{'email':'member','up':50,'down':100},{'email':'unrelated','up':999,'down':999}]))
        self.wait(lambda:'xray' in self.hub.sources)
        self.connection.frames.put(frame([{'email':'member','up':40,'down':80}],clientTrafficSource='tuic',clientTrafficIntervalMs=2000))
        self.wait(lambda:'tuic' in self.hub.sources)
        data=self.renew()
        self.assertEqual(data['speeds']['1'],{'upload':30,'download':60})
        self.assertEqual(data['speeds']['2'],{'upload':0,'download':0})
        self.assertNotIn('member',json.dumps(data));self.assertNotIn('private',json.dumps(data))
        self.now+=5
        self.assertEqual(self.renew()['speeds']['1']['upload'],10)
        self.now+=6
        self.assertEqual(self.renew()['speeds']['1']['upload'],0)
    def test_expired_viewer_stops_reader_without_any_background_service(self):
        self.start();self.now+=9
        self.wait(self.connection.closed.is_set)
        self.wait(lambda:self.hub.thread is None)
    def test_release_is_session_bound_and_revocation_cleans_all_tabs(self):
        self.start();self.hub.release('viewer-aaaaaaaaaaaaaaaa','wrong')
        self.assertFalse(self.connection.aborted.is_set())
        with self.assertRaises(ValueError):self.renew(session='wrong')
        self.hub.revoke('one');self.wait(self.connection.closed.is_set)
    def test_bad_login_has_no_retry_storm(self):
        self.login.side_effect=ValueError('sensitive password')
        self.renew();self.wait(lambda:self.hub.error=='panel_login_required')
        for _ in range(5):self.renew()
        self.login.assert_called_once();self.connector.assert_not_called()
        self.assertNotIn('sensitive',json.dumps(self.renew()))
    def test_new_login_clears_auth_cooldown_without_connecting(self):
        self.hub.error='panel_login_required';self.hub.retry_after=130
        self.hub.credentials_changed()
        self.assertIsNone(self.hub.error)
        self.assertEqual(self.hub.retry_after,0)
        self.assertIsNone(self.hub.thread)
        self.connector.assert_not_called()

    def test_stop_during_login_does_not_open_a_websocket_afterwards(self):
        entered=threading.Event();resume=threading.Event()
        def login(*_):entered.set();resume.wait(2);return 'sid=secret'
        self.hub.login=login
        self.renew();self.wait(entered.is_set)
        self.hub.release('viewer-aaaaaaaaaaaaaaaa','one');resume.set()
        self.wait(lambda:self.hub.thread is None)
        self.connector.assert_not_called()

    def test_missing_login_only_reports_relogin(self):
        self.provider.return_value=None
        self.renew();self.wait(lambda:self.hub.error=='panel_login_required')
        self.login.assert_not_called()
    def test_bad_frame_clears_data_and_returns_unknown(self):
        self.start();self.connection.frames.put(frame([{'email':'member','up':-1}]))
        self.wait(lambda:self.hub.error=='unavailable')
        self.assertIsNone(self.renew()['speeds']['1']['upload'])
    def test_reconnect_reuses_cookie_without_password(self):
        self.start();self.hub.release('viewer-aaaaaaaaaaaaaaaa','one')
        self.wait(lambda:self.hub.thread is None)
        other=FakeConnection();self.connector.return_value=other
        self.renew();self.wait(lambda:self.connector.call_count==2)
        self.provider.assert_called_once();self.login.assert_called_once()
    def test_invalid_viewer_does_not_start_any_connection(self):
        with self.assertRaises(ValueError):self.renew('bad')
        self.assertIsNone(self.hub.thread)


class ConnectorTests(unittest.TestCase):
    def test_preconnected_loopback_socket_prevents_proxies_and_redirects(self):
        from clash_sub.speeds import connect_panel
        sock=Mock();connection=Mock()
        with patch('clash_sub.speeds.socket.create_connection',return_value=sock) as tcp,patch('websocket.create_connection',return_value=connection) as ws:
            self.assertIs(connect_panel(12345,'/base/','sid=secret'),connection)
        tcp.assert_called_once_with(('127.0.0.1',12345),timeout=3)
        self.assertEqual(ws.call_args.args[0],'ws://127.0.0.1:12345/base/ws')
        self.assertEqual(ws.call_args.kwargs['redirect_limit'],0)
        self.assertIs(ws.call_args.kwargs['socket'],sock)
        self.assertTrue(ws.call_args.kwargs['suppress_origin'])
        connection.settimeout.assert_called_once_with(1)


class LoginTests(unittest.TestCase):
    def test_csrf_and_cookies_are_local_and_no_redirects(self):
        calls=[]
        responses=[(200,{'success':True,'obj':'csrf-value'},[('Set-Cookie','sid=before; HttpOnly; Path=/')]),
                   (200,{'success':True},[('Set-Cookie','sid=after; HttpOnly; Path=/')])]
        class Connection:
            def __init__(self,host,port,timeout):
                self.response=responses.pop(0);calls.append((host,port))
            def request(self,*args,**kwargs):calls.append((args,kwargs))
            def getresponse(self):
                status,body,headers=self.response
                return NS(status=status,read=lambda _:json.dumps(body).encode(),getheaders=lambda:headers)
            def close(self):pass
        cookie=panel_login(80,'/private/','name','password',connection_factory=Connection)
        self.assertEqual(cookie,'sid=after')
        self.assertEqual(calls[0],('127.0.0.1',80))
        self.assertEqual(calls[1][0],('GET','/private/csrf-token'))
        self.assertEqual(calls[3][1]['headers']['X-CSRF-Token'],'csrf-value')
        self.assertEqual(calls[3][1]['headers']['Cookie'],'sid=before')
