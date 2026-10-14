---
name: rivet
description: Recall and maintain engineering context when working with code annotated with Rivet record IDs, including decisions, requirements, evidence and corrections.
---

# Rivet

## Recall

Read the function or region you are trying to understand. Select the relevant nearby
`rivet:` IDs, including applicable class or file constraints. Legacy `context:` IDs
work too. Use exact IDs as roots; collecting every ID in a file is an optional audit,
not the normal first step. Opaque IDs do not themselves explain relevance: inspect
their claims before deciding which threads matter.

From the toolkit directory, with a built store at STORE, the compact lookup is:

```sh
python3 rivet.py --store STORE context ID [ID ...]
```

This returns current recorded claims first, then correction history, linked context,
authority and evidence references. It replaces the routine index → show → history
sequence. If it answers the question, avoid fetching the same claims again. Follow its
NEXT command when the selected scope is paginated; scope completeness is not a claim
that every relevant fact was captured.

If the task already needs proof for several linked claims, request context and captured
excerpts together on the first lookup:

```sh
python3 rivet.py --store STORE context ID [ID ...] --sources --budget 12000
```

Keep the roots relevant. The larger cap can avoid an extra page and reasoning turn;
it is a maximum, not a target. Follow NEXT if the selected scope still needs more pages.
For a specific evidence check, fetch only the needed source with
`python3 rivet.py --store STORE evidence SOURCE_ID`. Citation paths are relative
to the original capture source root; evidence lookup reads frozen bytes from the store
without guessing a live filesystem path. A captured excerpt does not prove current
production state. Claims and excerpts are never silently truncated; an oversized item
reports the required budget.

For wider dependencies or category filtering, use `python3 recall.py --store STORE
--seed ID --profile modification --depth 1`, or `dg.py --root STORE impact --entry ID`.
For historical approval, retain its original target; a replacement does not inherit it.

Choose depth and categories for the question. Follow omission notices, unresolved
replacements and evidence limits. If a filter may hide a needed path, widen to profile
`all` or inspect its endpoints directly. If nearby anchors do not answer the question,
search the store and maintained documents. Missing anchors or empty results do not
establish that no constraint exists.

## Capture

Record a concise scoped claim with its source, actor, authority and provisional status
where applicable. Link separate evidence, permission or rationale when it has its own
meaning or lifecycle. Use only relationships warranted by the evidence.

Create the record first with `dg_write.py add`; use the returned accepted ID in a
`rivet:` comment near the code it constrains. Today the writer requires `--label` and
derives the ID itself; do not calculate IDs yourself. Automatic allocation without a
label is not implemented. A successful write is not human approval, and does not mean
the source comment has been inserted.

Use `amend --supersedes ID` for a replacement, preserving the old assertion. Evidence
and affirmation can be new linked records. Retain a useful original anchor when its
replacement chain explains the current choice. When moving code, review whether each
anchor still applies. See ../../README.md for capture fields, validation and store builds.
