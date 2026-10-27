import io
import json
import os
import sqlite3
from contextlib import closing
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest.mock import patch

from clash_sub.domain import RuntimeState, UserState, XuiClient, Traffic
from clash_sub.state import save_state, airport_only_token
from clash_sub.web_server import Handler, Portal
from clash_sub.web_settings import admin_prefix, page_path, password_hash, provision_pages, read_settings, write_settings
from clash_sub.online import OnlineCache


class Request:
    def __init__(self, request):
        self.input = io.BytesIO(request)
        self.output = io.BytesIO()
    def makefile(self, *_):
        return self.input
    def settimeout(self, *_):
        pass
    def sendall(self, data):
        self.output.write(data)


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.state = RuntimeState(2, 1, {
            1: UserState(1, 'owner', 'A'*22+'-owner', None, True, 'release'),
            2: UserState(2, 'member', ('B'*21+'A')+'-member', None, True, 'release'),
        })
        save_state(self.root/'state.json', self.state)
        self.settings = provision_pages({'version':1, 'enabled':True, 'panel_public':False, 'pages':{}, 'password_hash':password_hash('a-strong-test-password')}, self.state)
        write_settings(self.root, self.settings)
        self.config = SimpleNamespace(private_root=self.root, public_root=self.root/'public', xui_database=self.root/'db', subscription_authority='sub.example.com', template_root=Path(__file__).resolve().parents[1]/'templates')
        for client_id in (1,2):
            path = self.config.public_root/'releases'/str(client_id)/'release'
            path.mkdir(parents=True)
            for name in ('Clash-Compat.yaml','Clash-Balance.yaml','Clash-Airport.yaml','Nodes.txt'):
                (path/name).write_text('test')
        self.now = [1800000000]
        self.clients = (XuiClient(1,'owner','s1',True,1,2,100,0), XuiClient(2,'member','s2',True,3,4,100,0))
        self.store = SimpleNamespace(traffic_for=lambda key:Traffic(key,2,100,0), airport_traffic=lambda:Traffic(20,30,1000,0))
        self.portal = Portal(self.config, clock=lambda:self.now[0], store=self.store, snapshot_reader=lambda _:SimpleNamespace(clients=self.clients))

    def request(self, path, *, method='GET', fields=None, cookie=None, origin=None, raw=False):
        if path.startswith('/admin/') and not raw:
            path = admin_prefix(read_settings(self.root)) + path[len('/admin'):]
        body = urlencode(fields or {}).encode()
        headers = ['%s %s HTTP/1.0' % (method,path), 'Host: sub.example.com']
        if cookie:
            headers.append('Cookie: '+cookie)
        if origin:
            headers.append('Origin: '+origin)
        if method == 'POST':
            headers.extend(['Content-Type: application/x-www-form-urlencoded','Content-Length: '+str(len(body))])
        request = Request(('\r\n'.join(headers)+'\r\n\r\n').encode()+body)
        Handler(request, ('local',0), SimpleNamespace(portal=self.portal))
        head, response = request.output.getvalue().split(b'\r\n\r\n',1)
        return int(head.split()[1]), head.decode(), response.decode()

    def login(self):
        status, headers, _ = self.request('/admin/login',method='POST',fields={'password':'a-strong-test-password'},origin='https://sub.example.com')
        self.assertEqual(status,303)
        return next(line.split(': ',1)[1].split(';')[0] for line in headers.splitlines() if line.startswith('Set-Cookie:'))

    def test_fixed_admin_entry_is_hidden_even_with_valid_session(self):
        cookie=self.login()
        for path in ('/admin/','/admin/login','/admin/onlines'):
            self.assertEqual(self.request(path,cookie=cookie,raw=True)[0],404)

    def test_owner_change_revokes_previous_admin_entry_and_session(self):
        cookie=self.login()
        old=admin_prefix(self.settings,self.state)
        state=RuntimeState(2,2,self.state.users)
        save_state(self.root/'state.json',state)
        self.assertEqual(self.request(old+'/',cookie=cookie,raw=True)[0],404)
        provision_pages(self.settings,state)
        write_settings(self.root,self.settings)
        self.assertNotEqual(admin_prefix(self.settings,state),old)
        self.assertEqual(self.request('/admin/',cookie=cookie)[0],303)

    def test_admin_links_api_returns_only_selected_member_capabilities(self):
        status,headers,body=self.request('/admin/user/2/links',cookie=self.login())
        self.assertEqual(status,200)
        self.assertIn(self.state.users[2].token,body)
        self.assertNotIn(self.state.users[1].token,body)
        self.assertNotIn(self.settings['admin']['token'],body)
        self.assertNotIn('Clash-Airport.yaml',body)
        self.assertIn("connect-src 'self'",headers)

    def test_tasks_require_session_csrf_and_explicit_confirmation(self):
        cookie=self.login()
        csrf=self.portal.session_record(cookie,self.settings,self.state)[3]
        fields={'action':'refresh-airport','request_id':'request-testing-123456','confirmed':'yes'}
        self.assertEqual(self.request('/admin/tasks',method='POST',fields=fields,origin='https://sub.example.com')[0],401)
        self.assertEqual(self.request('/admin/tasks',method='POST',fields=fields,cookie=cookie,origin='https://sub.example.com')[0],403)
        fields['csrf']=csrf
        del fields['confirmed']
        self.assertEqual(self.request('/admin/tasks',method='POST',fields=fields,cookie=cookie,origin='https://sub.example.com')[0],400)
        fields['confirmed']='yes'
        with patch('clash_sub.web_tasks.enqueue',return_value='a'*32) as enqueue:
            status,_,body=self.request('/admin/tasks',method='POST',fields=fields,cookie=cookie,origin='https://sub.example.com')
        self.assertEqual(status,202)
        self.assertEqual(json.loads(body)['task_id'],'a'*32)
        self.assertEqual(enqueue.call_args.args[1:4],('refresh-airport',None,'request-testing-123456'))

    def test_task_submission_rejects_wrong_origin_and_invalid_action(self):
        cookie=self.login()
        csrf=self.portal.session_record(cookie,self.settings,self.state)[3]
        fields={'action':'shell','csrf':csrf,'confirmed':'yes','request_id':'request-testing-123456'}
        self.assertEqual(self.request('/admin/tasks',method='POST',fields=fields,cookie=cookie,origin='https://evil.example')[0],403)
        self.assertEqual(self.request('/admin/tasks',method='POST',fields=fields,cookie=cookie,origin='https://sub.example.com')[0],400)
        self.assertFalse((self.root/'web-tasks.json').exists())

    def test_personal_owner_link_does_not_grant_admin_or_reveal_entry(self):
        token=self.settings['pages']['1']['token']
        status,_,body=self.request('/p/'+token+'/')
        self.assertEqual(status,200)
        self.assertNotIn(self.settings['admin']['token'],body)
        self.assertNotIn('csrf-token',body)

    def test_admin_requires_login_and_cannot_use_subscription_token(self):
        self.assertEqual(self.request('/admin/')[0],303)
        self.assertEqual(self.request('/p/'+self.state.users[1].token+'/')[0],404)

    def test_online_endpoint_requires_admin_and_never_returns_raw_email_list(self):
        self.portal.online=OnlineCache(lambda:frozenset({'member'}))
        self.assertEqual(self.request('/admin/onlines')[0],401)
        status,_,body=self.request('/admin/onlines',cookie=self.login())
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body),{'states':{'1':'offline','2':'online'}})
        self.assertNotIn('member',body)

    def test_online_failure_is_unknown_and_not_exposed_to_personal_pages(self):
        calls=[]
        def unavailable():
            calls.append(1)
            return None
        self.portal.online=OnlineCache(unavailable)
        token=self.settings['pages']['2']['token']
        self.assertEqual(self.request('/p/'+token+'/')[0],200)
        self.assertFalse(calls)
        status,_,body=self.request('/admin/onlines',cookie=self.login())
        self.assertEqual(status,200)
        self.assertEqual(set(json.loads(body)['states'].values()),{'unknown'})

    def test_online_column_is_immediately_after_username(self):
        self.portal.online=OnlineCache(lambda:frozenset({'member'}))
        _,_,body=self.request('/admin/',cookie=self.login())
        self.assertIn('<th>用户</th><th>在线状态</th><th>已用 / 额度</th>',body)
        self.assertIn('presence online',body)
        self.assertIn('presence offline',body)

    def test_member_page_does_not_leak_owner_or_airport_capabilities(self):
        token = self.settings['pages']['2']['token']
        status, headers, body = self.request('/p/'+token+'/')
        self.assertEqual(status,200)
        self.assertIn(self.state.users[2].token,body)
        self.assertNotIn(self.state.users[1].token,body)
        self.assertNotIn('Clash-Balance.yaml',body)
        self.assertNotIn('Clash-Airport.yaml',body)
        self.assertIn('no-store',headers)
        self.assertIn("frame-ancestors 'none'",headers)

    def test_airport_page_only_exposes_airport_capability(self):
        token = self.settings['pages']['airport']['token']
        status, _, body = self.request('/p/'+token+'/')
        self.assertEqual(status,200)
        self.assertIn(airport_only_token(self.state.users[1].token),body)
        self.assertNotIn(self.state.users[1].token,body)
        self.assertNotIn('Nodes.txt',body)
        self.assertNotIn('服务器概况',body)

    def test_unknown_page_and_admin_id_do_not_expose_data(self):
        self.assertEqual(self.request('/p/'+'X'*43+'/')[0],404)
        self.assertEqual(self.request('/admin/user/999/',cookie=self.login())[0],404)

    def test_readable_personal_and_airport_paths_match_existing_capabilities(self):
        token=self.settings['pages']['2']['token']
        status,_,body=self.request(page_path(token,'member'))
        self.assertEqual(status,200)
        self.assertIn(self.state.users[2].token,body)
        self.assertEqual(self.request(page_path(token,'owner'))[0],404)
        airport=self.settings['pages']['airport']['token']
        status,_,body=self.request(page_path(airport,'owner'))
        self.assertEqual(status,200)
        self.assertNotIn('Nodes.txt',body)
        self.assertEqual(self.request(page_path(airport,'member'))[0],404)

    def test_link_list_adds_email_without_rotating_page_or_subscription_token(self):
        from clash_sub.web_manage import execute
        old=self.settings['pages']['2']['token']
        with patch('clash_sub.web_manage.load_config',return_value=self.config),patch('clash_sub.web_manage._OperationLock'):
            listing=execute(self.root,'links')
        self.assertIn('https://sub.example.com'+page_path(old,'member'),listing)
        self.assertIn('owner（机场专用）',listing)
        self.assertEqual(read_settings(self.root)['pages']['2']['token'],old)

    def test_readable_path_accepts_valid_email_punctuation(self):
        users=dict(self.state.users)
        users[2]=replace(users[2],email='alice-test+tag@example.com',token=('B'*21+'A')+'-alice-test+tag@example.com')
        state=replace(self.state,users=users)
        save_state(self.root/'state.json',state)
        provision_pages(self.settings,state)
        write_settings(self.root,self.settings)
        self.portal.snapshot_reader=lambda _:SimpleNamespace(clients=(self.clients[0],replace(self.clients[1],email=users[2].email)))
        url=page_path(self.settings['pages']['2']['token'],users[2].email)
        self.assertEqual(self.request(url)[0],200)

    def test_rotating_page_invalidates_both_readable_and_legacy_paths(self):
        old=self.settings['pages']['2']['token']
        provision_pages(self.settings,self.state,rotate='2')
        write_settings(self.root,self.settings)
        self.assertEqual(self.request(page_path(old,'member'))[0],404)
        self.assertEqual(self.request('/p/'+old+'/')[0],404)

    def test_page_rotation_does_not_change_subscription(self):
        old = self.settings['pages']['2']['token']
        provision_pages(self.settings,self.state,rotate='2')
        write_settings(self.root,self.settings)
        self.assertEqual(self.request('/p/'+old+'/')[0],404)
        self.assertEqual(self.state.users[2].token,('B'*21+'A')+'-member')

    def test_subscription_rotation_invalidates_old_page_binding(self):
        old = self.settings['pages']['2']['token']
        users=dict(self.state.users)
        users[2]=replace(users[2],token=('C'*21+'A')+'-member')
        save_state(self.root/'state.json',replace(self.state,users=users))
        self.assertEqual(self.request('/p/'+old+'/')[0],404)

    def test_admin_session_is_secure_and_expires(self):
        cookie=self.login()
        self.assertEqual(self.request('/admin/',cookie=cookie)[0],200)
        self.assertEqual(self.request('/admin/user/2/',cookie=cookie)[0],200)
        self.now[0]+=28801
        self.assertEqual(self.request('/admin/',cookie=cookie)[0],303)

    def test_cross_origin_login_is_rejected(self):
        self.assertEqual(self.request('/admin/login',method='POST',fields={'password':'a-strong-test-password'},origin='https://evil.example')[0],403)

    def test_global_login_rate_limit_applies_after_five_failures(self):
        for _ in range(5):
            self.assertIsNone(self.portal.login('wrong-password-long',self.settings))
        self.assertIsNone(self.portal.login('a-strong-test-password',self.settings))
        self.now[0]+=301
        self.assertIsNotNone(self.portal.login('a-strong-test-password',self.settings))

    def test_password_reset_invalidates_existing_session(self):
        cookie=self.login()
        self.settings['password_hash']=password_hash('a-different-strong-password')
        write_settings(self.root,self.settings)
        self.assertEqual(self.request('/admin/',cookie=cookie)[0],303)

    def test_logout_revokes_cookie(self):
        cookie=self.login()
        self.assertEqual(self.request('/admin/logout',method='POST',fields={'csrf':self.portal.session_record(cookie,self.settings,self.state)[3]},cookie=cookie,origin='https://sub.example.com')[0],303)
        self.assertEqual(self.request('/admin/',cookie=cookie)[0],303)

    def test_no_mock_data_or_user_sum_in_admin_response(self):
        status, _, body=self.request('/admin/',cookie=self.login())
        self.assertEqual(status,200)
        self.assertIn('暂无数据',body)
        self.assertNotIn('2.38',body)
        self.assertNotIn('快捷访问',body)

    def test_non_private_settings_and_symlinks_are_rejected(self):
        os.chmod(self.root/'web-settings.json',0o644)
        with self.assertRaises(ValueError):read_settings(self.root)
        os.chmod(self.root/'web-settings.json',0o600)
        target=self.root/'saved.json'
        (self.root/'web-settings.json').rename(target)
        (self.root/'web-settings.json').symlink_to(target)
        with self.assertRaises(ValueError):read_settings(self.root)

    def test_disabled_portal_has_no_pages(self):
        self.settings['enabled']=False
        write_settings(self.root,self.settings)
        self.assertEqual(self.request('/admin/login')[0],404)

    def test_default_https_port_origin_is_accepted(self):
        self.config.subscription_authority='sub.example.com:443'
        self.assertTrue(self.login())

    def test_inbound_total_uses_current_counters_including_disabled_not_clients(self):
        from clash_sub.xui import read_inbound_traffic
        with closing(sqlite3.connect(self.config.xui_database)) as db, db:
            db.execute('CREATE TABLE inbounds (up INTEGER, down INTEGER, total INTEGER, enable INTEGER, all_time INTEGER)')
            db.executemany('INSERT INTO inbounds VALUES (?,?,?,?,?)',[(10,40,100,1,9999),(2,3,100,0,8888)])
        self.assertEqual(read_inbound_traffic(self.config.xui_database),{'upload':12,'download':43,'total':200})
        body=self.portal.admin(self.state,self.settings)
        self.assertIn('55.0 B',body)
        self.assertIn('每月由 3x-ui 重置',body)
        self.assertNotIn('9999',body)
        with closing(sqlite3.connect(self.config.xui_database)) as db, db:
            db.execute('UPDATE inbounds SET up=0,down=0')
        self.now[0]+=301
        self.assertIn('0.0 B',self.portal.inbound_card())
        self.assertNotIn('55.0 B',self.portal.inbound_card())

    def test_inbound_unlimited_or_missing_quota_does_not_invent_total(self):
        from clash_sub.xui import read_inbound_traffic
        with closing(sqlite3.connect(self.config.xui_database)) as db, db:
            db.execute('CREATE TABLE inbounds (up INTEGER, down INTEGER, total INTEGER)')
            db.executemany('INSERT INTO inbounds VALUES (?,?,?)',[(10,20,100),(1,2,0)])
        self.assertEqual(read_inbound_traffic(self.config.xui_database)['total'],None)
        self.assertIn('总额度未设置',self.portal.inbound_card())

    def test_missing_inbound_columns_and_corrupt_counts_are_unknown(self):
        from clash_sub.xui import read_inbound_traffic
        with closing(sqlite3.connect(self.config.xui_database)) as db, db:
            db.execute('CREATE TABLE inbounds (id INTEGER)')
        self.assertIsNone(read_inbound_traffic(self.config.xui_database))
        with closing(sqlite3.connect(self.config.xui_database)) as db, db:
            db.execute('DROP TABLE inbounds')
            db.execute('CREATE TABLE inbounds (up INTEGER, down INTEGER)')
            db.execute('INSERT INTO inbounds VALUES (-1,20)')
        self.assertIsNone(read_inbound_traffic(self.config.xui_database))

    def test_panel_closure_removes_proxy_and_favicon_but_keeps_subscriptions(self):
        from clash_sub.nginx import render_sub_server
        rendered=render_sub_server(self.config,domain='example.com',panel_port=46312,panel_base_path='/secret-panel',routes_include='/etc/nginx/clash-sub/routes.conf',fullchain='/etc/ssl/domain/fullchain.pem',privkey='/etc/ssl/domain/privkey.pem')
        self.assertNotIn('proxy_pass http://127.0.0.1:46312',rendered)
        self.assertIn('location ^~ /secret-panel/',rendered)
        self.assertIn('include /etc/nginx/clash-sub/routes.conf;',rendered)
        self.assertIn('proxy_pass http://unix:/run/clash-sub/web.sock;',rendered)
        self.settings['panel_public']=True
        write_settings(self.root,self.settings)
        rendered=render_sub_server(self.config,domain='example.com',panel_port=46312,panel_base_path='/secret-panel',routes_include='/etc/nginx/clash-sub/routes.conf',fullchain='/etc/ssl/domain/fullchain.pem',privkey='/etc/ssl/domain/privkey.pem')
        self.assertIn('proxy_pass http://127.0.0.1:46312',rendered)

    def test_failed_nginx_activation_restores_saved_policy(self):
        from clash_sub.web_manage import execute
        self.settings['panel_public']=True
        write_settings(self.root,self.settings)
        self.config.nginx_routes=Path('/etc/nginx/clash-sub/routes.conf')
        self.config.mihomo_binary=Path('/usr/local/bin/mihomo')
        with patch('clash_sub.web_manage.load_config',return_value=self.config), patch('clash_sub.web_manage._OperationLock'), patch('clash_sub.web_manage._load_install_state'), patch('clash_sub.web_manage._rerender_nginx',side_effect=RuntimeError('failed')):
            with self.assertRaises(RuntimeError):execute(self.root,'panel-off')
        self.assertTrue(read_settings(self.root)['panel_public'])


if __name__=='__main__':
    unittest.main()
