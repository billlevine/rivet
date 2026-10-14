#!/usr/bin/env python3
"""Decision graph capture writer."""

import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build
KINDS, AUTHORITIES, STATES, RELATIONS = (build.KINDS, build.AUTHORITIES, build.STATES, build.RELATIONS)
CONDITIONS, HOLDERS = (build.CONDITIONS, build.HOLDERS)
OVERTURN_SOURCES, PREMISE_KINDS = (build.OVERTURN_SOURCES, build.PREMISE_KINDS)
EDGE_OPTIONAL_KEYS = build.EDGE_OPTIONAL_KEYS
ID_RE, DATE_RE = (build.ID_RE, build.DATE_RE)
RECORD_KEYS, EDGE_KEYS, SPAN_KEYS = (build.RECORD_KEYS, build.EDGE_KEYS, build.SPAN_KEYS)
OPTIONAL_KEYS = build.OPTIONAL_KEYS
STRUCTURAL = {'applies_to', 'implements', 'maintains', 'requires', 'part_of'}
RECORD_ORDER = ('id', 'kind', 'topic', 'title', 'claim', 'actor', 'authority', 'state', 'recorded_at', 'sources', 'operator_mark')
EDGE_ORDER = ('from', 'to', 'relation', 'note', 'order', 'sources', 'operator_mark')
SCHEMAS = ('decision-records.v1', 'decision-records.v2')
ARM_FILES = {'store.json', 'metadata.json', 'NOTES.md', 'summaries.json', 'anchors.json', 'policy.json'}
MASKED = re.compile('\\[[A-Z_]{3,}\\]')
STOP = frozenset('a an and are as at be by for from has have how in into is it of on or that the their these this to was were what when where which who why with not no'.split())
RANDOM_LABEL = re.compile('(?:r-)?[0-9a-f-]{16,}\\Z')
SIMILARITY = 0.6
SUPPORT_WARN = 0.2
CLAUSE_KINDS = build.CLAUSE_KINDS

def derive_id(label):
    return 'r-' + hashlib.sha256(label.encode('utf-8')).hexdigest()[:16]

def stem(word):
    for suffix in ('ing', 'ed', 's'):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[:-len(suffix)]
    return word

def content_words(text, minimum=3):
    return {stem(w) for w in re.findall('[a-z0-9]+', text.lower()) if len(w) >= minimum and w not in STOP}

def title_tokens(title):
    return content_words(title, minimum=2)

def jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0

def is_clause(record, clauses):
    return isinstance(record, dict) and isinstance(record.get('id'), str) and (record['id'] in clauses)

def parse_span(text):
    path, _, lines = text.rpartition(':')
    start, _, end = lines.partition('-')
    try:
        return {'path': path, 'start_line': int(start), 'end_line': int(end or start)}
    except ValueError:
        return {'path': text, 'start_line': None, 'end_line': None}

def normalise_spans(value):
    if isinstance(value, list):
        return [parse_span(s) if isinstance(s, str) else s for s in value]
    return value

def arm_reason(path):
    path = Path(path).resolve()
    if path.name in ARM_FILES:
        return f'{path.name} is a built-store file'
    for parent in path.parents:
        if (parent / 'store.json').exists() and (parent / 'metadata.json').exists():
            return f'{parent} is an arm directory'
    return None

def outcome(kind, obj, ref, cause=None, detail=None):
    row = {'outcome': kind, 'object': obj, 'ref': ref}
    if cause:
        row['cause'] = cause
    if detail is not None:
        row['detail'] = detail
    return row

def render(row, as_json):
    if as_json:
        return json.dumps(row, ensure_ascii=False, sort_keys=True)
    text = f"{row['outcome']} {row['object']} {row['ref']}"
    if row.get('cause'):
        text += f" {row['cause']}"
    if row.get('detail') is not None:
        detail = row['detail'] if isinstance(row['detail'], str) else json.dumps(row['detail'], ensure_ascii=False)
        text += f': {detail}'
    return text

def mark_cause(mark):
    problem = build.mark_problem(mark)
    if problem is None:
        return None
    return ('empty_why' if 'why is empty' in problem else 'bad_mark', problem)

def operator_mark(args):
    return {'why': args.why if args.why is not None else '', 'by': args.by or 'operator', 'at': args.at or datetime.date.today().isoformat()}

def edge_ref(edge):
    if isinstance(edge, dict):
        return f"{edge.get('from')} {edge.get('relation')} {edge.get('to')}"
    return '?'

