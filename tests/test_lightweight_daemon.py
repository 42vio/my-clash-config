"""Fresh-process checks so CLI imports cannot hide resident-service regressions."""

import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HEAVY = ('clash_sub.cli','clash_sub.installer','clash_sub.manage','clash_sub.service','clash_sub.runtime','clash_sub.generator','jinja2')


class DaemonStartupTests(unittest.TestCase):
    def test_real_entry_point_dispatches_each_daemon_without_maintenance_imports(self):
        script = '''
import json,runpy,sys
from types import SimpleNamespace
from unittest.mock import patch
command=sys.argv[1]
target='clash_sub.web_server.serve' if command=='web-serve' else 'clash_sub.metadata_server.serve'
config=SimpleNamespace(xui_database='/db',private_root='/private')
with patch('clash_sub.config.load_config',return_value=config),patch('clash_sub.metadata_server.listener_from_environment',return_value=object()),patch(target) as serve:
 sys.argv=['bin/clash-sub',command]
 try:runpy.run_path('bin/clash-sub',run_name='__main__')
 except SystemExit as error:assert error.code==0
 assert serve.call_count==1
print(json.dumps(sorted(sys.modules)))
'''
        for command in ('metadata-serve','web-serve'):
            with self.subTest(command=command):
                modules=json.loads(subprocess.check_output([sys.executable,'-c',script,command],cwd=ROOT,text=True))
                self.assertFalse(set(HEAVY).intersection(modules))

    def test_admin_render_does_not_load_installation_or_generation_stack(self):
        script = '''
import json,sys
sys.path.insert(0,'tests')
from test_lightweight_web_portal import PortalTests
case=PortalTests()
case.setUp()
try:
 assert '服务器概况' in case.portal.admin(case.state,case.settings)
finally:case.doCleanups()
print(json.dumps(sorted(sys.modules)))
'''
        modules=json.loads(subprocess.check_output([sys.executable,'-c',script],cwd=ROOT,text=True))
        self.assertFalse(set(HEAVY).intersection(modules))

    def test_daemon_errors_never_echo_exception_details(self):
        from clash_sub.daemon import main
        for command,code in (('web-serve','web_serve_failed'),('metadata-serve','metadata_serve_failed')):
            output=io.StringIO()
            with patch('clash_sub.config.load_config',side_effect=RuntimeError('sensitive-value')):
                self.assertEqual(main(command,stderr=output),1)
            self.assertIn(code,output.getvalue())
            self.assertNotIn('sensitive-value',output.getvalue())

    def test_public_exports_and_lock_identity_are_compatible(self):
        from clash_sub import ServiceConfig,ConfigError,ClashSubService,ServiceError
        from clash_sub.domain import ServiceConfig as ExpectedConfig
        from clash_sub.config import ConfigError as ExpectedError
        from clash_sub.service import ClashSubService as ExpectedService,ServiceError as ExpectedServiceError,_OperationLock
        from clash_sub.operation import _OperationLock as ExpectedLock
        self.assertIs(ServiceConfig,ExpectedConfig)
        self.assertIs(ConfigError,ExpectedError)
        self.assertIs(ClashSubService,ExpectedService)
        self.assertIs(ServiceError,ExpectedServiceError)
        self.assertIs(_OperationLock,ExpectedLock)
