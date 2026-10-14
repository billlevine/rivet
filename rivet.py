#!/usr/bin/env python3
"""Rivet exact-ID context and captured evidence. Read-only; no inferred approvals."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex

CORRECTIONS = {'supersedes', 'qualifies', 'affirms'}

def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

def anchor_ids(path):
    return list(dict.fromkeys(ident for line in Path(path).read_text().splitlines()
                             if re.search(r'\b(?:rivet|context):', line)
                             for ident in re.findall(r'\br-[0-9a-f]{16}\b', re.split(r'\b(?:rivet|context):', line, maxsplit=1)[1])))

def select(store, seeds):
    records = {r['id']: r for r in store['records']}
    if len(records) != len(store['records']):
        raise ValueError('duplicate record IDs')
    if any(s not in records for s in seeds):
        raise ValueError('unknown entry ID')
    if any(e['from'] not in records or e['to'] not in records for e in store['edges']):
        raise ValueError('edge endpoint is missing')
    edges = store['edges']
    def closure(start, relations):
        found = set(start)
        while True:
            extra = {e[k] for e in edges if e['relation'] in relations and
                     (e['from'] in found or e['to'] in found) for k in ('from', 'to')}
            if extra <= found: return found
            found |= extra
    # Replacement chains are retained even when the source comment names an old judgment.
    spine = closure(seeds, {'supersedes'})
    neighbors = spine | {e[k] for e in edges if e['from'] in spine or e['to'] in spine for k in ('from', 'to')}
    selected = closure(neighbors, CORRECTIONS)
    ordered = sorted(selected, key=lambda ident: (ident not in spine, records[ident]['recorded_at'], ident))
    relevant_edges = [e for e in edges if e['from'] in selected and e['to'] in selected]
    boundary = sum((e['from'] in selected) != (e['to'] in selected) for e in edges)
    successors = {ident: [] for ident in records}
    for e in edges:
        if e['relation'] == 'supersedes': successors[e['to']].append(e['from'])
    tips = {}
    for seed in seeds:
        visited = set(); active = set(); leaves = set(); cyclic = False
        def walk(ident):
            nonlocal cyclic
            if ident in active: cyclic = True; return
            if ident in visited: return
            visited.add(ident); active.add(ident)
            if not successors[ident]: leaves.add(ident)
            for nxt in successors[ident]: walk(nxt)
            active.remove(ident)
        walk(seed)
        tips[seed] = {'recorded_tips': sorted(leaves), 'cycle': cyclic,
                      'ambiguous': cyclic or len(leaves) != 1}
    current = {ident for tip in tips.values() for ident in tip['recorded_tips']}
    ordered.sort(key=lambda ident: (ident not in current, ident not in spine, records[ident]['recorded_at'], ident))
    return records, ordered, relevant_edges, boundary, tips

def receipt(store, seeds, offset=0, count=20, sources=False, store_path="STORE"):
    evidence_index = source_index(store)
    records, ordered, edges, boundary, tips = select(store, seeds)
    if offset < 0 or count < 1 or offset >= len(ordered):
        raise ValueError('invalid page')
    chosen = ordered[offset:offset+count]
    # Emit an edge only once: with the first endpoint in the fixed global record order.
    rank = {ident: n for n, ident in enumerate(ordered)}
    page_edges = [e for e in edges if min((e['from'], e['to']), key=rank.get) in chosen]
    references = {}; artifacts = {}
    def refs(spans):
        ids = []
        for span in spans:
            summary = {k: span[k] for k in ('path', 'start_line', 'end_line', 'sha256') if k in span}
            ident = source_id(summary)
            artifact = {k: summary[k] for k in ('path','sha256') if k in summary}
            aid = 'a-' + hashlib.sha256(encode(artifact).encode()).hexdigest()[:10]
            artifacts[aid] = artifact
            references[ident] = {'artifact': aid, 'start_line': summary['start_line'], 'end_line': summary['end_line']}
            if sources:
                references[ident]['excerpt'] = evidence_index[ident].get('excerpt')
                references[ident]['excerpt_status'] = 'captured' if isinstance(evidence_index[ident].get('excerpt'), str) else 'unavailable'
            ids.append(ident)
        return ids
    cards = []
    for ident in chosen:
        r = records[ident]
        cards.append({**{k: r[k] for k in ('id','kind','state','authority','actor','recorded_at','claim')},
                      'sources': refs(r.get('sources', []))})
    links = [{**{k: e[k] for k in ('from','relation','to','note')},
              'sources': refs(e.get('sources', []))} for e in page_edges]
    end = offset + len(chosen)
    return {'schema': 'rivet-context.v1', 'entries': seeds, 'tips': tips,
            'scope': 'replacement chains, incident context, then correction/affirmation closure',
            'records': cards, 'relationships': links, 'sources': references, 'artifacts': artifacts,
            'source_lookup': f'python3 rivet.py --store {shlex.quote(store_path)} evidence SOURCE_ID',
            'source_paths': 'Relative to the capture source root, not the current working directory. Evidence lookup reads stored bytes, never live files.',
            'follow_up': (context_command(store_path, seeds, end, count, sources) if end < len(ordered) else None),
            'page': {'offset': offset, 'next_offset': end if end < len(ordered) else None,
                     'total_records': len(ordered), 'total_relationships': len(edges),
                     'scope_complete': offset == 0 and end == len(ordered), 'outside_scope_edges': boundary},
            'note': 'Recorded assertions, not proof of deployment. Approval of a prior record does not approve its replacement.'}

def render(data):
    if 'error' in data: return encode(data) + '\n'
    lines = ['RIVET CONTEXT — current recorded claims first; correction history follows']
    for entry, tip in data['tips'].items():
        lines.append(entry + ' -> ' + ('AMBIGUOUS ' if tip['ambiguous'] else '') + ', '.join(tip['recorded_tips']))
    for r in data['records']:
        lines += [f"\n{r['id']} | {r['recorded_at']} | {r['kind']}/{r['state']} | {r['authority']}:{r['actor']}",
                  r['claim'] + ' [' + ','.join(r['sources']) + ']']
    lines += ['\nRELATIONSHIPS']
    for e in data['relationships']:
        lines.append(f"{e['from']} --{e['relation']}--> {e['to']}: {e['note']} [" + ','.join(e['sources']) + ']')
    lines += ['\nEVIDENCE', data['source_paths'], data['source_lookup']]
    for aid, artifact in data['artifacts'].items():
        lines.append(f"{aid} {artifact['path']} sha256={artifact.get('sha256', 'unavailable')}")
    for ident, s in data['sources'].items():
        lines.append(f"{ident} {s['artifact']}:{s['start_line']}-{s['end_line']}")
        if 'excerpt' in s:
            lines.append(s['excerpt'] if s['excerpt_status']=='captured' else '[captured excerpt unavailable]')
    lines += ['\nPAGE ' + encode(data['page']), data['note']]
    if data['follow_up']: lines.append('NEXT: '+data['follow_up'])
    return '\n'.join(lines) + '\n'

def bounded(store, seeds, budget, offset=0, count=20, fmt='text', sources=False, store_path='STORE'):
    if budget < 300: raise ValueError('budget must be at least 300 bytes')
    if count < 1: raise ValueError('count must be positive')
    for n in range(count, 0, -1):
        result = receipt(store, seeds, offset, n, sources, store_path)
        if result['follow_up']:
            # A large record may shrink this page; later pages keep the caller's
            # original count allowance and are fitted to the byte cap afresh.
            result['follow_up'] = context_command(store_path, seeds, result['page']['next_offset'], count, sources)
            result['follow_up'] += f' --budget {budget} --format {fmt}'
        text = render(result) if fmt == 'text' else encode(result)+'\n'
        if len(text.encode()) <= budget: return result, text
    # Never truncate claims or suppress the existence of omitted context.
    result = {'error': 'one_record_and_its_relationships_exceed_budget', 'offset': offset,
              'required_bytes': len(text.encode()), 'budget': budget}
    return result, encode(result)+'\n'

def source_id(span):
    summary = {k: span[k] for k in ('path', 'start_line', 'end_line', 'sha256') if k in span}
    return 's-' + hashlib.sha256(encode(summary).encode()).hexdigest()[:16]

def source_index(store):
    index = {}
    for item in store['records'] + store['edges']:
        for span in item.get('sources', []):
            ident = source_id(span)
            if ident in index and index[ident] != span:
                raise ValueError('conflicting captured source spans: '+ident)
            index[ident] = span
    return index

def context_command(store_path, seeds, offset, count, sources):
    command = 'python3 rivet.py --store '+shlex.quote(store_path)+' context '
    command += ' '.join(shlex.quote(s) for s in seeds)
    command += f' --offset {offset} --count {count}'
    return command + (' --sources' if sources else '')

def evidence(store, ident, budget=6000, fmt='text'):
    if budget < 300: raise ValueError('budget must be at least 300 bytes')
    span = source_index(store).get(ident)
    if span is None: raise ValueError('unknown evidence ID: '+ident)
    result = {'schema': 'rivet-evidence.v1', 'id': ident, **span,
              'status': 'captured' if isinstance(span.get('excerpt'), str) else 'unavailable',
              'path_base': 'capture source root; no live file was read'}
    text = encode(result)+'\n'
    if fmt == 'text':
        text = (f"{ident} {span['path']}:{span['start_line']}-{span['end_line']} "
                f"sha256={span.get('sha256', 'unavailable')}\n"+result['path_base']+'\n'+
                (span['excerpt'] if result['status']=='captured' else '[captured excerpt unavailable]')+'\n')
    if len(text.encode()) > budget:
        result = {'error':'evidence_exceeds_budget','required_bytes':len(text.encode()),'budget':budget}
        text = encode(result)+'\n'
    return result, text

def find_evidence(store, query, budget=6000, offset=0, count=20, fmt='text', store_path='STORE'):
    """Literal search of captured spans; ownership enables a separate context check."""
    if not query.strip(): raise ValueError('query must not be blank')
    if budget < 300: raise ValueError('budget must be at least 300 bytes')
    if count < 1 or offset < 0: raise ValueError('invalid page')
    index = source_index(store)
    matches = sorted((sid for sid, span in index.items()
                      if isinstance(span.get('excerpt'), str) and query in span['excerpt']),
                     key=lambda sid: (index[sid]['path'], index[sid]['start_line'], index[sid]['end_line'], sid))
    if offset and offset >= len(matches): raise ValueError('invalid page')
    unavailable = sum(not isinstance(span.get('excerpt'), str) for span in index.values())
    def packet(n):
        items = []
        for sid in matches[offset:offset+n]:
            owners = [{'id': r['id'], 'state': r['state']} for r in store['records']
                      if any(source_id(s) == sid for s in r.get('sources', []))]
            links = [{k: e[k] for k in ('from','relation','to')} for e in store['edges']
                     if any(source_id(s) == sid for s in e.get('sources', []))]
            roots = sorted({r['id'] for r in owners} or {e[k] for e in links for k in ('from','to')})
            items.append({'id': sid, **index[sid], 'record_refs': owners, 'relationship_refs': links,
                          'context': context_command(store_path, roots, 0, 20, False) if roots else None})
        end = offset + len(items)
        return {'schema': 'rivet-evidence-search.v1', 'query': query, 'match': 'case-sensitive literal substring',
                'scope': 'captured excerpts only; no live source files read', 'matches': items,
                'total_matches': len(matches), 'unavailable_spans': unavailable,
                'captured_text_fully_searchable': unavailable == 0,
                'page': {'offset': offset, 'next_offset': end if end < len(matches) else None},
                'follow_up': ('python3 rivet.py --store '+shlex.quote(store_path)+' find-evidence '+shlex.quote(query)+
                              f' --offset {end} --count {count} --budget {budget} --format {fmt}') if end < len(matches) else None,
                'note': 'A text match is historical evidence, not a current-policy verdict. Check linked context for corrections. No match does not establish absence outside this capture.'}
    def render_search(data):
        if fmt == 'json': return encode(data)+'\n'
        lines = [f"CAPTURED EVIDENCE SEARCH {query!r}: {data['total_matches']} matches; {unavailable} unavailable spans", data['scope']]
        for item in data['matches']:
            lines += [f"\n{item['id']} {item['path']}:{item['start_line']}-{item['end_line']} sha256={item.get('sha256','unavailable')}",
                      'RECORDS '+encode(item['record_refs']), 'RELATIONSHIPS '+encode(item['relationship_refs']),
                      item['excerpt']]
            if item['context']: lines.append('CONTEXT: '+item['context'])
        lines += ['PAGE '+encode(data['page']), data['note']]
        if data['follow_up']: lines.append('NEXT: '+data['follow_up'])
        return '\n'.join(lines)+'\n'
    for n in range(min(count, len(matches)-offset), -1, -1):
        if n == 0 and matches: break  # Never emit an empty page instead of a whole match.
        result = packet(n); text = render_search(result)
        if len(text.encode()) <= budget: return result, text
    result = {'error': 'evidence_search_item_exceeds_budget', 'offset': offset,
              'required_bytes': len(text.encode()), 'budget': budget}
    return result, encode(result)+'\n'

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--store', type=Path, required=True)
    sub = p.add_subparsers(dest='command', required=True)
    ctx = sub.add_parser('context', help='current claims, corrections and linked context for exact IDs')
    ctx.add_argument('entries', nargs='+')
    ctx.add_argument('--sources', action='store_true', help='include captured source excerpts')
    ctx.add_argument('--offset', type=int, default=0)
    ctx.add_argument('--count', type=int, default=20)
    ev = sub.add_parser('evidence', help='fetch captured bytes by source ID; never opens a live citation path')
    ev.add_argument('source_id')
    find = sub.add_parser('find-evidence', help='find case-sensitive literal text in captured excerpts')
    find.add_argument('query')
    find.add_argument('--offset', type=int, default=0)
    find.add_argument('--count', type=int, default=20)
    for child in (ctx, ev, find):
        child.add_argument('--budget', type=int, default=6000)
        child.add_argument('--format', choices=('text','json'), default='text')
    args = p.parse_args()
    path = args.store/'store.json' if args.store.is_dir() else args.store
    try:
        store = json.loads(path.read_text())
        if store.get('schema') != 'decision-store.v1': raise ValueError('expected decision-store.v1')
        if args.command == 'context':
            seeds = list(dict.fromkeys(args.entries))
            result,text = bounded(store,seeds,args.budget,args.offset,args.count,args.format,args.sources,str(args.store))
        elif args.command == 'evidence':
            result,text = evidence(store,args.source_id,args.budget,args.format)
        else:
            result,text = find_evidence(store,args.query,args.budget,args.offset,args.count,args.format,str(args.store))
    except (ValueError,KeyError,OSError) as exc:
        p.error(str(exc))
    print(text,end='')
    return 1 if 'error' in result or result.get('status')=='unavailable' else 0

if __name__=='__main__':raise SystemExit(main())