class Graph:

    def __init__(self, data, sources, code=None, code_check=True, similarity=SIMILARITY):
        self.schema = data['schema']
        self.records = {}
        self.edges = []
        self.edge_index = {}
        self.triples = {}
        self.supersedes = {}
        self.titles = {}
        self.sources = Path(sources).resolve() if sources else None
        self.code = [Path(c) for c in code or []]
        self.code_check = code_check
        self.similarity = similarity
        self.lines = {}
        self.alias = {}
        self.clauses = set()
        self.flat = {}

    def resolve(self, name):
        if name.startswith('code/') and self.code:
            for tree in self.code:
                try:
                    return (build.source_path(tree.resolve(), name[5:]), None, None)
                except ValueError:
                    continue
            return (None, 'code_citation_missing', f'{name} not under any --code tree')
        if self.sources is None:
            return (None, 'not_checked', 'no --sources directory given')
        try:
            return (build.source_path(self.sources, name), None, None)
        except ValueError as exc:
            message = str(exc)
            if message.startswith('unsafe') or message.startswith('symlink'):
                return (None, 'unsafe_path', message)
            return (None, 'missing_source', message)

    def file_lines(self, name, target):
        if name not in self.lines:
            self.lines[name] = build.physical_lines(target.read_bytes(), name)
        return self.lines[name]

    def spans(self, spans, allow_empty):
        hard, warnings, excerpt = ([], [], [])
        if not isinstance(spans, list):
            return ([('bad_span', 'sources must be a list')], warnings, '')
        if not spans:
            return ([] if allow_empty else [('no_sources', 'sources is empty and this type may not inherit spans')], warnings, '')
        for index, span in enumerate(spans):
            if not isinstance(span, dict) or set(span) != SPAN_KEYS:
                hard.append(('bad_span', f'sources[{index}] needs exactly {sorted(SPAN_KEYS)}'))
                continue
            name, start, end = (span['path'], span['start_line'], span['end_line'])
            if not isinstance(name, str) or not name.strip():
                hard.append(('bad_span', f'sources[{index}] path is empty'))
                continue
            if type(start) is not int or type(end) is not int or start < 1 or (end < start):
                hard.append(('bad_span', f'{name}:{start}-{end}'))
                continue
            if name.startswith('code/') and (not self.code_check):
                warnings.append(('code_not_checked', f'{name}:{start}-{end}'))
                continue
            target, cause, detail = self.resolve(name)
            if cause:
                (warnings if cause == 'not_checked' else hard).append((cause, detail))
                continue
            try:
                lines = self.file_lines(name, target)
            except ValueError as exc:
                hard.append(('unreadable_source', str(exc)))
                continue
            if end > len(lines):
                hard.append(('span_out_of_range', f'{name}:{start}-{end}; file has {len(lines)} lines'))
                continue
            excerpt.append(''.join(lines[start - 1:end]))
        return (hard, warnings, ''.join(excerpt))

    def support(self, text, excerpt, spans, held, warnings, what):
        words = content_words(text)
        if len(words) < 3 or not excerpt:
            return
        share = len(words & content_words(excerpt)) / len(words)
        if share >= SUPPORT_WARN:
            return
        detail = {'share': round(share, 2), 'of': f"the {what}'s content words found in the cited lines"}
        better = self.nearby(spans, words, share)
        if better:
            detail['nearby'] = better
        held.append(('unsupported_span', detail)) if share == 0 else warnings.append(('weak_span_support', detail))

    def still_present(self, claim, spans):
        if not isinstance(claim, str) or not claim.split() or (not isinstance(spans, list)):
            return None
        wanted = ' '.join(claim.split())
        for span in spans:
            name = span.get('path') if isinstance(span, dict) else None
            if not isinstance(name, str) or name.startswith('code/'):
                continue
            if name not in self.flat:
                target, cause, _ = self.resolve(name)
                try:
                    self.flat[name] = None if cause else ' '.join(''.join(self.file_lines(name, target)).split())
                except (ValueError, OSError):
                    self.flat[name] = None
            if self.flat[name] is not None and wanted in self.flat[name]:
                return name
        return None

    def nearby(self, spans, words, share, reach=10):
        if len(spans) != 1 or spans[0]['path'] not in self.lines:
            return None
        span = spans[0]
        lines, width = (self.lines[span['path']], span['end_line'] - span['start_line'])
        best = None
        for shift in sorted(range(-reach, reach + 1), key=abs):
            start = span['start_line'] + shift
            if shift == 0 or start < 1 or start + width > len(lines):
                continue
            value = len(words & content_words(''.join(lines[start - 1:start + width]))) / len(words)
            if value >= max(2 * share, SUPPORT_WARN) and (best is None or value > best[0]):
                best = (value, f"{span['path']}:{start}-{start + width}")
        return {'window': best[1], 'share': round(best[0], 2)} if best else None

    def validate_record(self, record, label=None, imported=False, merge_identical=True, clause=False):
        hard, held, warnings = ([], [], [])
        if not isinstance(record, dict):
            return ([('fields', 'a record must be a JSON object')], held, warnings, None)
        ident = record.get('id')
        unlabelled = ident is None and (not imported)
        missing, extra = (sorted(RECORD_KEYS - set(record) - ({'id'} if unlabelled else set())), sorted(set(record) - RECORD_KEYS - OPTIONAL_KEYS))
        if missing or extra:
            hard.append(('fields', {'missing': missing, 'unknown': extra}))
        if 'operator_mark' in record:
            if not imported:
                hard.append(('mark_via_add', 'a mark is an explicit operator act: use `dg_write.py mark ID --why`'))
            elif mark_cause(record['operator_mark']):
                hard.append(mark_cause(record['operator_mark']))
        if unlabelled:
            hard.append(('id_without_label', 'give --label; the id is derived from it, never chosen'))
        elif not isinstance(ident, str) or not ID_RE.fullmatch(ident):
            hard.append(('bad_id', f'{ident!r} is not r- plus 16 lowercase hex'))
        elif not imported:
            if not isinstance(label, str) or not label.strip():
                hard.append(('id_without_label', 'give --label; the id is derived from it, never chosen'))
            elif RANDOM_LABEL.fullmatch(label.rpartition(':')[2].strip().lower()):
                hard.append(('random_label', f'{label!r} looks random; use a short stable phrase'))
            elif derive_id(label) != ident:
                hard.append(('id_not_derived', f'{ident} != {derive_id(label)} = derive({label!r})'))
        if isinstance(ident, str) and ident in self.records:
            existing = self.records[ident]
            if merge_identical and canonical_record(record) == existing:
                return ([], [], [], ident)
            hard.append(('duplicate_id', 'an existing record has this id' + ('' if merge_identical else ' (build.py refuses any repeat)')))
        for field, values, cause in (('kind', KINDS, 'bad_kind'), ('authority', AUTHORITIES, 'bad_authority'), ('state', STATES, 'bad_state')):
            if field in record and (not isinstance(record[field], str) or record[field] not in values):
                hard.append((cause, f'{record[field]!r} not in {sorted(values)}'))
        for field in ('topic', 'title', 'claim', 'actor'):
            if field in record and (not isinstance(record[field], str) or not record[field].strip()):
                hard.append(('empty_field', field))
        claim = record.get('claim')
        uncapped = clause and record.get('kind') in CLAUSE_KINDS
        if isinstance(claim, str) and len(claim) > build.CLAIM_MAX and (not uncapped):
            hard.append(('claim_too_long', f'{len(claim)} characters; split it'))
        elif isinstance(claim, str) and uncapped and (len(claim) > build.CLAUSE_CLAIM_WARN):
            warnings.append(('clause_claim_long', f'{len(claim)} characters; a clause has no cap, but over {build.CLAUSE_CLAIM_WARN} is rarely one decision point'))
        for field in ('title', 'claim'):
            if isinstance(record.get(field), str) and MASKED.search(record[field]):
                hard.append(('masked_token', f'{field} quotes {MASKED.search(record[field]).group(0)}'))
        if 'recorded_at' in record:
            date = record['recorded_at']
            if date != 'unknown':
                valid = isinstance(date, str) and DATE_RE.fullmatch(date)
                if valid:
                    try:
                        datetime.date.fromisoformat(date)
                    except ValueError:
                        valid = False
                if not valid:
                    hard.append(('bad_date', f'{date!r}'))
        excerpt = ''
        if 'sources' in record:
            span_hard, span_warn, excerpt = self.spans(record['sources'], allow_empty=False)
            hard += span_hard
            warnings += span_warn
        if isinstance(record.get('title'), str) and len(record['title']) > 60:
            warnings.append(('title_over_60', f"{len(record['title'])} characters"))
        if not hard:
            self.support(f"{record['title']} {record['claim']}", excerpt, record['sources'], held, warnings, 'claim')
            tokens = title_tokens(record['title'])
            best = max(((jaccard(tokens, other), rid) for rid, other in self.titles.items()), default=(0.0, None))
            if best[1] is not None and best[0] >= self.similarity:
                held.append(('near_duplicate_title', {'similar_to': best[1], 'jaccard': round(best[0], 2), 'kind': self.records[best[1]]['kind'], 'title': self.records[best[1]]['title']}))
        return (hard, held, warnings, None)

    def insert_record(self, record):
        record = canonical_record(record)
        self.records[record['id']] = record
        self.titles[record['id']] = title_tokens(record['title'])

    def validate_edge(self, edge, merge_identical=True, pending=None):
        hard, held, warnings = ([], [], [])
        records = self.records if not pending else {**self.records, pending['id']: pending}
        if not isinstance(edge, dict):
            return ([('fields', 'an edge must be a JSON object')], held, warnings, False)
        missing, extra = (sorted(EDGE_KEYS - set(edge)), sorted(set(edge) - EDGE_KEYS - EDGE_OPTIONAL_KEYS))
        if missing or extra:
            hard.append(('fields', {'missing': missing, 'unknown': extra}))
            return (hard, held, warnings, False)
        marked = 'operator_mark' in edge
        if marked and mark_cause(edge['operator_mark']):
            hard.append(mark_cause(edge['operator_mark']))
        source, target, relation, note = (edge['from'], edge['to'], edge['relation'], edge['note'])
        ends = [e for e in (source, target) if not isinstance(e, str) or e not in records]
        if ends:
            hard.append(('dangling_end', {'missing': [str(e) for e in ends]}))
        elif source == target:
            hard.append(('self_loop', source))
        if not isinstance(relation, str) or relation not in RELATIONS:
            hard.append(('unknown_relation', f'{relation!r} not in {sorted(RELATIONS)}'))
        elif not ends and source != target:
            fk, tk = (records[source]['kind'], records[target]['kind'])
            if relation == 'applies_to' and tk != 'mechanism':
                hard.append(('applies_to_target', f'target is a {tk}, not a mechanism'))
            if relation == 'implements' and fk != 'implementation':
                hard.append(('implements_source', f'source is a {fk}, not an implementation'))
            if relation in ('requires', 'maintains') and tk not in CONDITIONS:
                hard.append(('condition_target', f'{relation} must target an invariant or requirement, not a {tk}'))
            if relation == 'requires' and fk not in HOLDERS:
                hard.append(('holder_source', f'requires cannot start at a {fk}'))
            if relation == 'maintains' and fk not in ('mechanism', 'implementation'):
                hard.append(('holder_source', f'maintains must start at a mechanism or implementation, not a {fk}'))
            if relation == 'would_overturn' and (fk not in OVERTURN_SOURCES or tk != 'decision'):
                hard.append(('overturn_ends', f'would_overturn runs from a judgment or evidence record to a decision, not from a {fk} to a {tk}'))
            if relation == 'depends_on_premise' and (fk != 'decision' or tk not in PREMISE_KINDS):
                hard.append(('premise_ends', f'depends_on_premise runs from a decision to a judgment record, not from a {fk} to a {tk}'))
            if relation in ('part_of', 'deprecates') or 'order' in edge:
                problem = build.order_problem(edge, records)
                if problem:
                    hard.append(('part_of_order' if 'order' in problem else 'clause_ends', problem))
            if relation == 'supersedes' and self.reaches(target, source):
                hard.append(('supersedes_cycle', f'{target} already supersedes {source}, directly or through others'))
            if relation == 'supersedes':
                fd, td = (records[source]['recorded_at'], records[target]['recorded_at'])
                if 'unknown' not in (fd, td) and fd < td:
                    warnings.append(('supersedes_earlier', f'{source} ({fd}) supersedes a later record ({td})'))
            if relation == 'supersedes' and target in self.clauses and (tk in CLAUSE_KINDS):
                standing = self.still_present(records[target].get('claim'), records[source].get('sources'))
                subsumed = ' '.join(str(records[target].get('claim', '')).split()) in ' '.join(str(records[source].get('claim', '')).split())
                if standing is not None and (not subsumed):
                    warnings.append(('superseded_text_stands', {'in': standing, 'hint': 'earlier text still stands in the cited document; consider qualifies'}))
        if not isinstance(note, str) or not note.strip():
            hard.append(('empty_note', 'note says why the edge is warranted'))
        elif len(note) > 300:
            hard.append(('note_too_long', f'{len(note)} characters; 300 at most'))
        elif MASKED.search(note):
            hard.append(('masked_token', f'note quotes {MASKED.search(note).group(0)}'))
        span_hard, span_warn, excerpt = self.spans(edge['sources'], allow_empty=marked or (isinstance(relation, str) and relation in STRUCTURAL))
        hard += span_hard
        warnings += span_warn
        key = (source, target, relation, note)
        if not hard and key in self.edge_index:
            if merge_identical and canonical_edge(edge) == self.edge_index[key]:
                return ([], [], [], True)
            hard.append(('duplicate_edge', 'same from, to, type and note' + ('' if merge_identical else ' (build.py refuses any repeat)')))
        if not hard:
            if (source, target, relation) in self.triples:
                held.append(('duplicate_edge_triple', {'existing_notes': self.triples[source, target, relation]}))
            if excerpt:
                self.support(note, excerpt, normalise_spans(edge['sources']), held, warnings, 'note')
        return (hard, held, warnings, False)

    def reaches(self, start, goal):
        seen, stack = (set(), [start])
        while stack:
            node = stack.pop()
            if node == goal:
                return True
            if node not in seen:
                seen.add(node)
                stack.extend(self.supersedes.get(node, ()))
        return False

    def insert_edge(self, edge):
        edge = canonical_edge(edge)
        self.edges.append(edge)
        self.edge_index[edge['from'], edge['to'], edge['relation'], edge['note']] = edge
        self.triples.setdefault((edge['from'], edge['to'], edge['relation']), []).append(edge['note'])
        if edge['relation'] == 'supersedes':
            self.supersedes.setdefault(edge['from'], set()).add(edge['to'])
        if edge['relation'] == 'part_of':
            self.clauses.add(edge['from'])

    def data(self):
        return {'schema': self.schema, 'records': [self.records[i] for i in sorted(self.records)], 'edges': sorted(self.edges, key=lambda e: (e['from'], e['to'], e['relation'], e['note']))}

