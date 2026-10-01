"""Installer branch checks without modifying the host's printers or keychain."""
from pathlib import Path
import os
import plistlib
import subprocess
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PORTAL = 'https://www.bnbscheduler.top/print/'


class PrintSetupScriptsTests(unittest.TestCase):
    def run_mac(self, reachable, mode=None, install_fails=False):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            log = folder / 'calls'
            source = (ROOT / 'print-setup/uic-print.command').read_text()
            for tool in ('nc', 'open', 'lpadmin'):
                executable = folder / tool
                executable.write_text('#!/bin/bash\nprintf "%s\\n" "' + tool + ':$*" >> "$CALL_LOG"\n' + ('exit "$REACHABLE"\n' if tool == 'nc' else 'exit "$INSTALL_RESULT"\n' if tool == 'lpadmin' else 'exit 0\n'))
                executable.chmod(0o755)
                source = source.replace('/usr/' + ('sbin' if tool == 'lpadmin' else 'bin') + '/' + tool, str(executable))
            script = folder / 'installer'
            script.write_text(source)
            result = subprocess.run(['bash', str(script), *([mode] if mode else [])],
                                    env={**os.environ, 'CALL_LOG': str(log), 'REACHABLE': '0' if reachable else '1', 'INSTALL_RESULT': '1' if install_fails else '0'},
                                    capture_output=True, text=True, timeout=5)
            return result, log.read_text().splitlines() if log.exists() else []

    def test_isolated_network_opens_portal_without_installing_broken_queue(self):
        result, calls = self.run_mac(False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual([c for c in calls if c.startswith('open:')], ['open:' + PORTAL])
        self.assertFalse(any(c.startswith('lpadmin:') for c in calls))
        self.assertIn('学生 Wi-Fi', result.stdout)

    def test_reachable_network_configures_only_the_target_queue(self):
        result, calls = self.run_mac(True)
        self.assertEqual(result.returncode, 0)
        commands = [c for c in calls if c.startswith('lpadmin:')]
        self.assertEqual(len(commands), 1)
        self.assertIn('-p UICPrinter', commands[0])
        self.assertIn('smb://172.16.244.66/DP', commands[0])
        self.assertNotIn(' -x ', commands[0])
        self.assertFalse(any(c.startswith('open:') for c in calls))

    def test_explicit_direct_failure_does_not_change_printers_or_open_browser(self):
        result, calls = self.run_mac(False, '--direct')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith('nc:'))

    def test_explicit_web_does_not_probe_or_install(self):
        result, calls = self.run_mac(True, '--web')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls, ['open:' + PORTAL])

    def test_installation_failure_falls_back_to_a_usable_portal(self):
        result, calls = self.run_mac(True, install_fails=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls[-1], 'open:' + PORTAL)
        self.assertIn('系统打印机配置未完成', result.stdout)

    def test_downloaded_zip_contains_current_executable_installer(self):
        with zipfile.ZipFile(ROOT / 'print-setup/uic-print-mac.zip') as archive:
            self.assertEqual(archive.namelist(), ['uic-print.command'])
            self.assertEqual(archive.read('uic-print.command'), (ROOT / 'print-setup/uic-print.command').read_bytes())
            self.assertTrue(archive.getinfo('uic-print.command').external_attr >> 16 & 0o111)
        profile = plistlib.loads((ROOT / 'print-setup/uic-print.mobileconfig').read_bytes())
        self.assertIn(PORTAL, profile['PayloadDescription'])


if __name__ == '__main__':
    unittest.main()
