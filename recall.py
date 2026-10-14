#!/usr/bin/env python3
"""Deterministic, byte-bounded recall from a decision-store.v1 graph.

Public API: recall(store, *, seeds=None, query=None, profile='all', depth=1,
                   limit=10, budget=6000, sources=False).
`store` may be a decoded store, a store.json path, or its directory.
"""

import argparse
import json
import math
import re
import shlex
from collections import Counter, deque
from pathlib import Path


KINDS = frozenset("mechanism requirement proposal judgment decision evidence implementation review invariant gap document".split())
RELATIONS = frozenset("supports motivates qualifies supersedes affirms implements applies_to requires maintains would_overturn depends_on_premise part_of deprecates".split())
PROFILES = {
    "all": (KINDS, RELATIONS),
    "review": (frozenset("decision proposal judgment requirement invariant gap review evidence document".split()),
               frozenset("supports motivates qualifies supersedes affirms requires depends_on_premise would_overturn part_of deprecates".split())),
    "reasoning": (frozenset("decision proposal judgment requirement invariant gap evidence review".split()),
                  frozenset("supports motivates qualifies supersedes affirms requires depends_on_premise would_overturn deprecates".split())),
    "modification": (frozenset("decision requirement invariant gap mechanism implementation document judgment evidence".split()),
                     frozenset("qualifies supersedes affirms implements applies_to requires maintains depends_on_premise would_overturn part_of deprecates".split())),
}
WARN = frozenset(("supersedes", "deprecates", "qualifies", "depends_on_premise", "would_overturn"))
CORRECTION = frozenset(("supersedes", "deprecates"))
TOKEN = re.compile(r"\w+", re.UNICODE)
STOP = frozenset("a an and are as at be by for from has have how in into is it of on or that the their these this to was were what when where which who why with".split())


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def load_store(store):
    if isinstance(store, dict):
        data = store
    else:
        path = Path(store)
        data = json.loads((path / "store.json" if path.is_dir() else path).read_text(encoding="utf-8"))
    if data.get("schema") != "decision-store.v1" or not isinstance(data.get("records"), list) or not isinstance(data.get("edges"), list):
        raise ValueError("expected decision-store.v1 with records and edges")
    ids = [r["id"] for r in data["records"]]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate record ID")
    if any(e["from"] not in ids or e["to"] not in ids for e in data["edges"]):
        raise ValueError("edge has unknown endpoint")
    return data


def lexical_ids(records, query):
    """One profile-independent ranking, with no task-specific weights."""
    terms = set(TOKEN.findall(query.lower())) - STOP
    if not terms:
        return []
    frequency = Counter()
    for record in records:
        words = set(TOKEN.findall(" ".join(str(record[k]) for k in ("title", "topic", "claim", "kind")).lower()))
        frequency.update(words & terms)
    idf = {term: math.log((len(records) + 1) / (frequency[term] + 1)) + 1 for term in terms}
    ranked = []
    for record in records:
        score = 0.0
        for field, weight in (("title", 4), ("topic", 3), ("claim", 2), ("kind", 1)):
            words = TOKEN.findall(record[field].lower())
            score += weight * sum(words.count(term) * idf[term] for term in terms)
        if score:
            ranked.append((-score, record["id"]))
    ranked.sort()
    return [ident for _, ident in ranked]


def edge_view(edge, include_sources=False):
    result = {key: edge[key] for key in ("from", "relation", "to", "note")}
    for optional in ("order", "operator_mark"):
        if optional in edge:
            result[optional] = edge[optional]
    result["sources"] = (edge.get("sources", []) if include_sources else [
        {key: span[key] for key in ("path", "start_line", "end_line")}
        for span in edge.get("sources", [])])
    return result


def card(record, sources=False):
    result = {key: record[key] for key in ("id", "kind", "topic", "title", "claim", "actor", "authority", "state", "recorded_at")}
    result["sources"] = record.get("sources", []) if sources else [
        {key: span[key] for key in ("path", "start_line", "end_line")} for span in record.get("sources", [])]
    if "operator_mark" in record:
        result["operator_mark"] = record["operator_mark"]
    return result