def canonical_record(record):
    return {key: normalise_spans(record[key]) if key == 'sources' else record[key] for key in RECORD_ORDER if key in record}

def canonical_edge(edge):
    return {key: normalise_spans(edge[key]) if key == 'sources' else edge[key] for key in EDGE_ORDER if key in edge}

class Refusal(Exception):

    def __init__(self, row, code=1):
        super().__init__(row.get('detail'))
        self.row, self.code = (row, code)

def load(path, create=False):
    path = Path(path)
    reason = arm_reason(path)
    if reason:
        raise Refusal(outcome('refused', 'file', str(path), 'arm_directory', reason))
    if not path.exists():
        if create:
            return ({'schema': 'decision-records.v2', 'records': [], 'edges': []}, True)
        raise Refusal(outcome('refused', 'file', str(path), 'missing_file', 'no such records file'), 2)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise Refusal(outcome('refused', 'file', str(path), 'unreadable', str(exc)), 2)
    if not isinstance(data, dict) or set(data) != {'schema', 'records', 'edges'}:
        raise Refusal(outcome('refused', 'file', str(path), 'fields', 'a capture has exactly schema, records, edges'))
    if isinstance(data.get('schema'), str) and data['schema'].startswith('decision-store'):
        raise Refusal(outcome('refused', 'file', str(path), 'arm_directory', 'this is a built store, not a capture'))
    if data['schema'] not in SCHEMAS:
        raise Refusal(outcome('refused', 'file', str(path), 'bad_schema', f"{data['schema']!r} not in {list(SCHEMAS)}"))
    if not isinstance(data['records'], list) or not isinstance(data['edges'], list):
        raise Refusal(outcome('refused', 'file', str(path), 'fields', 'records and edges must be lists'))
    return (data, False)

