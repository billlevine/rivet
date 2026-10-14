#!/usr/bin/env python3
"""Compare the candidate reader with an external reader on invented data."""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / 'examples' / 'synthetic'

def call(command):
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f'command failed with exit {result.returncode}: {result.stderr[:300]}')
    return json.loads(result.stdout)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference-reader', required=True, type=Path,
                        help='path to an external recall.py; it is read but never copied')
    args = parser.parse_args()
    if not args.reference_reader.is_file():
        parser.error('reference reader is not a file')
    ids = json.loads((EXAMPLE / 'ids.json').read_text())
    with tempfile.TemporaryDirectory(prefix='dg-compat-') as temp:
        store = Path(temp) / 'store'
        call([sys.executable, str(ROOT / 'build.py'), '--records', str(EXAMPLE / 'records.json'),
              '--sources', str(EXAMPLE / 'sources'), '--out', str(store), '--derive-superseded'])
        cases = [
            ['--store', str(store), '--seed', ids['cache-new'], '--profile', 'reasoning', '--depth', '1', '--budget', '4000'],
            ['--store', str(store), '--seed', ids['cache-old'], '--profile', 'review', '--depth', '0', '--budget', '4000'],
            ['--store', str(store), '--query', 'audit log previews', '--profile', 'modification', '--limit', '1', '--budget', '2000'],
        ]
        for index, arguments in enumerate(cases, 1):
            candidate = call([sys.executable, str(ROOT / 'recall.py'), *arguments])
            reference = call([sys.executable, str(args.reference_reader), *arguments])
            if candidate != reference:
                print(json.dumps({'status': 'different', 'case': index}, sort_keys=True))
                return 1
    print(json.dumps({'status': 'same', 'cases': len(cases), 'scope': 'invented fixture; no pagination'}, sort_keys=True))
    return 0

if __name__ == '__main__':
    sys.exit(main())
