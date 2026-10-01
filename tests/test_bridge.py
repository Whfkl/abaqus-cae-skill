"""Behavioral tests with a fake GUI event loop and a real socket server."""

import atexit
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from abaqus_cae_skill.client import AbaqusBridgeClient, BridgeError
from abaqus_cae_skill.install import install, uninstall
from abaqus_cae_skill import cli


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_install_update_and_uninstall_owned_files(self):
        result = install(self.root / 'config', str(self.root / 'plugins'), 'chosen_plugin.py', 0,
                         str(self.root / 'skills'))
        target = Path(result['installation']['pluginPath'])
        self.assertTrue(target.exists())
        updated = install(self.root / 'config')
        self.assertEqual(result['installation']['installationId'], updated['installation']['installationId'])
        self.assertEqual(len(list(target.parent.glob('*_plugin.py'))), 1)
        uninstall(self.root / 'config')
        self.assertFalse(target.exists())

    def test_collision_and_modified_file_are_preserved(self):
        plugins = self.root / 'plugins'
        plugins.mkdir()
        foreign = plugins / 'other_plugin.py'
        foreign.write_text('foreign', encoding='utf-8')
        with self.assertRaises(FileExistsError):
            install(self.root / 'config', str(plugins), foreign.name, 0)
        self.assertEqual(foreign.read_text(), 'foreign')
        installed = install(self.root / 'config', str(plugins), 'ours_plugin.py', 0)
        ours = Path(installed['installation']['pluginPath'])
        ours.write_text('modified', encoding='utf-8')
        with self.assertRaises(FileExistsError):
            install(self.root / 'config')
        with self.assertRaises(ValueError):
            uninstall(self.root / 'config')
        self.assertEqual(ours.read_text(), 'modified')

    def test_invalid_plugin_name(self):
        for name in ('../x_plugin.py', 'x.py', 'bad-name_plugin.py'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                install(self.root / 'config', str(self.root / 'plugins'), name, 0)

    def test_modified_skill_is_preserved(self):
        skill = self.root / 'skills' / 'abaqus-cae-skill' / 'SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_text('mine')
        with self.assertRaises(FileExistsError):
            install(self.root / 'config', str(self.root / 'plugins'), port=0, skill_dir=str(skill.parent.parent))
        self.assertEqual(skill.read_text(), 'mine')
        self.assertFalse((self.root / 'plugins').exists())


class CliContractTests(unittest.TestCase):
    def invoke(self, args):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), self.assertRaises(SystemExit) as caught:
            cli.main(args)
        return caught.exception.code, json.loads(stream.getvalue())

    def test_mutually_exclusive_inputs_return_json(self):
        code, result = self.invoke(['run-python', '--code', '1', '--stdin'])
        self.assertEqual(code, 2)
        self.assertFalse(result['ok'])

    def test_unknown_execution_is_nonzero_and_never_retried(self):
        client = types.SimpleNamespace(execute=lambda *args: None)
        with patch.object(cli, 'selected_client', return_value=client), \
             patch.object(client, 'execute', side_effect=BridgeError('outcome unknown', 'test-id', 'UNKNOWN')) as execute:
            code, result = self.invoke(['run-python', '--code', 'x = 1'])
        self.assertEqual(code, 3)
        self.assertEqual(result['executionId'], 'test-id')
        self.assertEqual(result['state'], 'UNKNOWN')
        self.assertEqual(execute.call_count, 1)

    def test_response_correlation_rejects_another_execution(self):
        from abaqus_cae_skill.protocol import send_message, read_message
        import socketserver
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                request = read_message(self.request)
                send_message(self.request, {'id': request['id'], 'ok': True,
                             'result': {'executionId': 'some-other-execution', 'ok': True}})
        with socketserver.TCPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.handle_request)
            thread.start()
            with self.assertRaisesRegex(BridgeError, 'mismatched executionId'):
                AbaqusBridgeClient(port=server.server_address[1]).execute('1')
            thread.join(2)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.kernel = {}
        self.scheduled = []
        self.dispatch_threads = []
        self.app = types.SimpleNamespace(
            addTimeout=lambda delay, owner, ident: self.scheduled.append((owner, ident)),
            getAFXMainWindow=lambda: types.SimpleNamespace(getPluginToolset=lambda: self.toolset))
        self.toolset = types.SimpleNamespace(registerGuiMenuButton=lambda **kwargs: None)
        gui = types.ModuleType('abaqusGui')
        class Form:
            ID_LAST = 100
            def __init__(self, owner):
                pass
        gui.AFXForm = Form
        gui.AFXMode = types.SimpleNamespace(ID_ACTIVATE=1)
        gui.FXMAPFUNC = lambda *args: None
        gui.SEL_COMMAND = 1
        gui.SEL_TIMEOUT = 2
        gui.getAFXApp = lambda: self.app
        def send_command(code, *args):
            self.dispatch_threads.append(threading.get_ident())
            exec(code, self.kernel)
        gui.sendCommand = send_command
        gui.showAFXErrorDialog = lambda *args: None
        abaqus = types.ModuleType('abaqus')
        abaqus.mdb = types.SimpleNamespace(models={'Existing': object()}, jobs={})
        abaqus.session = types.SimpleNamespace(version='test-only', viewports={'Viewport: 1': object()})
        self.modules = patch.dict(sys.modules, {'abaqusGui': gui, 'abaqus': abaqus})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        installed = install(self.root / 'config', str(self.root / 'plugins'), port=0)
        spec = importlib.util.spec_from_file_location('test_cae_plugin', installed['installation']['pluginPath'])
        self.plugin = importlib.util.module_from_spec(spec)
        with contextlib.redirect_stdout(io.StringIO()):
            spec.loader.exec_module(self.plugin)
        self.assertIsNone(self.plugin._SERVER)
        owner, ident = self.scheduled.pop(0)
        self.assertEqual(ident, owner.ID_BOOT)
        with contextlib.redirect_stdout(io.StringIO()):
            owner.onBoot(None, None, None)
        self.addCleanup(atexit.unregister, self.plugin.stop_gui_agent)
        self.addCleanup(self.plugin.stop_gui_agent)
        self.client = AbaqusBridgeClient(port=self.plugin.PORT, timeout=1, session_id=self.plugin.SESSION_ID)

    def pump(self):
        pending = self.scheduled[:]
        self.scheduled.clear()
        for owner, ident in pending:
            if ident == owner.ID_POLL:
                owner.onPoll(None, None, None)

    def request(self, code, filename=None):
        values = []
        thread = threading.Thread(target=lambda: values.append(self.client.execute(code, filename)))
        thread.start()
        deadline = time.time() + 5
        while thread.is_alive() and time.time() < deadline:
            self.pump()
            time.sleep(.01)
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertTrue(values)
        return values[0]

    def run_cli(self, *args, stdin=None):
        process = subprocess.Popen([sys.executable, '-m', 'abaqus_cae_skill', '--config-dir',
                                   str(self.root / 'config'), *args], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL)
        if stdin is not None:
            process.stdin.write(stdin.encode('utf-8'))
            process.stdin.close()
            process.stdin = None
        deadline = time.time() + 10
        while process.poll() is None and time.time() < deadline:
            self.pump()
            time.sleep(.01)
        if process.poll() is None:
            process.kill()
            self.fail('CLI did not complete')
        out, err = process.communicate()
        self.assertEqual(err, b'')
        return process.returncode, json.loads(out.decode('utf-8'))

    def test_automatic_start_dispatch_and_persistent_namespace(self):
        first = self.request("shared = 41\nresult = shared")
        second = self.request('shared + 1')
        self.assertEqual(first['return_value'], 41)
        self.assertEqual(second['return_value'], 42)
        self.assertNotEqual(first['executionId'], second['executionId'])
        self.assertEqual(set(self.dispatch_threads), {threading.get_ident()})
        self.assertIsNone(self.request('x = 1')['return_value'])

    def test_code_file_stdin_and_script_context(self):
        code, value = self.run_cli('run-python', '--code', "result = '中文'")
        self.assertEqual(code, 0)
        self.assertEqual(value['value'], '中文')
        directory = self.root / '中文 scripts'
        directory.mkdir()
        (directory / 'helper.py').write_text('value = 73', encoding='utf-8')
        source = directory / 'edit model.py'
        source.write_text("from helper import value\nif __name__ == '__main__':\n    result = {'v': value, 'file': __file__}\n", encoding='utf-8')
        previous = os.getcwd()
        code, value = self.run_cli('run-python', '--file', str(source))
        self.assertEqual(code, 0)
        self.assertEqual(value['value']['v'], 73)
        self.assertEqual(value['value']['file'], str(source))
        self.assertEqual(os.getcwd(), previous)
        code, value = self.run_cli('run-python', '--stdin', stdin='result = 6 * 7\n')
        self.assertEqual((code, value['value']), (0, 42))
        self.assertFalse(self.request("'__file__' in globals()")['return_value'])

    def test_execution_error_exit_code_and_file_location(self):
        source = self.root / 'broken.py'
        source.write_text("a = 1\nraise ValueError('bad input')\n", encoding='utf-8')
        code, value = self.run_cli('run-python', '--file', str(source))
        self.assertEqual(code, 1)
        self.assertFalse(value['ok'])
        self.assertEqual(value['error']['at']['file'], str(source))
        self.assertEqual(value['error']['at']['line'], 2)
        code, value = self.run_cli('run-python', '--code', 'result = (')
        self.assertEqual(code, 1)
        self.assertIn('SyntaxError', value['error']['type'])

    def test_session_identity_and_ambiguous_selection(self):
        records = cli.discover(self.root / 'config')
        self.assertTrue(records[0]['reachable'])
        self.assertEqual(self.client.request('describe')['sessionId'], self.plugin.SESSION_ID)
        wrong = AbaqusBridgeClient(port=self.plugin.PORT, timeout=1, session_id='wrong')
        with self.assertRaises(BridgeError) as caught:
            wrong.execute('result = 7')
        self.assertEqual(caught.exception.state, 'NOT_STARTED')
        args = cli.parser().parse_args(['ping'])
        with patch.object(cli, 'discover', return_value=[dict(records[0]), dict(records[0], sessionId='other')]):
            with self.assertRaisesRegex(ValueError, 'Multiple CAE'):
                cli.selected_client(args, self.root / 'config')

    def test_cancelled_request_does_not_execute(self):
        item = self.plugin.GuiRequest('execute', {'code': 'result = 99'})
        self.assertTrue(item.cancel_if_queued())
        self.plugin._REQUESTS.put(item)
        self.pump()
        self.assertEqual(item.state, 'cancelled')
        self.assertIsNone(item.result)

    def test_port_conflict_publishes_actual_port(self):
        import socket
        self.plugin.stop_gui_agent()
        with socket.socket() as blocker:
            blocker.bind(('127.0.0.1', 0))
            blocker.listen()
            requested = blocker.getsockname()[1]
            self.plugin.PORT = requested
            with contextlib.redirect_stdout(io.StringIO()):
                self.plugin.start_gui_agent()
            self.assertNotEqual(self.plugin.PORT, requested)
            self.assertEqual(self.plugin._SESSION_INFO['port'], self.plugin.PORT)


if __name__ == '__main__':
    unittest.main()