def graph_discovery(data, roots, profile, depth):
    by_id = {r["id"]: r for r in data["records"]}
    kinds, relations = PROFILES[profile]
    adjacency = {ident: [] for ident in by_id}
    for index, edge in enumerate(data["edges"]):
        adjacency[edge["from"]].append((edge["to"], index))
        adjacency[edge["to"]].append((edge["from"], index))
    for entries in adjacency.values():
        entries.sort(key=lambda pair: (data["edges"][pair[1]]["relation"], pair[0], pair[1]))
    found = {ident: [] for ident in roots}
    queue = deque((ident, 0) for ident in roots)
    limits = Counter()
    filtered = set()
    boundary = set()
    while queue:
        ident, distance = queue.popleft()
        for other, index in adjacency[ident]:
            edge = data["edges"][index]
            if edge["relation"] not in relations:
                filtered.add((index, "filtered_relation"))
                continue
            if by_id[other]["kind"] not in kinds:
                filtered.add((index, "filtered_kind"))
                continue
            if distance >= depth:
                if other not in found:
                    boundary.add(index)
                continue
            if other not in found:
                found[other] = found[ident] + [index]
                queue.append((other, distance + 1))
    limits.update(cause for _, cause in filtered)
    limits["depth_limited"] = len(boundary)
    return found, adjacency, limits


def warning_closure(data, found, adjacency):
    """Visible incident warnings, then forward correction chains from each reached item."""
    warnings = {}
    extra = set()
    queue = deque(found)
    visited = set()
    while queue:
        ident = queue.popleft()
        if ident in visited:
            continue
        visited.add(ident)
        for other, index in adjacency[ident]:
            edge = data["edges"][index]
            if edge["relation"] not in WARN:
                continue
            warnings[index] = edge
            if other not in found:
                extra.add(other)
            # A replacement points from new to old. From an old record, follow
            # incoming corrections through every successive replacement.
            if edge["relation"] in CORRECTION and edge["to"] == ident:
                queue.append(edge["from"])
    return warnings, extra


