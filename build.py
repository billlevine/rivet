#!/usr/bin/env python3
"""Decision graph capture validator and store builder."""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
KINDS = {'mechanism', 'requirement', 'proposal', 'judgment', 'decision', 'evidence', 'implementation', 'review', 'invariant', 'gap', 'document'}
AUTHORITIES = {'reported_human', 'reported_agent', 'unspecified'}
STATES = {'provisional', 'accepted', 'rejected', 'observed', 'unknown'}
RELATIONS = {'supports', 'motivates', 'qualifies', 'supersedes', 'affirms', 'implements', 'applies_to', 'requires', 'maintains', 'would_overturn', 'depends_on_premise', 'part_of', 'deprecates'}
CONDITIONS = {'invariant', 'requirement'}
HOLDERS = {'mechanism', 'implementation', 'decision', 'invariant', 'requirement'}
OVERTURN_SOURCES = {'judgment', 'evidence'}
PREMISE_KINDS = {'judgment'}
CLAUSE_KINDS = {'requirement'}
DEPRECATORS = {'decision', 'requirement'}
ORDER_RE = re.compile('[0-9A-Za-z._-]{1,32}\\Z')
ID_RE = re.compile('r-[0-9a-f]{16}\\Z')
DATE_RE = re.compile('\\d{4}-\\d{2}-\\d{2}\\Z')
RECORD_KEYS = {'id', 'kind', 'topic', 'title', 'claim', 'actor', 'authority', 'state', 'recorded_at', 'sources'}
EDGE_KEYS = {'from', 'to', 'relation', 'note', 'sources'}
SPAN_KEYS = {'path', 'start_line', 'end_line'}
OPTIONAL_KEYS = {'operator_mark'}
EDGE_OPTIONAL_KEYS = OPTIONAL_KEYS | {'order'}
MARK_KEYS = {'why', 'by', 'at'}
MARK_WHY_MAX = 300
CLAIM_MAX = 900
CLAUSE_CLAIM_WARN = 4000

def fail(message):
    raise ValueError(message)

def exact_keys(value, keys, where, optional=frozenset()):
    if not isinstance(value, dict) or not keys <= set(value) <= keys | optional:
        fail(f'{where}: expected fields {sorted(keys)}' + (f' (optional {sorted(optional)})' if optional else ''))

def mark_problem(mark):
    if not isinstance(mark, dict) or set(mark) != MARK_KEYS:
        return f'operator_mark needs exactly {sorted(MARK_KEYS)}'
    if not isinstance(mark['why'], str) or not mark['why'].strip():
        return 'operator_mark.why is empty: a mark says why the operator placed it'
    if len(mark['why']) > MARK_WHY_MAX:
        return f'operator_mark.why exceeds {MARK_WHY_MAX} characters'
    if not isinstance(mark['by'], str) or not mark['by'].strip():
        return 'operator_mark.by is empty'
    at = mark['at']
    if not isinstance(at, str) or not DATE_RE.fullmatch(at):
        return 'operator_mark.at must be YYYY-MM-DD'
    try:
        __import__('datetime').date.fromisoformat(at)
    except ValueError:
        return 'operator_mark.at is not a calendar date'
    return None

def order_problem(edge, by_id):
    relation, has_order = (edge['relation'], 'order' in edge)
    if relation == 'part_of':
        if by_id[edge['from']]['kind'] not in CLAUSE_KINDS or by_id[edge['to']]['kind'] != 'document':
            return 'part_of must run from a requirement (a clause) to a document'
        if not has_order:
            return 'part_of needs an order key'
        if not isinstance(edge['order'], str) or not ORDER_RE.fullmatch(edge['order']):
            return 'order must be 1..32 characters of [0-9A-Za-z._-]'
    elif has_order:
        return 'only part_of carries an order key'
    if relation == 'deprecates' and (by_id[edge['from']]['kind'] not in DEPRECATORS or by_id[edge['to']]['kind'] not in CLAUSE_KINDS):
        return 'deprecates must run from a decision or a clause to a clause (requirement)'
    return None

