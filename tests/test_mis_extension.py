"""Regression checks against the actual downloadable extension ZIP."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from zipfile import ZipFile


ARCHIVE = Path(__file__).resolve().parents[1] / 'bnbu-mis-course-helper-extension.zip'
PREFIX = 'bnbu-mis-course-helper-extension/'

POPUP_HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(0, 'utf8');
const cases = [
  ['https://mis.bnbu.edu.cn/mis/student/es/index.do', true],
  ['https://mis.bnbu.edu.cn/mis/student/es/eleDetail.do?course=TEST#tab', true],
  ['https://mis.uic.edu.cn/mis/student/es/index.do', true],
  ['https://mis.uic.edu.cn/mis/student/es/elective.do', true],
  ['https://mis.bnbu.edu.cn/mis/student/as/home.do', true],
  ['https://mis.uic.edu.cn/mis/student/as/home.do?menu=1', true],
  ['https://mis.bnbu.edu.cn/mis/student/as/addSubject.do', true],
  ['https://mis.bnbu.edu.cn/mis/student/as/dropSubject.do', false],
  ['https://mis.bnbu.edu.cn/mis/student/as/replaceList.do', false],
  ['https://mis.bnbu.edu.cn/mis/student/as/home.do.evil', false],
  ['https://mis.bnbu.edu.cn/mis/login.jsp', false],
  ['https://mis.bnbu.edu.cn/mis/student/as/grades.do', false],
  ['https://mis.bnbu.edu.cn.evil.example/mis/student/es/index.do', false],
  ['https://mis.bnbu.edu.cn@evil.example/mis/student/es/index.do', false],
  ['https://mis.bnbu.edu.cn:8443/mis/student/es/index.do', false],
  ['http://mis.bnbu.edu.cn/mis/student/es/index.do', false],
  ['https://other.bnbu.edu.cn/mis/student/es/index.do', false],
  ['https://mis.bnbu.edu.cn/mis/student/es-evil/index.do', false],
  ['not a url', false]
];
(async () => {
  for (const [url, shouldConnect, hint] of cases) {
    const nodes = new Map();
    const sent = [];
    const context = {
      URL, console,
      document: { querySelector(selector) {
        if (!nodes.has(selector)) nodes.set(selector, { value: '', textContent: '', dataset: {}, addEventListener() {} });
        return nodes.get(selector);
      } },
      chrome: {
        storage: { local: { get: async () => ({}) } },
        tabs: {
          query: async () => [{ id: 7, url }],
          sendMessage: async (id, message) => {
            sent.push({ id, message });
            return { config: { enabled: false }, status: 'Connected to fixture' };
          }
        }
      }
    };
    vm.runInNewContext(source, context);
    await new Promise(setImmediate);
    assert.equal(sent.length, shouldConnect ? 1 : 0, url);
    assert.equal(nodes.get('#start').disabled, !shouldConnect, url);
    if (hint) assert.ok(nodes.get('#status-text').textContent.includes(hint), url);
    assert.equal(nodes.get('#min-interval').value, 0.5);
    assert.equal(nodes.get('#max-interval').value, 0.5);
    assert.equal(nodes.get('#submit-timeout').value, 10);
    assert.ok(sent.every(item => item.message.type === 'get-state'));
  }
  console.log(`${cases.length} popup URL cases passed; no selection actions sent`);
})().catch(error => { console.error(error); process.exit(1); });
"""


class MisExtensionTestCase(unittest.TestCase):
    def test_manifest_scopes_selection_and_add_without_drop(self):
        selection = {
            'https://mis.bnbu.edu.cn/mis/student/es/*',
            'https://mis.uic.edu.cn/mis/student/es/*',
        }
        add = {f'https://{host}/mis/student/as/{path}.do*'
               for host in ('mis.bnbu.edu.cn', 'mis.uic.edu.cn')
               for path in ('home', 'addSubject')}
        with ZipFile(ARCHIVE) as archive:
            manifest = json.loads(archive.read(PREFIX + 'manifest.json'))
        self.assertEqual(set(manifest['host_permissions']), selection | add)
        self.assertEqual(len(manifest['content_scripts']), 3)
        for script in manifest['content_scripts']:
            self.assertEqual(set(script['matches']), add if 'adddrop.js' in script['js'] else selection)
            if 'adddrop.js' in script['js']:
                self.assertEqual(script['world'], 'ISOLATED')
                self.assertNotIn('page-bridge.js', script['js'])

    @unittest.skipUnless(shutil.which('node'), 'Node.js is needed for the popup JS harness')
    def test_popup_domain_and_homepage_detection(self):
        with ZipFile(ARCHIVE) as archive:
            source = archive.read(PREFIX + 'popup.js').decode()
        result = subprocess.run(
            ['node', '-e', POPUP_HARNESS], input=source, text=True,
            capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