def graph_from(data, args):
    graph = Graph(data, args.sources, args.code, not args.no_code_check, args.similarity)
    for record in data['records']:
        if isinstance(record, dict) and isinstance(record.get('id'), str):
            graph.insert_record(record)
    for edge in data['edges']:
        if isinstance(edge, dict) and EDGE_KEYS <= set(edge) <= EDGE_KEYS | EDGE_OPTIONAL_KEYS:
            graph.insert_edge(edge)
    return graph

def save(graph, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(graph.data(), indent=1, ensure_ascii=False) + '\n'
    handle, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=str(path.parent))
    with os.fdopen(handle, 'w', encoding='utf-8') as stream:
        stream.write(payload)
    os.replace(temporary, path)

def emit(rows, as_json):
    for row in rows:
        sys.stdout.write(render(row, as_json) + '\n')

def judge(obj, ref, hard, held, warnings, force):
    rows = [outcome('refused', obj, ref, c, d) for c, d in hard]
    if hard:
        return (rows, False)
    if held and (not force):
        return ([outcome('held', obj, ref, c, d) for c, d in held] + [outcome('warning', obj, ref, c, d) for c, d in warnings], False)
    rows = [outcome('accepted', obj, ref)]
    rows += [outcome('warning', obj, ref, 'forced:' + c, d) for c, d in held]
    rows += [outcome('warning', obj, ref, c, d) for c, d in warnings]
    return (rows, True)

def record_input(args):
    if args.stdin:
        try:
            value = json.loads(sys.stdin.read())
        except ValueError as exc:
            raise Refusal(outcome('refused', 'record', '?', 'fields', f'stdin is not JSON: {exc}'))
        if not isinstance(value, dict):
            raise Refusal(outcome('refused', 'record', '?', 'fields', 'stdin must hold one JSON object'))
        label = value.pop('label', None) or args.label
        if 'notes' in value and 'claim' not in value:
            value['claim'] = value.pop('notes')
        if 'date' in value and 'recorded_at' not in value:
            value['recorded_at'] = value.pop('date')
        given = value.get('id')
        if given is None and isinstance(label, str):
            value['id'] = derive_id(label)
        value['sources'] = normalise_spans(value.get('sources'))
        return (value, label)
    record = {'kind': args.kind, 'topic': args.topic, 'title': args.title, 'claim': args.claim, 'actor': args.actor, 'authority': args.authority, 'state': args.state, 'recorded_at': args.date, 'sources': [parse_span(s) for s in args.source]}
    record = {k: v for k, v in record.items() if v is not None}
    if args.label:
        record = {'id': derive_id(args.label), **record}
    return (record, args.label)

