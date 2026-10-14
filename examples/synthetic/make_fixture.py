#!/usr/bin/env python3
"""Write a small, entirely invented capture and source packet."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LINES = [
    ('cache-old', 'decision', 'cache', 'Disk cache for previews', 'Use a local disk cache for thumbnail previews.', '2025-01-01'),
    ('cache-new', 'decision', 'cache', 'Bounded memory cache for previews', 'Use a bounded memory cache for thumbnail previews.', '2025-02-01'),
    ('cache-premise', 'judgment', 'cache', 'Preview memory assumption', 'Thumbnail previews fit within the memory budget under normal load.', '2025-02-01'),
    ('cache-overturn', 'evidence', 'cache', 'Preview memory pressure', 'Sustained preview traffic can raise memory pressure above the budget.', '2025-03-01'),
    ('audit-distractor', 'decision', 'audit', 'Memory cache for audit log previews', 'Use a memory cache for audit log previews in the test dashboard.', '2025-02-05'),
    ('doc', 'document', 'cache', 'Preview Cache Policy', 'Preview Cache Policy lists the current thumbnail cache rule.', '2025-01-01'),
    ('clause-old', 'requirement', 'cache', 'Initial cache clause', 'Thumbnail previews must use a local disk cache.', '2025-01-01'),
    ('clause-new', 'requirement', 'cache', 'Revised cache clause', 'Thumbnail previews must use a bounded memory cache.', '2025-02-01'),
    ('logging-distractor', 'proposal', 'logging', 'Bounded log memory', 'Consider a bounded memory buffer for debug logs.', '2025-02-02'),
]

def ident(label):
    return 'r-' + hashlib.sha256(label.encode()).hexdigest()[:16]

def main():
    source = ROOT / 'sources' / 'notes.md'
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(''.join(f'{title}: {claim}\n' for _, _, _, title, claim, _ in LINES), encoding='utf-8')
    by_label = {}
    for n, (label, kind, topic, title, claim, date) in enumerate(LINES, 1):
        by_label[label] = {'id': ident(label), 'kind': kind, 'topic': topic, 'title': title,
                           'claim': claim, 'actor': 'example-author', 'authority': 'unspecified',
                           'state': 'accepted' if kind in ('decision', 'requirement') else 'observed',
                           'recorded_at': date,
                           'sources': [{'path': 'notes.md', 'start_line': n, 'end_line': n}]}
    def edge(frm, to, relation, note, order=None):
        row = {'from': ident(frm), 'to': ident(to), 'relation': relation, 'note': note,
               'sources': by_label[frm]['sources']}
        if order is not None:
            row['order'] = order
        return row
    edges = [
        edge('cache-new', 'cache-old', 'supersedes', 'The bounded memory rule replaces the disk rule.'),
        edge('cache-new', 'cache-premise', 'depends_on_premise', 'Revisit the choice if previews exceed the memory budget.'),
        edge('cache-overturn', 'cache-new', 'would_overturn', 'Sustained memory pressure would reverse the choice.'),
        edge('clause-old', 'doc', 'part_of', 'Initial policy clause.', '010'),
        edge('clause-new', 'doc', 'part_of', 'Revised policy clause.', '010'),
        edge('clause-new', 'clause-old', 'supersedes', 'The revised policy clause replaces the initial clause.'),
    ]
    edges.sort(key=lambda e: (e['from'], e['to'], e['relation'], e['note']))
    capture = {'schema': 'decision-records.v1', 'records': sorted(by_label.values(), key=lambda r: r['id']), 'edges': edges}
    (ROOT / 'records.json').write_text(json.dumps(capture, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    (ROOT / 'ids.json').write_text(json.dumps({label: ident(label) for label, *_ in LINES}, indent=2, sort_keys=True) + '\n', encoding='utf-8')

if __name__ == '__main__':
    main()