def clause_ids(edges):
    return {edge['from'] for edge in edges if isinstance(edge, dict) and edge.get('relation') == 'part_of' and isinstance(edge.get('from'), str)}

def check_mark(value, where):
    if 'operator_mark' in value:
        problem = mark_problem(value['operator_mark'])
        if problem:
            fail(f'{where}: {problem}')

def nonempty(value, where, maximum=None):
    if not isinstance(value, str) or not value.strip():
        fail(f'{where}: expected nonempty string')
    if maximum is not None and len(value) > maximum:
        fail(f'{where}: exceeds {maximum} characters')

def source_path(root, name):
    nonempty(name, 'source path')
    relative = Path(name)
    if relative.is_absolute() or any((part in ('', '.', '..') for part in name.split('/'))) or '\\' in name:
        fail(f'unsafe source path: {name}')
    target = root / relative
    if any(((root / Path(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts) + 1))):
        fail(f'symlink in source path: {name}')
    if not target.is_file() or target.is_symlink() or target.resolve().is_relative_to(root.resolve()) is False:
        fail(f'missing or unsafe source file: {name}')
    return target

def physical_lines(data, name):
    try:
        value = data.decode('utf-8')
    except UnicodeDecodeError:
        fail(f'source is not UTF-8: {name}')
    parts = value.split('\n')
    lines = [part + '\n' for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--records', required=True, type=Path)
    parser.add_argument('--sources', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--summaries', type=Path, help='decision-summaries.v1 sidecar; stale entries are dropped, never regenerated here')
    parser.add_argument('--code', type=Path, action='append', help='code tree to scan for `rivet: r-…` (legacy `context:` also accepted) anchors (repeatable, e.g. src and tests); writes anchors.json')
    parser.add_argument('--derive-superseded', action='store_true', help='store state `superseded` on every target of a supersedes edge; the deriving edges go in metadata.json')
    args = parser.parse_args()
    root = args.sources.resolve()
    if not root.is_dir():
        fail('source packet directory does not exist')
    raw = json.loads(args.records.read_text(encoding='utf-8'))
    exact_keys(raw, {'schema', 'records', 'edges'}, 'capture')
    if raw['schema'] not in ('decision-records.v1', 'decision-records.v2'):
        fail('unsupported capture schema')
    if not isinstance(raw['records'], list) or not isinstance(raw['edges'], list):
        fail('records and edges must be lists')
    cache = {}
    coverage = {}
    STRUCTURAL = {'applies_to', 'implements', 'maintains', 'requires', 'part_of'}

    def attach(spans, where, inherit=None):
        if not isinstance(spans, list):
            fail(f'{where}: expected a sources list')
        if not spans:
            if inherit is None:
                fail(f'{where}: expected nonempty sources')
            return [dict(s, inherited_from=inherit[0]) for s in inherit[1]]
        result = []
        for index, span in enumerate(spans):
            exact_keys(span, SPAN_KEYS, f'{where}.sources[{index}]')
            name = span['path']
            if name.startswith('code/') and args.code:
                target = None
                for tree in args.code:
                    try:
                        target = source_path(tree.resolve(), name[5:])
                        break
                    except ValueError:
                        continue
                if target is None:
                    fail(f'{where}: code citation {name} not found under any --code tree')
            else:
                target = source_path(root, name)
            if name not in cache:
                data = target.read_bytes()
                lines = physical_lines(data, name)
                cache[name] = (hashlib.sha256(data).hexdigest(), lines)
            digest, lines = cache[name]
            start, end = (span['start_line'], span['end_line'])
            if type(start) is not int or type(end) is not int or start < 1 or (end < start) or (end > len(lines)):
                fail(f'{where}: invalid line span {name}:{start}-{end}; file has {len(lines)} lines')
            if not name.startswith('code/'):
                coverage.setdefault(name, set()).update(range(start, end + 1))
            result.append({'path': name, 'start_line': start, 'end_line': end, 'sha256': digest, 'excerpt': ''.join(lines[start - 1:end])})
        return result
    records = []
    ids = set()
    clauses = clause_ids(raw['edges'])
    for index, record in enumerate(raw['records']):
        where = f'records[{index}]'
        exact_keys(record, RECORD_KEYS, where, OPTIONAL_KEYS)
        check_mark(record, where)
        ident = record['id']
        if not isinstance(ident, str) or not ID_RE.fullmatch(ident) or ident in ids:
            fail(f'{where}: invalid or duplicate opaque ID')
        ids.add(ident)
        if not isinstance(record['kind'], str) or record['kind'] not in KINDS or (not isinstance(record['authority'], str)) or (record['authority'] not in AUTHORITIES) or (not isinstance(record['state'], str)) or (record['state'] not in STATES):
            fail(f'{where}: invalid kind, authority, or state')
        clause = record['kind'] in CLAUSE_KINDS and ident in clauses
        for field in ('topic', 'title', 'claim', 'actor'):
            nonempty(record[field], f'{where}.{field}', CLAIM_MAX if field == 'claim' and (not clause) else None)
        if clause and len(record['claim']) > CLAUSE_CLAIM_WARN:
            print(f"build: warning: {where}: clause claim is {len(record['claim'])} characters (over {CLAUSE_CLAIM_WARN}; no cap applies to a clause)", file=sys.stderr)
        date = record['recorded_at']
        if date != 'unknown':
            if not isinstance(date, str) or not DATE_RE.fullmatch(date):
                fail(f'{where}: invalid recorded_at')
            try:
                __import__('datetime').date.fromisoformat(date)
            except ValueError:
                fail(f'{where}: invalid calendar date')
        records.append({**record, 'sources': attach(record['sources'], where)})
    if [r['id'] for r in records] != sorted(ids):
        fail('records must be sorted by opaque ID')
    by_id = {record['id']: record for record in records}
    edges = []
    seen_edges = set()
    for index, edge in enumerate(raw['edges']):
        where = f'edges[{index}]'
        exact_keys(edge, EDGE_KEYS, where, EDGE_OPTIONAL_KEYS)
        check_mark(edge, where)
        if not isinstance(edge['from'], str) or not isinstance(edge['to'], str) or edge['from'] not in ids or (edge['to'] not in ids) or (edge['from'] == edge['to']):
            fail(f'{where}: invalid endpoint')
        if not isinstance(edge['relation'], str) or edge['relation'] not in RELATIONS:
            fail(f'{where}: unsupported relation')
        if edge['relation'] == 'applies_to' and by_id[edge['to']]['kind'] != 'mechanism':
            fail(f'{where}: applies_to must target a mechanism')
        if edge['relation'] == 'implements' and by_id[edge['from']]['kind'] != 'implementation':
            fail(f'{where}: implements must start at an implementation')
        if edge['relation'] == 'requires' and (by_id[edge['to']]['kind'] not in CONDITIONS or by_id[edge['from']]['kind'] not in HOLDERS):
            fail(f'{where}: requires must run from a mechanism, implementation, decision or condition to an invariant or requirement')
        if edge['relation'] == 'maintains' and (by_id[edge['to']]['kind'] not in CONDITIONS or by_id[edge['from']]['kind'] not in ('mechanism', 'implementation')):
            fail(f'{where}: maintains must run from a mechanism or implementation to an invariant or requirement')
        if edge['relation'] == 'would_overturn' and (by_id[edge['from']]['kind'] not in OVERTURN_SOURCES or by_id[edge['to']]['kind'] != 'decision'):
            fail(f'{where}: would_overturn must run from a judgment or evidence record to a decision')
        if edge['relation'] == 'depends_on_premise' and (by_id[edge['from']]['kind'] != 'decision' or by_id[edge['to']]['kind'] not in PREMISE_KINDS):
            fail(f'{where}: depends_on_premise must run from a decision to a judgment record')
        problem = order_problem(edge, by_id)
        if problem:
            fail(f'{where}: {problem}')
        nonempty(edge['note'], f'{where}.note', 300)
        key = (edge['from'], edge['to'], edge['relation'], edge['note'])
        if key in seen_edges:
            fail(f'{where}: duplicate edge')
        seen_edges.add(key)
        if 'operator_mark' in edge and edge['sources'] == []:
            edges.append({**edge, 'sources': []})
            continue
        inherit = (edge['from'], by_id[edge['from']]['sources']) if edge['relation'] in STRUCTURAL else None
        edges.append({**edge, 'sources': attach(edge['sources'], where, inherit)})
    edges.sort(key=lambda e: (e['from'], e['to'], e['relation'], e['note'], [(s['path'], s['start_line'], s['end_line']) for s in e['sources']]))
    derived = derive_superseded(by_id, edges) if args.derive_superseded else None
    store = {'schema': 'decision-store.v1', 'records': records, 'edges': edges}
    all_files = sorted((p for p in root.rglob('*') if p.is_file()))
    source_coverage = []
    for path in all_files:
        if path.is_symlink():
            fail(f'symlink in source packet: {path.relative_to(root)}')
        name = path.relative_to(root).as_posix()
        data = path.read_bytes()
        lines = physical_lines(data, name)
        source_coverage.append({'path': name, 'sha256': hashlib.sha256(data).hexdigest(), 'total_lines': len(lines), 'cited_lines': len(coverage.get(name, set()))})
    metadata = {'schema': 'decision-store-metadata.v1', 'record_count': len(records), 'edge_count': len(edges), 'source_coverage': source_coverage}
    if derived is not None:
        metadata['derived_states'] = {'rule': 'a record that is the target of a supersedes edge has state superseded; prior_state is the captured state, derived_from the edges', 'records': derived}
    args.out.mkdir(parents=True, exist_ok=True)
    canonical = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
    (args.out / 'store.json').write_text(canonical(store), encoding='utf-8')
    (args.out / 'metadata.json').write_text(canonical(metadata), encoding='utf-8')
    note_blocks = [canonical(item) for item in records + edges] + [canonical(metadata)]
    longest_tick_run = max((len(run) for block in note_blocks for run in re.findall('`+', block)), default=0)
    fence = '`' * max(3, longest_tick_run + 1)
    notes = ['# Decision records — full-fact flat view\n\n', 'Every JSON block below contains the same fields, source excerpts, and hashes as store.json.\n\n', '## Index\n\n', "One line per record: id, kind, date, state, authority, topic, title. Search this section first, then open the record's section below.\n\n"]
    for record in records:
        mark = f" | mark: {record['operator_mark']['why']}" if 'operator_mark' in record else ''
        notes.append(f"- {record['id']} | {record['kind']} | {record['recorded_at']} | {record['state']} | {record['authority']} | {record['topic']} | {record['title']}{mark}\n")
    notes.append('\n')
    for record in records:
        notes.extend((f"## Record {record['id']}\n\n", f'{fence}json\n', canonical(record), f'{fence}\n\n'))
    for index, edge in enumerate(edges):
        notes.extend((f"## Edge {index + 1}: {edge['from']} {edge['relation']} {edge['to']}\n\n", f'{fence}json\n', canonical(edge), f'{fence}\n\n'))
    notes.extend(('## Source coverage\n\n', f'{fence}json\n', canonical(metadata), f'{fence}\n'))
    (args.out / 'NOTES.md').write_text(''.join(notes), encoding='utf-8')
    report = {'records': len(records), 'edges': len(edges), 'sources': len(all_files), 'out': str(args.out)}
    if derived is not None:
        report['derived_superseded'] = len(derived)
    if args.summaries is not None:
        report['summaries'] = attach_summaries(args.summaries, records, args.out)
    if args.code:
        report['anchors'] = scan_anchors(args.code, ids, args.out)
    print(json.dumps(report, sort_keys=True))
SUMMARY_FIELDS = ('kind', 'topic', 'title', 'claim', 'actor', 'authority', 'state', 'recorded_at')
DERIVED_SUPERSEDED = 'superseded'

def derive_superseded(by_id, edges):
    deriving = {}
    for edge in edges:
        if edge['relation'] == 'supersedes':
            deriving.setdefault(edge['to'], []).append({'from': edge['from'], 'from_state': by_id[edge['from']]['state'], 'note': edge['note']})
    derived = []
    for ident in sorted(deriving):
        record = by_id[ident]
        derived.append({'id': ident, 'prior_state': record['state'], 'state': DERIVED_SUPERSEDED, 'derived_from': deriving[ident]})
    for item in derived:
        by_id[item['id']]['state'] = DERIVED_SUPERSEDED
    return derived
ANCHOR_LINE = re.compile(r'\b(?:rivet|context):\s*(.*)')
ANCHOR_ID = re.compile('\\br-[0-9a-f]{16}\\b')

def content_sha(record):
    return hashlib.sha256(json.dumps({k: record[k] for k in SUMMARY_FIELDS}, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()

def attach_summaries(path, records, out):
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('schema') != 'decision-summaries.v1':
        fail('unsupported summaries schema')
    by_id = {r['id']: r for r in records}
    kept, stale, unknown = ({}, 0, 0)
    for ident, entry in (data.get('records') or {}).items():
        record = by_id.get(ident)
        if record is None:
            unknown += 1
            continue
        if isinstance(entry, dict) and entry.get('content_sha256') == content_sha(record) and isinstance(entry.get('summary'), str) and entry['summary'].strip():
            kept[ident] = {'content_sha256': entry['content_sha256'], 'summary': ' '.join(entry['summary'].split())}
        else:
            stale += 1
    topics = {}
    for topic, entry in (data.get('topics') or {}).items():
        if isinstance(entry, dict) and isinstance(entry.get('summary'), str) and entry['summary'].strip():
            topics[topic] = {'summary': ' '.join(entry['summary'].split())}
    sidecar = {'schema': 'decision-summaries.v1', 'model': data.get('model', 'unknown'), 'records': dict(sorted(kept.items())), 'topics': dict(sorted(topics.items()))}
    (out / 'summaries.json').write_text(json.dumps(sidecar, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    return {'kept': len(kept), 'stale_dropped': stale, 'unknown_dropped': unknown, 'topics': len(topics), 'missing': len(by_id) - len(kept)}

def scan_anchors(trees, ids, out):
    anchors, unknown, scanned = ({}, {}, 0)
    cwd = Path.cwd().resolve()
    for code in trees:
        root = code.resolve()
        if not root.is_dir():
            fail(f'code directory does not exist: {code}')
        for path in sorted((p for p in root.rglob('*') if p.is_file())):
            if path.is_symlink():
                continue
            try:
                text = path.read_text(encoding='utf-8')
            except (OSError, UnicodeDecodeError):
                continue
            scanned += 1
            try:
                rel = path.resolve().relative_to(cwd).as_posix()
            except ValueError:
                rel = path.relative_to(root).as_posix()
            for number, line in enumerate(text.splitlines(), 1):
                m = ANCHOR_LINE.search(line)
                for ident in ANCHOR_ID.findall(m.group(1)) if m else []:
                    (anchors if ident in ids else unknown).setdefault(ident, []).append(f'{rel}:{number}')
    data = {'schema': 'decision-anchors.v1', 'files_scanned': scanned, 'anchors': dict(sorted(anchors.items())), 'unknown_ids': dict(sorted(unknown.items()))}
    (out / 'anchors.json').write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    return {'files_scanned': scanned, 'anchored_records': len(anchors), 'unknown_ids': len(unknown)}
if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f'build: {exc}')
