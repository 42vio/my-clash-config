import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml

from clash_sub import backup_subscription as backup
from clash_sub.domain import AirportDownload, AirportProvider, Traffic
from clash_sub.generator import render_subscription_bundle
from clash_sub.checks import validate_clash
from clash_sub.operation import ServiceError
from tests.test_lightweight_generator import reality_proxy

SOURCE='https://backup.example.test/private-source?token=placeholder'
NODES=[{'name':'Backup '+str(i),'type':'ss','server':'192.0.2.20','port':443,'cipher':'aes-128-gcm','password':'synthetic-backup-password'} for i in range(3)]


def download(nodes=NODES):
    return AirportDownload(yaml.safe_dump({'proxies':nodes,'rules':['MATCH,REJECT']}).encode(),Traffic(10,20,100,0))


class BackupSubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def seed(self):
        return backup.refresh(self.root,SOURCE,download=lambda *_:download(),now=100)

    def test_cache_is_private_and_status_never_exposes_source_or_node_credentials(self):
        self.seed()
        self.assertEqual((self.root/backup.FILENAME).stat().st_mode & 0o777,0o600)
        status=backup.status(self.root)
        self.assertEqual(status['nodes'],[NODES[0]['name']])
        self.assertEqual(status['traffic']['total'],100)
        text=json.dumps(status)
        self.assertNotIn(SOURCE,text)
        self.assertNotIn('synthetic-backup-password',text)
        self.assertNotIn('rules',backup.read(self.root))

    def test_failed_refresh_keeps_source_and_last_success_nodes(self):
        old=self.seed()
        with self.assertRaises(ServiceError):
            backup.refresh(self.root,'https://new.example.test/private',download=Mock(side_effect=RuntimeError('private-details')),now=200)
        current=backup.read(self.root)
        self.assertEqual(current['url'],old['url'])
        self.assertEqual(current['nodes'],old['nodes'])
        self.assertEqual(current['last_success'],100)
        self.assertEqual(current['last_error'],'backup_download_failed')
        self.assertNotIn('private-details',(self.root/backup.FILENAME).read_text())

    def test_first_failed_download_records_unready_source_without_nodes(self):
        with self.assertRaises(ServiceError):backup.refresh(self.root,SOURCE,download=lambda *_:download([]),now=100)
        self.assertEqual(backup.read(self.root)['nodes'],[])
        self.assertIsNone(backup.status(self.root)['last_success'])

    def test_core_validation_failure_preserves_previous_nodes(self):
        old=self.seed()
        with self.assertRaises(ServiceError):
            backup.refresh(self.root,SOURCE,download=lambda *_:download([dict(NODES[0],type='unsupported')]),validate=Mock(side_effect=ValueError('private core output')),now=200)
        self.assertEqual(backup.read(self.root)['nodes'],old['nodes'])
        self.assertEqual(backup.read(self.root)['last_success'],100)

    def test_invalid_nodes_and_source_are_rejected(self):
        for nodes in ([],NODES+[NODES[0]],[dict(NODES[0],port=0)],[dict(NODES[0],**{'dialer-proxy':'other'})]):
            with self.subTest(nodes=len(nodes)),self.assertRaises(Exception):backup.validate_nodes(nodes)
        with self.assertRaises(ServiceError):backup.refresh(self.root,'http://example.test/source')

    def test_cache_read_rejects_symlink_and_insecure_permissions(self):
        self.seed();p=self.root/backup.FILENAME;p.chmod(0o644)
        with self.assertRaises(ServiceError):backup.read(self.root)
        p.unlink();target=self.root/'other';target.write_text('{}');p.symlink_to(target)
        with self.assertRaises(ServiceError):backup.read(self.root)

    def test_delete_removes_only_backup_record_and_is_idempotent(self):
        self.seed();other=self.root/'state.json';other.write_text('unchanged')
        backup.remove(self.root);backup.remove(self.root)
        self.assertFalse(backup.status(self.root)['configured'])
        self.assertEqual(other.read_text(),'unchanged')

    def test_delete_can_recover_a_corrupt_private_record(self):
        self.seed()
        (self.root/backup.FILENAME).write_text('invalid JSON')
        backup.remove(self.root)
        self.assertFalse(backup.status(self.root)['configured'])

    def test_real_templates_keep_primary_group_and_only_compat_contains_backup(self):
        templates=Path(__file__).resolve().parents[1]/'templates'
        main=[reality_proxy('Actual name from 3x-ui')]
        airport=AirportProvider('https://sub.example.test/s/owner/AmyTelecom.yaml')
        for owner in (True,False):
            bundle=render_subscription_bundle(owner,main,airport if owner else None,templates,backup_nodes=NODES)
            for kind,text in bundle.items():
                if kind=='nodes':
                    from clash_sub.node_subscription import render_node_subscription
                    self.assertEqual(text,render_node_subscription(main));continue
                allowed=airport.url if owner else None
                doc=validate_clash(text,(),allowed)
                providers=doc.get('proxy-providers',{})
                self.assertEqual('Backup' in providers,kind=='compat')
                groups={g['name']:g for g in doc['proxy-groups']}
                if kind!='airport':
                    expected=['Actual name from 3x-ui']+([backup.BACKUP_GROUP] if kind=='compat' else [])
                    self.assertEqual(groups[backup.PRIMARY_GROUP]['proxies'],expected)
                    self.assertEqual(groups[backup.PRIMARY_GROUP]['type'],'fallback')
                if kind=='compat':
                    self.assertEqual(providers['Backup']['type'],'inline')
                    self.assertEqual(len(providers['Backup']['payload']),3)
                    self.assertNotIn(SOURCE,text)
                    for group in doc['proxy-groups']:
                        if group['name']!=backup.BACKUP_GROUP:self.assertNotIn('Backup',group.get('use',[]))
            plain=yaml.safe_load(render_subscription_bundle(owner,main,airport if owner else None,templates)['compat'])
            groups={g['name']:g for g in plain['proxy-groups']}
            self.assertIn(backup.PRIMARY_GROUP,groups)
            self.assertEqual(groups[backup.PRIMARY_GROUP]['proxies'],['Actual name from 3x-ui'])
            self.assertNotIn(backup.BACKUP_GROUP,groups)

    def test_service_passes_backup_only_to_eligible_users(self):
        from tests.test_lightweight_service import ServiceTests
        for email in ('42vio','openwrt','member'):
            harness=ServiceTests();harness.setUp();self.addCleanup(harness.doCleanups)
            backup.refresh(harness.config.private_root,SOURCE,download=lambda *_:download(),now=100)
            original=harness.service._render
            received=[]
            def render(owner,xui,provider,root,**kwargs):
                received.append(kwargs.get('backup_nodes'))
                return original(owner,xui,provider,root)
            harness.service._render=render
            from dataclasses import replace
            harness.clients=[harness.owner,replace(harness.member,email=email)]
            result=harness.bootstrap()
            self.assertFalse(result['errors'])
            self.assertEqual(bool(received[-1]),backup.eligible(email))

    def test_menu_status_and_actions_never_echo_subscription_source(self):
        from clash_sub.cli import _backup_subscription_dispatch
        from clash_sub.backup_subscription import status
        self.seed();report=status(self.root)
        service=Mock()
        service.replace_backup_source.return_value=report
        service.remove_backup_source.return_value=report
        service.backup_status.return_value=report
        out,err=io.StringIO(),io.StringIO()
        _backup_subscription_dispatch('1',io.StringIO(SOURCE+'\n'),out,err,lambda:service)
        service.replace_backup_source.assert_called_once_with(SOURCE)
        self.assertNotIn(SOURCE,out.getvalue()+err.getvalue())
        _backup_subscription_dispatch('3',io.StringIO('no\n'),out,err,lambda:service)
        service.remove_backup_source.assert_not_called()
        _backup_subscription_dispatch('3',io.StringIO('DELETE\n'),out,err,lambda:service)
        service.remove_backup_source.assert_called_once()

    def test_daily_refresh_is_due_only_after_24_hours(self):
        from clash_sub.service import ClashSubService
        self.seed()
        service=SimpleNamespace(config=SimpleNamespace(private_root=self.root),_clock=lambda:100+86399,refresh_backup=Mock())
        ClashSubService.refresh_backup_if_due(service)
        service.refresh_backup.assert_not_called()
        service._clock=lambda:100+86400
        ClashSubService.refresh_backup_if_due(service)
        service.refresh_backup.assert_called_once()


