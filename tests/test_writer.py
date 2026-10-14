"""Public capture behavior on invented source text."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class WriterTests(unittest.TestCase):
    def test_add_and_amend_preserve_explicit_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            sources = base / 'sources'
            sources.mkdir()
            (sources / 'notes.md').write_text('Use a disk queue.\nUse a bounded memory queue.\n', encoding='utf-8')
            records = base / 'records.json'
            common = ['--records', str(records), '--sources', str(sources)]
            def call(*args):
                return subprocess.run([sys.executable, str(ROOT / 'dg_write.py'), *args],
                                      capture_output=True, text=True)
            added = call('add', *common, '--label', 'old', '--kind', 'decision', '--topic', 'jobs',
                         '--title', 'Disk queue', '--claim', 'Use a disk queue.', '--actor', 'author',
                         '--authority', 'unspecified', '--state', 'accepted', '--date', '2025-01-01',
                         '--source', 'notes.md:1-1')
            self.assertEqual(0, added.returncode, added.stdout + added.stderr)
            old = json.loads(records.read_text())['records'][0]
            self.assertEqual('unspecified', old['authority'])
            changed = call('amend', *common, '--label', 'new', '--kind', 'decision', '--topic', 'jobs',
                           '--title', 'Bounded memory queue', '--claim', 'Use a bounded memory queue.',
                           '--actor', 'author', '--authority', 'unspecified', '--state', 'accepted',
                           '--date', '2025-02-01', '--source', 'notes.md:2-2', '--supersedes', old['id'],
                           '--note', 'The bounded rule replaces the disk rule.')
            self.assertEqual(0, changed.returncode, changed.stdout + changed.stderr)
            capture = json.loads(records.read_text())
            self.assertEqual(2, len(capture['records']))
            self.assertEqual({'unspecified'}, {row['authority'] for row in capture['records']})
            self.assertIn(old, capture['records'])
            self.assertEqual('supersedes', capture['edges'][0]['relation'])

if __name__ == '__main__':
    unittest.main()
