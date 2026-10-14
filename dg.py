#!/usr/bin/env python3
"""Decision graph bounded reader."""

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from collections import Counter, deque
from pathlib import Path
PRIORITY = {name: index for index, name in enumerate(('maintains', 'requires', 'supersedes', 'affirms', 'qualifies', 'depends_on_premise', 'would_overturn', 'supports', 'motivates', 'implements', 'applies_to', 'part_of', 'deprecates'))}
CONSIDERATION_RELATIONS = ('depends_on_premise', 'would_overturn')
EXPAND_LEXICAL_SHARE = 0.5
EXPAND_RULES = {'mechanism': ('applies_to',), 'invariant': ('maintains', 'requires')}
EXPAND_KINDS = {'applies_to': ('invariant', 'gap', 'requirement')}
CONDITION_KINDS = ('invariant', 'requirement')
TIMELINE_KINDS = ('decision', 'proposal', 'judgment', 'requirement')
TIMELINE_HOPS = ('supersedes', 'qualifies', 'affirms', 'motivates')
TIMELINE_MARKERS = ('supersedes', 'qualifies', 'affirms')
TOKEN = re.compile('\\w+', re.UNICODE)
STOPWORDS = frozenset('a an and are as at be by for from has have how in into is it of on or that the their these this to was were what when where which who why with'.split())
ANCHOR_LINE = re.compile(r'\b(?:rivet|context):\s*(.*)')
ANCHOR_ID = re.compile('\\br-[0-9a-f]{16}\\b')

def anchors_in_line(line):
    m = ANCHOR_LINE.search(line)
    return ANCHOR_ID.findall(m.group(1)) if m else []
SUMMARY_FIELDS = ('kind', 'topic', 'title', 'claim', 'actor', 'authority', 'state', 'recorded_at')
MODES = ('index', 'lexical', 'relations')
DEFAULT_BUDGET = 6000

def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')

def emit(value):
    sys.stdout.write(encoded(value).decode('utf-8') + '\n')

def content_sha(record):
    return hashlib.sha256(encoded({key: record[key] for key in SUMMARY_FIELDS})).hexdigest()

def source_ref(source):
    return {key: source[key] for key in ('path', 'start_line', 'end_line')}

def card(record):
    out = {key: record[key] for key in ('id', 'kind', 'topic', 'title', 'claim', 'actor', 'authority', 'state', 'recorded_at')} | {'sources': [source_ref(s) for s in record['sources']]}
    if 'operator_mark' in record:
        out['operator_mark'] = record['operator_mark']
    return out

def marked_first(idents, records):
    return [i for i in idents if 'operator_mark' in records[i]] + [i for i in idents if 'operator_mark' not in records[i]]

def relation_text(edge):
    text = f"{edge['from']} {edge['relation']} {edge['to']}"
    return text + ' (operator)' if 'operator_mark' in edge else text

def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))

def read_store(root):
    try:
        raw = (root / 'store.json').read_bytes()
        store = json.loads(raw.decode('utf-8'))
        if store.get('schema') != 'decision-store.v1':
            raise ValueError('unsupported store schema')
        store['_sha256'] = hashlib.sha256(raw).hexdigest()
        return store
    except (OSError, ValueError, KeyError) as exc:
        return {'error': str(exc)}

def read_summaries(root, records):
    path = root / 'summaries.json'
    result = {'records': {}, 'topics': {}, 'present': path.exists(), 'stale': 0}
    if not path.exists():
        return result
    try:
        data = read_json(path)
        if data.get('schema') != 'decision-summaries.v1':
            raise ValueError('unsupported summaries schema')
    except (OSError, ValueError) as exc:
        result['error'] = str(exc)
        return result
    for ident, entry in (data.get('records') or {}).items():
        record = records.get(ident)
        if record is None:
            continue
        if isinstance(entry, dict) and entry.get('content_sha256') == content_sha(record) and isinstance(entry.get('summary'), str):
            result['records'][ident] = ' '.join(entry['summary'].split())
        else:
            result['stale'] += 1
    for topic, entry in (data.get('topics') or {}).items():
        if isinstance(entry, dict) and isinstance(entry.get('summary'), str):
            result['topics'][topic] = ' '.join(entry['summary'].split())
    return result

def policy(root):
    path = root / 'policy.json'
    if not path.exists():
        return {'mode': 'index', 'byte_budget': DEFAULT_BUDGET}
    try:
        value = read_json(path)
        if not isinstance(value, dict) or value.get('mode') not in MODES:
            raise ValueError('policy mode must be index, lexical or relations')
        budget = value.get('byte_budget', DEFAULT_BUDGET)
        if type(budget) is not int or budget < 160 or budget > 100000:
            raise ValueError('byte_budget must be 160..100000')
        return {'mode': value['mode'], 'byte_budget': budget}
    except (OSError, ValueError) as exc:
        return {'error': str(exc)}

def ranking(records, query):
    terms = set(TOKEN.findall(query.lower())) - STOPWORDS
    if not terms:
        return []
    document_frequency = Counter()
    for record in records:
        words = set(TOKEN.findall(' '.join((str(record[field]) for field in ('title', 'topic', 'claim', 'kind'))).lower()))
        document_frequency.update(words & terms)
    idf = {term: math.log((len(records) + 1) / (document_frequency[term] + 1)) + 1 for term in terms}
    ranks = []
    for record in records:
        score = 0
        for field, weight in (('title', 4), ('topic', 3), ('claim', 2), ('kind', 1)):
            words = TOKEN.findall(record[field].lower())
            score += weight * sum((words.count(term) * idf[term] for term in terms))
        if score:
            ranks.append((-score, record['id']))
    ranks.sort()
    return [ident for _, ident in ranks]

def neighbors(edges, ident):
    options = []
    for index, edge in enumerate(edges):
        if edge['from'] == ident:
            options.append((PRIORITY[edge['relation']], edge['to'], index))
        elif edge['to'] == ident:
            options.append((PRIORITY[edge['relation']], edge['from'], index))
    return sorted(options)

def relation_expansion(edges, seeds, depth=2):
    found = set(seeds)
    queue = deque(((seed, [], 0) for seed in seeds))
    result = []
    while queue:
        ident, path, distance = queue.popleft()
        if distance >= depth:
            continue
        for _, neighbor, index in neighbors(edges, ident):
            if neighbor in found:
                continue
            found.add(neighbor)
            next_path = path + [index]
            result.append((neighbor, next_path))
            queue.append((neighbor, next_path, distance + 1))
    return result

def bounded(store, before):
    if not before:
        return store
    kept = [r for r in store['records'] if r['recorded_at'] == 'unknown' or r['recorded_at'] <= before]
    ids = {r['id'] for r in kept}
    return {**store, 'records': kept, 'edges': [e for e in store['edges'] if e['from'] in ids and e['to'] in ids]}

def anchor_ids(path):
    text = Path(path).read_text(encoding='utf-8', errors='replace')
    return list(dict.fromkeys((i for line in text.splitlines() for i in anchors_in_line(line))))

def entry(record, summaries, edge_count):
    out = {'id': record['id'], 'kind': record['kind'], 'date': record['recorded_at'], 'state': record['state'], 'authority': record['authority'], 'topic': record['topic'], 'summary': summaries['records'].get(record['id'], record['title']), 'edges': edge_count}
    if 'operator_mark' in record:
        out['mark'] = record['operator_mark']['why']
    return out

def via_text(edge, ident):
    return f"{edge['relation']} {('from' if edge['to'] == ident else 'to')} {(edge['from'] if edge['to'] == ident else edge['to'])}"

def expansion(hits, records, edges):
    shown, reached, via = (set(hits), [], {})
    matched = Counter()
    for hit in hits:
        kind = records[hit]['kind']
        relations = EXPAND_RULES.get(kind)
        if not relations:
            continue
        matched[kind] += 1
        options = []
        for index, e in enumerate(edges):
            if e['relation'] not in relations or hit not in (e['from'], e['to']):
                continue
            if e['relation'] == 'applies_to':
                if e['to'] != hit:
                    continue
                other = e['from']
            elif kind in CONDITION_KINDS:
                other = e['to'] if e['from'] == hit else e['from']
            else:
                if e['from'] != hit:
                    continue
                other = e['to']
            allowed = EXPAND_KINDS.get(e['relation'])
            if allowed and records[other]['kind'] not in allowed:
                continue
            options.append((PRIORITY[e['relation']], other, index))
        for _, other, index in sorted(options):
            if other in shown or other in via:
                continue
            reached.append(other)
            via[other] = via_text(edges[index], other)
    return (reached, via, {kind: matched[kind] for kind in EXPAND_RULES})

def candidate_order(store, query, entries, topic):
    records = {r['id']: r for r in store['records']}
    edges = store['edges']
    ranked = ranking(store['records'], query or '')
    if topic is not None:
        topical = [r['id'] for r in store['records'] if r['topic'] == topic]
        if not topical:
            return None
        seeds = topical
    elif entries:
        seeds = list(entries)
    else:
        seeds = ranked[:3]
    order, via = ([], {})
    for ident in seeds:
        if ident not in order:
            order.append(ident)
    for ident, path in relation_expansion(edges, seeds, depth=1):
        if ident not in order:
            order.append(ident)
            e = edges[path[-1]]
            via[ident] = f"{e['relation']} {('from' if e['to'] == ident else 'to')} {(e['from'] if e['to'] == ident else e['to'])}"
    for ident in ranked:
        if ident not in order:
            order.append(ident)
    return (marked_first(order, records), via, ranked)