def resolve_end(value, label):
    return derive_id(label) if label else value

def cmd_add(args):
    data, created = load(args.records, create=True)
    graph = graph_from(data, args)
    record, label = record_input(args)
    ref = record.get('id') or '?'
    if args.clause_of is not None:
        return add_clause(args, graph, record, label, ref)
    hard, held, warnings, merged = graph.validate_record(record, label=label)
    if merged:
        return ([outcome('merged', 'record', ref, 'identical_record', 'already present; nothing written')], 0)
    rows, ok = judge('record', ref, hard, held, warnings, args.force)
    if ok and (not args.dry_run):
        graph.insert_record(record)
        save(graph, args.records)
        if created:
            rows.insert(0, outcome('created', 'file', str(args.records)))
    if ok and args.dry_run:
        rows[0] = outcome('valid', 'record', ref, None, 'dry run; nothing written')
    return (rows, 0 if ok else 1)

def clause_edge(args, graph, ref):
    doc = graph.records.get(args.clause_of)
    name = str(doc['title'])[:200] if isinstance(doc, dict) and 'title' in doc else str(args.clause_of)
    note = args.clause_note or (f'Clause of {name} at order {args.order}' if args.order is not None else f'Clause of {name}')
    edge = {'from': ref, 'to': args.clause_of, 'relation': 'part_of', 'note': note, 'sources': []}
    if args.order is not None:
        edge['order'] = args.order
    return edge

def add_clause(args, graph, record, label, ref):
    hard, held, warnings, merged = graph.validate_record(record, label=label, clause=True)
    if merged:
        rows, ok = ([outcome('merged', 'record', ref, 'identical_record', 'already present; nothing written')], True)
    else:
        rows, ok = judge('record', ref, hard, held, warnings, args.force)
    edge = clause_edge(args, graph, ref)
    if not ok:
        return (rows + [outcome('refused', 'edge', edge_ref(edge), 'record_refused', 'nothing written')], 1)
    e_hard, e_held, e_warn, e_merged = graph.validate_edge(edge, pending=None if merged else canonical_record(record))
    if e_merged:
        e_rows, e_ok = ([outcome('merged', 'edge', edge_ref(edge), 'identical_edge', 'already present; nothing written')], True)
    else:
        e_rows, e_ok = judge('edge', edge_ref(edge), e_hard, e_held, e_warn, args.force)
    if not e_ok:
        return ([outcome('refused', 'record', ref, 'edge_refused', 'nothing written')] + e_rows, 1)
    if merged and e_merged:
        return (rows + e_rows, 0)
    if args.dry_run:
        if not merged:
            rows[0] = outcome('valid', 'record', ref, None, 'dry run; nothing written')
        if not e_merged:
            e_rows[0] = outcome('valid', 'edge', edge_ref(edge), None, 'dry run; nothing written')
        return (rows + e_rows, 0)
    if not merged:
        graph.insert_record(record)
    if not e_merged:
        graph.insert_edge(edge)
    save(graph, args.records)
    return (rows + e_rows, 0)

def edge_input(args):
    edge = {'from': resolve_end(args.source_id, args.from_label), 'to': resolve_end(args.target_id, args.to_label), 'relation': args.type, 'note': args.note, 'sources': [parse_span(s) for s in args.source]}
    if args.order is not None:
        edge['order'] = args.order
    if args.operator:
        edge['operator_mark'] = operator_mark(args)
    return edge

def cmd_link(args):
    data, _ = load(args.records)
    graph = graph_from(data, args)
    edge = edge_input(args)
    hard, held, warnings, merged = graph.validate_edge(edge)
    if merged:
        return ([outcome('merged', 'edge', edge_ref(edge), 'identical_edge', 'already present; nothing written')], 0)
    rows, ok = judge('edge', edge_ref(edge), hard, held, warnings, args.force)
    if ok and (not args.dry_run):
        graph.insert_edge(edge)
        save(graph, args.records)
    if ok and args.dry_run:
        rows[0] = outcome('valid', 'edge', edge_ref(edge), None, 'dry run; nothing written')
    return (rows, 0 if ok else 1)

def cmd_amend(args):
    data, _ = load(args.records)
    graph = graph_from(data, args)
    old, relation = next(((value, name) for name, value in (('supersedes', args.supersedes), ('qualifies', args.qualifies), ('deprecates', args.deprecates)) if value))
    if old not in graph.records:
        return ([outcome('refused', 'edge', f'? {relation} {old}', 'dangling_end', {'missing': [old]})], 1)
    record, label = record_input(args)
    ref = record.get('id') or '?'
    hard, held, warnings, merged = graph.validate_record(record, label=label, merge_identical=False, clause=args.clause_of is not None)
    rows, ok = judge('record', ref, hard, held, warnings, args.force)
    if not ok:
        return (rows + [outcome('refused', 'edge', f'{ref} {relation} {old}', 'record_refused', 'nothing written')], 1)
    edge_sources = [parse_span(s) for s in args.edge_source]
    note_rows = []
    if not edge_sources:
        edge_sources = record['sources']
        note_rows.append(outcome('warning', 'edge', f'{ref} {relation} {old}', 'edge_sources_from_record', "no --edge-source; the new record's own spans are the edge's evidence"))
    edge = {'from': ref, 'to': old, 'relation': relation, 'note': args.note, 'sources': edge_sources}
    candidate = canonical_record(record)
    e_hard, e_held, e_warn, _ = graph.validate_edge(edge, merge_identical=False, pending=candidate)
    if relation == 'supersedes' and any((e['relation'] == 'supersedes' and e['to'] == old for e in graph.edges)):
        e_warn.append(('already_superseded', [e['from'] for e in graph.edges if e['relation'] == 'supersedes' and e['to'] == old]))
    e_rows, e_ok = judge('edge', edge_ref(edge), e_hard, e_held, e_warn, args.force)
    c_edge, c_rows, c_ok = (None, [], True)
    if args.clause_of is not None:
        c_edge = clause_edge(args, graph, ref)
        c_hard, c_held, c_warn, _ = graph.validate_edge(c_edge, merge_identical=False, pending=candidate)
        c_rows, c_ok = judge('edge', edge_ref(c_edge), c_hard, c_held, c_warn, args.force)
    if not e_ok or not c_ok:
        failed = (e_rows if not e_ok else []) + (c_rows if not c_ok else [])
        return ([outcome('refused', 'record', ref, 'edge_refused', 'nothing written')] + failed, 1)
    if not args.dry_run:
        graph.insert_record(record)
        graph.insert_edge(edge)
        if c_edge is not None:
            graph.insert_edge(c_edge)
        save(graph, args.records)
    else:
        rows[0] = outcome('valid', 'record', ref, None, 'dry run; nothing written')
        e_rows[0] = outcome('valid', 'edge', edge_ref(edge), None, 'dry run; nothing written')
        if c_rows:
            c_rows[0] = outcome('valid', 'edge', edge_ref(c_edge), None, 'dry run; nothing written')
    return (rows + e_rows + c_rows + note_rows, 0)

