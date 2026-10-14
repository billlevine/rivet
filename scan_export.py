#!/usr/bin/env python3
"""Inspect this candidate tree for unexpected files and sensitive markers."""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ROOT_FILES = {'README.md', 'LICENSE', 'pyproject.toml', 'build.py', 'dg.py', 'dg_write.py', 'recall.py', 'rivet.py', 'scan_export.py'}
PREFIXES = {
    'bench/': {'.py'},
    'tests/': {'.py'},
    'examples/synthetic/': {'.py', '.json', '.md'},
    'examples/synthetic/sources/': {'.md', '.txt'},
}
MAX_BYTES = 1_000_000

def allowed(path):
    if path in ROOT_FILES or path in {'skills/rivet/SKILL.md', 'bench/candidate-evals.json'}:
        return True
    return any(path.startswith(prefix) and Path(path).suffix in suffixes for prefix, suffixes in PREFIXES.items())

def rules():
    return {
        'absolute_home_path': re.compile(r'/' + 'home' + r'/|/' + 'Users' + r'/|[A-Za-z]:\\' + 'Users' + r'\\', re.I),
        'credential_marker': re.compile(r'ANTHROPIC_' + r'API_KEY|OPENAI_' + r'API_KEY|-----BEGIN ' + r'(?:RSA |EC |OPENSSH )?PRIVATE KEY-----', re.I),
        'token_shape': re.compile(r'ghp_' + r'[A-Za-z0-9]{20,}|sk-' + r'ant-' + r'[A-Za-z0-9_-]{12,}|AKIA' + r'[A-Z0-9]{16}'),
    }

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true', help='print JSON report')
    parser.add_argument('--deny-file', type=Path, help='optional external UTF-8 file with one literal forbidden term per line')
    args = parser.parse_args()
    try:
        denied = [line.strip().casefold() for line in args.deny_file.read_text(encoding='utf-8').splitlines()
                  if line.strip() and not line.lstrip().startswith('#')] if args.deny_file else []
    except (OSError, UnicodeError) as exc:
        parser.error(f'could not read deny file: {type(exc).__name__}')
    findings = []
    checked = 0
    patterns = rules()
    for path in sorted(ROOT.rglob('*')):
        rel = path.relative_to(ROOT).as_posix()
        if rel == '.git' or rel.startswith('.git/'):
            continue  # Repository metadata is not part of the exported source tree.
        if path.is_symlink():
            findings.append({'path': rel, 'category': 'symlink'})
            continue
        if path.is_dir():
            continue
        if not allowed(rel):
            findings.append({'path': rel, 'category': 'unexpected_file'})
            continue
        if path.stat().st_size > MAX_BYTES:
            findings.append({'path': rel, 'category': 'oversize_file'})
            continue
        try:
            value = path.read_text(encoding='utf-8')
        except UnicodeError:
            findings.append({'path': rel, 'category': 'non_utf8'})
            continue
        if '\x00' in value:
            findings.append({'path': rel, 'category': 'binary_content'})
            continue
        checked += 1
        for category, pattern in patterns.items():
            if pattern.search(value):
                findings.append({'path': rel, 'category': category})
        if any(term in value.casefold() for term in denied):
            findings.append({'path': rel, 'category': 'external_deny_term'})
    report = {'status': 'pass' if not findings else 'fail', 'files_checked': checked,
              'findings': findings, 'scope': 'candidate export only'}
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"{report['status']}: {checked} files checked, {len(findings)} findings")
        for finding in findings:
            print(f"{finding['path']}: {finding['category']}")
    return 0 if not findings else 1

if __name__ == '__main__':
    sys.exit(main())