def index_context(store, summaries, budget, query, entries, topic, mode_label, seed_note, expand=False):
    records = {r['id']: r for r in store['records']}
    edges = store['edges']
    degree = Counter()
    for edge in edges:
        degree[edge['from']] += 1
        degree[edge['to']] += 1
    found = candidate_order(store, query, entries, topic)
    if found is None:
        return {'status': 'unavailable', 'reason': 'unknown_topic', 'topic': topic, 'byte_budget': budget}
    order, via, ranked = found
    expand_note = None
    if expand:
        hits = pack_order(order, records, summaries, degree, via, budget * EXPAND_LEXICAL_SHARE, skeleton(mode_label, budget, seed_note, order, ranked, records))
        reached, reached_via, matched = expansion(hits, records, edges)
        reached = marked_first(reached, records)
        via.update(reached_via)
        taken = set(hits) | set(reached)
        order = hits + reached + [i for i in order if i not in taken]
        expand_note = {'lexical_hits': len(hits), 'reached': len(reached), 'shown': len(reached), 'lexical_share': EXPAND_LEXICAL_SHARE}
        expand_note.update({f'matched_{kind}s': n for kind, n in matched.items()})
    output = skeleton(mode_label, budget, seed_note, order, ranked, records)
    if expand_note is not None:
        output['expand'] = expand_note
    if not order:
        output['status'] = 'no_results'
        return output if len(encoded(output)) + 1 <= budget else {'status': 'no_results', 'byte_budget': budget, 'total_candidates': 0}
    selected = []
    for ident in order:
        item = entry(records[ident], summaries, degree[ident])
        if ident in via:
            item['via'] = via[ident]
        trial = dict(output, records=output['records'] + [item], omissions={'records': len(order) - len(selected) - 1, 'relations': 0})
        if len(encoded(trial)) + 1 > budget:
            continue
        output = trial
        selected.append(ident)
    chosen = set(selected)
    related = [e for e in edges if e['from'] in chosen and e['to'] in chosen]
    related.sort(key=lambda e: (PRIORITY[e['relation']], e['from'], e['to']))
    output['omissions']['relations'] = len(related)
    for e in related:
        trial = dict(output, relations=output['relations'] + [relation_text(e)])
        trial['omissions'] = {**output['omissions'], 'relations': output['omissions']['relations'] - 1}
        if len(encoded(trial)) + 1 <= budget:
            output = trial
    while len(encoded(output)) + 1 > budget and output['records']:
        dropped = output['records'].pop()['id']
        output['relations'] = [r for r in output['relations'] if dropped not in r.split(' ')]
        output['omissions']['records'] += 1
    if expand_note is not None:
        shown = {r['id'] for r in output['records']}
        output['expand'] = dict(output['expand'], shown=sum((1 for i in reached if i in shown)))
    if not output['records']:
        output['status'] = 'record_too_large'
    if len(encoded(output)) + 1 > budget:
        output = {'status': 'record_too_large', 'byte_budget': budget, 'total_candidates': len(order)}
    return output

def skeleton(mode_label, budget, seed_note, order, ranked, records):
    candidate_topics = Counter((records[i]['topic'] for i in order))
    return {'status': 'ok', 'mode': mode_label, 'byte_budget': budget, 'seed': seed_note, 'total_candidates': len(order), 'lexical_candidates': len(ranked), 'records': [], 'relations': [], 'topics': dict(sorted(candidate_topics.items(), key=lambda kv: (-kv[1], kv[0]))[:12]), 'omissions': {'records': len(order), 'relations': 0}, 'next': 'show ID for the claim and links; show ID --sources for exact excerpts; context --topic NAME to widen'}

def pack_order(order, records, summaries, degree, via, limit, output):
    selected = []
    for ident in order:
        item = entry(records[ident], summaries, degree[ident])
        if ident in via:
            item['via'] = via[ident]
        trial = dict(output, records=output['records'] + [item], omissions={'records': len(order) - len(selected) - 1, 'relations': 0})
        if len(encoded(trial)) + 1 > limit:
            continue
        output = trial
        selected.append(ident)
    return selected

def edge_card(edge):
    out = {key: edge[key] for key in ('from', 'to', 'relation', 'note')} | {'sources': [source_ref(s) for s in edge['sources']]}
    if 'operator_mark' in edge:
        out['operator_mark'] = edge['operator_mark']
    return out

def card_context(store, configuration, query, entry_id):
    mode, budget = (configuration['mode'], configuration['byte_budget'])
    records = {r['id']: r for r in store['records']}
    edges = store['edges']
    ranked = ranking(store['records'], query or '')
    if entry_id and entry_id not in records:
        return {'status': 'unavailable', 'reason': 'unknown_entry', 'byte_budget': budget}
    seeds = [entry_id] if entry_id else ranked[:3]
    order = []
    paths = {}
    for ident in seeds:
        if ident not in order:
            order.append(ident)
    if mode == 'relations':
        for ident, path in relation_expansion(edges, seeds):
            if ident not in order:
                order.append(ident)
                paths[ident] = path
    for ident in ranked:
        if ident not in order:
            order.append(ident)
    candidates = set(order)
    total_relations = sum((1 for e in edges if e['from'] in candidates and e['to'] in candidates))
    output = {'status': 'ok', 'mode': mode, 'byte_budget': budget, 'total_candidates': len(order), 'lexical_candidates': len(ranked), 'total_relation_candidates': total_relations, 'seed_ids': seeds, 'selected_ids': [], 'edge_paths': [], 'records': [], 'relations': [], 'omissions': {'records': len(order), 'relations': total_relations}}
    if not order:
        output['status'] = 'no_results'
        if len(encoded(output)) + 1 > budget:
            output = {'status': 'no_results', 'byte_budget': budget, 'total_candidates': 0, 'omissions': {'records': 0, 'relations': 0}}
        return output
    included_edges = set()
    selected = set()
    for ident in order:
        new_edges = [i for i in paths.get(ident, []) if i not in included_edges]
        trial = json.loads(json.dumps(output))
        trial['records'].append(card(records[ident]))
        trial['selected_ids'].append(ident)
        if any((edges[i]['from'] not in selected | {ident} or edges[i]['to'] not in selected | {ident} for i in new_edges)):
            continue
        trial['relations'].extend((edge_card(edges[i]) for i in new_edges))
        trial['omissions']['relations'] -= len(new_edges)
        if ident in paths:
            trial['edge_paths'].append({'to': ident, 'edges': [{'from': edges[i]['from'], 'relation': edges[i]['relation'], 'to': edges[i]['to']} for i in paths[ident]]})
        trial['omissions']['records'] -= 1
        if len(encoded(trial)) + 1 > budget:
            continue
        output = trial
        selected.add(ident)
        included_edges.update(new_edges)
    related = [i for i, edge in enumerate(edges) if edge['from'] in selected and edge['to'] in selected and (i not in included_edges)]
    related.sort(key=lambda i: (PRIORITY[edges[i]['relation']], edges[i]['from'], edges[i]['to'], i))
    for index in related:
        trial = json.loads(json.dumps(output))
        trial['relations'].append(edge_card(edges[index]))
        trial['omissions']['relations'] -= 1
        if len(encoded(trial)) + 1 <= budget:
            output = trial
            included_edges.add(index)
    if not selected:
        output['status'] = 'record_too_large'
        output['omissions']['records'] = len(order)
    while len(encoded(output)) + 1 > budget and output['records']:
        removed = output['records'].pop()
        ident = removed['id']
        output['selected_ids'].remove(ident)
        output['edge_paths'] = [p for p in output['edge_paths'] if p['to'] != ident]
        output['relations'] = [e for e in output['relations'] if e['from'] != ident and e['to'] != ident]
        output['omissions']['records'] += 1
        output['omissions']['relations'] = total_relations - len(output['relations'])
    if not output['records']:
        output['status'] = 'record_too_large'
    if len(encoded(output)) + 1 > budget:
        output = {'status': 'record_too_large', 'byte_budget': budget, 'total_candidates': len(order), 'omissions': {'records': len(order), 'relations': total_relations}}
    return output

def line_ranges(numbers):
    ranges, start, previous = ([], None, None)
    for n in sorted(numbers):
        if start is None:
            start = previous = n
        elif n == previous + 1:
            previous = n
        else:
            ranges.append(f'{start}-{previous}' if start != previous else str(start))
            start = previous = n
    if start is not None:
        ranges.append(f'{start}-{previous}' if start != previous else str(start))
    return ranges

def merged_evidence(citations):
    by_path = {}
    for label, source in citations:
        by_path.setdefault(source['path'], []).append((source['start_line'], source['end_line'], label, source))
    blocks = []
    for path in sorted(by_path):
        windows = sorted(by_path[path], key=lambda w: (w[0], w[1]))
        current = None
        for start, end, label, source in windows:
            if current and start <= current['end_line'] + 1:
                current['end_line'] = max(current['end_line'], end)
                current['cited_by'].append(label)
                current['_sources'].append(source)
            else:
                if current:
                    blocks.append(current)
                current = {'path': path, 'start_line': start, 'end_line': end, 'cited_by': [label], '_sources': [source]}
        if current:
            blocks.append(current)
    for block in blocks:
        lines = {}
        digests = set()
        for source in block.pop('_sources'):
            if 'sha256' in source:
                digests.add(source['sha256'])
            if 'excerpt' in source:
                for offset, text in enumerate(source['excerpt'].splitlines(keepends=True)):
                    lines.setdefault(source['start_line'] + offset, text)
        missing = [n for n in range(block['start_line'], block['end_line'] + 1) if n not in lines]
        block['excerpt'] = ''.join((lines[n] for n in range(block['start_line'], block['end_line'] + 1) if n in lines))
        if digests:
            block['sha256'] = sorted(digests)[0] if len(digests) == 1 else sorted(digests)
        if missing:
            block['lines_unavailable'] = line_ranges(missing)
        block['cited_by'] = sorted(set(block['cited_by']))
    return blocks