def cmd_mark(args):
    data, _ = load(args.records)
    graph = graph_from(data, args)
    ident = args.id
    if ident not in graph.records:
        return ([outcome('refused', 'record', ident, 'unknown_id', 'no record has this id')], 1)
    mark = operator_mark(args)
    bad = mark_cause(mark)
    if bad:
        return ([outcome('refused', 'record', ident, *bad)], 1)
    existing = graph.records[ident].get('operator_mark')
    if existing == mark:
        return ([outcome('merged', 'record', ident, 'identical_mark', 'already marked so; nothing written')], 0)
    rows = [outcome('marked', 'record', ident, None, mark)]
    if existing is not None:
        if not args.force:
            return ([outcome('held', 'record', ident, 'already_marked', existing)], 1)
        rows.append(outcome('warning', 'record', ident, 'forced:already_marked', {'replaced': existing}))
    if args.dry_run:
        return ([outcome('valid', 'record', ident, None, 'dry run; nothing written')] + rows[1:], 0)
    graph.records[ident] = canonical_record({**graph.records[ident], 'operator_mark': mark})
    save(graph, args.records)
    return (rows, 0)

def cmd_unmark(args):
    data, _ = load(args.records)
    graph = graph_from(data, args)
    ident = args.id
    if ident not in graph.records:
        return ([outcome('refused', 'record', ident, 'unknown_id', 'no record has this id')], 1)
    existing = graph.records[ident].get('operator_mark')
    if existing is None:
        return ([outcome('refused', 'record', ident, 'not_marked', 'the record carries no operator_mark')], 1)
    if args.dry_run:
        return ([outcome('valid', 'record', ident, None, 'dry run; nothing written')], 0)
    graph.records[ident] = {k: v for k, v in graph.records[ident].items() if k != 'operator_mark'}
    save(graph, args.records)
    return ([outcome('unmarked', 'record', ident, None, {'removed': existing})], 0)

def run_check(data, args):
    graph = Graph(data, args.sources, args.code, not args.no_code_check, args.similarity)
    clauses = build.clause_ids(data['edges'])
    graph.clauses = set(clauses)
    rows, hard_count, not_checked = ([], 0, 0)
    for index, record in enumerate(data['records']):
        ref = record.get('id', f'records[{index}]') if isinstance(record, dict) else f'records[{index}]'
        hard, held, warnings, _ = graph.validate_record(record, imported=True, merge_identical=False, clause=is_clause(record, clauses))
        rows += [outcome('fail', 'record', ref, c, d) for c, d in hard]
        rows += [outcome('finding', 'record', ref, c, d) for c, d in held + warnings]
        hard_count += len(hard)
        not_checked += sum((1 for c, _ in warnings if c == 'not_checked'))
        if not hard and isinstance(record, dict) and (record.get('id') not in graph.records):
            graph.insert_record(record)
    ids = [r.get('id') for r in data['records'] if isinstance(r, dict)]
    if ids != sorted(ids):
        rows.append(outcome('fail', 'file', str(args.records), 'unsorted', 'records must be sorted by id'))
        hard_count += 1
    for index, edge in enumerate(data['edges']):
        hard, held, warnings, _ = graph.validate_edge(edge, merge_identical=False)
        rows += [outcome('fail', 'edge', edge_ref(edge), c, d) for c, d in hard]
        rows += [outcome('finding', 'edge', edge_ref(edge), c, d) for c, d in held + warnings]
        hard_count += len(hard)
        not_checked += sum((1 for c, _ in warnings if c == 'not_checked'))
        if not hard:
            graph.insert_edge(edge)
    keepers = {e['to'] for e in graph.edges if e['relation'] == 'maintains'}
    dependents = {e['to'] for e in graph.edges if e['relation'] == 'requires'}
    qualified = {e['from'] for e in graph.edges if e['relation'] == 'qualifies'}
    for ident, record in sorted(graph.records.items()):
        if record['kind'] == 'invariant' and ident not in keepers:
            rows.append(outcome('finding', 'record', ident, 'invariant_without_keeper'))
        if record['kind'] == 'invariant' and ident not in dependents:
            rows.append(outcome('finding', 'record', ident, 'invariant_without_dependent'))
        if record['kind'] == 'gap' and ident not in qualified:
            rows.append(outcome('finding', 'record', ident, 'gap_qualifies_nothing'))
    return (rows, hard_count, not_checked)

