import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs

from clash_sub.bandwagon import UsageCache, delete_api, load_credentials, query_usage, save_api
from clash_sub.online import read_token, save_token


KEY = 'synthetic-bandwagon-key-for-tests'


class BandwagonTests(unittest.TestCase):
    def test_usage_cache_success_failure_and_recovery_are_rate_limited(self):
        now = [1000]
        first = {'used': 100, 'total': 500, 'next_reset': 1800000000}
        second = dict(first, used=200)
        reader = Mock(side_effect=[first, ValueError(KEY), second])
        cache = UsageCache(credentials_reader=lambda: ('123', KEY), reader=reader, clock=lambda: now[0])
        result = cache.get()
        self.assertEqual(result, {'usage': first, 'updated': 1000, 'status': 'ok'})
        result['usage']['used'] = 0
        now[0] = 1299
        self.assertEqual(cache.get()['usage'], first)
        self.assertEqual(reader.call_count, 1)
        now[0] = 1300
        self.assertEqual(cache.get(), {'usage': first, 'updated': 1000, 'status': 'error'})
        now[0] = 1599
        self.assertEqual(cache.get()['status'], 'error')
        self.assertEqual(reader.call_count, 2)
        now[0] = 1600
        self.assertEqual(cache.get(), {'usage': second, 'updated': 1600, 'status': 'ok'})
        self.assertNotIn(KEY, repr(cache.get()))

    def test_usage_cache_never_reuses_data_across_credentials_changes_or_deletion(self):
        credentials = Mock(return_value=('123', KEY))
        reader = Mock(return_value={'used': 100, 'total': 500, 'next_reset': 1800000000})
        cache = UsageCache(credentials_reader=credentials, reader=reader, clock=lambda: 1000)
        cache.get()
        credentials.return_value = ('456', KEY)
        reader.side_effect = ValueError('sensitive upstream failure')
        self.assertEqual(cache.get(), {'usage': None, 'updated': None, 'status': 'error'})
        credentials.return_value = None
        self.assertEqual(cache.get(), {'usage': None, 'updated': None, 'status': 'unconfigured'})
        self.assertEqual(reader.call_count, 2)
        credentials.side_effect = ValueError(KEY)
        self.assertEqual(cache.get(), {'usage': None, 'updated': None, 'status': 'error'})

    def test_usage_cache_coalesces_concurrent_queries(self):
        from concurrent.futures import ThreadPoolExecutor
        reader = Mock(return_value={'used': 100, 'total': 500, 'next_reset': 1800000000})
        cache = UsageCache(credentials_reader=lambda: ('123', KEY), reader=reader)
        with ThreadPoolExecutor(max_workers=8) as pool:
            values = list(pool.map(lambda _: cache.get(), range(16)))
        self.assertEqual(reader.call_count, 1)
        self.assertTrue(all(value['status'] == 'ok' for value in values))

    def connection(self, payload, status=200):
        connection = Mock()
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        connection.getresponse.return_value = SimpleNamespace(status=status, read=lambda limit: raw[:limit])
        return connection

    def payload(self, **changes):
        return dict(error=0, data_counter=100, plan_monthly_data=500,
                    monthly_data_multiplier=2, data_next_reset=1800000000, **changes)

    def test_shared_credentials_preserve_other_integrations_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'private'/'credentials.json'
            save_token('synthetic-original-xui-token', path)
            save_api('1234567', KEY, path)
            self.assertEqual(read_token(path), 'synthetic-original-xui-token')
            save_token('synthetic-replacement-xui-token', path)
            self.assertEqual(load_credentials(path), ('1234567', KEY))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            delete_api(path)
            self.assertIsNone(load_credentials(path))
            self.assertEqual(read_token(path), 'synthetic-replacement-xui-token')

    def test_delete_is_idempotent_and_clears_malformed_api_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'credentials.json'
            delete_api(path)
            self.assertFalse(path.exists())
            path.write_text(json.dumps({'BWH_API_KEY': 'invalid'}))
            path.chmod(0o600)
            delete_api(path)
            self.assertIsNone(load_credentials(path))
            delete_api(path)

    def test_credentials_reject_symlinks_and_hardlinks_without_changing_target(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'credentials.json'
            save_api('1234567', KEY, path)
            alias = path.with_name('alias.json')
            alias.symlink_to(path)
            with self.assertRaises(ValueError):save_api('7654321', KEY, alias)
            hard = path.with_name('hard.json')
            os.link(path, hard)
            with self.assertRaises(ValueError):load_credentials(hard)
            hard.unlink()
            self.assertEqual(load_credentials(path), ('1234567', KEY))

    def test_query_only_posts_to_fixed_usage_endpoint_and_applies_multiplier(self):
        connection = self.connection(self.payload())
        factory = Mock(return_value=connection)
        self.assertEqual(query_usage('1234567', KEY, connection_factory=factory),
                         {'used':200, 'total':1000, 'next_reset':1800000000})
        factory.assert_called_once_with('api.64clouds.com',443,timeout=8)
        args, kwargs = connection.request.call_args
        self.assertEqual(args, ('POST','/v1/getServiceInfo'))
        self.assertEqual(parse_qs(kwargs['body'].decode()), {'veid':['1234567'],'api_key':[KEY]})
        self.assertNotIn(KEY, repr(args))
        connection.close.assert_called_once()

    def test_query_errors_and_redirects_never_expose_response_or_credentials(self):
        cases = [(self.payload(),302), ({'error':1,'message':KEY},200), (b'x'*65537,200), (b'not json',200)]
        for payload,status in cases:
            with self.subTest(status=status):
                connection=self.connection(payload,status)
                with self.assertRaisesRegex(ValueError,'^bwh_query_failed$'):
                    query_usage('1234567',KEY,connection_factory=lambda *a,**k:connection)
                connection.close.assert_called_once()

    def test_invalid_fields_and_credentials_are_rejected(self):
        for field,value in [('data_counter',-1),('data_counter',True),('monthly_data_multiplier',0),
                            ('monthly_data_multiplier',float('nan')),('plan_monthly_data',0),('data_next_reset',10**20)]:
            payload=self.payload();payload[field]=value
            with self.subTest(field=field,value=value), self.assertRaises(ValueError):
                query_usage('1234567',KEY,connection_factory=lambda *a,**k:self.connection(payload))
        factory=Mock()
        for veid,key in [('0',KEY),('12x',KEY),('123','short'),('123',KEY+'\n')]:
            with self.assertRaises(ValueError):query_usage(veid,key,connection_factory=factory)
        factory.assert_not_called()

    def test_management_verifies_before_saving_and_never_restarts_services(self):
        from clash_sub.web_manage import execute
        config=SimpleNamespace(private_root='/private')
        runner=Mock()
        with patch('clash_sub.web_manage.load_config',return_value=config), patch('clash_sub.web_manage._OperationLock'), \
             patch('clash_sub.bandwagon.query_usage',side_effect=ValueError('bwh_query_failed')), \
             patch('clash_sub.bandwagon.save_api') as save:
            with self.assertRaises(ValueError):execute('/repo','set-bwh-api',bwh_veid='1234567',bwh_api_key=KEY,runner=runner)
            save.assert_not_called()
        with patch('clash_sub.web_manage.load_config',return_value=config), patch('clash_sub.web_manage._OperationLock'), \
             patch('clash_sub.bandwagon.query_usage',return_value={'used':100,'total':500,'next_reset':1800000000}), \
             patch('clash_sub.bandwagon.save_api') as save:
            message=execute('/repo','set-bwh-api',bwh_veid='1234567',bwh_api_key=KEY,runner=runner)
            save.assert_called_once_with('1234567',KEY)
            self.assertIn('验证成功',message)
            self.assertNotIn(KEY,message)
        runner.assert_not_called()