def show(store, summaries, ident, with_sources, limit):
    records = {r['id']: r for r in store['records']}
    record = records.get(ident)
    if record is None:
        return {'status': 'unavailable', 'reason': 'unknown_id', 'id': ident}
    touching = [e for e in store['edges'] if e['from'] == ident or e['to'] == ident]
    touching.sort(key=lambda e: (PRIORITY[e['relation']], e['from'], e['to']))
    shown = touching[:limit]
    links = []
    for e in shown:
        other = e['to'] if e['from'] == ident else e['from']
        link = {'relation': e['relation'], 'direction': 'out' if e['from'] == ident else 'in', 'id': other, 'note': e['note'], 'summary': summaries['records'].get(other, records[other]['title']), 'kind': records[other]['kind'], 'date': records[other]['recorded_at'], 'state': records[other]['state']}
        if 'operator_mark' in e:
            link['operator_mark'] = e['operator_mark']
        if 'operator_mark' in records[other]:
            link['mark'] = records[other]['operator_mark']['why']
        links.append(link)
    output = {'status': 'ok', 'record': card(record), 'links': links, 'total_links': len(touching), 'omitted_links': max(0, len(touching) - limit)}
    if with_sources:
        citations = [(ident, s) for s in record['sources']]
        for e in shown:
            label = f"{e['from']} {e['relation']} {e['to']}"
            citations.extend(((label, s) for s in e['sources']))
        output['evidence'] = merged_evidence(citations)
        output['evidence_note'] = 'each cited window is returned once; cited_by lists the record and links that cite it'
    return output
REASON_RELATIONS = ('supersedes', 'deprecates', 'qualifies', 'would_overturn', 'depends_on_premise', 'affirms', 'motivates', 'supports')

def reason_bundle(store, ident, with_sources, budget, offset=0):
    records = {r['id']: r for r in store['records']}
    if ident not in records:
        return {'status': 'unavailable', 'reason': 'unknown_id'}
    touching = [e for e in store['edges'] if ident in (e['from'], e['to'])]
    eligible = sorted((e for e in touching if e['relation'] in REASON_RELATIONS), key=lambda e: (REASON_RELATIONS.index(e['relation']), e['from'], e['to'], encoded(e)))
    groups = {}
    for e in eligible:
        other = e['to'] if e['from'] == ident else e['from']
        groups.setdefault(other, []).append(e)
    neighbors = list(groups)
    if offset < 0 or offset > len(neighbors):
        return {'status': 'unavailable', 'reason': 'offset_out_of_range'}

    def page(selected):
        end = offset + len(selected)
        result = {'status': 'partial' if offset or end < len(neighbors) else 'ok', 'scope': 'one_hop_recorded_context', 'record': card(records[ident]), 'neighbors': [{'record': card(records[n]), 'links': [edge_card(e) for e in groups[n]]} for n in selected], 'total_neighbors': len(neighbors), 'omitted_before': offset, 'omitted_after': len(neighbors) - end, 'next_offset': end if end < len(neighbors) else None, 'other_links': len(touching) - len(eligible)}
        if with_sources:
            citations = [(ident, s) for s in records[ident]['sources']]
            for n in selected:
                citations.extend(((n, s) for s in records[n]['sources']))
                for e in groups[n]:
                    label = f"{e['from']} {e['relation']} {e['to']}"
                    citations.extend(((label, s) for s in e['sources']))
            windows = {}
            for label, source in citations:
                key = encoded(source)
                if key not in windows:
                    windows[key] = dict(source) | {'cited_by': []}
                if label not in windows[key]['cited_by']:
                    windows[key]['cited_by'].append(label)
            result['evidence'] = [windows[k] for k in sorted(windows)]
        return result
    selected = []
    result = page(selected)
    minimum = len(encoded(result)) + 1
    if minimum > budget:
        return {'status': 'unavailable', 'reason': 'seed_exceeds_budget', 'minimum_budget': minimum}
    for n in neighbors[offset:]:
        candidate = page(selected + [n])
        required = len(encoded(candidate)) + 1
        if required > budget:
            if not selected:
                return {'status': 'unavailable', 'reason': 'neighbor_exceeds_budget', 'next_offset': offset, 'minimum_budget': required}
            break
        selected.append(n)
        result = candidate
    return result

def current(store, summaries, ident, limit, budget):
    records = {r['id']: r for r in store['records']}
    if ident not in records:
        return {'status': 'unavailable', 'reason': 'unknown_id', 'id': ident}
    degree, newer = (Counter(), {})
    for e in store['edges']:
        degree[e['from']] += 1
        degree[e['to']] += 1
        if e['relation'] == 'supersedes':
            newer.setdefault(e['to'], []).append(e)

    def rank(i):
        date = records[i]['recorded_at']
        return (date if date != 'unknown' else '', i)
    chain, seen, branches, cycle, node = ([entry(records[ident], summaries, degree[ident])], {ident}, [], False, ident)
    while True:
        options = {}
        for e in sorted(newer.get(node, []), key=lambda e: (e['from'], e['note'])):
            options.setdefault(e['from'], e)
        if any((i in seen for i in options)):
            cycle = True
        fresh = sorted((i for i in options if i not in seen), key=rank, reverse=True)
        if not fresh:
            break
        if len(fresh) > 1:
            branches.append({'at': node, 'followed': fresh[0], 'not_followed': fresh[1:]})
        step = options[fresh[0]]
        chain.append(entry(records[step['from']], summaries, degree[step['from']]) | {'supersedes': node, 'note': step['note']})
        seen.add(step['from'])
        node = step['from']
    card_ = show(store, summaries, node, False, limit)
    out = {'status': 'ok', 'start': ident, 'current_id': node, 'chain_length': len(chain), 'chain': chain, 'branches': branches, 'current': {key: card_[key] for key in ('record', 'links', 'total_links', 'omitted_links')}, 'byte_budget': budget, 'omitted_chain': 0, 'note': 'supersedes edges are recorded assertions; the newest record on them is not proof nothing later changed the rule', 'next': "show ID --sources for any record's evidence"}
    if cycle:
        out['cycle'] = True
    while len(encoded(out)) + 1 > budget and out['current']['links']:
        out['current']['links'].pop()
        out['current']['omitted_links'] += 1
    while len(encoded(out)) + 1 > budget and len(out['chain']) > 2:
        out['chain'].pop(1)
        out['omitted_chain'] += 1
    if len(encoded(out)) + 1 > budget:
        return {'status': 'record_too_large', 'byte_budget': budget, 'start': ident, 'current_id': node, 'chain_length': len(chain)}
    return out

