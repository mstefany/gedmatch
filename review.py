#!/usr/bin/env python3
"""
review.py - triage a gedmatch diff.json for human review.

A confirmed match is only worth attention if it might be WRONG. After the
matcher's identity gate, an auto-confirmed match already has real evidence
(a matched relative, or an agreeing date). So this tool flags only:

  * a match whose birth/death years actually CONFLICT (likely two people)
  * a match with NO corroboration at all -- name only, no date, no relative

"blocking-pass" and "attribute-only" are reported as context, not risk: a
blocking match on identical name + identical birth + identical death is sound.
It also lays out discoveries (with how each attaches), exclusions, and the
uncertain queue -- showing close relatives for dateless cases so you can judge.

Usage:
  python3 review.py diff.json --ged-a gramps.ged --ged-b ancestry.ged
  python3 review.py diff.json --ged-a A.ged --ged-b B.ged --csv flagged.csv
"""
import argparse, csv, json, re, sys

try:
    from gedmatch import (parse_gedcom, describe_relatives,
                          score_pair, soundex, surname_stem)
except Exception:
    parse_gedcom = None
    def describe_relatives(*a, **k): return ""
    score_pair = None

DATE_CONFLICT_THRESHOLD = 5

class Empty:
    people = {}
    def parents(self, x): return []
    def spouses(self, x): return []
    def children(self, x): return []

def load_tree(path):
    if not path or parse_gedcom is None:
        return Empty()
    try:
        return parse_gedcom(path)
    except Exception as e:
        print(f"(could not read {path}: {e})", file=sys.stderr)
        return Empty()

def who(tree, xid):
    p = tree.people.get(xid)
    if not p:
        return xid
    b = p.birth_year or "?"; d = p.death_year or "?"
    name = f"{p.given} {p.surname}".strip() or "(no name)"
    return f"{name} [{b}-{d}] {xid}"

def has(ev, needle):
    return any(needle in e for e in ev)

def is_trusted(ev):
    return has(ev, "seed") or has(ev, "_UID") or has(ev, "auto-seed")

_DATE_EV = re.compile(r"^(?:birth|death)\s.*=([0-9.]+)$")
def date_agrees(ev):
    for e in ev:
        mo = _DATE_EV.match(e)
        if mo and float(mo.group(1)) >= 0.6:
            return True
    return False

def corroborated(ev):
    return is_trusted(ev) or has(ev, "matched (+") or date_agrees(ev)

