"""
tools/cpanel_deploy.py with a fake cPanel API (no network).
Run:  python3 -m unittest tests.test_cpanel_deploy -v
"""
import io
import os
import sys
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'tools'))

import cpanel_deploy as cd                               # noqa: E402

ROOT = '/home/u/repositories/APQMS-'
NEW, OLD = 'abc1234def5678', '9999999aaaaaa'


class FakeCpanel:
    def __init__(self, pull_moves_head=True, deploy_result='succeeded'):
        self.head = OLD
        self.calls = []
        self.pull_moves_head = pull_moves_head
        self.deploy_result = deploy_result

    def __call__(self, module, function, **params):
        self.calls.append((module, function, params))
        if (module, function) == ('VersionControl', 'retrieve'):
            return {'status': 1, 'data': [
                {'repository_root': '/home/u/repositories/other', 'source_repository': {'url': 'git@github.com:x/other.git'}},
                {'repository_root': ROOT, 'branch': 'main', 'last_update': {'identifier': self.head},
                 'source_repository': {'url': 'git@github.com:manavbudhia-dev/APQMS-.git'}}]}
        if (module, function) == ('VersionControl', 'update'):
            if self.pull_moves_head:
                self.head = NEW
            return {'status': 1, 'data': {}}
        if (module, function) == ('VersionControlDeployment', 'create'):
            return {'status': 1, 'data': {'deploy_id': 7}}
        if (module, function) == ('VersionControlDeployment', 'retrieve'):
            return {'status': 1, 'data': [{'deploy_id': 7, 'log_path': '/home/u/.cpanel/logs/vc_7.log',
                                           'repository_state': {'identifier': NEW},
                                           'timestamps': {self.deploy_result: '1700000000'}}]}
        raise AssertionError((module, function))


class Deploy(unittest.TestCase):
    def setUp(self):
        self.orig = (cd.uapi, cd.HOST, cd.USER, cd.TOKEN, cd.REPO_ROOT, cd.time.sleep)
        cd.HOST, cd.USER, cd.TOKEN, cd.REPO_ROOT = 'host', 'u', 't', ''
        cd.time.sleep = lambda s: None

    def tearDown(self):
        cd.uapi, cd.HOST, cd.USER, cd.TOKEN, cd.REPO_ROOT, cd.time.sleep = self.orig

    def run_deploy(self, fake):
        cd.uapi = fake
        sys.argv = ['x', NEW]
        out = io.StringIO()
        with redirect_stdout(out):
            try:
                code = cd.main()
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def test_success(self):
        fake = FakeCpanel()
        code, out = self.run_deploy(fake)
        self.assertEqual(code, 0, out)
        self.assertIn('Deployed abc1234def', out)
        self.assertIn(('VersionControl', 'update', {'repository_root': ROOT, 'branch': 'main'}), fake.calls)
        self.assertIn(('VersionControlDeployment', 'create', {'repository_root': ROOT}), fake.calls)

    def test_pull_did_not_move(self):
        code, out = self.run_deploy(FakeCpanel(pull_moves_head=False))
        self.assertEqual(code, 1)
        self.assertIn('not abc1234def', out)

    def test_deploy_failed(self):
        code, out = self.run_deploy(FakeCpanel(deploy_result='failed'))
        self.assertEqual(code, 1)
        self.assertIn('vc_7.log', out)

    def test_api_error(self):
        code, out = self.run_deploy(lambda m, f, **p: {'status': 0, 'errors': ['Access denied']})
        self.assertEqual(code, 1)
        self.assertIn('Access denied', out)


if __name__ == '__main__':
    unittest.main()