def timeline(store, budget, query, entries, topic, seed_note):
    records = {r['id']: r for r in store['records']}
    found = candidate_order(store, query, entries, topic)
    if found is None:
        return {'status': 'unavailable', 'reason': 'unknown_topic', 'topic': topic, 'byte_budget': budget}
    hops, marks, newer = ({}, {}, {})
    for e in store['edges']:
        if e['relation'] in TIMELINE_HOPS:
            hops.setdefault(e['from'], set()).add((PRIORITY[e['relation']], e['to']))
            hops.setdefault(e['to'], set()).add((PRIORITY[e['relation']], e['from']))
        if e['relation'] in TIMELINE_MARKERS:
            marks.setdefault(e['from'], set()).add((TIMELINE_MARKERS.index(e['relation']), e['to']))
        if e['relation'] == 'supersedes':
            newer.setdefault(e['to'], set()).add(e['from'])
    on_chain = set(newer) | {i for later in newer.values() for i in later}
    picked, seen = ([], set())
    for ident in found[0]:
        if records[ident]['kind'] not in TIMELINE_KINDS:
            continue
        for other in [ident] + [o for _, o in sorted(hops.get(ident, ()))]:
            if other not in seen:
                seen.add(other)
                picked.append(other)
    if not picked:
        return {'status': 'no_results', 'reason': 'no decision, proposal, judgment or requirement among the candidates', 'seed': seed_note, 'byte_budget': budget, 'total_candidates': len(found[0])}

    def rank(i):
        date = records[i]['recorded_at']
        return (date if date != 'unknown' else '', i)
    heads = {}

    def head(ident):
        if ident not in heads:
            node, walked = (ident, {ident})
            while True:
                fresh = sorted((i for i in newer.get(node, ()) if i not in walked), key=rank, reverse=True)
                if not fresh:
                    break
                node = fresh[0]
                walked.add(node)
            heads[ident] = node
        return heads[ident]

    def chronological(ids):
        days = {}
        for i in ids:
            days.setdefault(records[i]['recorded_at'], []).append(i)
        out = []
        for day in sorted(days, key=lambda d: (d == 'unknown', d)):
            remaining = set(days[day])
            while remaining:
                ready = sorted((i for i in remaining if not any((to in remaining and to != i for _, to in marks.get(i, ())))))
                step = (ready or sorted(remaining))[0]
                out.append(step)
                remaining.discard(step)
        return out

    def line(ident):
        r = records[ident]
        text = f"{r['recorded_at']} {ident} {r['kind']} {r['state']} {r['authority']} | {' '.join(r['title'].split())}"
        own = [f'{TIMELINE_MARKERS[k]} {to}' for k, to in sorted(marks.get(ident, ()))]
        return text + (' | ' + '; '.join(own) if own else '')

    def footer(ordered):
        chains = {}
        for ident in ordered:
            if ident in on_chain:
                chains.setdefault(head(ident), []).append(ident)
        if not chains:
            return 'current: no supersedes chain among the records shown'
        shown, parts = (set(ordered), [])
        for top in sorted(chains, key=rank):
            notes = [records[top]['recorded_at']] + (['not listed'] if top not in shown else [])
            over = [i for i in chains[top] if i != top]
            if over:
                notes.append('over ' + ' '.join(over))
            parts.append(f"{top} ({', '.join(notes)})")
        return 'current: ' + '; '.join(parts)

    def view(chosen):
        ordered = chronological(chosen)
        omitted = len(picked) - len(chosen)
        return {'status': 'ok', 'seed': seed_note, 'byte_budget': budget, 'total_candidates': len(picked), 'header': f'timeline {seed_note}: {len(chosen)} of {len(picked)} records, oldest first (date id kind state authority | title | chain markers)', 'lines': [line(i) for i in ordered], 'current': footer(ordered), 'omitted': f'omitted: {omitted} records for the {budget}-byte budget' + ('; narrow with --topic or --entry, or raise --budget' if omitted else ''), 'next': "next: show ID for a record's claim and links; current ID walks one supersedes chain", 'shown': ordered}
    chosen = []
    for ident in picked:
        if len(timeline_text(view(chosen + [ident])).encode('utf-8')) <= budget:
            chosen.append(ident)
    out = view(chosen)
    if not chosen or len(timeline_text(out).encode('utf-8')) > budget:
        return {'status': 'record_too_large', 'seed': seed_note, 'byte_budget': budget, 'total_candidates': len(picked)}
    return out

def timeline_text(view):
    if view.get('status') != 'ok':
        return json.dumps(view, ensure_ascii=False) + '\n'
    return '\n'.join([view['header'], *view['lines'], view['current'], view['omitted'], view['next']]) + '\n'
ASOF_REMOVERS = ('supersedes', 'deprecates')

def find_document(store, name):
    docs = [r for r in store['records'] if r['kind'] == 'document']
    for test in (lambda r: r['id'] == name, lambda r: r['title'] == name, lambda r: r['title'].startswith(name + ':') or r['title'].startswith(name + ' ')):
        found = [r for r in docs if test(r)]
        if len(found) == 1:
            return (found[0], None)
        if len(found) > 1:
            return (None, {'status': 'unavailable', 'reason': 'ambiguous_document', 'name': name, 'candidates': sorted((r['id'] for r in found))})
    return (None, {'status': 'unavailable', 'reason': 'unknown_document', 'name': name, 'documents': sorted((f"{r['id']} {r['title']}" for r in docs))})

def asof_state(store, doc, date):
    records = {r['id']: r for r in store['records']}

    def dated(ident):
        stamp = records[ident]['recorded_at']
        return stamp != 'unknown' and (date is None or stamp <= date)
    parts = {}
    for e in store['edges']:
        if e['relation'] == 'part_of' and e['to'] == doc['id']:
            parts[e['from']] = min(parts.get(e['from'], e['order']), e['order'])
    removed = {}
    for e in store['edges']:
        if e['relation'] in ASOF_REMOVERS and dated(e['from']):
            removed.setdefault(e['to'], []).append(e)
    standing = sorted(((order, ident) for ident, order in parts.items() if dated(ident) and ident not in removed))
    orders = Counter((order for order, _ in standing))
    counts = {'part_of': len(parts), 'standing': len(standing), 'after_date': sum((1 for i in parts if records[i]['recorded_at'] != 'unknown' and (not dated(i)))), 'undated': sum((1 for i in parts if records[i]['recorded_at'] == 'unknown')), 'superseded': sum((1 for i in parts if dated(i) and any((e['relation'] == 'supersedes' for e in removed.get(i, ()))))), 'deprecated': sum((1 for i in parts if dated(i) and i in removed and all((e['relation'] == 'deprecates' for e in removed[i])))), 'order_ties': sum((n - 1 for n in orders.values() if n > 1))}
    return ([records[i] | {'_order': order} for order, i in standing], counts)

def asof_text(clauses):
    return ''.join((c['claim'].rstrip('\n') + '\n' + ('\n' if n < len(clauses) - 1 else '') for n, c in enumerate(clauses)))

def asof(store, name, date, other=None):
    doc, problem = find_document(store, name)
    if problem:
        return problem
    head = {'id': doc['id'], 'title': doc['title'], 'recorded_at': doc['recorded_at']}
    clauses, counts = asof_state(store, doc, date)
    if other is None:
        out = {'status': 'ok', 'document': head, 'as_of': date, 'counts': counts, 'clauses': [{'id': c['id'], 'order': c['_order'], 'recorded_at': c['recorded_at'], 'title': c['title'], 'claim': c['claim']} for c in clauses], 'text': asof_text(clauses), 'note': 'rebuilt from recorded clauses and their supersedes/deprecates edges; a clause nobody recorded is absent'}
        if date is not None and doc['recorded_at'] != 'unknown' and (doc['recorded_at'] > date):
            out['document_after_date'] = True
        return out
    later, later_counts = asof_state(store, doc, other)
    before_ids, after_ids = ({c['id'] for c in clauses}, {c['id'] for c in later})
    import difflib
    diff = list(difflib.unified_diff(asof_text(clauses).splitlines(), asof_text(later).splitlines(), fromfile=f"{doc['title']} as of {date}", tofile=f"{doc['title']} as of {other}", lineterm=''))
    return {'status': 'ok', 'document': head, 'from': date, 'to': other, 'added': [c['id'] for c in later if c['id'] not in before_ids], 'removed': [c['id'] for c in clauses if c['id'] not in after_ids], 'counts': {'from': counts, 'to': later_counts}, 'diff': diff}

def asof_render(view):
    if view.get('status') != 'ok':
        return json.dumps(view, ensure_ascii=False) + '\n'
    if 'diff' in view:
        return ''.join((line + '\n' for line in view['diff']))
    return view['text']

def git_output(repo, *args):
    try:
        done = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, check=False)
    except OSError:
        return None
    return done.stdout if done.returncode == 0 else None

def committed_store(root, commit, records_rel=None, sources_rel=None, code_rels=None):
    root = Path(root).resolve()
    top = git_output(root, 'rev-parse', '--show-toplevel')
    if top is None:
        return (None, {'status': 'unavailable', 'reason': 'not_a_repository', 'root': str(root)})
    repo = Path(top.decode('utf-8').strip())
    sha = git_output(repo, 'rev-parse', '--verify', '--quiet', f'{commit}^{{commit}}')
    if sha is None:
        return (None, {'status': 'unavailable', 'reason': 'unknown_commit', 'commit': commit})
    sha = sha.decode('utf-8').strip()
    try:
        arm_rel = root.relative_to(repo).as_posix()
    except ValueError:
        arm_rel = None
    base = Path(arm_rel).parent if arm_rel else Path('.')
    records_rel = records_rel or (base / 'records.json').as_posix()
    sources_rel = sources_rel or (base / 'sources').as_posix()
    code_fallback = False
    if code_rels is None:
        code_rels, code_fallback = ([], True)
        try:
            for sites in read_json(root / 'anchors.json').get('anchors', {}).values():
                for site in sites:
                    path = Path(site.rsplit(':', 1)[0])
                    if path.is_absolute():
                        try:
                            path = path.relative_to(repo)
                        except ValueError:
                            continue
                    head = path.parts[0] if path.parts else ''
                    if head and head not in code_rels:
                        code_rels.append(head)
        except (OSError, ValueError, AttributeError):
            code_rels = []
    where = {'commit': sha, 'records': records_rel, 'sources': sources_rel}
    raw = git_output(repo, 'show', f'{sha}:{records_rel}')
    if raw is None:
        return (None, {'status': 'unavailable', 'reason': 'no_committed_capture', **where, 'note': "the records file is not in that commit's tree; HEAD and the working tree were not read"})
    derive = None
    if arm_rel:
        committed_meta = git_output(repo, 'show', f'{sha}:{arm_rel}/metadata.json')
        if committed_meta is not None:
            try:
                derive = 'derived_states' in json.loads(committed_meta.decode('utf-8'))
            except ValueError:
                derive = None
    if derive is None:
        try:
            derive = 'derived_states' in read_json(root / 'metadata.json')
        except (OSError, ValueError):
            derive = False
    tar_bytes = git_output(repo, 'archive', '--format=tar', sha)
    if tar_bytes is None:
        return (None, {'status': 'unavailable', 'reason': 'archive_failed', **where})
    build_py = Path(__file__).resolve().parent / 'build.py'
    with tempfile.TemporaryDirectory(prefix='dg-asof-commit-') as temp:
        tree = Path(temp) / 'tree'
        tree.mkdir()
        tar_path = Path(temp) / 'tree.tar'
        tar_path.write_bytes(tar_bytes)
        with tarfile.open(tar_path) as archive:
            archive.extractall(tree, filter='data')
        if not (tree / sources_rel).is_dir():
            return (None, {'status': 'unavailable', 'reason': 'no_committed_sources', **where})
        code_present = [c for c in code_rels if (tree / c).is_dir()]
        if not code_present and code_fallback and (b'"code/' in raw):
            skip = {Path(sources_rel).parts[0], *(Path(arm_rel).parts[:1] if arm_rel else ())}
            code_present = sorted((d.name for d in tree.iterdir() if d.is_dir() and d.name not in skip))
        argv = [sys.executable, '-B', str(build_py), '--records', str(tree / records_rel), '--sources', str(tree / sources_rel), '--out', str(Path(temp) / 'arm')]
        for c in code_present:
            argv += ['--code', str(tree / c)]
        if derive:
            argv.append('--derive-superseded')
        built = subprocess.run(argv, capture_output=True, text=True)
        if built.returncode != 0:
            return (None, {'status': 'unavailable', 'reason': 'build_failed', **where, 'stderr': built.stderr[-600:]})
        store = read_store(Path(temp) / 'arm')
    if 'error' in store:
        return (None, {'status': 'unavailable', 'reason': store['error'], **where})
    provenance = {**where, 'records_sha256': hashlib.sha256(raw).hexdigest(), 'code': code_present, 'derive_superseded': bool(derive), 'build_py_sha256': hashlib.sha256(build_py.read_bytes()).hexdigest(), 'records_in_store': len(store['records']), 'edges_in_store': len(store['edges'])}
    return (store, provenance)

