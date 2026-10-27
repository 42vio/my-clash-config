import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from clash_sub.online import OnlineCache, parse_online, query_online, read_token, save_token


class OnlineTests(unittest.TestCase):
    def test_code_update_preserves_web_shutdown_instead_of_restarting_it(self):
        from clash_sub.manage import run_post_update
        config=SimpleNamespace(xui_database=Path('/db'),private_root=Path('/private'),public_root=Path('/public'),nginx_routes=Path('/routes'),mihomo_binary=Path('/mihomo'),systemctl_binary=Path('/bin/systemctl'))
        runner=unittest.mock.Mock()
        for enabled in (False,True):
            with self.subTest(enabled=enabled),patch('clash_sub.manage.load_config',return_value=config),patch('clash_sub.manage.Installer') as installer,patch('clash_sub.manage._load_install_state'),patch('clash_sub.manage._rerender_nginx'),patch('clash_sub.web_settings.read_settings',return_value={'enabled':enabled}),patch('clash_sub.state.load_state',return_value=None),patch('clash_sub.manage._stop_web_services',return_value=True) as stop:
                self.assertTrue(run_post_update('/repo',runner))
                if enabled:
                    stop.assert_not_called()
                    self.assertEqual(installer.return_value._run.call_count,3)
                else:
                    stop.assert_called_once_with(config,runner)
                    installer.return_value._run.assert_not_called()

    def test_shutdown_disables_activation_then_stops_both_processes_even_on_error(self):
        from clash_sub.manage import _stop_web_services
        for first in (SimpleNamespace(returncode=0),SimpleNamespace(returncode=1),TimeoutError()):
            runner=unittest.mock.Mock(side_effect=[first,SimpleNamespace(returncode=0)])
            result=_stop_web_services(SimpleNamespace(systemctl_binary='/bin/systemctl'),runner)
            self.assertEqual(result,not isinstance(first,Exception) and first.returncode==0)
            self.assertEqual([c.args[0] for c in runner.call_args_list],[
                ['/bin/systemctl','disable','--now','clash-sub-web.socket','clash-sub-online.socket'],
                ['/bin/systemctl','stop','clash-sub-web.service','clash-sub-online.service'],
            ])

    def test_setting_token_while_closed_does_not_start_any_service(self):
        from clash_sub.web_manage import execute
        config=SimpleNamespace(private_root='/private',xui_database='/db')
        runner=unittest.mock.Mock()
        with patch('clash_sub.web_manage.load_config',return_value=config),patch('clash_sub.web_manage._OperationLock'),patch('clash_sub.web_manage.read_settings',return_value={'enabled':False}),patch('clash_sub.online.save_token'),patch('clash_sub.online.query_online',return_value=frozenset()):
            message=execute('/repo','set-api-token',api_token='synthetic-token',runner=runner)
        self.assertIn('保持停止',message)
        runner.assert_not_called()

    def test_menu_save_restarts_service_and_checks_query_without_exposing_token(self):
        from clash_sub.web_manage import execute
        config=SimpleNamespace(private_root='/private',systemctl_binary='/bin/systemctl',xui_database='/db')
        for result,query,label in ((0,frozenset(),'验证成功'),(0,None,'查询未成功'),(1,None,'重启失败')):
            with self.subTest(result=result,query=query), patch('clash_sub.web_manage.load_config',return_value=config), patch('clash_sub.web_manage._OperationLock'), patch('clash_sub.online.save_token') as save, patch('clash_sub.online.query_online',return_value=query), patch('clash_sub.web_manage.read_settings',return_value={'enabled':True}) as settings:
                runner=unittest.mock.Mock(return_value=SimpleNamespace(returncode=result))
                message=execute('/repo','set-api-token',api_token='synthetic-private-token',runner=runner)
                save.assert_called_once_with('synthetic-private-token')
                self.assertIn(label,message)
                self.assertNotIn('synthetic-private-token',message)
                self.assertEqual(runner.call_args.args[0],['/bin/systemctl','restart','clash-sub-online.service'])
                settings.assert_called_once()

    def test_save_and_replace_token_privately_without_following_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'private'/'credentials.json'
            save_token('first-synthetic-token',path)
            self.assertEqual(read_token(path),'first-synthetic-token')
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777,0o700)
            save_token('second-synthetic-token',path)
            self.assertEqual(read_token(path),'second-synthetic-token')
            alias=path.parent/'alias'
            alias.symlink_to(path)
            with self.assertRaises(ValueError):save_token('another-synthetic-token',alias)
            self.assertEqual(read_token(path),'second-synthetic-token')
            with self.assertRaises(ValueError):save_token('short',path)
            self.assertEqual(list(path.parent.glob('.credentials-*')),[])

    def test_empty_panel_result_is_offline_but_error_is_unknown(self):
        self.assertEqual(parse_online({'success':True,'obj':None}),frozenset())
        self.assertEqual(parse_online({'success':True,'obj':[]}),frozenset())
        for invalid in ({'success':False,'obj':[]},{'success':True},{'success':True,'obj':'member'},{'success':True,'obj':[1]}):
            with self.assertRaises(ValueError):parse_online(invalid)

    def test_cache_single_read_per_window_and_failure_does_not_keep_stale_online(self):
        now=[0]
        calls=[]
        values=iter([frozenset({'member'}),None,frozenset()])
        def read():
            calls.append(1)
            return next(values)
        cache=OnlineCache(read,clock=lambda:now[0])
        self.assertEqual(cache.get(),frozenset({'member'}))
        self.assertEqual(cache.get(),frozenset({'member'}))
        self.assertEqual(len(calls),1)
        now[0]=10
        self.assertIsNone(cache.get())
        now[0]=20
        self.assertEqual(cache.get(),frozenset())

    def test_credentials_read_only_from_private_project_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json'
            path.write_text(json.dumps({'XUI_API_TOKEN':'synthetic-token-for-online-tests'}))
            os.chmod(path,0o600)
            self.assertEqual(read_token(path),'synthetic-token-for-online-tests')
            os.chmod(path,0o644)
            with self.assertRaises(ValueError):read_token(path)
            os.chmod(path,0o600)
            alias=Path(directory)/'alias.json'
            alias.symlink_to(path)
            with self.assertRaises(ValueError):read_token(alias)

    def test_query_uses_only_loopback_and_exact_3x_ui_read_endpoint(self):
        class Connection:
            def __init__(self,*args,**kwargs):
                self.args=args;self.kwargs=kwargs;self.closed=False
            def request(self,*args,**kwargs):
                self.request_args=args;self.request_kwargs=kwargs
            def getresponse(self):return self
            status=200
            def read(self,limit):
                self.limit=limit
                return b'{"success":true,"obj":["member"]}'
            def close(self):self.closed=True
        conn=Connection()
        def factory(*args,**kwargs):
            conn.args=args;conn.kwargs=kwargs
            return conn
        with patch('clash_sub.online.read_panel_settings',return_value=(46312,'/panel-path/','0.0.0.0')),patch('clash_sub.online.read_token',return_value='synthetic-token'):
            self.assertEqual(query_online('/db',connection_factory=factory),frozenset({'member'}))
        self.assertEqual(conn.args,('127.0.0.1',46312))
        self.assertEqual(conn.request_args,('POST','/panel-path/panel/api/clients/onlines'))
        self.assertEqual(conn.request_kwargs['headers']['Authorization'],'Bearer synthetic-token')
        self.assertTrue(conn.closed)

    def test_failed_credentials_produce_unknown_without_exposing_details(self):
        with patch('clash_sub.online.read_panel_settings',side_effect=RuntimeError('sensitive-detail')):
            self.assertIsNone(query_online('/db'))

    def test_redirect_and_auth_failure_are_unknown_not_offline(self):
        for status in (302,401,403,500):
            with self.subTest(status=status):
                class Connection:
                    def request(self,*args,**kwargs):pass
                    def getresponse(self):return type('Response',(),{'status':status})()
                    def close(self):pass
                with patch('clash_sub.online.read_panel_settings',return_value=(46312,'/path/','127.0.0.1')),patch('clash_sub.online.read_token',return_value='synthetic-token'):
                    self.assertIsNone(query_online('/db',connection_factory=lambda *a,**kw:Connection()))