def assess(match, conflicts_by_pair):
    ev = match.get("evidence", [])
    key = (match["a"], match["b"])
    reasons = []
    worst = 0
    for c in conflicts_by_pair.get(key, []):
        if c["field"] in ("birth_year", "death_year"):
            try:
                worst = max(worst, abs(int(c["a_val"]) - int(c["b_val"])))
            except (TypeError, ValueError):
                pass
    if worst > DATE_CONFLICT_THRESHOLD:
        reasons.append(f"date conflict d={worst}y")
    if not corroborated(ev):
        reasons.append("uncorroborated (name only, no date/relative)")
    sev = (worst, not corroborated(ev), -match.get("confidence", 0))
    return reasons, sev

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("diff_json")
    ap.add_argument("--ged-a", default="")
    ap.add_argument("--ged-b", default="")
    ap.add_argument("--csv", default="")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)

    with open(args.diff_json, encoding="utf-8") as fh:
        d = json.load(fh)
    A = load_tree(args.ged_a)
    B = load_tree(args.ged_b)

    matches = d.get("matches", [])
    conflicts_by_pair = {}
    for c in d.get("conflicts", []):
        conflicts_by_pair.setdefault((c["a"], c["b"]), []).append(c)

    flagged = []
    n_block = n_attr = n_uncorr = 0
    for mt in matches:
        ev = mt.get("evidence", [])
        if has(ev, "(blocking)"): n_block += 1
        if not is_trusted(ev) and not has(ev, "matched (+"): n_attr += 1
        if not corroborated(ev): n_uncorr += 1
        reasons, sev = assess(mt, conflicts_by_pair)
        if reasons:
            flagged.append((sev, reasons, mt))
    flagged.sort(key=lambda x: x[0], reverse=True)

    print("=" * 70)
    print(f"MATCH AUDIT  ({len(matches)} confirmed matches)")
    print(f"  from blocking pass (context, not risk) : {n_block}")
    print(f"  no matched relative (context, not risk): {n_attr}")
    print(f"  UNCORROBORATED (name only, real risk)  : {n_uncorr}")
    print(f"  --> {len(flagged)} matches flagged for review")
    print("=" * 70)
    shown = flagged if args.limit == 0 else flagged[:args.limit]
    for _, reasons, mt in shown:
        print(f"\n[{mt.get('confidence',0):.2f}] {' | '.join(reasons)}")
        print(f"   A  {who(A, mt['a'])}")
        ra = describe_relatives(A, mt['a'])
        if ra: print(f"      {ra}")
        print(f"   B  {who(B, mt['b'])}")
        rb = describe_relatives(B, mt['b'])
        if rb: print(f"      {rb}")
        print(f"   why: {'; '.join(mt.get('evidence', []))}")
    if args.limit and len(flagged) > args.limit:
        print(f"\n... {len(flagged) - args.limit} more (raise --limit)")

    only_b = d.get("only_in_b", [])
    attach = {}
    for br in d.get("bridges", []):
        for np_ in br["new_people"]:
            anchors = ", ".join(a["name"] for a in np_.get("attaches_to", []))
            attach[np_["b"]] = f'{np_["role"]} of {anchors}'
    print("\n" + "=" * 70)
    print(f"DISCOVERIES TO IMPORT  ({len(only_b)} in-scope new people)")
    print("=" * 70)
    for xid in only_b:
        tail = f"   ({attach[xid]})" if xid in attach else ""
        print(f"   {who(B, xid)}{tail}")

    # sanity check: does a "discovery" closely resemble someone already in A?
    # if so it's probably NOT new -- a structural/spelling difference kept them
    # apart, or A's copy is matched elsewhere. Attribute-only score.
    if score_pair is not None and A.people and only_b:
        a_matched = {m["a"]: m["b"] for m in matches}
        from collections import defaultdict as _dd
        abk = _dd(list)
        for xid, p in A.people.items():
            abk[soundex(surname_stem(p.surname))].append(xid)
        alerts = []
        for b in only_b:
            pb = B.people.get(b)
            if not pb:
                continue
            best = None
            for a in abk.get(soundex(surname_stem(pb.surname)), []):
                try:
                    s, _ = score_pair(a, b, A, B, {})
                except Exception:
                    continue
                if best is None or s > best[0]:
                    best = (s, a)
            if best and best[0] >= 0.75:
                alerts.append((b, best[1], best[0]))
        if alerts:
            print("\n" + "=" * 70)
            print(f"DISCOVERY SANITY CHECK  ({len(alerts)}) -- look like someone "
                  "already in your tree")
            print("=" * 70)
            for b, a, s in alerts:
                where = (f"but your record is matched to {a_matched[a]}"
                         if a in a_matched else "and your record is UNMATCHED")
                print(f"\n   B  {who(B, b)}")
                print(f"   ~  A  {who(A, a)}  (resemblance {s:.2f})")
                print(f"      {where}")
                print(f"      diagnose: gedmatch.py ... --explain \"{a}={b}\"")

    dups = d.get("duplicates_in_b", [])
    if dups:
        print("\n" + "=" * 70)
        print(f"DUPLICATES IN THE IMPORTED TREE  ({len(dups)}) -- NOT imported; "
              "consider merging in Ancestry")
        print("=" * 70)
        for x in dups:
            print(f"   {who(B, x['b'])}  ==  already matched via {x['duplicate_of_b']}")

    oos = d.get("only_in_b_out_of_scope", [])
    if oos:
        print("\n" + "=" * 70)
        print(f"EXCLUDED AS OUT OF SCOPE  ({len(oos)}) -- sanity check these")
        print("=" * 70)
        for xid in oos:
            print(f"   {who(B, xid)}")

    unc = d.get("uncertain", [])
    if unc:
        print("\n" + "=" * 70)
        print(f"RECORDED UNCERTAIN  ({len(unc)}) -- with family context")
        print("=" * 70)
        for u in unc:
            print(f"\n   [{u.get('confidence',0):.2f}]")
            print(f"   A  {who(A, u['a'])}")
            ra = describe_relatives(A, u['a'])
            if ra: print(f"      {ra}")
            print(f"   B  {who(B, u['b'])}")
            rb = describe_relatives(B, u['b'])
            if rb: print(f"      {rb}")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["confidence", "risk", "a_id", "a_name",
                        "b_id", "b_name", "evidence"])
            for _, reasons, mt in flagged:
                w.writerow([mt.get("confidence", 0), "; ".join(reasons),
                            mt["a"], who(A, mt["a"]).rsplit(" ", 1)[0],
                            mt["b"], who(B, mt["b"]).rsplit(" ", 1)[0],
                            "; ".join(mt.get("evidence", []))])
        print(f"\nwrote {args.csv}")

if __name__ == "__main__":
    main()