def asof_committed(root, name, commit, date=None, records_rel=None, sources_rel=None, code_rels=None):
    store, info = committed_store(root, commit, records_rel, sources_rel, code_rels)
    if store is None:
        return info
    view = asof(store, name, date)
    view['committed'] = info
    if view.get('status') == 'ok':
        view['note'] = f"rebuilt from {info['records']} as committed at {info['commit'][:12]}: what that store asserted at the commit, not when any decision happened; a clause nobody recorded is absent"
    return view
RENDER_FORMS = ('adr', 'changelog', 'faq')
RENDER_CHANGES = ('supersedes', 'qualifies', 'deprecates')
RENDER_REASON_KINDS = ('judgment', 'evidence', 'decision')
RENDER_PROVENANCE = re.compile('^\\[clause r-[0-9a-f]{16} ')
RENDER_NEXT = "next: show ID for a record's full claim and links"

def render(store, summaries, name, date, form, budget):
    doc, problem = find_document(store, name)
    if problem:
        return problem
    records = {r['id']: r for r in store['records']}

    def dated(ident):
        stamp = records[ident]['recorded_at']
        return stamp != 'unknown' and stamp <= date

    def gist(ident):
        return ' '.join(summaries['records'].get(ident, records[ident]['title']).split())

    def latest(edge):
        return (records[edge['from']]['recorded_at'], edge['from'], edge['relation'])
    clauses, counts = asof_state(store, doc, date)
    parts, into, out, ends = ({}, {}, {}, {})
    for e in store['edges']:
        if e['relation'] == 'part_of' and e['to'] == doc['id']:
            parts[e['from']] = min(parts.get(e['from'], e['order']), e['order'])
    for e in store['edges']:
        if e['relation'] in RENDER_CHANGES and dated(e['from']) and (e['relation'] != 'qualifies' or e['from'] in parts):
            into.setdefault(e['to'], []).append(e)
            out.setdefault(e['from'], []).append(e)
        ends.setdefault((e['relation'], 'into', e['to']), set()).add(e['from'])
        ends.setdefault((e['relation'], 'from', e['from']), set()).add(e['to'])

    def linked(relation, way, ident):
        return sorted((i for i in ends.get((relation, way, ident), ()) if dated(i)), key=lambda i: (records[i]['recorded_at'], i))

    def setters(ident):
        found = [ident]
        for e in sorted(out.get(ident, ()), key=lambda e: (e['to'], e['relation'])):
            if e['relation'] == 'deprecates':
                continue
            for other in sorted(into.get(e['to'], ()), key=latest):
                if other['relation'] != 'deprecates' and other['from'] not in parts and (other['from'] not in found):
                    found.append(other['from'])
        return found

    def last_change(ident):
        edges = into.get(ident, ())
        return (max(edges, key=latest), len(edges)) if edges else (None, 0)

    def provenance(ident):
        bits = [f"[clause {ident} {records[ident]['recorded_at']}"]
        bits += [f"{e['relation']} {e['to']} of {records[e['to']]['recorded_at']}" for e in sorted(out.get(ident, ()), key=lambda e: (e['relation'], e['to']))]
        bits += [f"with {x} {records[x]['kind']}: {gist(x)}" for x in setters(ident)[1:]]
        last, n = last_change(ident)
        if last:
            x = last['from']
            bits.append(f"last changed {records[x]['recorded_at']} by {x} {records[x]['kind']} ({last['relation']}): {gist(x)}" + (f'; {n - 1} earlier' if n > 1 else ''))
        return ' | '.join(bits) + ']'

    def considerations(ident):
        sources = setters(ident)
        last, _ = last_change(ident)
        if last and last['from'] not in sources:
            sources.append(last['from'])
        rows, seen = ([], set())
        for s in sources:
            found = [('motivates', x) for x in linked('motivates', 'into', s) if records[x]['kind'] in RENDER_REASON_KINDS]
            found += [('supports', x) for x in linked('supports', 'into', s) if records[x]['kind'] in RENDER_REASON_KINDS]
            found += [('premise', x) for x in linked('depends_on_premise', 'from', s)]
            found += [('would overturn', x) for x in linked('would_overturn', 'into', s)]
            found += [('qualifies', x) for x in linked('qualifies', 'into', s) if x not in parts]
            for label, x in found:
                if x not in seen:
                    seen.add(x)
                    rows.append(f"- {label} {x} {records[x]['kind']}: {gist(x)}" + (f' (of {s})' if s != ident else ''))
        return rows
    title, standing = (' '.join(doc['title'].split()), [c['id'] for c in clauses])
    if form == 'adr':
        header = f"{title} as of {date} ({doc['id']}): {len(standing)} clauses standing of {counts['part_of']} recorded ({counts['superseded']} superseded, {counts['deprecated']} deprecated, {counts['after_date']} after the date); each clause's text, then [clause ID DATE | what it supersedes or qualifies | the record that last changed it]"
        blocks = {c['id']: c['claim'].rstrip('\n') + '\n' + provenance(c['id']) + '\n' for c in clauses}
        priority, unit = (standing, 'clauses')
    elif form == 'faq':
        header = f"{title} as of {date} ({doc['id']}): {len(standing)} clauses standing, one entry each: the clause, then why: the judgment, evidence and decision records that motivate or support the record that set or last changed it, and that record's premises and overturning facts"
        blocks = {}
        for c in clauses:
            rows = considerations(c['id'])
            blocks[c['id']] = f"clause {c['id']} ({c['recorded_at']}):\n{c['claim'].rstrip(chr(10))}\nwhy:" + ('\n' + '\n'.join(rows) + '\n' if rows else ' none recorded\n')
        priority, unit = (standing, 'clauses')
    else:
        first = doc['recorded_at'] if doc['recorded_at'] != 'unknown' else ''
        initial, events = (0, [])
        for c in parts:
            if not dated(c) or any((e['to'] in parts for e in out.get(c, ()))):
                continue
            if records[c]['recorded_at'] > first:
                events.append(('adds', c, c))
            else:
                initial += 1
        events += [(e['relation'], target, e['from']) for target in parts for e in into.get(target, ())]
        events.sort(key=lambda ev: (records[ev[2]]['recorded_at'], parts.get(ev[2], parts[ev[1]]), ev[2], ev[0], ev[1]))
        header = f"{title} changelog to {date} ({doc['id']}): first version {first or 'undated'} with {initial} clauses; {len(events)} changes, oldest first: date, relation, the clause, the record that made the change and its summary; why: the records that motivate that record"
        blocks = {}
        for n, (relation, target, by) in enumerate(events):
            r = records[by]
            if relation == 'adds':
                line = f"{r['recorded_at']} adds {by}: {gist(by)}"
            else:
                line = f"{r['recorded_at']} {relation} {target} ({gist(target)}) by {by} {r['kind']}: {gist(by)}"
            why = [f"{x} {records[x]['kind']}: {gist(x)}" for x in linked('motivates', 'into', by)]
            blocks[n] = line + '\n  why: ' + ('; '.join(why) if why else 'none recorded') + '\n'
        priority, unit = (list(reversed(range(len(events)))), 'changes')

    def view(chosen):
        taken = set(chosen)
        shown = [k for k in (sorted(chosen) if form == 'changelog' else priority) if k in taken]
        left = [k for k in priority if k not in taken]
        omitted = f'omitted: {len(left)} of {len(priority)} {unit} for the {budget}-byte budget'
        if left and form == 'changelog':
            dates = sorted((records[events[k][2]]['recorded_at'] for k in left))
            omitted += f', the oldest ({dates[0]} to {dates[-1]}); raise --budget'
        elif left:
            omitted += ': ' + ' '.join(left) + '; raise --budget'
        return {'status': 'ok', 'form': form, 'as_of': date, 'byte_budget': budget, 'document': {'id': doc['id'], 'title': doc['title'], 'recorded_at': doc['recorded_at']}, 'header': header, 'entries': [blocks[k] for k in shown], 'shown': [events[k][2] for k in shown] if form == 'changelog' else shown, 'omitted': omitted, 'next': RENDER_NEXT}
    chosen = []
    for key in priority:
        if len(render_text(view(chosen + [key])).encode('utf-8')) > budget:
            break
        chosen.append(key)
    result = view(chosen)
    if len(render_text(result).encode('utf-8')) > budget:
        return {'status': 'record_too_large', 'form': form, 'byte_budget': budget, 'document': doc['id']}
    return result

