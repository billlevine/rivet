"""Rivet marker compatibility and selective entry behavior."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class AnchorTests(unittest.TestCase):
    def test_rivet_legacy_unknown_and_exact_selection(self):
        fixture = ROOT / 'examples/synthetic'
        ids = json.loads((fixture / 'ids.json').read_text())
        old, unrelated = ids['cache-old'], ids['audit-distractor']
        unknown = 'r-0000000000000000'
        with tempfile.TemporaryDirectory(prefix='rivet-anchors-') as tmp:
            work = Path(tmp)
            code = work / 'code'
            code.mkdir()
            (code / 'sample.py').write_text(
                f'def changed():\n    # rivet: {old}\n    pass\n'
                f'def elsewhere():\n    # context: {unrelated}\n    pass\n'
                f'# rivet: {unknown}\n')
            def call(script, *args):
                result = subprocess.run([sys.executable, str(ROOT/script), *map(str, args)],
                                        capture_output=True, text=True, check=True)
                return json.loads(result.stdout)
            store = work / 'store'
            call('build.py', '--records', fixture/'records.json', '--sources', fixture/'sources',
                 '--code', code, '--out', store, '--derive-superseded')
            anchors = json.loads((store/'anchors.json').read_text())
            self.assertEqual({old, unrelated}, set(anchors['anchors']))
            self.assertEqual({unknown}, set(anchors['unknown_ids']))
            focused = call('dg.py', '--root', store, 'context', '--entry', old)
            self.assertNotIn(unrelated, {r['id'] for r in focused['records']})
            self.assertIn(old, {r['id'] for r in focused['records']})
            # A missing ID is explicitly unavailable, never a successful empty answer.
            missing = call('dg.py', '--root', store, 'context', '--entry', unknown)
            self.assertEqual('unavailable', missing['status'])


if __name__ == '__main__':
    unittest.main()
