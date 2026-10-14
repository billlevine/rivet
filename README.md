# Rivet

Recorded Intent, Verification, Evidence & Traceability.

A standard-library Python toolkit for recording engineering decisions with citations, building a validated read-only store, and recalling bounded views. This is a prototype with synthetic regression examples, not a production readiness claim.

Requires Python 3.10 or newer; validation was run on Python 3.14. No install is needed for the commands below. Core capture and recall use only the Python standard library. Git is needed only for checks against Git revisions and reconstruction from commits. Optional installation uses setuptools.

## Quickstart

From this directory, use a fresh output directory for each build:

```sh
export PYTHONDONTWRITEBYTECODE=1
python3 build.py --records examples/synthetic/records.json --sources examples/synthetic/sources --out /tmp/dg-example --derive-superseded
python3 dg.py --root /tmp/dg-example context 'audit log previews' --budget 1000
python3 dg.py --root /tmp/dg-example asof --doc 'Preview Cache Policy' --date 2025-02-15 --format text
python3 recall.py --store /tmp/dg-example --query 'thumbnail preview cache' --profile reasoning
python3 bench/run.py
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
python3 scan_export.py --json
```

The example is invented. `bench/run.py` checks search distractors, supersession, premises, dated clauses, and category recall. It is an engineering regression suite, not a benchmark for historical generalization or model quality. It writes its built store to a temporary directory and needs no network service.

## Capture and recall

Records hold requirements, decisions, judgments and evidence. Typed relationships record how they support or replace one another. Stable IDs in code comments provide entry points; a fresh build discovers their current locations. Keep the graph capture and its source evidence under version control. Build new stores separately from readers; concurrent publication is not implemented.

Keep cited UTF-8 source files under one directory. Create a record with a stable label and an exact source line span:

```sh
mkdir -p /tmp/dg-work/sources
printf 'Use a bounded queue for image jobs.\n' > /tmp/dg-work/sources/notes.md
python3 dg_write.py add --records /tmp/dg-work/records.json --sources /tmp/dg-work/sources --label queue-choice --kind decision --topic jobs --title 'Bounded image queue' --claim 'Use a bounded queue for image jobs.' --actor author --authority unspecified --state accepted --date 2025-01-01 --source notes.md:1-1
python3 dg_write.py check --records /tmp/dg-work/records.json --sources /tmp/dg-work/sources --against none
python3 build.py --records /tmp/dg-work/records.json --sources /tmp/dg-work/sources --out /tmp/dg-work/store --derive-superseded
python3 dg.py --root /tmp/dg-work/store context 'image jobs'
```

Use `dg_write.py link` for a typed relation. `amend --supersedes ID` appends a replacement decision and relation while retaining the old record. `dg.py current ID` follows that chain. `dg.py show ID --sources` returns the claim, links, and exact source excerpts. `dg.py impact --entry ID` returns recorded dependencies and premises. `dg.py asof` rebuilds dated document clauses. Use `--help` on each command for fields and limits.

Keep a decision claim short. Link separate judgment or evidence records when the reason needs detail; a cited decision alone is also valid. Record a premise with `depends_on_premise` and a fact that would reverse a decision with `would_overturn`. Keep authority explicit on each record: `unspecified` stays unspecified. After capture, review new decisions with no edges and add relations where the source warrants them. The tools do not install capture hooks or watch a repository.

For a code entry point, put the IDs returned by the writer on a nearby comment line,
for example `# rivet: r-d2d2f8eb63651058 r-f6340b0aad386e37`. Legacy `context:` comments
remain readable. The writer still requires a stable label (or an explicit ID through
JSON); automatic ID allocation without a label is planned.

Read the function or code region you are modifying and choose its relevant IDs. Start
with `python3 rivet.py --store /tmp/dg-example context ID` (supply several IDs as needed).
This returns claims and correction context together. The older `dg.py context` remains
an index for search/discovery.
Follow specific evidence or dependency relationships as needed. Include relevant class
or file constraints; do not gather every ID in a file by default. `context --file
path/to/file.py` remains an optional discovery/audit tool. An absent anchor is a prompt
to search, not evidence that no related decision exists. The bundled
[skill](skills/rivet/SKILL.md) describes selective recall and capture; it is not installed
by these commands. The name is provisional and package-name availability is unchecked.

## One lookup from code

```sh
python3 rivet.py --store /tmp/dg-example context r-d2d2f8eb63651058
python3 rivet.py --store /tmp/dg-example context ID1 ID2 --sources --budget 12000
python3 rivet.py --store /tmp/dg-example evidence SOURCE_ID
```

