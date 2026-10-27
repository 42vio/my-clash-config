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
from clash_sub.web_tasks import authorization, enqueue, history, run


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
        for action,target in [('shell',None),('update-sync',';reboot'),('rotate-sub','airport'),('rotate-sub','999')]:
            with self.subTest(action=action,target=target),self.assertRaises(ValueError):self.queue(action,target)
        self.runner.assert_not_called()

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
        run(self.config,self.root,task,service_factory=lambda:service,runner=self.runner)
        run(self.config,self.root,task,service_factory=lambda:service,runner=self.runner)
        service.refresh_airport.assert_called_once()
        self.assertEqual(history(self.config)[0]['status'],'success')
        self.assertEqual(self.queue(),task)

    def test_revoked_owner_cannot_run_queued_operation(self):
        task=self.queue()
        state=RuntimeState(2,2,self.state.users)
        save_state(self.root/'state.json',state)
        service=Mock()
        run(self.config,self.root,task,service_factory=lambda:service,runner=self.runner)
        service.refresh_airport.assert_not_called()
        self.assertEqual(history(self.config)[0]['status'],'failed')

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