def render_text(view):
    if view.get('status') != 'ok':
        return json.dumps(view, ensure_ascii=False) + '\n'
    body = ('' if view['form'] == 'changelog' else '\n').join(view['entries'])
    return view['header'] + '\n\n' + (body + '\n' if body else '') + view['omitted'] + '\n' + view['next'] + '\n'

def impact(store, summaries, seeds, files, budget=DEFAULT_BUDGET, known_anchors=None):
    records = {r['id']: r for r in store['records']}
    anchors = known_anchors or {}
    edges = store['edges']

    def members(ident):
        return [e['from'] for e in edges if e['to'] == ident and e['relation'] in ('implements', 'applies_to') and (records[e['from']]['kind'] in ('implementation', 'mechanism'))]

    def line(ident):
        r = records[ident]
        row = {'id': ident, 'kind': r['kind'], 'state': r['state'], 'date': r['recorded_at'], 'summary': summaries['records'].get(ident, r['title'])}
        if 'operator_mark' in r:
            row['mark'] = r['operator_mark']['why']
        if ident in anchors:
            row['anchors'] = anchors[ident]
        return row

    def first(rows, key=lambda row: row['id']):
        return [x for x in rows if 'operator_mark' in records[key(x)]] + [x for x in rows if 'operator_mark' not in records[key(x)]]

    def flagged(row, edge):
        return row | {'operator_edge': True} if 'operator_mark' in edge else row

    def gaps_for(ident):
        return first([flagged(line(e['from']) | {'note': e['note']}, e) for e in edges if e['to'] == ident and e['relation'] == 'qualifies' and (records[e['from']]['kind'] == 'gap')])

    def considerations_for(ident):
        rows = []
        for relation in CONSIDERATION_RELATIONS:
            for e in edges:
                if e['relation'] != relation:
                    continue
                other = e['to'] if relation == 'depends_on_premise' and e['from'] == ident else e['from'] if relation == 'would_overturn' and e['to'] == ident else None
                if other is not None:
                    rows.append(flagged(line(other) | {'relation': relation, 'decision': ident, 'note': e['note']}, e))
        return first(rows)

    def conditions(ident, relation):
        found = []
        for holder in [ident] + members(ident):
            for e in edges:
                if e['from'] == holder and e['relation'] == relation and (e['to'] not in [c for c, _ in found]):
                    found.append((e['to'], holder))
        return found
    out = {'status': 'ok', 'seeds': [], 'files': files, 'byte_budget': budget, 'omitted_seeds': [], 'note': 'edges are recorded assertions; a listed dependency is a reason to check, not proof'}
    unknown = [s for s in seeds if s not in records]
    if unknown:
        return {'status': 'unavailable', 'reason': 'unknown_entry', 'unknown': unknown}
    seeds = first(seeds, key=lambda i: i)
    full = []
    for seed in seeds:
        item = {'record': line(seed), 'maintains': [], 'requires': []}
        own = {seed} | set(members(seed))
        for relation, key in (('maintains', 'maintains'), ('requires', 'requires')):
            for cond, holder in first(conditions(seed, relation), key=lambda pair: pair[0]):
                dependents = first([flagged(line(e['from']) | {'relation': e['relation']}, e) for e in edges if e['to'] == cond and e['relation'] in ('requires', 'maintains') and (e['from'] not in own)])
                held_by = next((e for e in edges if e['from'] == holder and e['to'] == cond and (e['relation'] == relation)))
                entry_ = flagged({'condition': line(cond), 'note': held_by['note'], 'others': dependents}, held_by)
                weak = gaps_for(cond)
                if weak:
                    entry_['gaps'] = weak
                if holder != seed:
                    entry_['via'] = holder
                item[key].append(entry_)
        applied = [e['from'] for e in edges if e['to'] == seed and e['relation'] == 'applies_to' and (records[e['from']]['kind'] in ('invariant', 'requirement'))]
        if applied:
            item['applies_here'] = first([line(i) for i in applied])
        if records[seed]['kind'] in ('invariant', 'requirement', 'decision', 'mechanism'):
            weak = gaps_for(seed)
            if weak:
                item['weakened_by'] = weak
        reached = [seed] if records[seed]['kind'] == 'decision' else []
        for group in ('maintains', 'requires'):
            for cond in item[group]:
                reached += [o['id'] for o in cond['others'] if o['kind'] == 'decision' and o['id'] not in reached]
        considered = [row for ident in reached for row in considerations_for(ident)]
        if considered:
            item['considerations'] = considered
        item['detail_omitted'] = False
        full.append(item)
    skeleton = [{'record': item['record'], 'maintains': [], 'requires': [], 'detail_omitted': bool(item['maintains'] or item['requires'] or item.get('applies_here') or item.get('considerations'))} for item in full]
    out['seeds'] = []
    for item in skeleton:
        trial = dict(out, seeds=out['seeds'] + [item])
        if len(encoded(trial)) + 1 > budget:
            out['omitted_seeds'].append(item['record']['id'])
            continue
        out = trial

    def trimmed(item, keep):
        copy_ = json.loads(json.dumps(item))
        for group in ('maintains', 'requires'):
            for cond in copy_[group]:
                if len(cond['others']) > keep:
                    cond['others_omitted'] = len(cond['others']) - keep
                    cond['others'] = cond['others'][:keep]
        copy_['detail_omitted'] = False
        copy_['detail_trimmed'] = keep < max((len(c['others']) for g in ('maintains', 'requires') for c in item[g]), default=0)
        return copy_
    for index, item in enumerate(full):
        if index >= len(out['seeds']) or out['seeds'][index]['record']['id'] != item['record']['id']:
            break
        most = max((len(c['others']) for g in ('maintains', 'requires') for c in item[g]), default=0)
        for keep in range(most, -1, -1):
            trial = json.loads(json.dumps(out))
            trial['seeds'][index] = trimmed(item, keep)
            if len(encoded(trial)) + 1 <= budget:
                out = trial
                break
    while len(encoded(out)) + 1 > budget and out['seeds']:
        out['omitted_seeds'].insert(0, out['seeds'].pop()['record']['id'])
        if len(encoded(out)) + 1 > budget and out['seeds']:
            out['omitted_seed_count'] = len(out['omitted_seeds'])
            out['omitted_seeds'] = []
    if not out['seeds']:
        out['status'] = 'record_too_large'
    return out

def topics(store, summaries):
    rows = {}
    for r in store['records']:
        row = rows.setdefault(r['topic'], {'topic': r['topic'], 'records': 0, 'kinds': Counter(), 'dates': []})
        row['records'] += 1
        row['kinds'][r['kind']] += 1
        if r['recorded_at'] != 'unknown':
            row['dates'].append(r['recorded_at'])
    out = []
    for topic in sorted(rows):
        row = rows[topic]
        item = {'topic': topic, 'records': row['records'], 'kinds': dict(sorted(row['kinds'].items())), 'from': min(row['dates']) if row['dates'] else 'unknown', 'to': max(row['dates']) if row['dates'] else 'unknown'}
        if topic in summaries['topics']:
            item['summary'] = summaries['topics'][topic]
        out.append(item)
    return {'status': 'ok', 'topics': out, 'total_topics': len(out), 'next': "context --topic NAME lists that topic's records"}

def relative_name(path, base):
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.resolve().relative_to(Path(base).resolve()).as_posix()

def marked(row):
    return f"  mark: {row['mark']}" if row.get('mark') else ''