class ManualBackupTests(unittest.TestCase):
    setUp=BackupSubscriptionTests.setUp
    seed=BackupSubscriptionTests.seed
    LINK='trojan://placeholder-manual-password@192.0.2.30:443?sni=example.com#Manual'

    def test_manual_and_subscription_can_be_changed_and_deleted_independently(self):
        self.seed()
        backup.import_nodes(self.root,self.LINK,now=200)
        backup.refresh(self.root,SOURCE,download=lambda *_:download([dict(NODES[0],port=8443)]),now=300)
        data=backup.read(self.root)
        self.assertEqual(data['manual_updated'],200)
        self.assertEqual(len(backup.combined_nodes(data)),2)
        backup.remove(self.root)
        data=backup.read(self.root)
        self.assertIsNone(data['url'])
        self.assertEqual(backup.status(self.root)['manual_nodes'],['Manual'])
        from clash_sub.service import ClashSubService
        service=SimpleNamespace(config=SimpleNamespace(private_root=self.root),_clock=lambda:9999999,refresh_backup=Mock())
        ClashSubService.refresh_backup_if_due(service)
        service.refresh_backup.assert_not_called()
        backup.refresh(self.root,download=Mock(side_effect=AssertionError('must not download')))
        backup.remove_manual(self.root)
        self.assertFalse(backup.status(self.root)['configured'])

    def test_remove_manual_retains_subscription_and_failed_import_preserves_both(self):
        self.seed();backup.import_nodes(self.root,self.LINK,now=200)
        previous=(self.root/backup.FILENAME).read_bytes()
        with self.assertRaises(ServiceError):backup.import_nodes(self.root,self.LINK+'\ninvalid',now=300)
        self.assertEqual((self.root/backup.FILENAME).read_bytes(),previous)
        with self.assertRaises(ValueError):backup.import_nodes(self.root,self.LINK,validate=Mock(side_effect=ValueError('core rejected')))
        self.assertEqual((self.root/backup.FILENAME).read_bytes(),previous)
        backup.remove_manual(self.root)
        self.assertEqual(backup.read(self.root)['url'],SOURCE)
        self.assertEqual(backup.status(self.root)['manual_nodes'],[])

    def test_merge_deduplicates_configuration_and_disambiguates_names(self):
        first=dict(NODES[0],name='Same')
        data={'nodes':[first,dict(first,name='Other')],'manual_nodes':[dict(first,port=8443)]}
        merged=backup.combined_nodes(data)
        self.assertEqual([n['name'] for n in merged],['Same','Same (2)'])
        self.assertEqual(data['manual_nodes'][0]['name'],'Same')

    def test_multiline_menu_import_and_separate_delete(self):
        from clash_sub.cli import _backup_subscription_dispatch
        service=Mock();report={'configured':True,'nodes':['Manual'],'last_success':None,'last_error':None}
        service.import_backup_nodes.return_value=report
        service.remove_manual_backup_nodes.return_value=report
        out,err=io.StringIO(),io.StringIO()
        value=self.LINK+'\n'+self.LINK.replace(':443',':8443')
        self.assertEqual(_backup_subscription_dispatch('5',io.StringIO(value+'\n\n'),out,err,lambda:service),(0,False))
        service.import_backup_nodes.assert_called_once_with(value)
        self.assertNotIn('placeholder-manual-password',out.getvalue()+err.getvalue())
        _backup_subscription_dispatch('6',io.StringIO('DELETE\n'),out,err,lambda:service)
        service.remove_manual_backup_nodes.assert_called_once()
