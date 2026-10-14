#!/usr/bin/env python3
"""Deterministic synthetic engineering regression benchmark."""
import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / 'examples' / 'synthetic'

def invoke(script, *args):
    start = time.perf_counter()
    result = subprocess.run([sys.executable, str(ROOT / script), *map(str, args)], text=True,
                            capture_output=True, check=False)
    elapsed = round((time.perf_counter() - start) * 1000, 2)
    if result.returncode:
        raise RuntimeError(f'{script} exited {result.returncode}: {result.stderr[:500]}')
    return json.loads(result.stdout), elapsed

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true', help='print full JSON report')
    args = parser.parse_args()
    ids = json.loads((EXAMPLE / 'ids.json').read_text())
    cases = []
    def check(name, passed, detail, ms):
        cases.append({'case': name, 'pass': bool(passed), 'detail': detail, 'elapsed_ms': ms})
    with tempfile.TemporaryDirectory(prefix='dg-bench-') as temp:
        arm = Path(temp) / 'store'
        built, ms = invoke('build.py', '--records', EXAMPLE / 'records.json', '--sources', EXAMPLE / 'sources',
                           '--out', arm, '--derive-superseded')
        check('capture_build', built.get('records') == 9 and built.get('edges') == 6, '9 records, 6 edges', ms)
        view, ms = invoke('dg.py', '--root', arm, 'context', 'audit log previews', '--budget', 1000)
        found = [row['id'] for row in view.get('records', [])]
        check('distractor_ranking', bool(found) and found[0] == ids['audit-distractor'],
              'specific audit record ranks first among lexical distractors', ms)
        view, ms = invoke('dg.py', '--root', arm, 'current', ids['cache-old'], '--budget', 3000)
        check('supersession', view.get('current_id') == ids['cache-new'] and
              [row['id'] for row in view.get('chain', [])] == [ids['cache-old'], ids['cache-new']],
              'old decision resolves to revised decision', ms)
        view, ms = invoke('dg.py', '--root', arm, 'impact', '--entry', ids['cache-new'])
        considerations = view.get('seeds', [{}])[0].get('considerations', [])
        got = {(row['id'], row['relation']) for row in considerations}
        check('premise_and_overturn', got == {(ids['cache-premise'], 'depends_on_premise'),
                                              (ids['cache-overturn'], 'would_overturn')},
              'both recorded considerations are returned', ms)
        old, ms_old = invoke('dg.py', '--root', arm, 'asof', '--doc', 'Preview Cache Policy', '--date', '2025-01-15')
        new, ms_new = invoke('dg.py', '--root', arm, 'asof', '--doc', 'Preview Cache Policy', '--date', '2025-02-15')
        check('dated_clauses', [r['id'] for r in old.get('clauses', [])] == [ids['clause-old']] and
              [r['id'] for r in new.get('clauses', [])] == [ids['clause-new']],
              'clause changes across the recorded date', ms_old + ms_new)
        view, ms = invoke('recall.py', '--store', arm, '--seed', ids['cache-new'], '--profile', 'reasoning',
                          '--depth', 1, '--budget', 4000)
        found = {row['card']['id'] for row in view.get('records', [])}
        check('reasoning_profile', view.get('complete') and
              {ids['cache-new'], ids['cache-premise'], ids['cache-overturn']} <= found,
              'decision, premise, and overturning evidence are visible', ms)
        view, ms = invoke('recall.py', '--store', arm, '--seed', ids['cache-old'], '--profile', 'review',
                          '--depth', 0, '--budget', 2500)
        warnings = {(edge['from'], edge['relation']) for edge in view.get('warnings', [])}
        check('review_warning_closure', (ids['cache-new'], 'supersedes') in warnings and
              view.get('counts', {}).get('depth_limited', 0) > 0 and not view.get('complete'),
              'correction remains visible when depth is zero; omission is explicit', ms)
        view, ms = invoke('recall.py', '--store', arm, '--query', 'audit log previews', '--profile',
                          'modification', '--depth', 0, '--limit', 1, '--budget', 2000)
        check('modification_query_distractor', view.get('roots') == [ids['audit-distractor']] and
              view.get('counts', {}).get('query_limited', 0) > 0,
              'specific lexical hit is selected and other hits are counted', ms)
        view, ms = invoke('rivet.py', '--store', arm, 'context', ids['cache-old'],
                          '--format', 'json', '--budget', 12000)
        check('combined_current_context', view['tips'][ids['cache-old']]['recorded_tips'] == [ids['cache-new']]
              and view['records'][0]['id'] == ids['cache-new']
              and {ids['cache-old'], ids['cache-new']} <= {r['id'] for r in view['records']},
              'one exact-ID call preserves history and puts the current claim first', ms)
        source_id = next(iter(view['sources']))
        evidence, ms = invoke('rivet.py', '--store', arm, 'evidence', source_id, '--format', 'json')
        source_lines = (EXAMPLE / 'sources' / evidence['path']).read_text().splitlines(keepends=True)
        check('captured_source_lookup', evidence['status'] == 'captured' and
              evidence['excerpt'] == ''.join(source_lines[evidence['start_line']-1:evidence['end_line']]),
              'source ID resolves to exact captured bytes without opening a citation path', ms)
        found, ms = invoke('rivet.py', '--store', arm, 'find-evidence', 'memory pressure', '--format', 'json')
        matches = found.get('matches', [])
        expected_excerpt = (EXAMPLE / 'sources' / 'notes.md').read_text().splitlines(keepends=True)[3]
        check('named_captured_evidence', found.get('total_matches') == 1 and
              found.get('captured_text_fully_searchable') and len(matches) == 1 and
              matches[0]['excerpt'] == expected_excerpt and
              ids['cache-overturn'] in {r['id'] for r in matches[0]['record_refs']},
              'literal receipt search returns exact captured span and its owning record', ms)
    report = {'suite': 'synthetic engineering regression', 'scope': 'invented examples only',
              'passed': sum(c['pass'] for c in cases), 'total': len(cases), 'cases': cases}
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print('| Case | Result | Milliseconds |')
        print('| --- | --- | ---: |')
        for case in cases:
            print(f"| {case['case']} | {'PASS' if case['pass'] else 'FAIL'} | {case['elapsed_ms']} |")
        print(f"\n{report['passed']}/{report['total']} passed")
    return 0 if report['passed'] == report['total'] else 1

if __name__ == '__main__':
    sys.exit(main())