def impact_text(view):
    if view.get('status') != 'ok':
        return json.dumps(view, ensure_ascii=False)
    lines = []
    for item in view['seeds']:
        r = item['record']
        lines.append(f"{r['id']} [{r['kind']}] {r['summary']}{marked(r)}" + (f"  @ {', '.join(r['anchors'])}" if r.get('anchors') else ''))
        for group, verb in (('maintains', 'keeps'), ('requires', 'needs')):
            for cond in item[group]:
                c = cond['condition']
                via = f" (via {cond['via']})" if cond.get('via') else ''
                via += ' (operator)' if cond.get('operator_edge') else ''
                lines.append(f"  {verb}{via} {c['id']} [{c['kind']}/{c['state']}] {c['summary']}{marked(c)}")
                lines.append(f"      why: {cond['note']}")
                for other in cond['others']:
                    site = f"  @ {', '.join(other['anchors'])}" if other.get('anchors') else ''
                    relation = other['relation'] + (' (operator)' if other.get('operator_edge') else '')
                    lines.append(f"      {relation:9} {other['id']} [{other['kind']}] {other['summary']}{marked(other)}{site}")
                if cond.get('others_omitted'):
                    lines.append(f"      ... {cond['others_omitted']} more dependents (impact --entry {c['id']})")
                for gap in cond.get('gaps', []):
                    lines.append(f"      weakened by {gap['id']} [gap/{gap['state']}] {gap['summary']}: {gap['note']}")
        for gap in item.get('weakened_by', []):
            lines.append(f"  weakened by {gap['id']} [gap/{gap['state']}] {gap['summary']}: {gap['note']}")
        for applied in item.get('applies_here', []):
            lines.append(f"  applies here {applied['id']} [{applied['kind']}/{applied['state']}] {applied['summary']}{marked(applied)}")
        for row in item.get('considerations', []):
            label = 'premise of' if row['relation'] == 'depends_on_premise' else 'would overturn'
            lines.append(f"  {label} {row['decision']}: {row['id']} [{row['kind']}/{row['state']}] {row['summary']}{marked(row)}" + (' (operator)' if row.get('operator_edge') else ''))
            lines.append(f"      {('if it fails' if row['relation'] == 'depends_on_premise' else 'how')}: {row['note']}")
        if item.get('detail_omitted'):
            lines.append('  (detail omitted for budget: impact --entry ' + r['id'] + ')')
    if view.get('omitted_seeds'):
        lines.append(f"omitted seeds (budget): {', '.join(view['omitted_seeds'])}")
    if view.get('omitted_seed_count'):
        lines.append(f"omitted seeds (budget): {view['omitted_seed_count']}")
    lines.append('note: ' + view['note'])
    return '\n'.join(lines) + '\n'

def anchors(store, root, code):
    records = {r['id']: r for r in store['records']}
    found = {}
    scanned = 0
    source = None
    if code:
        source = 'scan'
        for tree in code:
            base = Path(tree)
            for path in sorted((p for p in base.rglob('*') if p.is_file() and (not p.is_symlink()))):
                try:
                    text = path.read_text(encoding='utf-8')
                except (OSError, UnicodeDecodeError):
                    continue
                scanned += 1
                for number, line in enumerate(text.splitlines(), 1):
                    for ident in anchors_in_line(line):
                        found.setdefault(ident, []).append(f'{relative_name(path, base)}:{number}')
    elif (root / 'anchors.json').exists():
        source = 'anchors.json'
        try:
            data = read_json(root / 'anchors.json')
            found = {k: list(v) for k, v in data.get('anchors', {}).items()}
            for k, v in data.get('unknown_ids', {}).items():
                found.setdefault(k, []).extend(v)
            scanned = data.get('files_scanned', 0)
        except (OSError, ValueError) as exc:
            return {'status': 'unavailable', 'reason': str(exc)}
    else:
        return {'status': 'unavailable', 'reason': 'no anchors.json in the arm and no --code directory given'}
    unknown = sorted((i for i in found if i not in records))
    anchored = {i: found[i] for i in sorted(found) if i in records}
    mechanisms = [r for r in store['records'] if r['kind'] == 'mechanism']
    reach = set(anchored)
    for ident, _ in relation_expansion(store['edges'], list(anchored), depth=1):
        reach.add(ident)
    return {'status': 'ok', 'source': source, 'files_scanned': scanned, 'anchored_records': anchored, 'unknown_ids_in_code': unknown, 'records_reachable_in_one_hop': len(reach), 'records_total': len(records), 'mechanisms_without_anchor': [m['id'] for m in mechanisms if m['id'] not in anchored], 'mechanisms_total': len(mechanisms), 'meaning': 'a listed anchor is a placed comment, not proof the code still matches the record'}

