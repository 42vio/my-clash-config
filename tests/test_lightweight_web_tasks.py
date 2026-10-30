import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from clash_sub.domain import RuntimeState, UserState
from clash_sub.service import ServiceError
from clash_sub.state import generate_token, load_state, save_state
from clash_sub.web_settings import password_hash, provision_pages, read_settings, write_settings
from clash_sub.web_tasks import authorization, delete_history, enqueue, history, run, update


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = SimpleNamespace(private_root=self.root,systemctl_binary=Path('/usr/bin/systemctl'))
        self.state = RuntimeState(2,1,{1:UserState(1,'owner',generate_token('owner'),None,True,'release'),2:UserState(2,'member',generate_token('member'),None,True,'release')})
        save_state(self.root/'state.json',self.state)
        self.settings = provision_pages({'version':1,'enabled':True,'panel_public':False,'pages':{},'password_hash':password_hash('synthetic-task-password')},self.state)
        write_settings(self.root,self.settings)
        self.actor = authorization(self.settings,self.state)
        self.runner = Mock(return_value=SimpleNamespace(returncode=0))

    def queue(self,action='refresh-airport',target=None,key='request-aaaaaaaaaaaaaaaa'):
        return enqueue(self.config,action,target,key,self.actor,runner=self.runner)

    def test_allowlist_and_target_validation_precede_any_command(self):
        for action,target in [('shell',None),('update','1'),('sync','1'),('update-sync',';reboot'),('rotate-sub','admin'),('rotate-sub','999')]:
            with self.subTest(action=action,target=target),self.assertRaises(ValueError):self.queue(action,target)
        self.runner.assert_not_called()

    def test_airport_subscription_rotation_is_an_authorized_distinct_target(self):
        task=self.queue('rotate-sub','airport')
        item=history(self.config)[0]
        self.assertEqual(item['id'],task)
        self.assertEqual(item['target'],'airport')
        service=Mock()
        run(self.config,self.root,task,service_factory=lambda:service)
        service.rotate_airport_link.assert_called_once_with()
        service.rotate_link.assert_not_called()
        self.assertEqual(history(self.config)[0]['status'],'success')

    def test_history_deletion_clears_finished_records_and_preserves_pending(self):
        task=self.queue()
        delete_history(self.config,self.actor)
        self.assertEqual([r['id'] for r in history(self.config)],[task])
        update(self.config,task,'running','执行中')
        delete_history(self.config,self.actor)
        self.assertEqual([r['id'] for r in history(self.config)],[task])
        update(self.config,task,'failed','失败')
        other=self.queue(key='request-bbbbbbbbbbbbbbbb')
        delete_history(self.config,self.actor)
        self.assertEqual([r['id'] for r in history(self.config)],[other])
        delete_history(self.config,self.actor)
        self.assertEqual([r['id'] for r in history(self.config)],[other])

    def test_history_deletion_requires_current_owner_authority(self):
        task=self.queue()
        update(self.config,task,'success','已完成')
        with self.assertRaises(ValueError):delete_history(self.config,'invalid')
        self.assertEqual(len(history(self.config)),1)

    def test_duplicate_request_returns_same_job_and_other_request_is_busy(self):
        task=self.queue()
        self.assertEqual(self.queue(),task)
        self.runner.assert_called_once()
        with self.assertRaises(ServiceError):self.queue(key='request-bbbbbbbbbbbbbbbb')

    def test_queue_starts_fixed_template_unit_and_does_not_log_capabilities(self):
        task=self.queue()
        args=self.runner.call_args.args[0]
        self.assertEqual(args,['/usr/bin/systemctl','start','--no-block','clash-sub-task@'+task+'.service'])
        text=(self.root/'web-tasks.json').read_text()
        for user in self.state.users.values():self.assertNotIn(user.token,text)
        self.assertNotIn(self.settings['admin']['token'],text)
        self.assertEqual((self.root/'web-tasks.json').stat().st_mode & 0o777,0o600)

    def test_worker_does_not_replay_completed_task(self):
        task=self.queue()
        service=Mock()
        service.auto_refresh_airport.return_value={'updated':True}
        run(self.config,self.root,task,service_factory=lambda:service,runner=self.runner)
        run(self.config,self.root,task,service_factory=lambda:service,runner=self.runner)
        service.auto_refresh_airport.assert_called_once()
        service.refresh_airport.assert_not_called()
        self.assertEqual(history(self.config)[0]['status'],'success')
        self.assertEqual(self.queue(),task)

    def test_revoked_owner_cannot_run_queued_operation(self):
        task=self.queue()
        state=RuntimeState(2,2,self.state.users)
        save_state(self.root/'state.json',state)
        service=Mock()
        run(self.config,self.root,task,service_factory=lambda:service,runner=self.runner)
        service.auto_refresh_airport.assert_not_called()
        self.assertEqual(history(self.config)[0]['status'],'failed')

    def test_unavailable_airport_skip_is_not_reported_as_success(self):
        task=self.queue()
        service=Mock()
        service.auto_refresh_airport.return_value={'updated':False,'skipped':True}
        run(self.config,self.root,task,service_factory=lambda:service)
        record=history(self.config)[0]
        self.assertEqual(record['status'],'failed')
        self.assertIn('保留原配置',record['phase'])

    def test_airport_failure_shows_safe_reason_without_sensitive_details(self):
        task=self.queue()
        service=Mock()
        service.auto_refresh_airport.side_effect=ServiceError('airport_download_failed')
        run(self.config,self.root,task,service_factory=lambda:service)
        self.assertIn('下载失败',history(self.config)[0]['phase'])

    def test_update_only_never_generates_or_refreshes_subscriptions(self):
        task=self.queue('update')
        self.assertEqual(history(self.config)[0]['target_label'],'项目')
        self.runner.reset_mock()
        service=Mock()
        with patch('clash_sub.manage.run_update') as updater:
            run(self.config,self.root,task,service_factory=lambda:service,runner=self.runner)
        updater.assert_called_once()
        self.assertTrue(callable(updater.call_args.kwargs['authorize']))
        self.runner.assert_not_called()
        service.sync_all.assert_not_called()
        service.auto_refresh_airport.assert_not_called()
        self.assertEqual(history(self.config)[0]['status'],'success')

    def test_generate_only_uses_current_code_without_upstream_refresh(self):
        task=self.queue('sync')
        self.runner.reset_mock()
        service=Mock();service.sync_all.return_value={'errors':()}
        with patch('clash_sub.manage.run_update') as updater:
            run(self.config,self.root,task,service_factory=lambda:service,runner=self.runner)
        service.sync_all.assert_called_once_with()
        service.auto_refresh_airport.assert_not_called()
        updater.assert_not_called()
        self.runner.assert_not_called()
        self.assertEqual(history(self.config)[0]['status'],'success')

    def test_partial_generation_failure_is_not_reported_as_success(self):
        task=self.queue('sync')
        service=Mock();service.sync_all.return_value={'errors':('member_update_failed',)}
        run(self.config,self.root,task,service_factory=lambda:service)
        service.sync_all.assert_called_once_with()
        self.assertEqual(history(self.config)[0]['status'],'failed')

    def test_update_only_failure_is_recorded_without_generating(self):
        task=self.queue('update')
        service=Mock()
        with patch('clash_sub.manage.run_update',side_effect=RuntimeError('private-value')):
            run(self.config,self.root,task,service_factory=lambda:service)
        service.sync_all.assert_not_called()
        self.assertEqual(history(self.config)[0]['status'],'failed')
        self.assertNotIn('private-value',(self.root/'web-tasks.json').read_text())

    def test_split_tasks_cannot_run_after_owner_authority_is_revoked(self):
        for index,action in enumerate(('sync','update')):
            task=self.queue(action,key='request-revoked-0000'+str(index))
            original=read_settings(self.root)
            settings=dict(original,password_hash=password_hash('revoked-password'))
            write_settings(self.root,settings)
            service=Mock()
            with patch('clash_sub.manage.run_update') as updater:
                run(self.config,self.root,task,service_factory=lambda:service)
            updater.assert_not_called()
            service.sync_all.assert_not_called()
            self.assertEqual(history(self.config)[0]['status'],'failed')
            write_settings(self.root,original)

    def test_update_failure_never_runs_sync(self):
        task=self.queue('update-sync')
        self.runner.reset_mock()
        with patch('clash_sub.manage.run_update',side_effect=RuntimeError('sensitive-error')):
            run(self.config,self.root,task,runner=self.runner)
        self.runner.assert_not_called()
        self.assertEqual(history(self.config)[0]['status'],'failed')
        self.assertNotIn('sensitive-error',(self.root/'web-tasks.json').read_text())

    def test_update_success_runs_new_code_with_task_bound_sync(self):
        task=self.queue('update-sync')
        self.runner.reset_mock()
        with patch('clash_sub.manage.run_update') as update:
            run(self.config,self.root,task,runner=self.runner)
        update.assert_called_once()
        args=self.runner.call_args.args[0]
        self.assertEqual(args[-3:],['web-task',task,'--sync-only'])
        self.assertEqual(history(self.config)[0]['status'],'success')

    def test_subscription_rotation_preserves_independent_pages(self):
        task=self.queue('rotate-sub','2')
        old_page=self.settings['pages']['2']['token']
        def rotate(user):
            state=load_state(self.root/'state.json')
            old=state.users[2]
            users=dict(state.users)
            users[2]=UserState(2,old.email,generate_token(old.email),None,True,old.current_release)
            save_state(self.root/'state.json',RuntimeState(2,1,users))
        service=Mock();service.rotate_link.side_effect=rotate
        run(self.config,self.root,task,service_factory=lambda:service)
        settings=read_settings(self.root)
        self.assertEqual(settings['pages']['2']['token'],old_page)
        self.assertNotEqual(load_state(self.root/'state.json').users[2].token,self.state.users[2].token)
        self.assertEqual(history(self.config)[0]['status'],'success')

    def test_failed_service_start_is_recorded_without_automatic_retry(self):
        self.runner.return_value.returncode=1
        with self.assertRaises(ValueError):self.queue()
        self.assertEqual(history(self.config)[0]['status'],'failed')
        self.runner.assert_called_once()

    def test_rebooted_pending_job_is_interrupted(self):
        with patch('clash_sub.web_tasks._boot_id',return_value='boot-one'):
            self.queue()
        with patch('clash_sub.web_tasks._boot_id',return_value='boot-two'):
            self.assertEqual(history(self.config)[0]['status'],'interrupted')

    def test_sync_only_requires_correct_running_task_and_phase(self):
        task=self.queue('refresh-airport')
        with self.assertRaises(ValueError):run(self.config,self.root,task,sync_only=True)

    def test_reused_target_id_does_not_rotate_new_identity(self):
        task=self.queue('rotate-sub','2')
        users=dict(self.state.users)
        users[2]=UserState(2,'different',generate_token('different'),None,True,'release')
        save_state(self.root/'state.json',RuntimeState(2,1,users))
        service=Mock()
        run(self.config,self.root,task,service_factory=lambda:service)
        service.rotate_link.assert_not_called()
        self.assertEqual(history(self.config)[0]['status'],'failed')

    def test_fresh_process_sync_rechecks_authority_and_result(self):
        from clash_sub.web_tasks import update
        task=self.queue('update-sync')
        update(self.config,task,'running','代码更新成功，正在生成配置')
        service=Mock();service.sync_all.return_value={'errors':()}
        run(self.config,self.root,task,service_factory=lambda:service,sync_only=True)
        service.sync_all.assert_called_once()
        self.settings['password_hash']=password_hash('changed-owner-password')
        write_settings(self.root,self.settings)
        with self.assertRaises(ValueError):run(self.config,self.root,task,service_factory=lambda:service,sync_only=True)
