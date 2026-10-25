from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import bootstrap_ai_collaboration_studio as bootstrap


class BootstrapChildPathTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='bootstrap-child-paths-')
        self.root = Path(self.temporary.name)
        self.project = self.root/'project'
        self.frontend = self.project/'frontend'
        self.frontend.mkdir(parents=True)
        (self.project/'server.py').write_text('pass\n',encoding='utf-8')
        (self.project/'requirements.txt').write_text('fixture==1.0\n',encoding='ascii')
        (self.project/'requirements-lock-win-py314.txt').write_text('fixture==1.0 --hash=sha256:'+'0'*64+'\n',encoding='ascii')
        (self.frontend/'package.json').write_text(json.dumps({'version':'fixture'}),encoding='utf-8')
        (self.frontend/'package-lock.json').write_text('{}',encoding='utf-8')
        self.runtime = self.project/'runtime'/'bootstrap'
        self.links = []
        tools = {'node': 'fixture-node-never-executed', 'npm': 'fixture-npm-never-executed'}
        self.version_commands = []
        verify_toolchain = bootstrap.portable_runtime.verify_toolchain
        def version_runner(arguments, **kwargs):
            self.version_commands.append(arguments)
            name = next(name for name, executable in tools.items() if executable == arguments[0])
            self.assertEqual(arguments, [tools[name], '--version'])
            return subprocess.CompletedProcess(arguments, 0,
                bootstrap.portable_runtime.EXPECTED_TOOLCHAIN[name] + '\n', '')
        def verify(node, npm, **kwargs):
            return verify_toolchain(node, npm, **kwargs, runner=version_runner)
        self.toolchain = patch.object(bootstrap.portable_runtime, 'verify_toolchain', side_effect=verify)
        self.toolchain.start()
        self.npm = patch.object(bootstrap.shutil,'which',side_effect=lambda name: tools[name.removesuffix('.exe').removesuffix('.cmd')])
        self.npm.start()
        self.commands = patch.object(bootstrap,'command_result',side_effect=AssertionError('no_install_command_permitted'))
        self.command_mock = self.commands.start()

    def tearDown(self):
        self.command_mock.assert_not_called()
        self.commands.stop()
        self.npm.stop()
        self.toolchain.stop()
        for path in reversed(self.links):
            # Remove this owned junction entry itself; never recurse into its target.
            os.rmdir(path)
        self.temporary.cleanup()

    def junction(self, destination):
        if os.name != 'nt':
            self.skipTest('real Windows junction test')
        import _winapi
        target = self.root/('independent-fixture-'+str(len(self.links)))
        target.mkdir()
        sentinel = target/'preserve.txt'
        sentinel.write_bytes(b'owned synthetic target must remain unchanged')
        destination.parent.mkdir(parents=True,exist_ok=True)
        _winapi.CreateJunction(str(target),str(destination))
        self.assertTrue(destination.lstat().st_file_attributes & bootstrap.REPARSE_POINT_ATTRIBUTE)
        self.links.append(destination)
        return target,sentinel

    def call(self, *, check_only=False, allow=True):
        return bootstrap.bootstrap(project_root=self.project,runtime_root=self.runtime,
            allow_dependency_downloads=allow,check_only=check_only,report_path=None)

    def test_python_junction_is_rejected_before_install_or_check_only_plan(self):
        target,sentinel = self.junction(self.runtime/'python')
        before = sentinel.read_bytes()
        for check_only in (False,True):
            with self.subTest(check_only=check_only), self.assertRaisesRegex(bootstrap.BootstrapError,'dependency destination contains a reparse point'):
                self.call(check_only=check_only)
        self.assertEqual(sentinel.read_bytes(),before)
        self.assertEqual(sorted(p.name for p in target.iterdir()),['preserve.txt'])

    def test_node_modules_junction_rejected_before_python_runtime_is_created(self):
        target,sentinel = self.junction(self.frontend/'node_modules')
        with self.assertRaisesRegex(bootstrap.BootstrapError,'dependency destination contains a reparse point'):
            self.call()
        self.assertFalse(self.runtime.exists())
        self.assertEqual(self.version_commands, [])
        self.assertEqual(sentinel.read_bytes(),b'owned synthetic target must remain unchanged')
        self.assertEqual(sorted(p.name for p in target.iterdir()),['preserve.txt'])

    def test_managed_python_scripts_junction_is_rejected(self):
        target,sentinel = self.junction(self.runtime/'python'/'Scripts')
        (target/'python.exe').write_bytes(b'not executable: owned fixture only')
        with self.assertRaisesRegex(bootstrap.BootstrapError,'dependency destination contains a reparse point'):
            self.call()
        self.assertEqual((target/'python.exe').read_bytes(),b'not executable: owned fixture only')
        self.assertEqual(sentinel.read_bytes(),b'owned synthetic target must remain unchanged')

    def test_runtime_ancestor_junction_is_rejected_even_for_direct_function_caller(self):
        self.runtime = self.project/'runtime'/'linked'/'bootstrap'
        target,sentinel = self.junction(self.project/'runtime'/'linked')
        with self.assertRaisesRegex(bootstrap.BootstrapError,'dependency destination contains a reparse point'):
            self.call(check_only=True)
        self.assertFalse((target/'bootstrap').exists())
        self.assertTrue(sentinel.exists())

    def test_clean_check_only_does_not_create_runtime_or_claim_installation(self):
        result = self.call(check_only=True,allow=False)
        self.assertTrue(result['check_only'])
        self.assertFalse(result['dependency_downloads_authorized'])
        self.assertFalse(result['safety']['application_started'])
        self.assertNotIn('python_resolution',result)
        self.assertNotIn('frontend_build',result)
        self.assertFalse(self.runtime.exists())
        self.assertTrue(result['tools']['toolchain']['matches'])
        self.assertEqual(len(self.version_commands), 2)

    def test_existing_independent_dependency_directories_are_valid_for_check_only(self):
        (self.runtime/'python'/'Scripts').mkdir(parents=True)
        executable = self.runtime/'python'/'Scripts'/'python.exe'
        executable.write_bytes(b'owned fixture; not executed')
        (self.frontend/'node_modules').mkdir()
        self.assertTrue(self.call(check_only=True)['check_only'])
        self.assertEqual(executable.read_bytes(),b'owned fixture; not executed')

    def test_download_permission_is_still_required_on_clean_paths(self):
        with self.assertRaisesRegex(bootstrap.BootstrapError,'allow-dependency-downloads'):
            self.call(allow=False)
        self.assertFalse(self.runtime.exists())

    def test_platform_lock_is_still_required_before_runtime_creation(self):
        with patch.object(bootstrap,'python_lock_profile',return_value={'compatible':False}):
            with self.assertRaisesRegex(bootstrap.BootstrapError,'Windows x64 CPython 3.14'):
                self.call()
        self.assertFalse(self.runtime.exists())


if __name__ == '__main__':
    unittest.main()