def capture_of(data, label):
    if not isinstance(data, dict) or set(data) != {'schema', 'records', 'edges'}:
        raise Refusal(outcome('refused', 'file', label, 'fields', 'a capture has exactly schema, records, edges'))
    if isinstance(data.get('schema'), str) and data['schema'].startswith('decision-store'):
        raise Refusal(outcome('refused', 'file', label, 'arm_directory', 'this is a built store, not a capture'))
    if data['schema'] not in SCHEMAS:
        raise Refusal(outcome('refused', 'file', label, 'bad_schema', f"{data['schema']!r} not in {list(SCHEMAS)}"))
    if not isinstance(data['records'], list) or not isinstance(data['edges'], list):
        raise Refusal(outcome('refused', 'file', label, 'fields', 'records and edges must be lists'))
    return data

def resolve_baseline(records, against):
    records = Path(records).resolve()
    if against == 'none':
        return (None, 'disabled by --against none')
    if against is not None and Path(against).is_file():
        data, _ = load(against)
        return (data, str(against))
    try:
        top = subprocess.run(['git', '-C', str(records.parent), 'rev-parse', '--show-toplevel'], capture_output=True, text=True, check=True).stdout.strip()
        relative = records.relative_to(Path(top).resolve()).as_posix()
    except (OSError, subprocess.CalledProcessError, ValueError):
        return (None, f'{records.parent} is not a git checkout' if against is None else f'{against} is not a file, and {records.parent} is not a git checkout')
    ref = against if against is not None else 'HEAD'
    shown = subprocess.run(['git', '-C', top, 'show', f'{ref}:{relative}'], capture_output=True)
    if shown.returncode != 0:
        return (None, f'{ref}:{relative} is not in the repository at {top}')
    try:
        data = json.loads(shown.stdout.decode('utf-8'))
    except ValueError as exc:
        raise Refusal(outcome('refused', 'file', f'{ref}:{relative}', 'unreadable', str(exc)), 2)
    return (capture_of(data, f'{ref}:{relative}'), f'{ref}:{relative}')
BASELINE_IGNORED = {'operator_mark'}
EDGE_KEY = ('from', 'to', 'relation', 'note')

def authored(obj, canonical):
    return {k: v for k, v in canonical(obj).items() if k not in BASELINE_IGNORED}

def baseline_rows(candidate, baseline, label):
    rows = []
    old = {r['id']: authored(r, canonical_record) for r in baseline['records'] if isinstance(r, dict) and isinstance(r.get('id'), str)}
    new = {r['id']: authored(r, canonical_record) for r in candidate['records'] if isinstance(r, dict) and isinstance(r.get('id'), str)}
    for ident in sorted(old):
        if ident not in new:
            rows.append(outcome('fail', 'record', ident, 'committed_record_missing', f'in {label}, not in the candidate'))
        elif new[ident] != old[ident]:
            fields = [k for k in RECORD_ORDER if old[ident].get(k) != new[ident].get(k)]
            rows.append(outcome('fail', 'record', ident, 'committed_record_changed', f"{', '.join(fields)} changed since {label}"))
    unmatched = [authored(e, canonical_edge) for e in candidate['edges'] if isinstance(e, dict) and EDGE_KEYS <= set(e)]
    pending = []
    for edge in (authored(e, canonical_edge) for e in baseline['edges'] if isinstance(e, dict) and EDGE_KEYS <= set(e)):
        if edge in unmatched:
            unmatched.remove(edge)
        else:
            pending.append(edge)
    for edge in sorted(pending, key=lambda e: tuple((str(e.get(k)) for k in EDGE_KEY))):
        match = next((e for e in unmatched if all((e.get(k) == edge.get(k) for k in EDGE_KEY))), None) or next((e for e in unmatched if all((e.get(k) == edge.get(k) for k in EDGE_KEY[:3]))), None)
        if match is None:
            rows.append(outcome('fail', 'edge', edge_ref(edge), 'committed_edge_missing', f'in {label}, not in the candidate'))
        else:
            unmatched.remove(match)
            fields = [k for k in EDGE_ORDER if edge.get(k) != match.get(k)]
            rows.append(outcome('fail', 'edge', edge_ref(edge), 'committed_edge_changed', f"{', '.join(fields)} changed since {label}"))
    return rows

def cmd_check(args):
    data, _ = load(args.records)
    rows, hard, not_checked = run_check(data, args)
    baseline, label = resolve_baseline(args.records, getattr(args, 'against', None))
    gate = None
    if baseline is not None:
        gate = baseline_rows(data, baseline, label)
        rows += gate
        hard += len(gate)
    elif getattr(args, 'against', None) not in (None, 'none'):
        rows.append(outcome('not_checked', 'file', str(args.records), 'no_baseline', label))
        not_checked += 1
    else:
        sys.stderr.write(f'check: no baseline, skipped ({label})\n')
    causes = Counter((r['cause'] for r in rows if r['outcome'] == 'fail'))
    findings = Counter((r['cause'] for r in rows if r['outcome'] == 'finding'))
    verdict = 'fail' if hard else 'not_checked' if not_checked else 'pass'
    summary = outcome(verdict, 'file', str(args.records), None, {'records': len(data['records']), 'edges': len(data['edges']), 'hard_by_cause': dict(sorted(causes.items())), 'findings_by_cause': dict(sorted(findings.items()))})
    if gate is not None:
        summary['detail']['baseline'] = {'against': label, 'records': len(baseline['records']), 'edges': len(baseline['edges']), 'differences': len(gate)}
    shown = rows if not args.summary_only else []
    return (shown + [summary], 1 if hard else 2 if not_checked else 0)