def trace(args, size, status, cached):
    target = os.environ.get('DG_LOG')
    if not target:
        return
    try:
        if Path(target).resolve().is_relative_to(args.root.resolve()):
            return
        row = {'at': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(timespec='seconds'), 'argv': sys.argv[1:], 'bytes': size, 'status': status, 'cached': cached}
        with open(target, 'a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
    except OSError:
        pass

def cache_lookup(cache_dir, key):
    if cache_dir is None:
        return (None, None)
    path = Path(cache_dir) / (key + '.json')
    if path.is_file():
        return (path, path.read_bytes())
    return (path, None)

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', required=True, type=Path, help='arm directory containing store.json (+ policy.json, summaries.json, anchors.json)')
    parser.add_argument('--cache-dir', default=os.environ.get('DG_CACHE_DIR'), help='optional directory for byte-identical result reuse; never inside the arm')
    sub = parser.add_subparsers(dest='command', required=True)
    search = sub.add_parser('search', help='bounded lexical candidate cards (legacy)')
    search.add_argument('query')
    search.add_argument('--limit', type=int, default=10)
    showp = sub.add_parser('show', help='one record: full claim and typed links; --sources adds merged excerpts')
    showp.add_argument('id')
    showp.add_argument('--sources', action='store_true', help='include each cited source window once, with hashes')
    showp.add_argument('--limit', type=int, default=20, help='maximum links (1..20)')
    showp.add_argument('--reasons', action='store_true', help='full claims of one-hop reasons and corrections; bounded and paginated')
    showp.add_argument('--budget', type=int, help='with --reasons: UTF-8 byte limit (160..100000; default: policy budget)')
    showp.add_argument('--offset', type=int, default=0, help='with --reasons: neighbor page offset from next_offset')
    ctx = sub.add_parser('context', help='tier-0 index of what a question, ID, topic or code anchor reaches')
    ctx.add_argument('query', nargs='?')
    ctx.add_argument('--entry', action='append', default=[], help='opaque record ID seed (repeatable)')
    ctx.add_argument('--file', action='append', default=[], help='code file whose `rivet: r-…` (legacy `context:` also accepted) anchors seed the index')
    ctx.add_argument('--topic', help='seed with every record of one topic')
    ctx.add_argument('--budget', type=int, help='override the policy byte budget (160..100000)')
    ctx.add_argument('--before', help='only records recorded on or before this date (YYYY-MM-DD); unknown dates kept')
    ctx.add_argument('--expand', action='store_true', help='after the lexical hits, list what applies_to a matched mechanism and what is one maintains/requires hop from a matched invariant')
    sub.add_parser('topics', help='every topic with counts, date span and digest')
    cur = sub.add_parser('current', help='follow supersedes edges from ID to the newest record: chain as index lines, then its card')
    cur.add_argument('id')
    cur.add_argument('--limit', type=int, default=20, help="maximum links on the newest record's card (1..20)")
    cur.add_argument('--budget', type=int, help='byte budget (160..100000; default: policy budget)')
    tl = sub.add_parser('timeline', help='dated text view: the decisions, proposals, judgments and requirements a question, topic or ID reaches, with their supersedes/qualifies/affirms/motivates neighbours, oldest first, and the newest record on each supersedes chain')
    tl.add_argument('query', nargs='?')
    tl.add_argument('--entry', action='append', default=[], help='opaque record ID seed (repeatable)')
    tl.add_argument('--topic', help='seed with every record of one topic')
    tl.add_argument('--budget', type=int, help='byte budget (160..100000; default: policy budget)')
    imp = sub.add_parser('impact', help='what the anchored records maintain and who requires it (refactoring view)')
    imp.add_argument('--entry', action='append', default=[], help='record ID (repeatable)')
    imp.add_argument('--file', action='append', default=[], help='code file whose anchors are the seeds')
    imp.add_argument('--budget', type=int, help='byte budget for the impact view (default: policy budget)')
    imp.add_argument('--format', choices=('json', 'text'), default='json', help='text: one line per condition and dependent')
    anch = sub.add_parser('anchors', help='which records have code entry points, which mechanisms have none')
    anch.add_argument('--code', action='append', help="scan these directories (repeatable) instead of the arm's anchors.json")
    sub.add_parser('stats', help='store counts and source coverage')
    asp = sub.add_parser('asof', help='rebuild a document from its part_of clauses as of a date; --diff DATE2 for a unified diff')
    asp.add_argument('--doc', required=True, help='document record id, exact title, or title prefix (e.g. ADR-002)')
    asp.add_argument('--date', help='YYYY-MM-DD: clauses recorded on or before it, less those removed on or before it (required without --commit; with --commit, optional: the day rule inside that store)')
    asp.add_argument('--diff', dest='other', help='YYYY-MM-DD: show the unified diff from --date to this date')
    asp.add_argument('--format', choices=('json', 'text'), default='json', help='text: the rebuilt document (or the diff) alone')
    asp.add_argument('--commit', help="a commit of the repository holding --root; the capture as committed there is rebuilt in a temporary directory and read instead of --root's store (unavailable, exit 1, when that commit holds no capture); text output names the commit on stderr")
    asp.add_argument('--records', help="with --commit: the records file's path inside the repository (default: the arm's sibling records.json)")
    asp.add_argument('--sources', help="with --commit: the source packet's path inside the repository (default: the arm's sibling sources)")
    asp.add_argument('--code', action='append', help="with --commit: a code tree inside the repository for code/ citations and anchors, repeatable (default: the trees the arm's anchors.json names)")
    rnd = sub.add_parser('render', help='a document as of a date as text: adr (clauses with provenance), changelog (each change, the record that made it and its reasons) or faq (clauses with their considerations), byte-bounded')
    rnd.add_argument('--doc', required=True, help='document record id, exact title, or title prefix (e.g. ADR-002)')
    rnd.add_argument('--as-of', dest='as_of', required=True, help='YYYY-MM-DD: the date the document is rendered as of')
    rnd.add_argument('--form', required=True, choices=RENDER_FORMS)
    rnd.add_argument('--budget', type=int, help='byte budget (160..100000; default: policy budget)')
    args = parser.parse_args(argv)
    store = read_store(args.root)
    if 'error' in store:
        emit({'status': 'unavailable', 'reason': store['error']})
        return
    by_id = {r['id']: r for r in store['records']}
    summaries = read_summaries(args.root, by_id)
    cache_dir = args.cache_dir
    if cache_dir and Path(cache_dir).resolve().is_relative_to(args.root.resolve()):
        emit({'status': 'unavailable', 'reason': 'cache dir must be outside the arm directory'})
        return
    key_material = [store['_sha256'], hashlib.sha256(encoded(summaries)).hexdigest(), encoded(policy(args.root)).decode(), *(argv if argv is not None else sys.argv[1:])]
    cache_path, hit = cache_lookup(cache_dir, hashlib.sha256('\x00'.join(key_material).encode()).hexdigest())
    if hit is not None and args.command in ('search', 'show', 'context', 'topics', 'stats', 'current', 'timeline', 'asof', 'render') and (not getattr(args, 'commit', None)):
        trace(args, len(hit), 'cached', True)
        sys.stdout.write(hit.decode('utf-8'))
        return
    if args.command == 'stats':
        try:
            result = read_json(args.root / 'metadata.json')
            result['summaries'] = {'present': summaries['present'], 'records': len(summaries['records']), 'topics': len(summaries['topics']), 'stale': summaries['stale']}
        except (OSError, ValueError) as exc:
            result = {'status': 'unavailable', 'reason': str(exc)}
    elif args.command == 'search':
        limit = max(1, min(args.limit, 20))
        ranked = ranking(store['records'], args.query)
        result = {'status': 'ok' if ranked else 'no_results', 'total_candidates': len(ranked), 'cards': [card(by_id[ident]) for ident in ranked[:limit]], 'omitted': max(0, len(ranked) - limit)}
    elif args.command == 'show':
        if args.reasons:
            configuration = policy(args.root)
            budget = args.budget if args.budget is not None else configuration.get('byte_budget') if 'error' not in configuration else DEFAULT_BUDGET
            if not 160 <= budget <= 100000:
                parser.error('--budget must be 160..100000')
            if args.limit != 20:
                parser.error('--limit is not supported with --reasons; use --budget and --offset')
            result = reason_bundle(store, args.id, args.sources, budget, args.offset)
        else:
            if args.budget is not None or args.offset != 0:
                parser.error('show --budget and --offset require --reasons')
            result = show(store, summaries, args.id, args.sources, max(1, min(args.limit, 20)))
    elif args.command == 'topics':
        result = topics(store, summaries)
    elif args.command == 'current':
        configuration = policy(args.root)
        budget = args.budget or (configuration.get('byte_budget') if 'error' not in configuration else DEFAULT_BUDGET)
        if not 160 <= budget <= 100000:
            parser.error('--budget must be 160..100000')
        result = current(store, summaries, args.id, max(1, min(args.limit, 20)), budget)
    elif args.command == 'anchors':
        result = anchors(store, args.root, args.code)
    elif args.command == 'asof':
        if args.date is None and (not args.commit):
            asp.error('the following arguments are required: --date')
        if args.commit and args.other is not None:
            asp.error('--diff cannot be combined with --commit')
        for value in (args.date, args.other):
            if value is not None:
                try:
                    __import__('datetime').date.fromisoformat(value)
                    if len(value) != 10:
                        raise ValueError(value)
                except ValueError:
                    parser.error('--date and --diff must be YYYY-MM-DD')
        if args.commit:
            result = asof_committed(args.root, args.doc, args.commit, args.date, args.records, args.sources, args.code)
        else:
            result = asof(store, args.doc, args.date, args.other)
    elif args.command == 'render':
        try:
            __import__('datetime').date.fromisoformat(args.as_of)
            if len(args.as_of) != 10:
                raise ValueError(args.as_of)
        except ValueError:
            parser.error('--as-of must be YYYY-MM-DD')
        configuration = policy(args.root)
        budget = args.budget or (configuration.get('byte_budget') if 'error' not in configuration else DEFAULT_BUDGET)
        if not 160 <= budget <= 100000:
            parser.error('--budget must be 160..100000')
        result = render(store, summaries, args.doc, args.as_of, args.form, budget)
    elif args.command == 'timeline':
        configuration = policy(args.root)
        budget = args.budget or (configuration.get('byte_budget') if 'error' not in configuration else DEFAULT_BUDGET)
        if not 160 <= budget <= 100000:
            parser.error('--budget must be 160..100000')
        if not (args.query or args.entry or args.topic):
            parser.error('timeline requires a query, --entry or --topic')
        unknown = sorted((i for i in args.entry if i not in by_id))
        if unknown:
            result = {'status': 'unavailable', 'reason': 'unknown_entry', 'unknown': unknown, 'byte_budget': budget}
        else:
            seed_note = 'topic:' + args.topic if args.topic else 'entry:' + ','.join(args.entry) if args.entry else 'question'
            result = timeline(store, budget, args.query, args.entry, args.topic, seed_note)
    elif args.command == 'impact':
        seeds = list(args.entry)
        for name in args.file:
            try:
                seeds.extend(anchor_ids(name))
            except OSError:
                seeds = None
                break
        if seeds is None:
            result = {'status': 'unavailable', 'reason': 'unreadable anchor file'}
        elif not seeds:
            result = {'status': 'no_results', 'reason': 'no seeds: give --entry or a --file with context anchors'}
        else:
            configuration = policy(args.root)
            budget = args.budget or (configuration.get('byte_budget') if 'error' not in configuration else DEFAULT_BUDGET)
            known = {}
            if (args.root / 'anchors.json').exists():
                try:
                    known = read_json(args.root / 'anchors.json').get('anchors', {})
                except (OSError, ValueError):
                    known = {}
            result = impact(store, summaries, list(dict.fromkeys(seeds)), args.file, budget, known)
    else:
        configuration = policy(args.root)
        if 'error' in configuration:
            result = {'status': 'unavailable', 'reason': configuration['error']}
        else:
            budget = configuration['byte_budget']
            if args.budget is not None:
                if not 160 <= args.budget <= 100000:
                    parser.error('--budget must be 160..100000')
                budget = args.budget
            entries = list(args.entry)
            seed_note = 'question'
            if args.before:
                try:
                    __import__('datetime').date.fromisoformat(args.before)
                except ValueError:
                    parser.error('--before must be YYYY-MM-DD')
                store = bounded(store, args.before)
                by_id = {r['id']: r for r in store['records']}
            missing_files = []
            for name in args.file:
                try:
                    entries.extend(anchor_ids(name))
                except OSError:
                    missing_files.append(name)
            if missing_files:
                result = {'status': 'unavailable', 'reason': 'unreadable anchor file', 'files': missing_files}
            elif args.file and (not entries):
                result = {'status': 'no_results', 'reason': 'no context anchors found in file', 'files': args.file, 'next': 'search or context with a question; an absent anchor is not proof of no constraint'}
            elif not (args.query or entries or args.topic):
                parser.error('context requires a query, --entry, --file or --topic')
            elif any((i not in by_id for i in entries)):
                result = {'status': 'unavailable', 'reason': 'unknown_entry', 'unknown': sorted((i for i in entries if i not in by_id)), 'byte_budget': budget}
            elif args.expand and configuration['mode'] in ('lexical', 'relations') and (not args.file) and (not args.topic):
                result = {'status': 'unavailable', 'reason': "--expand needs the index tier; this arm's policy is " + configuration['mode']}
            elif configuration['mode'] in ('lexical', 'relations') and (not args.file) and (not args.topic):
                result = card_context(store, {'mode': configuration['mode'], 'byte_budget': budget}, args.query, entries[0] if entries else None)
            else:
                if args.file:
                    seed_note = 'anchors:' + ','.join(args.file)
                elif args.topic:
                    seed_note = 'topic:' + args.topic
                elif entries:
                    seed_note = 'entry:' + ','.join(entries)
                result = index_context(store, summaries, budget, args.query, entries, args.topic, 'index', seed_note, expand=args.expand)
                if args.before and result.get('status') in ('ok', 'no_results'):
                    result['before'] = args.before
    if args.command == 'impact' and args.format == 'text':
        payload = impact_text(result).encode('utf-8')
    elif args.command == 'timeline':
        payload = timeline_text(result).encode('utf-8')
    elif args.command == 'asof' and args.format == 'text':
        payload = asof_render(result).encode('utf-8')
    elif args.command == 'render':
        payload = render_text(result).encode('utf-8')
    else:
        payload = encoded(result) + b'\n'
    trace(args, len(payload), result.get('status'), hit is not None)
    committed = getattr(args, 'commit', None) and result.get('committed')
    if cache_path is not None and args.command != 'anchors' and (not getattr(args, 'commit', None)):
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(payload)
        except OSError:
            pass
    sys.stdout.write(payload.decode('utf-8'))
    if committed and args.format == 'text':
        sys.stderr.write(f"asof --commit {committed['commit']}: records {committed['records']} sources {committed['sources']} build.py {committed['build_py_sha256'][:12]}\n")
    if getattr(args, 'commit', None) and result.get('status') != 'ok':
        sys.exit(1)
if __name__ == '__main__':
    main()
