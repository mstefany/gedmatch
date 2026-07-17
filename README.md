# gedmatch

A stdlib-only Python tool for reconciling two GEDCOM files that describe
overlapping people but use **different, incompatible IDs** — typically your own
[Gramps](https://gramps-project.org/) database (tree **A**) against an
Ancestry or MyHeritage export of a relative's tree (tree **B**).

It figures out *who is who* across the two files, tells you which people in B
are genuinely new relatives worth importing, and writes a pruned GEDCOM you can
import into a scratch tree — without dragging in that relative's in-laws,
duplicates, or placeholder records.

There is no machine learning and no network access. Matching is classical
record linkage: attribute similarity plus **graph propagation** through the
family structure, seeded from a handful of anchors you know are the same
person.

---

## Why it exists

Every genealogy platform rewrites xref IDs on export, so `@I0042@` in your
Gramps file and `@I36906@` in a cousin's MyHeritage export can be the same
grandfather with no shared key to prove it. Names are spelled inconsistently
(`Ján` vs `Johann`, `Nováková` vs `Novák`), dates are approximate or missing,
and the same person is often duplicated inside a single export. Merging by hand
across trees of tens of thousands of people is impractical and error-prone.

`gedmatch` automates the safe part (confident matches and obvious duplicates),
isolates the genuinely ambiguous decisions for you to answer once, and refuses
to guess when the evidence is only circumstantial.

---

## The two tools

- **`gedmatch.py`** — the matcher. Parses both files, matches people, and
  writes `diff.json` (the full reconciliation) and `new_only.ged` (the pruned
  import file).
- **`review.py`** — a read-only triage viewer for `diff.json`. Prints matches,
  conflicts, discoveries grouped by how they attach to your tree, duplicates,
  and the pairs still needing your judgement, in human-readable form.

Both are pure Python 3, no dependencies.

---

## Quick start

```bash
# 1. Match a cousin's export (B) against your Gramps export (A),
#    anchored on one person you know is the same in both, and scoped to
#    the blood relatives of your root person + their spouses.
python3 gedmatch.py A.ged B.ged --root "@I1@" --seed "@I1@=@X1@"

# 2. Read the result.
python3 review.py diff.json --ged-a A.ged --ged-b B.ged

# 3. Answer the ambiguous pairs once; they're remembered for future runs.
python3 gedmatch.py A.ged B.ged --root "@I1@" --seed "@I1@=@X1@" \
        --interactive --answers answers.json

# 4. Import new_only.ged into a *scratch* Gramps tree, review, then merge.
```

`--seed` takes one or more `Aid=Bid` anchor pairs (comma-separated). One good
anchor is usually enough; propagation spreads outward from it.

---

## How matching works

### 1. Scoring a candidate pair

Each candidate A↔B pair gets a weighted attribute score:

| Feature | Weight | Notes |
|---|---|---|
| Given name | 0.30 | Jaro–Winkler + Soundex, with Slovak/Czech nickname and cross-language maps (`Ján`↔`Johann`) |
| Surname | 0.10–0.20 | Feminine `-ová` stemming (`Nováková`→`Novák`), diacritic folding; weight lowered for women (married-name changes) |
| Birth date | 0.30 | See *Date handling* below |
| Death date | 0.10 | " |
| Sex | 0.10 | Normally a veto on mismatch, with one override (below) |

### 2. Structural bonus

Already-matched relatives are strong evidence. A pair gains bonus points for
each matched parent (+0.12, cap 0.24), spouse (+0.20, cap 0.20), child (+0.06,
cap 0.18), and sibling (+0.05, cap 0.15). This is what lets the tool confirm a
person with a garbled name or wrong date once their family lines up.

### 3. The identity gate

A shared surname is *corroborating*, not *identifying* — lots of unrelated
people share one. Before a pair can auto-confirm, the gate requires real
identity evidence: **a matched relative, an exact shared calendar date, or a
strong given-name match.** A mere same-*year* coincidence with a mediocre name
and no matched relatives is sent to review rather than auto-confirmed.

```
Stanislav Tomka  b.1978   vs   Stefan Tomaschko  b.1978   (year only, no relatives)
  -> REVIEW  (0.76 name + shared year is not proof of identity)

Stanislav Tomka  b.12 MAR 1978  vs  Stefan Tomaschko  b.12 MAR 1978
  -> AUTO-CONFIRM  (an exact shared birthday is identity-grade evidence)
```

### 4. Propagation to a fixpoint

Starting from the seeds, the matcher alternates *propagate* (score relatives of
matched people) and *block* (score name-bucketed candidates), re-running until
no new matches appear. High-confidence pairs (≥ 0.85) auto-confirm; anything
below 0.55 is rejected; the band between is deferred and asked only if it never
gains structural support.

### 5. Scope

With `--root`, matching is restricted to the **blood kindred** of the root
person(s) plus their spouses. In-laws are terminal — the tool will match your
aunt's husband but will not wander up into *his* ancestors. This is what keeps a
58k-person shared tree from flooding you with strangers. `--scope-hops` further
limits how far from the matched set the blocking pass will look.

### 6. Duplicate detection

People duplicated *inside* B (the same ancestor entered twice, a common
copy/import artifact) are detected — by attributes and by shared matched
spouses — and reported separately, not treated as discoveries.

---

## Date handling

Genealogical dates are messy, and different tools disagree about them. The
parser reads a structured date (`ABT 1872`, `BEF 12 MAR 1900`, `MAR 1978`,
`BET 1800 AND 1810`, bare years) and applies these rules:

- **Baptism/christening fallback.** If a birth date is absent, the baptism
  (`BAPM`) or christening (`CHR`) date is used as the effective birth date —
  the same precedence Gramps uses. Likewise burial (`BURI`) stands in for a
  missing death date. This matters because Ancestry ignores baptism entirely,
  and MyHeritage often imports baptisms as births or FamilySearch christenings
  as `CHR` events.
- **Approximate dates are treated gently.** When either date is approximate
  (`ABT`/`EST`/`CAL`/`BEF`/`AFT`) or year-only, a gap between them is penalised
  half as hard as a gap between two exact dates.
- **No false conflicts.** Two *approximate* dates that merely differ are **not**
  flagged as a contradiction; only two reasonably certain dates more than two
  years apart are.

```
William Olson  b.ABT 1872  d.1946      (spouse Susanna matched)
William Olson  b.ABT 1895              (same spouse in B)
  -> MATCH via spouse, with ZERO date conflicts
     (23 years apart, but both births are guesses — not a contradiction)
```

---

## The ignore list

`--ignore` (default `Private,Living`) drops people whose given name matches one
of the listed tokens — case- and diacritic-insensitive — from **both** trees,
*before* matching, scoping, and emission. This removes the placeholder records
exports are full of (living people hidden as `Private`, unnamed `kind`/child
stubs) so they never surface as bogus "discoveries" in `new_only.ged`.

```bash
python3 gedmatch.py A.ged B.ged --root "@I1@" --seed "@I1@=@X1@" \
        --ignore "Private,Living,kind"
# -> ignored: 0 in A, 2 in B (given name in ['Living', 'Private', 'kind'])
```

Pass `--ignore ""` to disable.

---

## Output

### `new_only.ged` — the pruned import file

Contains only what you don't already have, ready to import into a scratch tree:

- **Discoveries** — genuinely new relatives, emitted with their **full record
  from B**: birth/baptism/death/burial events with dates and places,
  occupation, residence, inline notes — not just a name-and-date skeleton.
  Cross-record pointers (source citations, media, note records) are stripped so
  nothing dangles on import; family links are rewritten by the tool.
- **Anchor stubs** — people who are *already in your tree* but are needed to
  connect a discovery. Emitted minimally, carrying `REFN <your Gramps id>` and a
  `NOTE` telling you which of your existing records to merge them into.
- **Families** — emitted only when they connect at least two kept people, with
  `HUSB`/`WIFE` placed by sex so a swapped export doesn't create a duplicate
  family.

### `diff.json` — the full reconciliation

`matches`, `conflicts`, `only_in_a`, `only_in_b`, `only_in_b_out_of_scope`,
`duplicates_in_b`, `bridges` (new people attaching to existing ones),
`discovery_info` (distance from root + attachment), and `uncertain` (pairs you
haven't resolved). Feed it to `review.py` for a readable summary.

---

## The answer cache

With `--interactive`, ambiguous pairs are shown one at a time — you answer
`y`/`n`/`s`(kip). With `--answers FILE`, your **yes/no** answers are saved
(keyed to a fingerprint of both people, so an answer is never misapplied if an
ID is later reused) and replayed on future runs, letting you re-run freely with
different `--grow` or `--max-root-distance` settings without re-answering.

A **skip is not remembered** — it means "ask me again next time", so skipped
pairs resurface until you make a real decision.

---

## Phased import

Big trees are easier to absorb in rings. Import close relatives first, merge,
then widen:

```bash
# Inner ring: only discoveries within 2 relationship hops of the root
python3 gedmatch.py A.ged B.ged --root "@I1@" --seed "@I1@=@X1@" \
        --answers answers.json --max-root-distance 2

# Only new descendants (children/grandchildren), not new ancestors
python3 gedmatch.py A.ged B.ged --root "@I1@" --seed "@I1@=@X1@" \
        --answers answers.json --grow down
```

---

## CLI reference

```
positional:
  gedcom_a                A = your Gramps export
  gedcom_b                B = the Ancestry/MyHeritage export

options:
  --seed Aid=Bid,...      anchor matches you know are the same person
  --auto-seed             derive anchors automatically (exact name+date)
  --use-uid               exact _UID/RFN/_FSFTID pre-pass before scoring
  --root Aid,...          restrict to blood kindred of root(s) + spouses
  --scope-hops N          limit blocking to N family-hops of matched set (def 2)
  --max-root-distance N   emit only discoveries within N hops of root (0 = all)
  --grow {both,down,up}   phased import by attachment type (def both)
  --interactive           prompt on ambiguous pairs
  --answers FILE          replay/save interactive yes/no decisions
  --ignore "A,B,..."      given-name tokens to drop entirely (def Private,Living)
  --explain Aid=Bid,...   diagnose why specific pairs did/didn't match
  --out-json FILE         default diff.json
  --out-ged FILE          default new_only.ged
```

`--explain` is the debugging workhorse — it prints the blocking bucket, the
per-feature scores, the structural bonus, and the final verdict for any pair.

---

## Testing

```bash
pytest        # run the whole suite (unit + E2E tests)
pytest -v     # print every passing check too
```

The suite (17 tests covering unit behavior and 57 E2E checks) has two layers:

- **Regression** — the canonical match / uncertain / duplicate counts for every
  fixture pair in `tests/fixtures/`, so a change that silently alters matching
  behaviour is caught.
- **Behavioral** — the specific cases the tool was built to get right: weak
  identity gating, fuzzy dates and the baptism fallback, the ignore list, full
  record transfer, the skip-vs-recall answer cache, and structural integrity of
  every emitted GEDCOM (valid levels, no dangling pointers).

Each fixture pair is a tiny hand-written scenario:

| Fixture | Exercises |
|---|---|
| `A2`/`B2` | basic multi-person match across rewritten IDs |
| `tA`/`tB` | deferred prompts and genuinely ambiguous pairs |
| `dupA`/`dupB` | duplicate *inside* B |
| `laA`/`laB` | scoping to living/anchor kindred |
| `sxA`/`sxB` | sex-mismatch override (name + both dates exact → data error) |
| `mhA`/`mhB` | `--scope-hops` neighborhood limit |
| `phA`/`phB` | distance and `--grow` direction |
| `dpA`/`dpB` | duplicate parent within one family |
| `opA`/`opB` | out-of-scope in-law parent correctly excluded |
| `d2A`/`d2B` | spouse-corroborated duplicate (mis-dated but same person) |
| `upA`/`upB` | growing upward into ancestors |
| `genA`/`genB` | cross-generation namesakes not conflated |
| `st4A`/`st4B` | weak-identity gate (year-only coincidence) |
| `wilA`/`wilB` | approximate dates, no false conflict |
| `bapA`/`bapB` | christening used as birth fallback |
| `fullA`/`fullB` | ignore list + full-record transfer |

---

## Project structure

```
gedmatch/
├── gedmatch.py              # the matcher (parse, score, propagate, emit)
├── review.py                # read-only triage viewer for diff.json
├── README.md                # this file
└── tests/
    ├── run_tests.py         # regression + behavioral suite (stdlib only)
    └── fixtures/            # hand-written synthetic GEDCOM pairs
        ├── A2.ged / B2.ged
        ├── tA.ged / tB.ged
        └── ...
```

---

## Known limitations & frontier

- **Option B not built.** The tool imports genuinely new people ("Option A": a
  scratch tree you merge). It does not yet write *new facts onto people you
  already have* — e.g. attaching newly-discovered parents to an existing person
  in place. Bridges are reported so you can do this by hand.
- **Adjectival surnames.** Feminine stemming covers the dominant `-ová` suffix;
  adjectival forms (`Veselý`/`Veselá`) are a known gap awaiting a dictionary.
- **Full-record transfer keeps inline data only.** Events, places, and inline
  notes ride along with a discovery, but citations that point to separate
  source records are dropped (to avoid dangling pointers on import) rather than
  emitted with their source records.
- **The spouse-corroborated duplicate rule** could, in principle, misflag a
  genuine remarriage to an identically-named different person. This is
  astronomically rare and always visible in the duplicates list for review.
- **Nameless records are not auto-ignored** by default — only explicit name
  patterns — so a nameless but structurally important linking ancestor is never
  dropped silently.
