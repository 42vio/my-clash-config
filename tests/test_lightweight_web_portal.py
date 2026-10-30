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
from clash_sub.bandwagon import UsageCache


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
    def test_airport_link_manager_is_separate_authenticated_and_uses_independent_token(self):
        from clash_sub.state import generate_token
        self.assertEqual(self.request('/admin/airport/links')[0],401)
        self.state=replace(self.state,airport_token=generate_token('airport'))
        save_state(self.root/'state.json',self.state)
        status,_,body=self.request('/admin/airport/links',cookie=self.login())
        self.assertEqual(status,200)
        data=json.loads(body)
        self.assertEqual(data['scope'],'airport')
        self.assertIn(self.state.airport_token,body)
        self.assertNotIn(self.state.users[1].token,body)
        self.assertEqual(data['links'][-1]['target'],'airport')
        self.assertEqual(data['links'][-1]['rotate'],'rotate-sub')
        personal=self.portal.user_links(self.state,self.settings,1)
        self.assertEqual([item['title'] for item in personal['links']],['个人网页','标准订阅','Balance 订阅','仅节点'])
        self.assertNotIn(self.state.airport_token,json.dumps(personal))
        page=self.portal.account('airport',self.state)
        self.assertIn(self.state.airport_token,page)
        self.assertNotIn(self.state.users[1].token,page)

    def test_member_table_replaces_reset_with_cumulative_transfers(self):
        from html.parser import HTMLParser
        class Counter(HTMLParser):
            def __init__(self):
                super().__init__()
                self.headers=0
                self.cells=[]
            def handle_starttag(self,tag,attrs):
                if tag=='th':self.headers+=1
                if tag=='tr':self.cells.append(0)
                if tag=='td':self.cells[-1]+=1
        body=self.portal.admin(self.state,self.settings)
        table=body.split('<table class="user-table',1)[1].split('</table>',1)[0]
        parser=Counter();parser.feed('<table'+table+'</table>')
        self.assertEqual(parser.headers,6)
        self.assertTrue(all(count==6 for count in parser.cells[1:]))
        self.assertNotIn('流量重置',table)
        self.assertIn('上传 / 下载',table)
        self.assertIn('member-transfer',table)
        self.assertIn('title="累计上传"',table)
        self.assertIn('title="累计下载"',table)
        self.assertIn('管理链接',table)
        self.assertIn('data-online-client=',table)

    def test_maintenance_uses_separate_actions_and_hides_idle_progress(self):
        body=self.portal.maintenance_card()
        self.assertIn('data-task="update"',body)
        self.assertIn('data-task="sync"',body)
        self.assertNotIn('data-task="update-sync"',body)
        self.assertIn('class="maintenance-buttons"',body)
        self.assertIn('class="section-shortcut history-toggle"',body)
        self.assertIn('class="maintenance-footer" hidden',body)
        self.assertIn('<strong>AmyTelecom 订阅</strong>',body)
        self.assertNotIn('当前无执行中的任务',body)
        self.assertNotIn('更新订阅内容，保留现有链接',body)

    def test_admin_header_links_to_owners_independent_pages_in_new_tabs(self):
        body=self.portal.admin(self.state,self.settings)
        header=body.split('</header>',1)[0]
        self.assertIn('<h1>管理总览</h1>',body)
        self.assertIn('<title>42 / 管理总览</title>',body)
        for key,label in (('1','个人网页'),('airport','机场订阅')):
            path=page_path(self.settings['pages'][key]['token'],'owner')
            self.assertIn('href="%s" target="_blank" rel="noopener noreferrer">%s ↗' % (path,label),header)
        self.assertNotIn(self.settings['pages']['2']['token'],header)
        self.assertNotIn('>管理总览</a>',header)

    def test_admin_shortcuts_omit_unavailable_pages_and_are_not_on_member_pages(self):
        self.settings['pages'].pop('airport')
        self.settings['pages']['1']['binding']='invalid-binding'
        header=self.portal.admin(self.state,self.settings).split('</header>',1)[0]
        self.assertNotIn('个人网页 ↗',header)
        self.assertNotIn('机场订阅 ↗',header)
        member=self.portal.account('2',self.state)
        self.assertNotIn('nav-shortcuts',member)

    def test_airport_reset_date_uses_beijing_day_and_handles_year_boundary(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from clash_sub.web_server import airport_reset_date
        for value, expected in (
            ('2026-10-18T23:59:00','10 月 19 日重置'),
            ('2026-10-19T23:59:00','10 月 19 日重置'),
            ('2026-10-20T00:00:00','11 月 19 日重置'),
            ('2026-12-20T00:00:00','1 月 19 日重置'),
        ):
            with self.subTest(value=value):
                stamp=datetime.fromisoformat(value).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
                self.assertEqual(airport_reset_date(stamp),expected)

    def test_provider_is_admin_only_and_matches_approved_layout(self):
        reader = unittest.mock.Mock(return_value={'used': 200 * 1024**3, 'total': 1000 * 1024**3, 'next_reset': 1800000000})
        self.portal.provider = UsageCache(credentials_reader=lambda: ('123', 'synthetic-private-api-key'), reader=reader, clock=lambda:self.now[0])
        self.request('/admin/')
        self.request('/p/'+self.settings['pages']['2']['token']+'/')
        reader.assert_not_called()
        cookie = self.login()
        with patch('clash_sub.web_server.read_inbound_traffic',return_value={'upload':10,'download':20,'total':100}):
            code, _, body = self.request('/admin/',cookie=cookie)
            self.assertEqual(code, 200)
            self.assertIn('搬瓦工服务器',body)
            self.assertIn('200.0 GB <small>/ 1000.0 GB',body)
            self.assertIn('剩余 800.0 GB',body)
            self.assertIn('套餐收发合计',body)
            self.assertIn('KiwiVM API',body)
            self.assertIn('代理用量',body)
            self.assertIn('30.0 <small>B</small>', body.split('id="members"',1)[1])
            self.assertNotIn('synthetic-private-api-key',body)
        self.portal.store.refresh = lambda: None
        self.request('/admin/overview/refresh',cookie=cookie)
        reader.assert_called_once()

    def test_provider_missing_and_stale_failure_do_not_show_false_zero(self):
        self.portal.provider = SimpleNamespace(get=lambda:{'usage':None,'updated':None,'status':'unconfigured'})
        body = self.portal.provider_card()
        self.assertIn('未配置',body)
        self.assertNotIn('0.0',body)
        self.portal.provider = SimpleNamespace(get=lambda:{'usage':None,'updated':None,'status':'error'})
        self.assertIn('查询失败',self.portal.provider_card())
        self.portal.provider = SimpleNamespace(get=lambda:{'usage':{'used':600,'total':500,'next_reset':1800000000},'updated':self.now[0],'status':'error'})
        body = self.portal.provider_card()
        self.assertIn('保留上次成功数据',body)
        self.assertIn('aria-valuenow="100.0"',body)
        self.assertIn('剩余 0.0 B',body)

    def test_default_online_reader_uses_same_process_only_for_admin(self):
        with patch('clash_sub.web_server.query_online',return_value=frozenset({'member'})) as query:
            self.portal=Portal(self.config,clock=lambda:self.now[0],store=self.store,snapshot_reader=lambda _:SimpleNamespace(clients=self.clients))
            token=self.settings['pages']['2']['token']
            self.request('/p/'+token+'/')
            query.assert_not_called()
            cookie=self.login()
            self.assertEqual(self.request('/admin/onlines',cookie=cookie)[0],200)
            self.request('/admin/onlines',cookie=cookie)
            query.assert_called_once_with(self.config.xui_database)

    def test_account_is_required_and_eight_character_password_is_supported(self):
        from clash_sub.web_settings import verify_password
        self.settings['username']='web-admin'
        self.settings['password_hash']=password_hash('test1234')
        write_settings(self.root,self.settings)
        self.assertTrue(verify_password('test1234',self.settings['password_hash']))
        with self.assertRaises(ValueError):password_hash('1234567')
        for username in ('','owner','wrong-user'):
            self.assertEqual(self.request('/admin/login',method='POST',fields={'username':username,'password':'test1234'},origin='https://sub.example.com')[0],429)
        self.assertEqual(self.request('/admin/login',method='POST',fields={'username':'web-admin','password':'test1234'},origin='https://sub.example.com')[0],303)
        body=self.request('/admin/login')[2]
        self.assertIn('autocomplete="username"',body)
        self.assertIn('minlength="8"',body)

    def test_account_change_revokes_sessions_and_queued_authorization(self):
        from clash_sub.web_tasks import authorization
        cookie=self.login()
        previous=authorization(self.settings,self.state)
        self.settings['username']='new-admin'
        write_settings(self.root,self.settings)
        self.assertEqual(self.request('/admin/',cookie=cookie)[0],303)
        self.assertNotEqual(authorization(self.settings,self.state),previous)

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
        status, headers, _ = self.request('/admin/login',method='POST',fields={'username':'owner','password':'a-strong-test-password'},origin='https://sub.example.com')
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

    def test_backup_status_requires_admin_and_redacts_credentials(self):
        from tests.test_lightweight_backup_subscription import SOURCE,NODES,download
        from clash_sub import backup_subscription as backup
        backup.refresh(self.root,SOURCE,download=lambda *_:download(),now=100)
        path=self.config.public_root/'releases/2/release/Clash-Compat.yaml'
        path.write_text('proxy-providers:\n  Backup: {type: inline}\n')
        self.assertEqual(self.request('/admin/backup')[0],401)
        status,_,body=self.request('/admin/backup',cookie=self.login())
        self.assertEqual(status,200)
        data=json.loads(body)
        self.assertEqual(data['users'],['member'])
        self.assertEqual(data['nodes'],[NODES[0]['name']])
        self.assertNotIn(SOURCE,body)
        self.assertNotIn('synthetic-backup-password',body)
        token=self.settings['pages']['2']['token']
        self.assertNotIn('backup-dialog',self.request('/p/'+token+'/')[2])
        self.assertEqual(self.request('/p/'+token+'/backup')[0],404)

    def test_standard_composition_describes_published_config_not_current_cache(self):
        path=self.config.public_root/'releases/2/release/Clash-Compat.yaml'
        for providers,label in [('{}','仅自建节点'),('{Backup: {type: inline}}','自建节点 + 备用节点'),('{AmyTelecom: {type: http}}','自建节点 + 机场')]:
            with self.subTest(label=label):
                path.write_text('proxy-providers: '+providers+'\n')
                data=self.portal.user_links(self.state,self.settings,2)
                standard=next(item for item in data['links'] if item['title']=='标准订阅')
                self.assertEqual(standard['composition'],label)
                self.assertTrue(all(not item.get('composition') for item in data['links'] if item['title']!='标准订阅'))

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

    def test_history_delete_requires_session_origin_csrf_and_confirmation(self):
        cookie=self.login()
        csrf=self.portal.session_record(cookie,self.settings,self.state)[3]
        fields={'csrf':csrf,'confirmed':'yes'}
        with patch('clash_sub.web_tasks.delete_history') as remove:
            self.assertEqual(self.request('/admin/tasks/delete',method='POST',fields=fields,origin='https://sub.example.com')[0],401)
            self.assertEqual(self.request('/admin/tasks/delete',method='POST',fields=fields,cookie=cookie,origin='https://evil.example')[0],403)
            self.assertEqual(self.request('/admin/tasks/delete',method='POST',fields={**fields,'csrf':'wrong'},cookie=cookie,origin='https://sub.example.com')[0],403)
            self.assertEqual(self.request('/admin/tasks/delete',method='POST',fields={**fields,'confirmed':'no'},cookie=cookie,origin='https://sub.example.com')[0],400)
            remove.assert_not_called()
            self.assertEqual(self.request('/admin/tasks/delete',method='POST',fields=fields,cookie=cookie,origin='https://sub.example.com')[0],200)
            remove.assert_called_once()

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

    def test_server_refresh_requires_admin_and_returns_only_the_card(self):
        with patch.object(self.portal,'system_card',return_value='<section id="server-overview">fresh metrics</section>') as metrics:
            self.assertEqual(self.request('/admin/server')[0],401)
            metrics.assert_not_called()
            status,headers,body=self.request('/admin/server',cookie=self.login())
            self.assertEqual(status,200)
            self.assertEqual(body,'<section id="server-overview">fresh metrics</section>')
            self.assertIn('no-store',headers)
            metrics.assert_called_once()
        token=self.settings['pages']['2']['token']
        self.assertEqual(self.request('/p/'+token+'/server')[0],404)

    def test_full_overview_refresh_is_admin_only_and_rereads_local_traffic(self):
        calls=[]
        self.store.refresh=lambda:calls.append('refresh')
        for endpoint in ('/admin/overview','/admin/overview/refresh'):
            self.assertEqual(self.request(endpoint)[0],401)
        self.assertEqual(calls,[])
        cookie=self.login()
        self.request('/admin/',cookie=cookie)
        self.assertIsNotNone(self.portal._snapshot)
        with patch.object(self.portal,'snapshot_reader',return_value=SimpleNamespace(clients=self.clients)) as reader:
            status,headers,body=self.request('/admin/overview/refresh',cookie=cookie)
        self.assertEqual(status,200)
        self.assertIn('no-store',headers)
        self.assertEqual(calls,['refresh'])
        reader.assert_called_once()
        for marker in ('traffic-card','id="members"','id="server-overview"','class="card maintenance"'):
            self.assertIn(marker,body)
        token=self.settings['pages']['2']['token']
        self.assertEqual(self.request('/p/'+token+'/overview/refresh')[0],404)

    def test_automatic_overview_refresh_reuses_local_cache(self):
        calls=[]
        self.store.refresh=lambda:calls.append('refresh')
        cookie=self.login()
        self.request('/admin/',cookie=cookie)
        with patch.object(self.portal,'snapshot_reader') as reader:
            status,_,body=self.request('/admin/overview',cookie=cookie)
        self.assertEqual(status,200)
        self.assertIn('id="members"',body)
        self.assertEqual(calls,[])
        reader.assert_not_called()

    def test_server_refresh_button_is_only_in_administrator_overview(self):
        self.assertIn('data-action="refresh-overview"',self.request('/admin/',cookie=self.login())[2])
        for key in ('1','2','airport'):
            token=self.settings['pages'][key]['token']
            self.assertNotIn('data-action="refresh-overview"',self.request('/p/'+token+'/')[2])

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
        self.assertIn('<th>用户</th><th>在线</th><th>已用 / 额度</th>',body)
        self.assertIn('presence online',body)
        self.assertIn('presence offline',body)

    def test_admin_detail_links_open_the_actual_personal_page(self):
        _,_,body=self.request('/admin/',cookie=self.login())
        for key in ('1','2'):
            path=page_path(self.settings['pages'][key]['token'],self.state.users[int(key)].email)
            self.assertIn('class="user-page-icon" target="_blank" rel="noopener noreferrer" href="'+path+'"',body)
            status,_,personal=self.request(path)
            self.assertEqual(status,200)
            self.assertNotIn('data-admin-base="/admin/',personal)
        self.assertNotIn('/user/2/">查看详情',body)
        del self.settings['pages']['2']
        write_settings(self.root,self.settings)
        self.assertIn('网页未启用',self.request('/admin/',cookie=self.login())[2])

    def test_member_usage_has_accessible_bar_but_unlimited_is_not_zero_percent(self):
        self.store.traffic_for=lambda key:Traffic(10,40,100 if key==2 else 0,0)
        body=self.portal.admin(self.state,self.settings)
        self.assertIn('class="mini-bar" role="progressbar" aria-label="member 流量使用比例"',body)
        self.assertIn('aria-valuenow="50.0"',body)
        self.assertNotIn('aria-label="owner 流量使用比例"',body)
        self.assertNotIn('class="ring"',self.portal.account('2',self.state))
        unlimited=self.portal.account('1',self.state)
        self.assertIn('不限额',unlimited)
        self.assertNotIn('role="progressbar"',unlimited)

    def test_provider_cards_share_linear_layout_and_remaining_caption(self):
        self.portal.provider = SimpleNamespace(get=lambda:{'usage':{'used':50,'total':1000,'next_reset':1800000000},'updated':self.now[0],'status':'ok'})
        with patch('clash_sub.web_server.read_inbound_traffic',return_value={'upload':20,'download':30,'total':1000}):
            body=self.portal.admin(self.state,self.settings)
        self.assertEqual(body.count('class="traffic-part'),2)
        self.assertEqual(body.count('class="usage usage-linear"'),2)
        self.assertEqual(body.count('class="remaining">剩余 950.0 B'),2)
        self.assertNotIn('class="ring"',body)

    def test_public_download_page_contains_no_account_capabilities(self):
        status,_,body=self.request('/clients/')
        self.assertEqual(status,200)
        for record in self.settings['pages'].values():
            self.assertNotIn(record['token'],body)
        self.assertNotIn(self.settings['admin']['token'],body)
        self.assertEqual(body.count('class="recommended"'),3)
        self.assertIn('.recommended{',self.request('/portal-assets/portal.css')[2])
        self.assertEqual(self.request('/clients/',method='POST',origin='https://sub.example.com')[0],404)
        self.assertEqual(self.request('/portal-assets/../../private/state.json')[0],404)

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
        self.assertNotIn('服务器概况',body)
        self.assertNotIn('到期时间',body)
        self.assertIn('重置时间待确认',body)

    def test_system_metrics_are_only_read_for_admin_overview(self):
        with patch.object(self.portal, 'system_card', return_value='SERVER-METRICS') as metrics:
            for key in ('1', '2', 'airport'):
                token = self.settings['pages'][key]['token']
                self.assertNotIn('SERVER-METRICS',self.request('/p/'+token+'/')[2])
            self.assertNotIn('SERVER-METRICS',self.request('/admin/user/2/',cookie=self.login())[2])
            metrics.assert_not_called()
            self.assertIn('SERVER-METRICS',self.request('/admin/',cookie=self.login())[2])
            metrics.assert_called_once()

    def test_reset_rules_are_independent_cached_and_not_inferred_from_expiry(self):
        from clash_sub.xui import read_traffic_resets
        with closing(sqlite3.connect(self.config.xui_database)) as db, db:
            db.execute('CREATE TABLE inbounds (traffic_reset TEXT, traffic_reset_day INTEGER)')
            db.execute("INSERT INTO inbounds VALUES ('monthly',17)")
            db.execute('CREATE TABLE clients (id INTEGER, traffic_reset TEXT, traffic_reset_day INTEGER, reset INTEGER)')
            db.executemany('INSERT INTO clients VALUES (?,?,?,?)',[(1,'monthly',17,0),(2,'never',1,17)])
        self.assertEqual(read_traffic_resets(self.config.xui_database),{'inbound':'每月 17 日','clients':{1:'每月 17 日',2:'未设置自动重置'}})
        self.assertEqual(self.portal.reset_rule(),'每月 17 日')
        self.assertIn('未设置自动重置', self.portal.account('2',self.state))
        self.assertIn('每月 19 日', self.portal.account('airport',self.state))
        self.assertIn('每月 19 日', self.portal.admin(self.state,self.settings))
        self.assertNotIn('到期时间', self.portal.admin(self.state,self.settings))
        with closing(sqlite3.connect(self.config.xui_database)) as db, db:
            db.execute('UPDATE inbounds SET traffic_reset_day=31')
        self.assertEqual(self.portal.reset_rule(),'每月 17 日')
        self.now[0]+=301
        self.assertEqual(self.portal.reset_rule(),'每月 31 日（不足该日按月末）')

    def test_missing_or_different_reset_schedules_are_not_fabricated(self):
        from clash_sub.xui import read_traffic_resets
        self.assertEqual(read_traffic_resets(self.config.xui_database)['inbound'],'待确认')
        with closing(sqlite3.connect(self.config.xui_database)) as db, db:
            db.execute('CREATE TABLE inbounds (traffic_reset TEXT, traffic_reset_day INTEGER)')
            db.executemany('INSERT INTO inbounds VALUES (?,?)',[('monthly',17),('monthly',18)])
        self.assertEqual(read_traffic_resets(self.config.xui_database)['inbound'],'各入站重置时间不同')

    def test_system_card_uses_preview_percentages_bars_and_sampled_speed(self):
        files = {
            '/proc/meminfo':'MemTotal: 1000 kB\nMemAvailable: 750 kB\nSwapTotal: 512 kB\nSwapFree: 500 kB\n',
            '/proc/uptime':'288000 0', '/proc/loadavg':'0.03 0.06 0.02 1/1 1',
            '/proc/stat':'cpu 100 0 0 900 0 0 0 0',
            '/proc/1/net/dev':'header\nheader\neth0: 1000 0 0 0 0 0 0 0 2000 0 0 0 0 0 0 0',
            '/proc/1/net/route':'header\neth0 00000000 0100007F',
            '/proc/1/net/if_inet6':'26070000000000000000000000000002 02 40 00 80 eth0',
        }
        with patch.object(Path,'read_text',autospec=True,side_effect=lambda path,**_:files[str(path)]), patch('clash_sub.web_server.shutil.disk_usage',return_value=SimpleNamespace(used=50,total=200)):
            first = self.portal.system_card()
            self.assertIn('等待下一次访问采样',first)
            self.now[0]+=30
            files['/proc/stat']='cpu 110 0 0 990 0 0 0 0'
            body = self.portal.system_card()
        self.assertIn('10.0 <small>%</small>',body)
        self.assertIn('25.0 <small>%</small>',body)
        self.assertIn('Swap 12.0 KB / 512.0 KB',body)
        self.assertIn('平均网速',body)
        self.assertIn('width:25.00%',body)
        self.assertIn('负载 0.03 / 0.06 / 0.02',body)
        self.assertIn('3 天 8 小时',body)
        self.assertIn('IPv6 已配置',body)

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
        self.assertEqual(self.request('/admin/login',method='POST',fields={'username':'owner','password':'a-strong-test-password'},origin='https://evil.example')[0],403)

    def test_form_policy_preserves_origin_without_leaking_capability_path(self):
        status,headers,body=self.request('/admin/login')
        self.assertEqual(status,200)
        self.assertIn('Referrer-Policy: strict-origin',headers)
        self.assertIn('<meta name="referrer" content="strict-origin">',body)
        self.assertNotIn('content="no-referrer"',body)
        self.assertEqual(self.request('/admin/login',method='POST',fields={'username':'owner','password':'a-strong-test-password'},origin='null')[0],403)
        self.assertTrue(self.login())

    def test_global_login_rate_limit_applies_after_five_failures(self):
        for _ in range(5):
            self.assertIsNone(self.portal.login('wrong-password-long',self.settings,username='owner'))
        self.assertIsNone(self.portal.login('a-strong-test-password',self.settings,username='owner'))
        self.now[0]+=301
        self.assertIsNotNone(self.portal.login('a-strong-test-password',self.settings,username='owner'))

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
        self.assertIn('55.0 <small>B</small>',body)
        self.assertIn('重置时间待确认',self.portal.inbound_card())
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

    def test_setup_enables_pages_without_changing_panel_access(self):
        from clash_sub.web_manage import execute
        self.config.nginx_routes=Path('/etc/nginx/clash-sub/routes.conf')
        self.config.mihomo_binary=Path('/usr/local/bin/mihomo')
        self.config.systemctl_binary=Path('/bin/systemctl')
        for panel_public in (True,False):
            with self.subTest(panel_public=panel_public):
                self.settings.update(enabled=False,panel_public=panel_public)
                write_settings(self.root,self.settings)
                with patch('clash_sub.web_manage.load_config',return_value=self.config), patch('clash_sub.web_manage._OperationLock'), patch('clash_sub.web_manage._load_install_state'), patch('clash_sub.web_manage._rerender_nginx'), patch.object(Path,'is_file',return_value=True):
                    execute(self.root,'setup',username='owner',password='test-password',runner=lambda *a,**kw:SimpleNamespace(returncode=0))
                saved=read_settings(self.root)
                self.assertTrue(saved['enabled'])
                self.assertEqual(saved['panel_public'],panel_public)
                self.assertEqual(saved['pages'],self.settings['pages'])

    def test_web_switch_preserves_links_password_and_panel_setting(self):
        from clash_sub.web_manage import execute
        from clash_sub.nginx import render_sub_server
        self.config.nginx_routes=Path('/etc/nginx/clash-sub/routes.conf')
        self.config.mihomo_binary=Path('/usr/local/bin/mihomo')
        self.config.systemctl_binary=Path('/bin/systemctl')
        self.settings['panel_public']=True
        write_settings(self.root,self.settings)
        with patch('clash_sub.web_manage.load_config',return_value=self.config), patch('clash_sub.web_manage._OperationLock'), patch('clash_sub.web_manage._load_install_state'), patch('clash_sub.web_manage._rerender_nginx') as render:
            message=execute(self.root,'web-off',runner=lambda *a,**kw:SimpleNamespace(returncode=0))
            closed=read_settings(self.root)
            self.assertFalse(closed['enabled'])
            self.assertIn('订阅下载',message)
            self.assertEqual(self.request('/p/'+self.settings['pages']['2']['token']+'/')[0],404)
            self.assertEqual(self.request('/admin/login')[0],404)
            rendered=render_sub_server(self.config,domain='example.com',panel_port=46312,panel_base_path='/secret-panel',routes_include='/etc/nginx/clash-sub/routes.conf',fullchain='/etc/ssl/domain/fullchain.pem',privkey='/etc/ssl/domain/privkey.pem')
            self.assertNotIn('proxy_pass http://unix:/run/clash-sub/web.sock;',rendered)
            self.assertIn('include /etc/nginx/clash-sub/routes.conf;',rendered)
            self.assertIn('proxy_pass http://127.0.0.1:46312',rendered)
            with patch.object(Path,'is_file',return_value=True):
                execute(self.root,'web-on',runner=lambda *a,**kw:SimpleNamespace(returncode=0))
            self.assertEqual(render.call_count,2)
        self.assertEqual(read_settings(self.root),self.settings)
        self.assertEqual(self.request('/p/'+self.settings['pages']['2']['token']+'/')[0],200)

    def test_web_switch_failure_restores_flag_and_enable_requires_setup(self):
        from clash_sub.web_manage import execute
        self.config.nginx_routes=Path('/etc/nginx/clash-sub/routes.conf')
        self.config.mihomo_binary=Path('/usr/local/bin/mihomo')
        with patch('clash_sub.web_manage.load_config',return_value=self.config), patch('clash_sub.web_manage._OperationLock'), patch('clash_sub.web_manage._load_install_state'), patch('clash_sub.web_manage._rerender_nginx',side_effect=RuntimeError('failed')):
            with self.assertRaises(RuntimeError):execute(self.root,'web-off')
        self.assertEqual(read_settings(self.root),self.settings)
        self.settings['enabled']=False
        self.settings['password_hash']=''
        write_settings(self.root,self.settings)
        with patch('clash_sub.web_manage.load_config',return_value=self.config), patch('clash_sub.web_manage._OperationLock'):
            with self.assertRaisesRegex(ValueError,'initialize_web_first'):execute(self.root,'web-on')
        self.assertFalse(read_settings(self.root)['enabled'])

    def test_shutdown_failure_keeps_page_closed_and_reports_incomplete_release(self):
        from clash_sub.web_manage import execute
        self.config.nginx_routes=Path('/etc/nginx/clash-sub/routes.conf')
        self.config.mihomo_binary=Path('/usr/local/bin/mihomo')
        with patch('clash_sub.web_manage.load_config',return_value=self.config),patch('clash_sub.web_manage._OperationLock'),patch('clash_sub.web_manage._load_install_state'),patch('clash_sub.web_manage._rerender_nginx'),patch('clash_sub.web_manage._stop_web_services',return_value=False):
            message=execute(self.root,'web-off')
        self.assertFalse(read_settings(self.root)['enabled'])
        self.assertIn('内存释放未完成',message)


if __name__=='__main__':
    unittest.main()