def main(argv=None):
    env_code = [c for c in os.environ.get('DG_CODE', '').split(os.pathsep) if c]
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--sources', default=os.environ.get('DG_SOURCES'), help='source packet directory (default $DG_SOURCES)')
    common.add_argument('--code', action='append', default=None, help='code tree for `code/` citations (repeatable; default $DG_CODE)')
    common.add_argument('--no-code-check', action='store_true', help='do not resolve `code/` citations (reported as code_not_checked)')
    common.add_argument('--similarity', type=float, default=SIMILARITY, help=f'near-duplicate title Jaccard (default {SIMILARITY})')
    common.add_argument('--json', action='store_true', help='one JSON object per outcome line')
    target = argparse.ArgumentParser(add_help=False)
    target.add_argument('--records', default=os.environ.get('DG_RECORDS'), help='records.json to write (default $DG_RECORDS)')
    target.add_argument('--force', action='store_true', help='accept a HELD outcome (near-duplicate, unsupported span, second edge of a type)')
    target.add_argument('--dry-run', action='store_true', help='validate only; write nothing')
    fields = argparse.ArgumentParser(add_help=False)
    fields.add_argument('--stdin', action='store_true', help='read the record as one JSON object (may carry `label`)')
    fields.add_argument('--label', help='stable label; the id is r- + first 16 hex of sha256(label)')
    fields.add_argument('--kind')
    fields.add_argument('--topic')
    fields.add_argument('--title')
    fields.add_argument('--claim', '--notes', dest='claim')
    fields.add_argument('--actor')
    fields.add_argument('--authority')
    fields.add_argument('--state')
    fields.add_argument('--date', help='recorded_at: YYYY-MM-DD or unknown')
    fields.add_argument('--source', action='append', default=[], help='path:start-end (repeatable)')
    clause = argparse.ArgumentParser(add_help=False)
    clause.add_argument('--clause-of', help="document id: write the record as a clause of it (part_of, with --order) in the same call; a clause's claim has no 900-character cap")
    clause.add_argument('--order', help="with --clause-of: the clause's order key in the document")
    clause.add_argument('--clause-note', help="with --clause-of: the part_of edge's note (default names the document and order)")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('add', parents=[common, target, fields, clause], help='create one record (--clause-of: and its part_of edge)')
    link = sub.add_parser('link', parents=[common, target], help='add one typed edge')
    link.add_argument('--from', dest='source_id')
    link.add_argument('--to', dest='target_id')
    link.add_argument('--from-label', help='derive the from-id from a label instead of --from')
    link.add_argument('--to-label', help='derive the to-id from a label instead of --to')
    link.add_argument('--type', '--relation', dest='type', required=True)
    link.add_argument('--note', required=True)
    link.add_argument('--source', action='append', default=[], help='path:start-end (repeatable); omit only for a structural type or with --operator')
    link.add_argument('--order', help="part_of only; a string whose sort order is the clause's place in the document")
    link.add_argument('--operator', action='store_true', help='a relationship the operator knows and the sources do not state; needs --why, may omit --source for any type')
    for p in (link,):
        p.add_argument('--why', help='with --operator: why the operator asserts this edge')
        p.add_argument('--by', help='with --operator: who placed it (default: operator)')
        p.add_argument('--at', help='with --operator: YYYY-MM-DD (default: today)')
    markp = sub.add_parser('mark', parents=[common, target], help='set operator_mark {why, by, at} on one record')
    markp.add_argument('id')
    markp.add_argument('--why', required=True, help='why the operator marks this record (refused if empty)')
    markp.add_argument('--by', help='who placed the mark (default: operator)')
    markp.add_argument('--at', help='YYYY-MM-DD (default: today)')
    unmarkp = sub.add_parser('unmark', parents=[common, target], help="remove a record's operator_mark")
    unmarkp.add_argument('id')
    amend = sub.add_parser('amend', parents=[common, target, fields, clause], help='new record + supersedes/qualifies/deprecates edge, old record untouched')
    which = amend.add_mutually_exclusive_group(required=True)
    which.add_argument('--supersedes', help='id of the record the new one supersedes (its text is replaced)')
    which.add_argument('--qualifies', help='id of the record the new one qualifies (its text stands)')
    which.add_argument('--deprecates', help='id of the clause the new record removes with no replacement (settle)')
    amend.add_argument('--note', required=True, help='the edge note: why the old record no longer stands as written')
    amend.add_argument('--edge-source', action='append', default=[], help="evidence for the edge (default: the new record's spans)")
    check = sub.add_parser('check', parents=[common], help="build.py's validations without building, by cause")
    check.add_argument('--records', default=os.environ.get('DG_RECORDS'))
    check.add_argument('--summary-only', action='store_true')
    check.add_argument('--against', help="committed capture to compare with: a path, a git ref (the records file's own path in its checkout), or none; default HEAD, skipped outside a checkout")
    args = parser.parse_args(argv)
    if args.code is None:
        args.code = env_code
    if getattr(args, 'records', None) is None:
        parser.error('--records (or $DG_RECORDS) is required')
    if args.command == 'link' and (not ((args.source_id or args.from_label) and (args.target_id or args.to_label))):
        parser.error('link needs --from/--from-label and --to/--to-label')
    if args.command == 'link' and (not args.operator) and any((v is not None for v in (args.why, args.by, args.at))):
        parser.error('--why, --by and --at belong to link --operator')
    if args.command == 'link' and args.operator and (args.why is None):
        parser.error('link --operator needs --why')
    if args.command in ('add', 'amend') and args.clause_of is None and (args.order is not None or args.clause_note is not None):
        parser.error('--order and --clause-note belong to --clause-of')
    spans_cited = args.command not in ('link', 'mark', 'unmark') or (args.command == 'link' and args.source)
    if spans_cited and (not args.sources):
        emit([outcome('not_checked', 'file', '-', 'no_sources_dir', 'give --sources (or $DG_SOURCES): a span that cannot be checked is never accepted')], args.json)
        return 2
    if args.sources and (not Path(args.sources).is_dir()):
        emit([outcome('not_checked', 'file', str(args.sources), 'missing_sources_dir', 'no such sources directory')], args.json)
        return 2
    handler = {'add': cmd_add, 'link': cmd_link, 'amend': cmd_amend, 'check': cmd_check, 'mark': cmd_mark, 'unmark': cmd_unmark}[args.command]
    try:
        rows, code = handler(args)
    except Refusal as refusal:
        rows, code = ([refusal.row], refusal.code)
    emit(rows, args.json)
    return code
if __name__ == '__main__':
    sys.exit(main())
