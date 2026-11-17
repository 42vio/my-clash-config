import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from clash_sub import traffic_reset as reset


class TrafficResetTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.units = Path(directory.name)
        self.runner = Mock()
        self.confirm = Mock(return_value=True)
        self.patch('time.time', return_value=1)
        self.patch('read_token', return_value='test-token')
        self.patch('bandwagon.load_credentials', return_value=('123', 'test-key'))
        self.query = self.patch('bandwagon.query_usage', return_value={
            'next_reset': datetime(2027, 1, 16, 23, 33, 45, tzinfo=timezone.utc).timestamp()})

    def patch(self, name, **kwargs):
        patcher = patch('clash_sub.traffic_reset.' + name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def configure(self):
        return reset.configure(self.confirm, units=self.units, runner=self.runner)

    def test_configure_uses_single_lookup_and_local_calendar(self):
        self.configure()
        self.query.assert_called_once()
        self.assertIn('每月 17 日 07:33:45', self.confirm.call_args.args[0])
        timer = (self.units / reset.TIMER).read_text()
        self.assertIn('OnCalendar=*-*-17 07:33:45 Asia/Shanghai', timer)
        self.assertNotIn('test-key', timer)
        self.runner.assert_called_with(['systemctl', 'restart', reset.TIMER], check=True)

    def test_cancel_and_stale_time_leave_existing_schedule_alone(self):
        self.confirm.return_value = False
        self.configure()
        self.query.return_value = {'next_reset': 0}
        with self.assertRaises(reset.ScheduleError):
            self.configure()
        self.runner.assert_not_called()
        self.assertEqual(list(self.units.iterdir()), [])

    def test_failed_reschedule_restores_old_calendar(self):
        self.configure()
        previous = (self.units / reset.TIMER).read_bytes()
        self.query.return_value = {'next_reset': 2000000000}
        failure = subprocess.CalledProcessError(1, 'systemctl')
        self.runner.side_effect = [Mock(returncode=0), Mock(returncode=0), None, None,
                                   failure, None, None]
        with self.assertRaises(subprocess.CalledProcessError):
            self.configure()
        self.assertEqual((self.units / reset.TIMER).read_bytes(), previous)

    def test_short_months_have_a_matching_schedule(self):
        rules = reset.calendar_rules(datetime(2027, 1, 31, 14, 33))
        self.assertEqual([rule.split()[0] for rule in rules],
                         ['OnCalendar=*-*-31', 'OnCalendar=*-02~01', 'OnCalendar=*-04,06,09,11~01'])

    def test_timer_resets_both_clients_and_inbounds_and_checks_result(self):
        self.patch('read_panel_settings', return_value=(1024, '/panel-base/', None))
        connection = self.patch('http.client.HTTPConnection').return_value
        response = connection.getresponse.return_value
        response.status = 200
        response.read.return_value = b'{"success":true}'
        reset.reset_inbounds('db')
        self.assertEqual([item.args for item in connection.request.call_args_list], [
            ('POST', '/panel-base/panel/api/clients/resetAllTraffics'),
            ('POST', '/panel-base/panel/api/inbounds/resetAllTraffics'),
        ])
        response.read.return_value = b'{"success":false}'
        with self.assertRaises(ValueError):
            reset.reset_inbounds('db')
        self.query.assert_not_called()

    def test_partial_failure_still_attempts_other_scope_and_reports_failed_scope(self):
        self.patch('read_panel_settings', return_value=(1024, '/panel-base/', None))
        factory = self.patch('http.client.HTTPConnection')
        for failed_scope in ('clients', 'inbounds'):
            with self.subTest(failed_scope=failed_scope):
                clients, inbounds = Mock(), Mock()
                factory.side_effect = [clients, inbounds]
                for scope, connection in (('clients', clients), ('inbounds', inbounds)):
                    response = connection.getresponse.return_value
                    response.status = 200
                    response.read.return_value = (
                        b'{"success":false}' if scope == failed_scope else b'{"success":true}')
                with self.assertRaisesRegex(ValueError, '^traffic_reset_failed_' + failed_scope + '$'):
                    reset.reset_inbounds('db')
                clients.request.assert_called_once()
                inbounds.request.assert_called_once()
                clients.close.assert_called_once()
                inbounds.close.assert_called_once()

    def test_personal_label_reads_installed_timer_and_handles_missing_schedule(self):
        self.assertEqual(reset.scheduled_reset_label(self.units), '未设置定时重置')
        self.configure()
        self.assertEqual(reset.scheduled_reset_label(self.units),
                         '每月 17 日 07:33:45（北京时间）')
        self.assertEqual(reset.scheduled_reset_label(self.units, compact=True),
                         '每月 17 日 07:33')
        (self.units / reset.TIMER).write_text('invalid')
        self.assertEqual(reset.scheduled_reset_label(self.units), '定时重置时间待确认')