def recall(store, *, seeds=None, query=None, profile="all", depth=1, limit=10, budget=6000,
           sources=False, warning_offset=0):
    if profile not in PROFILES:
        raise ValueError("unknown profile")
    if type(depth) is not int or depth < 0 or type(limit) is not int or limit < 1:
        raise ValueError("depth must be >= 0 and limit >= 1")
    if type(budget) is not int or budget < 300:
        raise ValueError("budget must be at least 300 bytes")
    if type(warning_offset) is not int or warning_offset < 0:
        raise ValueError("warning_offset must be >= 0")
    if (seeds is None) == (query is None):
        raise ValueError("supply exactly one of seeds or query")
    data = load_store(store)
    by_id = {r["id"]: r for r in data["records"]}
    candidate = list(dict.fromkeys(seeds)) if seeds is not None else lexical_ids(data["records"], query)
    unknown = [ident for ident in candidate if ident not in by_id]
    roots = [ident for ident in candidate if ident in by_id]
    if query is not None:
        roots = roots[:limit]
    query_limited = max(0, len(candidate) - len(unknown) - len(roots)) if query is not None else 0
    found, adjacency, limits = graph_discovery(data, roots, profile, depth)
    warnings, extra = warning_closure(data, found, adjacency)
    counts = {
        "matched_roots": len(candidate) - len(unknown), "selected_roots": len(roots),
        "discovered": len(found), "warning_edges": len(warnings), "warning_endpoints": len(extra),
        "filtered_kind": limits["filtered_kind"], "filtered_relation": limits["filtered_relation"],
        "depth_limited": limits["depth_limited"], "query_limited": query_limited,
        "unknown_seeds": len(unknown), "budget_omitted": 0,
        "warning_rows_omitted": 0, "warning_details_omitted": 0, "cards_omitted": 0,
    }
    store_name = str(store) if not isinstance(store, dict) else "STORE"
    next_input = ("".join(" --seed " + shlex.quote(ident) for ident in candidate) if seeds is not None else
                  " --query " + shlex.quote(query or "QUERY") + " --limit " + str(limit))
    same_search = ("python3 recall.py --store " + shlex.quote(store_name) + " --profile " + profile +
                   " --depth " + str(depth) + " --budget " + str(max(budget * 4, 24000)) + next_input)
    broader_search = ("python3 recall.py --store " + shlex.quote(store_name) + " --profile all --depth " +
                      str(max(depth + 1, 2)) + " --budget " + str(max(budget * 4, 24000)) + next_input)
    output = {"schema": "decision-recall.v1", "profile": profile, "depth": depth, "roots": roots,
              "unknown_seeds": unknown, "records": [], "warnings": [], "omitted": [],
              "counts": counts, "complete": False, "follow_up": broader_search,
              "warning_page": {"offset": warning_offset, "next_offset": None}}
    # Root cards receive capacity before warning details. Reserve a small margin
    # for counters and the page cursor, whose decimal length can grow later.
    cap = budget - 64
    ordered = roots + [ident for ident in found if ident not in roots] + [
        ident for ident in sorted(extra) if ident not in found]

    def add_card(ident):
        reason = "traversal" if ident in found else "warning_endpoint"
        path = [edge_view(data["edges"][i], sources) for i in found.get(ident, [])]
        row = {"card": card(by_id[ident], sources), "reason": reason, "path": path}
        if ident in roots:
            row["reason"] = "root"
            if by_id[ident]["kind"] not in PROFILES[profile][0]:
                row["filtered_root_kind"] = True
        output["records"].append(row)
        if len(encoded(output)) > cap:
            output["records"].pop()
            counts["budget_omitted"] += 1
            counts["cards_omitted"] += 1
            omission = {"id": ident, "reason": "byte_budget", "category": reason}
            output["omitted"].append(omission)
            if len(encoded(output)) > cap:
                output["omitted"].pop()

    for ident in roots:
        add_card(ident)
    warning_order = sorted(warnings, key=lambda i: (
        warnings[i]["from"], warnings[i]["relation"], warnings[i]["to"], warnings[i]["note"], i))
    warning_page = warning_order[warning_offset:]
    shown = 0
    first_compact = None
    for index in warning_page:
        edge = warnings[index]
        warning = edge_view(edge, sources)
        output["warnings"].append(warning)
        if len(encoded(output)) > cap:
            output["warnings"].pop()
            # Keep endpoint IDs and relation when the full edge note does not fit.
            compact = {key: edge[key] for key in ("from", "relation", "to")}
            output["warnings"].append(compact)
            if len(encoded(output)) > cap:
                output["warnings"].pop()
                break
            counts["warning_details_omitted"] += 1
            if first_compact is None:
                first_compact = warning_offset + shown
        shown += 1
    output["warning_page"]["next_offset"] = (
        warning_offset + shown if warning_offset + shown < len(warning_order) else None)
    counts["warning_rows_omitted"] = max(0, len(warning_page) - shown)
    counts["budget_omitted"] += counts["warning_rows_omitted"]
    detail_or_page = first_compact if first_compact is not None else output["warning_page"]["next_offset"]
    if detail_or_page is not None:
        output["follow_up"] = same_search + " --warning-offset " + str(detail_or_page)
    for ident in ordered:
        if ident not in roots:
            add_card(ident)
    output["complete"] = warning_offset == 0 and not any(counts[key] for key in
                                 ("filtered_kind", "filtered_relation", "depth_limited", "query_limited",
                                  "unknown_seeds", "budget_omitted", "warning_details_omitted"))
    if len(encoded(output)) > budget:
        # An unusually long supplied path or seed can exceed the minimum envelope.
        output = {"schema": "decision-recall.v1", "error": "budget_too_small_for_envelope",
                  "required_bytes": len(encoded(output)), "budget": budget}
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--seed", action="append", dest="seeds")
    group.add_argument("--query")
    parser.add_argument("--profile", choices=sorted(PROFILES), default="all")
    parser.add_argument("--depth", type=int, default=1)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--budget", type=int, default=6000)
    parser.add_argument("--sources", action="store_true")
    parser.add_argument("--warning-offset", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        result = recall(args.store, seeds=args.seeds, query=args.query, profile=args.profile,
                        depth=args.depth, limit=args.limit, budget=args.budget, sources=args.sources,
                        warning_offset=args.warning_offset)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    print(encoded(result).decode("utf-8"))
    return 1 if "error" in result else 0


if __name__ == "__main__":
    raise SystemExit(main())
