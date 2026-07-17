#!/usr/bin/env python3
"""
Regression + behavioral test suite for gedmatch.

Runs the matcher against the synthetic GEDCOM fixtures in tests/fixtures/ and
checks both:

  * regression  -- the canonical match / uncertain / duplicate counts for each
                   fixture pair stay stable across changes; and
  * behavioral  -- the specific edge cases the tool was built to get right
                   (fuzzy dates, baptism fallback, weak-identity gating, the
                   ignore list, full-record transfer, and the answer cache).

Stdlib only, no third-party deps -- same as gedmatch itself.

    python3 tests/run_tests.py          # run everything
    python3 tests/run_tests.py -v       # also print each passing check

Exit code 0 = all passed, 1 = at least one failure.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIX = os.path.join(HERE, "fixtures")
GED = os.path.join(ROOT, "gedmatch.py")
VERBOSE = "-v" in sys.argv[1:]

_PASS = 0
_FAIL = 0


def ok(name, cond, detail=""):
    assert cond, f"{name}: {detail}"


def fx(fname):
    return os.path.join(FIX, fname)


def run(args, stdin="", answers=None, extra=None):
    """Run gedmatch.py with output written to a throwaway temp dir.
    Returns (combined_output, diff_dict, new_only_text)."""
    td = tempfile.mkdtemp(prefix="gmtest_")
    outj = os.path.join(td, "diff.json")
    outg = os.path.join(td, "new_only.ged")
    cmd = [sys.executable, GED] + args + ["--out-json", outj, "--out-ged", outg]
    if answers is not None:
        cmd += ["--answers", answers]
    if extra:
        cmd += extra
    p = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
    diff = json.load(open(outj)) if os.path.exists(outj) else {}
    ged = open(outg).read() if os.path.exists(outg) else ""
    return p.stdout + p.stderr, diff, ged


def counts(diff):
    return (len(diff.get("matches", [])),
            len(diff.get("uncertain", [])),
            len(diff.get("duplicates_in_b", [])),
            len(diff.get("conflicts", [])))


# --------------------------------------------------------------------------
# 1. Regression: canonical counts per fixture pair.
#    (matched, uncertain, duplicates) -- None means "don't care".
# --------------------------------------------------------------------------
REGRESSION = [
    # name        args                                                    m  unc dup
    ("A2   basic multi-person",
     ["A2.ged", "B2.ged", "--root", "@I5@", "--seed", "@I1@=@X1@"],        6, None, 0),
    ("tA   deferred prompts / ambiguous",
     ["tA.ged", "tB.ged", "--seed", "@I0@=@X0@"],                          8, 2, 0),
    ("dup  B-internal duplicate detection",
     ["dupA.ged", "dupB.ged"],                                            7, None, 1),
    ("la   living/anchor scoping",
     ["laA.ged", "laB.ged", "--seed", "@R@=@XR@"],                        4, None, 0),
    ("sx   sex-mismatch override (data error)",
     ["sxA.ged", "sxB.ged"],                                              1, None, None),
    ("mh   scope-hops neighborhood limit",
     ["mhA.ged", "mhB.ged", "--root", "@R@", "--seed", "@R@=@XR@"],       3, None, 0),
    ("ph   distance / grow direction",
     ["phA.ged", "phB.ged", "--root", "@R@", "--seed", "@R@=@XR@"],       3, None, 0),
    ("dp   duplicate parent, one family",
     ["dpA.ged", "dpB.ged", "--root", "@R@", "--seed", "@R@=@XR@"],       3, None, 1),
    ("op   out-of-scope in-law parent",
     ["opA.ged", "opB.ged", "--root", "@R@", "--seed", "@R@=@XR@"],       4, None, 0),
    ("d2   spouse-corroborated duplicate",
     ["d2A.ged", "d2B.ged", "--root", "@R@", "--seed", "@R@=@XR@"],       3, None, 1),
    ("up   grow upward (ancestors)",
     ["upA.ged", "upB.ged", "--root", "@R@", "--seed", "@R@=@XR@"],       2, None, 0),
    ("gen  cross-generation namesakes",
     ["genA.ged", "genB.ged"],                                           2, None, 0),
]


def test_regression():
    print("regression (canonical counts)")
    for name, args, m, unc, dup in REGRESSION:
        fargs = [fx(a) if a.endswith(".ged") else a for a in args]
        _, diff, _ = run(fargs)
        gm, gunc, gdup, _ = counts(diff)
        ok(f"{name}: matched={m}", gm == m, f"got {gm}")
        if unc is not None:
            ok(f"{name}: uncertain={unc}", gunc == unc, f"got {gunc}")
        if dup is not None:
            ok(f"{name}: duplicates={dup}", gdup == dup, f"got {gdup}")


# --------------------------------------------------------------------------
# 2. Behavioral tests: the reasons the tool exists.
# --------------------------------------------------------------------------
def verdict(out):
    m = re.search(r"verdict for this pair at final state:\s*(.*)", out)
    return m.group(1).strip() if m else "(no verdict)"


def test_item4_weak_identity_gate():
    """A same-YEAR-only coincidence with a mediocre name and no matched
    relatives must NOT auto-confirm; an exact identical calendar date may."""
    print("item 4: weak-identity gating")
    out, _, _ = run([fx("st4A.ged"), fx("st4B.ged"), "--explain", "@T@=@X@"])
    ok("Stanislav/Stefan year-only -> REVIEW", "REVIEW" in verdict(out), verdict(out))

    # same people, but an exact shared birthday: identity-grade evidence
    a = open(fx("st4A.ged")).read().replace("2 DATE 1978", "2 DATE 12 MAR 1978")
    b = open(fx("st4B.ged")).read().replace("2 DATE 1978", "2 DATE 12 MAR 1978")
    td = tempfile.mkdtemp(prefix="gmtest_")
    pa, pb = os.path.join(td, "a.ged"), os.path.join(td, "b.ged")
    open(pa, "w").write(a); open(pb, "w").write(b)
    out, _, _ = run([pa, pb, "--explain", "@T@=@X@"])
    ok("same names + exact 12 MAR 1978 -> AUTO-CONFIRM",
       "AUTO-CONFIRM" in verdict(out), verdict(out))


def test_item1_fuzzy_dates_and_fallback():
    """Two approximate (ABT) dates that differ are not a hard conflict; a
    christening date stands in for a missing birth date."""
    print("item 1: date handling")
    out, diff, _ = run([fx("wilA.ged"), fx("wilB.ged"), "--seed", "@S@=@XS@"])
    m, _, _, conf = counts(diff)
    ok("William Olson matches via spouse", m == 2, f"matched={m}")
    ok("both births ABT -> no false conflict", conf == 0, f"conflicts={conf}")

    out, _, _ = run([fx("bapA.ged"), fx("bapB.ged"), "--explain", "@P@=@XP@"])
    ok("christening used as birth fallback -> AUTO-CONFIRM",
       "AUTO-CONFIRM" in verdict(out), verdict(out))
    ok("effective birth compared (1900~1901)",
       "birth 1900~1901" in out, "date evidence line missing")


def test_items_23_7_ignore_and_full_transfer():
    """Placeholder people are dropped everywhere; a real discovery is emitted
    with its full record (events/places/notes) minus dangling pointers."""
    print("items 2/3/7: ignore list + full-record transfer")
    out, diff, ged = run([fx("fullA.ged"), fx("fullB.ged"), "--root", "@R@",
                          "--seed", "@R@=@XR@"],
                         extra=["--ignore", "Private,Living,kind"])
    ok("2 placeholders ignored in B", "ignored: 0 in A, 2 in B" in out, out.strip().splitlines()[:3])
    ok("placeholders absent from new_only.ged",
       not re.search(r"Private|kind|@PRIV@|@KID@", ged))
    # NK carries its full record
    nk = re.search(r"^0 @NK@ INDI.*?(?=^0 @)", ged, re.M | re.S)
    nk = nk.group(0) if nk else ""
    for want in ("OCCU Engineer", "Bratislava", "1 CHR", "Inline biography"):
        ok(f"discovery keeps {want!r}", want in nk, "missing from @NK@")
    ok("source pointer dropped", "@S1@" not in nk and "PAGE 42" not in nk)

    # same placeholders dropped by record id (bare and B:-prefixed) instead
    # of by given-name token
    out, _, ged = run([fx("fullA.ged"), fx("fullB.ged"), "--root", "@R@",
                       "--seed", "@R@=@XR@"],
                      extra=["--ignore", "@PRIV@,B:@KID@"])
    ok("2 placeholders ignored by id in B", "ignored: 0 in A, 2 in B" in out,
       out.strip().splitlines()[:3])
    ok("id-ignored placeholders absent from new_only.ged",
       not re.search(r"Private|kind|@PRIV@|@KID@", ged))

    # an A:-prefixed id must not touch B even if B has that id
    out, _, ged = run([fx("fullA.ged"), fx("fullB.ged"), "--root", "@R@",
                       "--seed", "@R@=@XR@"],
                      extra=["--ignore", "A:@PRIV@"])
    ok("A:-prefixed id leaves B alone", "ignored:" not in out,
       out.strip().splitlines()[:3])
    ok("B placeholder still emitted without ignore", "@PRIV@" in ged)


def test_item5_answer_cache_skip():
    """A 'skip' is never cached (asked again next run); a definitive yes/no is
    remembered and recalled."""
    print("item 5: answer cache treats skip as 'ask again'")
    td = tempfile.mkdtemp(prefix="gmtest_")
    ans = os.path.join(td, "ans.json")
    base = [fx("tA.ged"), fx("tB.ged"), "--seed", "@I0@=@X0@", "--interactive"]

    run(base, stdin="s\ns\n", answers=ans)                 # skip both
    stored = json.load(open(ans)) if os.path.exists(ans) else {}
    ok("skips are not persisted", stored == {}, f"stored={stored}")

    out, _, _ = run(base, stdin="s\ns\n", answers=ans)     # must ask again
    ok("skipped pairs are re-asked", out.count("same person?") == 2,
       f"prompts={out.count('same person?')}")

    ans2 = os.path.join(td, "ans2.json")
    run(base, stdin="n\nn\n", answers=ans2)                # answer no
    stored2 = json.load(open(ans2))
    decisions = [v["decision"] for v in stored2.values()]
    ok("definitive answers are stored", decisions == [False, False], decisions)
    out, _, _ = run(base, stdin="", answers=ans2)          # must recall
    ok("stored answers are recalled", out.count("recalled: no") == 2,
       f"recalled={out.count('recalled: no')}")


def test_structural_integrity():
    """Every emitted new_only.ged must import cleanly: valid level numbers,
    no dangling family<->individual pointers, no leftover source pointers."""
    print("structural integrity of emitted GEDCOMs")
    cases = [
        [fx("tA.ged"), fx("tB.ged"), "--seed", "@I0@=@X0@"],
        [fx("phA.ged"), fx("phB.ged"), "--root", "@R@", "--seed", "@R@=@XR@"],
        [fx("opA.ged"), fx("opB.ged"), "--root", "@R@", "--seed", "@R@=@XR@"],
        [fx("fullA.ged"), fx("fullB.ged"), "--root", "@R@", "--seed",
         "@R@=@XR@", "--ignore", "Private,kind"],
    ]
    for args in cases:
        label = os.path.basename(args[0])
        _, _, ged = run(args)
        lines = ged.splitlines()
        indis = set(re.findall(r"^0 (@\w+@) INDI", ged, re.M))
        fams = set(re.findall(r"^0 (@\w+@) FAM", ged, re.M))
        levels_ok = bool(lines) and lines[0] == "0 HEAD" and lines[-1] == "0 TRLR" \
            and all(re.match(r"^[0-6] ", l) for l in lines)
        fam_bad = [r for body in re.findall(r"^0 @\w+@ FAM(.*?)(?=^0 |\Z)", ged, re.M | re.S)
                   for r in re.findall(r"^1 (?:HUSB|WIFE|CHIL) (@\w+@)", body, re.M)
                   if r not in indis]
        indi_bad = [r for body in re.findall(r"^0 @\w+@ INDI(.*?)(?=^0 |\Z)", ged, re.M | re.S)
                    for r in re.findall(r"^1 (?:FAMC|FAMS) (@\w+@)", body, re.M)
                    if r not in fams]
        leftover = re.findall(r"^\d+ (?:SOUR|OBJE|SUBM|ASSO) (@\w+@)", ged, re.M)
        ok(f"{label}: valid level numbers", levels_ok)
        ok(f"{label}: no dangling FAM->INDI", not fam_bad, fam_bad)
        ok(f"{label}: no dangling INDI->FAM", not indi_bad, indi_bad)
        ok(f"{label}: no leftover source pointers", not leftover, leftover)


def main():
    if not os.path.exists(GED):
        sys.exit(f"cannot find gedmatch.py at {GED}")
    test_regression()
    test_item4_weak_identity_gate()
    test_item1_fuzzy_dates_and_fallback()
    test_items_23_7_ignore_and_full_transfer()
    test_item5_answer_cache_skip()
    test_structural_integrity()
    print(f"\n{_PASS} passed, {_FAIL} failed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