Use IDs returned by the writer or shown in code; ID1/ID2/SOURCE_ID above are placeholders.
The command returns whole current claims first, replacement history, incident context,
and corrections/affirmations of that context. It retains authority and the original
target of each approval. Forks and cycles are explicit. This is the compact-history
reader promoted into the toolkit; its selection is a bounded neighborhood, not a claim
that the whole graph or world has been covered.

`--budget` defaults to 6000 UTF-8 bytes. `--format json` returns the same structure.
Follow the printed NEXT command for additional pages. Missing IDs fail, and an oversized
record with its relationships reports required bytes instead of truncating a claim.
NEXT preserves the requested record-count limit; each new page is independently fitted
to the byte budget, so a large early record does not force all later pages to stay small.
`--sources` includes frozen excerpts; `evidence SOURCE_ID` retrieves one without a live
file lookup. Source paths remain relative to the original capture source root. A source
ID identifies a span within the built snapshot, not a code anchor or a live source file.

Use compact context for orientation. When the question already needs proof for several
linked claims, request `--sources --budget 12000` on the first lookup to reduce pagination.
For one specific evidence check, use `evidence SOURCE_ID`. The larger allowance is a cap,
not a target; relevant roots and completeness notices still matter. This guidance saved
one reasoning turn in a small synthetic comparison whose full proof view narrowly
exceeded 6000 bytes. It is not evidence that a larger budget wins for every graph size.

## Category recall

`recall.py` reads the same built store. Use `--query TEXT` for lexical roots or repeat `--seed ID` for exact roots. Profiles are `all` (default), `review`, `reasoning`, and `modification`:

```sh
python3 recall.py --store /tmp/dg-example --seed r-d2d2f8eb63651058 --profile review --depth 0
python3 recall.py --store /tmp/dg-example --seed r-f6340b0aad386e37 --profile reasoning --sources
```

| Profile | Ordinary context emphasized | Important limit |
| --- | --- | --- |
| `review` | Decisions, requirements, invariants, judgments and evidence | May exclude implementation and mechanism records needed for an explanation. |
| `reasoning` | Decisions and their recorded supporting or overturning considerations | For source verification add `--sources`; a recorded assertion is not proof. |
| `modification` | Requirements, mechanisms, implementations and their constraints | May exclude proposals and supporting links; broaden if those matter. |
| `all` | All record and relationship kinds within the selected neighborhood | Depth and byte limits still apply. |

Roots are retained regardless of kind. Corrections, qualifications and premise warnings remain visible even outside the ordinary profile, subject to explicit pagination. Profile filters can block traversal through an excluded kind; a view is not a complete account of the code merely because it answered a query.

Defaults are depth 1, query root limit 10, 6000 UTF-8 bytes, and source references without excerpts. `--sources` includes excerpts. The result reports filtered kinds and relations, depth and query limits, unknown IDs, and byte-budget omissions; `complete: false` means the view is partial. Follow its `follow_up` command to widen a partial view. For warning pagination, that command preserves the profile, depth, and roots so the cursor stays in the same ordered warning set. If a focused profile or query misses a suspected record, retry `--profile all` with a broader query, or use `dg.py context` and `show` to inspect exact claims. An empty result only describes this capture and query. A profile is a presentation filter, not a separate capture or an authority judgment.

A record is a cited assertion, not proof that the world still matches it. Capture source packets with permission and review the cited spans before relying on a decision. The built store includes source excerpts, so keep private packets and stores out of any export.

## Optional installation

If packaging tools are already available, `python3 -m pip install .` provides `rivet`, `rivet-build`, `rivet-read`, `rivet-write`, and `rivet-recall` (legacy `dg-*` aliases remain) commands. Installation is optional; this candidate was validated through the direct Python entry points. No optional service or API key is required for those entry points.

## Export check

Run `python3 scan_export.py --json` before copying this directory. The scanner checks this candidate tree only, rejects unexpected file types and symlinks, and reports categories and relative paths without printing matched content. Supply an external UTF-8 list of additional literal terms with `--deny-file PATH` when needed; keep that list outside the candidate. It is a guardrail, not a complete security review. Inspect the final tree yourself. A public license has not yet been chosen; the `LICENSE` file is a placeholder.

The category reader is derived from a frozen standalone implementation. To compare it with a reference reader without copying that reader into the candidate, run `python3 tests/compat.py --reference-reader /path/to/recall.py`. This builds the invented fixture and compares three ordinary views. The candidate additionally fixes warning-page continuation so its cursor keeps the same profile and depth; the compatibility comparison excludes pagination because that difference is intentional.

## Candidate scope

Archive replay/merge and archive-specific authority normalization are excluded.
Capture authority stays explicit; similarity to another record does not promote
its actor or approval. The license placeholder must be resolved before distribution.
