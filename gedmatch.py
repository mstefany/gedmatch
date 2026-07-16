#!/usr/bin/env python3
"""
gedmatch - reconcile two GEDCOM exports where IDs are NOT trustworthy.

Workflow (Option A): parse two GEDCOMs (A = your Gramps export, B = the
Ancestry/MyHeritage export), match individuals across them using attribute
similarity + family-graph structure, then emit:
  - diff.json     : matches (with confidence + evidence), only_in_A,
                    only_in_B, and field-level conflicts on matched pairs
  - new_only.ged  : a pruned GEDCOM of the people that exist only in B,
                    ready to import into a *scratch* Gramps tree and prune
                    further before merging into your real tree.

Matching strategy:
  1. (optional, off by default) exact join on the _UID tag  --use-uid
  2. seed with a few hand-given anchor matches              --seed A=B,...
     (or --auto-seed to bootstrap from strong unique attribute matches)
  3. flood-fill outward from confirmed matches through relatives, scoring
     each candidate pair with attribute similarity + a structural bonus for
     already-matched parents / spouses / children / siblings
  4. auto-confirm score >= TAU_HIGH; queue TAU_LOW..TAU_HIGH (and near-ties)
     for review; reject below TAU_LOW
  5. blocking pass (soundex(surname)+birth-decade buckets) to catch
     disconnected fragments the flood-fill never reached

Pure standard library. Python 3.8+.
"""
from __future__ import annotations
import argparse, json, re, sys
from dataclasses import dataclass, field
from collections import defaultdict
import heapq

# ---------------------------------------------------------------- thresholds
TAU_HIGH = 0.85          # auto-confirm at or above this
TAU_LOW = 0.55           # reject below this; between = ask the user
TIE_MARGIN = 0.08        # if top two candidates are this close, ask

# Diacritic folding for Slovak/Czech (and general Latin) orthography.
_DIA = {
    'á':'a','ä':'a','à':'a','â':'a','ã':'a','å':'a',
    'č':'c','ć':'c','ç':'c','ĉ':'c',
    'ď':'d','đ':'d',
    'é':'e','ě':'e','è':'e','ê':'e','ë':'e','ę':'e',
    'í':'i','ï':'i','î':'i','ì':'i',
    'ĺ':'l','ľ':'l','ł':'l','ļ':'l',
    'ň':'n','ñ':'n','ń':'n',
    'ó':'o','ô':'o','ö':'o','ò':'o','õ':'o','ø':'o','ő':'o',
    'ŕ':'r','ř':'r',
    'š':'s','ś':'s','ş':'s','ŝ':'s',
    'ť':'t','ţ':'t',
    'ú':'u','ů':'u','ü':'u','ù':'u','û':'u','ű':'u',
    'ý':'y','ÿ':'y',
    'ž':'z','ź':'z','ż':'z',
}
_DIA_TABLE = str.maketrans(_DIA)

# Canonical given-name groups. FIRST word of each string is the canonical key;
# the rest are variants (diminutives, cross-language forms, Anglicizations).
# This table is meant to be curated by you — it encodes family-specific
# knowledge. Forms are folded automatically, so write them naturally.
_GIVEN_GROUPS = [
    "daniel danko danik dano dan danko",
    "eduard edo edko edino edic ed",
    "anna hana hanka anka ania anicka anca",
    "gabriela gaba gabika gabca gabi gabriella",
    "jan john johnny janko jano jani janko",
    "juraj george duro djuro jurko jur",
    "jozef joseph joe joey jozko jozo dodo",
    "stefan stephen steve steven stevo pisto",
    "michal michael mike mikey misko miso",
    "maria mary marka majka maja marka mara",
    "katarina catherine katherine kate katka kata",
    "andrej andrew andy ondrej ondro andrik",
    "pavol paul pavel palo pavko",
    "jozefina josephine jozka",
    "frantisek francis frank fero ferko frantik",
    "ladislav laco lacko ladik",
    "vojtech adalbert bela vojto",
    "stefania stephanie stefka stefka",
]

def _build_given_map():
    m = {}
    for grp in _GIVEN_GROUPS:
        toks = [t.translate(_DIA_TABLE) for t in grp.split()]
        canon = toks[0]
        for t in toks:
            m[t] = canon
    return m

_GIVEN_MAP = _build_given_map()

# ------------------------------------------------------------------- models
@dataclass
class Person:
    xid: str
    given: str = ""
    surname: str = ""
    sex: str = ""
    birth_year: int | None = None
    birth_raw: str = ""
    death_year: int | None = None
    death_raw: str = ""
    uid: str = ""
    famc: list[str] = field(default_factory=list)   # families where child
    fams: list[str] = field(default_factory=list)   # families where spouse

@dataclass
class Family:
    xid: str
    husb: str = ""
    wife: str = ""
    chil: list[str] = field(default_factory=list)

@dataclass
class Tree:
    people: dict[str, Person]
    fams: dict[str, Family]

    def parents(self, pid: str) -> list[str]:
        out = []
        for fc in self.people[pid].famc:
            fam = self.fams.get(fc)
            if not fam:
                continue
            if fam.husb:
                out.append(fam.husb)
            if fam.wife:
                out.append(fam.wife)
        return out

    def spouses(self, pid: str) -> list[str]:
        out = []
        for fs in self.people[pid].fams:
            fam = self.fams.get(fs)
            if not fam:
                continue
            other = fam.wife if fam.husb == pid else fam.husb
            if other:
                out.append(other)
        return out

    def children(self, pid: str) -> list[str]:
        out = []
        for fs in self.people[pid].fams:
            fam = self.fams.get(fs)
            if fam:
                out.extend(fam.chil)
        return out

    def siblings(self, pid: str) -> list[str]:
        out = []
        for fc in self.people[pid].famc:
            fam = self.fams.get(fc)
            if fam:
                out.extend(c for c in fam.chil if c != pid)
        return out

# ----------------------------------------------------------- gedcom parsing
YEAR_RE = re.compile(r"\b(\d{4})\b")

def parse_gedcom(path: str) -> Tree:
    """Structure-aware GEDCOM reader. Tracks the tag open at each level so a
    DATE is captured only when its DIRECT parent is BIRT or DEAT -- never a
    DATE nested under CHAN, SOUR, RESI, CENS, etc. Takes the first BIRT/DEAT
    date and ignores later events. Dialect-tolerant (Gramps, Ancestry, MH)."""
    people: dict[str, Person] = {}
    fams: dict[str, Family] = {}
    cur = None; kind = None
    tagpath: dict[int, str] = {}     # level -> tag currently open at that level
    got_name = False                 # primary NAME already captured?
    name_open = False                # still inside the primary NAME block?
    line_re = re.compile(r"^(\d+)\s+(?:(@[^@]+@)\s+)?(\S+)(?:\s(.*))?$")
    with open(path, encoding="utf-8-sig", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\r\n")
            if not line.strip():
                continue
            m = line_re.match(line)
            if not m:
                continue
            level = int(m.group(1)); xref = m.group(2)
            tag = m.group(3); val = (m.group(4) or "").strip()
            # maintain the open-tag path: drop anything at this level or deeper
            for lvl in [l for l in tagpath if l >= level]:
                del tagpath[lvl]
            tagpath[level] = tag
            parent = tagpath.get(level - 1)

            if level == 0:
                got_name = False; name_open = False
                if tag == "INDI" and xref:
                    cur = Person(xid=xref); kind = "INDI"; people[xref] = cur
                elif tag == "FAM" and xref:
                    cur = Family(xid=xref); kind = "FAM"; fams[xref] = cur
                else:
                    cur = None; kind = None
                continue
            if cur is None:
                continue

            if level == 1 and tag != "NAME":
                name_open = False

            if kind == "INDI":
                if tag == "NAME" and level == 1:
                    if not got_name:
                        cur.given, cur.surname = split_name(val)
                        got_name = True; name_open = True
                    else:
                        name_open = False
                elif tag == "GIVN" and parent == "NAME" and name_open and val:
                    cur.given = val
                elif tag == "SURN" and parent == "NAME" and name_open and val:
                    cur.surname = val
                elif tag == "SEX" and level == 1:
                    cur.sex = val.upper()[:1]
                elif tag in ("_UID", "RFN", "RIN", "_FSFTID") and level == 1 \
                        and val and not cur.uid:
                    cur.uid = val.replace(" ", "").upper()
                elif tag == "DATE" and parent in ("BIRT", "DEAT"):
                    y = extract_year(val)
                    if parent == "BIRT" and cur.birth_year is None and not cur.birth_raw:
                        cur.birth_year, cur.birth_raw = y, val
                    elif parent == "DEAT" and cur.death_year is None and not cur.death_raw:
                        cur.death_year, cur.death_raw = y, val
                elif tag == "FAMC" and level == 1 and val:
                    cur.famc.append(val)
                elif tag == "FAMS" and level == 1 and val:
                    cur.fams.append(val)
            else:  # FAM
                if tag == "HUSB" and level == 1 and val:
                    cur.husb = val
                elif tag == "WIFE" and level == 1 and val:
                    cur.wife = val
                elif tag == "CHIL" and level == 1 and val:
                    cur.chil.append(val)
    t = Tree(people, fams)
    _reconcile_links(t)
    return t

def _reconcile_links(t: "Tree") -> None:
    """Ensure person.fams/famc agree with family HUSB/WIFE/CHIL, regardless of
    which side the exporter populated. Deduplicate."""
    for fam in t.fams.values():
        for sp in (fam.husb, fam.wife):
            if sp and sp in t.people and fam.xid not in t.people[sp].fams:
                t.people[sp].fams.append(fam.xid)
        for c in fam.chil:
            if c in t.people and fam.xid not in t.people[c].famc:
                t.people[c].famc.append(fam.xid)
    # also honor INDI-side pointers that lack the FAM-side entry
    for p in t.people.values():
        for fx in p.fams:
            fam = t.fams.get(fx)
            if fam and p.xid not in (fam.husb, fam.wife):
                if p.sex == "F" and not fam.wife:
                    fam.wife = p.xid
                elif not fam.husb:
                    fam.husb = p.xid
                elif not fam.wife:
                    fam.wife = p.xid
        for fx in p.famc:
            fam = t.fams.get(fx)
            if fam and p.xid not in fam.chil:
                fam.chil.append(p.xid)

def split_name(raw: str) -> tuple[str, str]:
    m = re.match(r"^(.*?)/([^/]*)/?(.*)$", raw)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return raw.strip(), ""

def extract_year(raw: str) -> int | None:
    m = YEAR_RE.search(raw)
    return int(m.group(1)) if m else None

# ---------------------------------------------------------- string metrics
def norm(s: str) -> str:
    return re.sub(r"[^a-z]", "", s.lower().translate(_DIA_TABLE))

def surname_stem(s: str) -> str:
    """Reduce a surname to a gender-neutral stem so Novák == Nováková.
    Handles the dominant Slovak/Czech feminine suffix -ová (and its
    diacritic-stripped/Anglicized form -ova). Adjectival names
    (Veselý/Veselá) are a known gap for a future dictionary."""
    n = norm(s)
    if n.endswith("ova") and len(n) > 4:
        return n[:-3]
    return n

def first_token(s: str) -> str:
    return norm(s.split()[0]) if s.split() else ""

def jaro(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    md = max(len(a), len(b)) // 2 - 1
    md = max(md, 0)
    a_m = [False] * len(a); b_m = [False] * len(b); matches = 0
    for i, ca in enumerate(a):
        for j in range(max(0, i - md), min(i + md + 1, len(b))):
            if not b_m[j] and b[j] == ca:
                a_m[i] = b_m[j] = True; matches += 1; break
    if matches == 0:
        return 0.0
    t = 0; k = 0
    for i in range(len(a)):
        if a_m[i]:
            while not b_m[k]:
                k += 1
            if a[i] != b[k]:
                t += 1
            k += 1
    t /= 2
    return (matches / len(a) + matches / len(b) + (matches - t) / matches) / 3

def jaro_winkler(a: str, b: str) -> float:
    j = jaro(a, b)
    p = 0
    for ca, cb in zip(a, b):
        if ca == cb and p < 4:
            p += 1
        else:
            break
    return j + p * 0.1 * (1 - j)

def soundex(s: str) -> str:
    s = norm(s).upper()
    if not s:
        return ""
    codes = {**dict.fromkeys("BFPV", "1"), **dict.fromkeys("CGJKQSXZ", "2"),
             **dict.fromkeys("DT", "3"), "L": "4",
             **dict.fromkeys("MN", "5"), "R": "6"}
    first = s[0]
    tail = []
    prev = codes.get(first, "")
    for ch in s[1:]:
        c = codes.get(ch, "")
        if c and c != prev:
            tail.append(c)
        if ch not in "HW":
            prev = c
    return (first + "".join(tail) + "000")[:4]

def canon_given(g: str) -> str:
    t = norm(g.split()[0]) if g.split() else ""
    return _GIVEN_MAP.get(t, t)

# --------------------------------------------------------- pairwise scoring
def year_score(ya, yb):
    if ya is None or yb is None:
        return None            # neutral: no evidence either way
    d = abs(ya - yb)
    if d == 0: return 1.0
    if d == 1: return 0.95
    if d == 2: return 0.85
    if d <= 5: return 0.6
    if d <= 10: return 0.3
    return 0.0

def given_sim(a: Person, b: Person) -> float:
    ga, gb = canon_given(a.given), canon_given(b.given)
    if ga and ga == gb:
        return 1.0
    jw = jaro_winkler(first_token(a.given), first_token(b.given))
    ph = 0.9 if (ga and soundex(ga) == soundex(gb)) else 0.0
    return max(jw, ph)

def surname_sim(a: Person, b: Person, ta: Tree, tb: Tree) -> tuple[float, float]:
    """Return (similarity, weight). Weight is lowered for women to blunt the
    married-name problem; also cross-checks the other side's spouse surname."""
    sa, sb = surname_stem(a.surname), surname_stem(b.surname)
    base = max(jaro_winkler(sa, sb),
               0.9 if (sa and soundex(sa) == soundex(sb)) else 0.0)
    is_female = "F" in (a.sex, b.sex)
    if is_female:
        # maybe B recorded her under a spouse's surname (or vice versa)
        for sp in tb.spouses(b.xid):
            base = max(base, jaro_winkler(sa, surname_stem(tb.people[sp].surname)))
        for sp in ta.spouses(a.xid):
            base = max(base, jaro_winkler(sb, surname_stem(ta.people[sp].surname)))
        return base, 0.10
    return base, 0.20

def attribute_score(a: Person, b: Person, ta: Tree, tb: Tree) -> tuple[float, list[str]]:
    ev = []
    feats = []  # (value, weight)
    gs = given_sim(a, b)
    feats.append((gs, 0.30)); ev.append(f"given {a.given!r}~{b.given!r}={gs:.2f}")
    ss, sw = surname_sim(a, b, ta, tb)
    feats.append((ss, sw)); ev.append(f"surname {a.surname!r}~{b.surname!r}={ss:.2f}")
    by = year_score(a.birth_year, b.birth_year)
    if by is not None:
        feats.append((by, 0.30)); ev.append(f"birth {a.birth_year}~{b.birth_year}={by:.2f}")
    dy = year_score(a.death_year, b.death_year)
    if dy is not None:
        feats.append((dy, 0.10)); ev.append(f"death {a.death_year}~{b.death_year}={dy:.2f}")
    if a.sex and b.sex:
        sxs = 1.0 if a.sex == b.sex else 0.0
        feats.append((sxs, 0.10)); ev.append(f"sex {a.sex}~{b.sex}={sxs:.0f}")
    wsum = sum(w for _, w in feats) or 1.0
    score = sum(v * w for v, w in feats) / wsum
    # Sex mismatch is normally a veto (distinguishes same-named brother/sister).
    # But when the given name AND both dates match exactly, two different people
    # are essentially impossible -- it's a data-entry error -- so let it match
    # and surface the sex difference as a conflict instead of vetoing.
    if a.sex and b.sex and a.sex != b.sex:
        by = year_score(a.birth_year, b.birth_year)
        dy2 = year_score(a.death_year, b.death_year)
        overwhelming = (given_sim(a, b) >= STRONG_GIVEN
                        and by is not None and by >= 0.85
                        and dy2 is not None and dy2 >= 0.85)
        if overwhelming:
            ev.append("SEX MISMATCH overridden (name+both dates exact; "
                      "likely a data error -- see conflicts)")
        else:
            score = min(score, 0.20)
    return score, ev

def structural_bonus(a: str, b: str, ta: Tree, tb: Tree, matched: dict[str, str]) -> tuple[float, list[str]]:
    """Reward already-matched relatives. matched maps A-id -> B-id."""
    bonus = 0.0; ev = []
    def rel_overlap(rel_a, rel_b, label, per, cap):
        nonlocal bonus
        hit = 0
        rb = set(rel_b)
        for ra in rel_a:
            if matched.get(ra) in rb:
                hit += 1
        if hit:
            add = min(hit * per, cap)
            bonus += add
            ev.append(f"{label}:{hit} matched (+{add:.2f})")
    rel_overlap(ta.parents(a), tb.parents(b), "parents", 0.12, 0.24)
    rel_overlap(ta.spouses(a), tb.spouses(b), "spouse", 0.20, 0.20)
    rel_overlap(ta.children(a), tb.children(b), "children", 0.06, 0.18)
    rel_overlap(ta.siblings(a), tb.siblings(b), "siblings", 0.05, 0.15)
    return bonus, ev

STRONG_GIVEN = 0.85       # given-name similarity that counts as identity evidence

def identity_gate(score, gs, by, dy, structural):
    """Surname is corroborating, not identifying. To CONFIRM a match we need
    real identity evidence: a matched relative, an agreeing date, or a strong
    given-name match. by/dy are birth/death year_scores (None if a date is
    missing on either side). Returns a possibly-capped score:
      * structural support, or a date agreeing within ~5y -> unchanged
      * all present dates clearly disagree (>10y), no structure -> reject
      * no usable date, weak given, no structure -> reject (shared surname only)
      * no usable date, strong given, no structure -> review band (ambiguous)
    """
    if structural > 0:
        return score
    present = [x for x in (by, dy) if x is not None]
    if present:
        best = max(present)
        if best >= 0.6:                    # a date agrees within ~5 years
            return score
        if best == 0.0:                    # every present date is >10y off
            return min(score, TAU_LOW - 0.01)
        # 6-10y gap: too weak to rely on; fall through to the name test
    if gs >= STRONG_GIVEN:
        return min(score, TAU_HIGH - 0.01)   # plausible, not certain -> review
    return min(score, TAU_LOW - 0.01)        # surname only -> reject

def score_pair(a, b, ta, tb, matched):
    pa, pb = ta.people[a], tb.people[b]
    base, ev = attribute_score(pa, pb, ta, tb)
    sb, sev = structural_bonus(a, b, ta, tb, matched)
    raw = min(1.0, base + sb)
    gated = identity_gate(raw, given_sim(pa, pb),
                          year_score(pa.birth_year, pb.birth_year),
                          year_score(pa.death_year, pb.death_year), sb)
    ev = ev + sev
    if gated < raw - 1e-9:
        ev = ev + [f"gated {raw:.2f}->{gated:.2f} (surname-only, no date/relative)"]
    return gated, ev

# ---------------------------------------------------------------- scope
def blood_kindred(tree: Tree, root: str) -> set:
    """All consanguineal relatives of root: BFS over parent/child edges only,
    never through spouses. Yields ancestors, descendants, siblings, cousins,
    aunts/uncles, nieces/nephews — everyone blood-related."""
    if root not in tree.people:
        return set()
    seen = {root}; stack = [root]
    while stack:
        x = stack.pop()
        for r in tree.parents(x) + tree.children(x):
            if r not in seen:
                seen.add(r); stack.append(r)
    return seen

def scope_sets(tree: Tree, root: str) -> tuple[set, set]:
    """Return (blood, scope). blood = kindred; scope = blood + their spouses
    (spouses are terminal leaves, never traversed through)."""
    blood = blood_kindred(tree, root)
    scope = set(blood)
    for k in blood:
        scope.update(tree.spouses(k))
    return blood, scope

# ------------------------------------------------------------- match engine
class Matcher:
    def __init__(self, ta: Tree, tb: Tree, review, blood_a=None, scope_a=None):
        self.ta, self.tb = ta, tb
        self.review = review                 # callable(a,b,score,ev)->bool|None
        self.matched: dict[str, str] = {}    # A-id -> B-id  (confirmed)
        self.used_b: set[str] = set()
        self.evidence: dict[str, dict] = {}
        self.uncertain: list[dict] = []
        # scope: if provided, only A-people in scope_a are eligible; in-law
        # anchors (in scope but not blood) are terminal and never expand.
        self.blood_a = blood_a               # None => scoping disabled
        self.scope_a = scope_a
        self.scope_hops = 2                   # blocking B-neighborhood radius
        self.asked: dict = {}                # (a,b) -> decision, so the
                                             # fixpoint loop never re-asks
        self.pending: dict = {}              # (a,b)->(s,ev): review-band pairs
                                             # deferred until structure settles
        self.dup_b: dict = {}                # b -> (b_kept, a): Ancestry-side
                                             # duplicate of an already-matched b

    def _review(self, a, b, s, ev):
        key = (a, b)
        if key in self.asked:
            return self.asked[key][0]
        d = self.review(a, b, s, ev)
        self.asked[key] = (d, s, ev)
        return d

    def confirm(self, a, b, score, ev):
        if a in self.matched or b in self.used_b:
            return
        self.matched[a] = b; self.used_b.add(b)
        self.evidence[a] = {"b": b, "confidence": round(score, 3), "evidence": ev}

    def uid_pass(self):
        by_uid = defaultdict(list)
        for p in self.tb.people.values():
            if p.uid:
                by_uid[p.uid].append(p.xid)
        n = 0
        for a in self.ta.people.values():
            if a.uid and len(by_uid.get(a.uid, [])) == 1:
                b = by_uid[a.uid][0]
                if a.xid not in self.matched and b not in self.used_b:
                    self.confirm(a.xid, b, 1.0, [f"_UID exact {a.uid}"]); n += 1
        return n

    def seed(self, pairs):
        for a, b in pairs:
            if a in self.ta.people and b in self.tb.people:
                self.confirm(a, b, 1.0, ["seed (user-provided)"])

    def auto_seed(self):
        """Bootstrap from strong, unique attribute matches (given+surname
        canonical + birth year exact) that are unambiguous on both sides."""
        def key(p, tree):
            if p.birth_year is None:
                return None
            return (canon_given(p.given), soundex(p.surname), p.birth_year)
        a_keys, b_keys = defaultdict(list), defaultdict(list)
        for p in self.ta.people.values():
            if not self._in_scope_a(p.xid):
                continue
            k = key(p, self.ta)
            if k: a_keys[k].append(p.xid)
        for p in self.tb.people.values():
            k = key(p, self.tb)
            if k: b_keys[k].append(p.xid)
        for k, alist in a_keys.items():
            blist = b_keys.get(k, [])
            if len(alist) == 1 and len(blist) == 1:
                self.confirm(alist[0], blist[0], 0.99, ["auto-seed unique attr key"])

    def _in_scope_a(self, a):
        return self.scope_a is None or a in self.scope_a

    def _is_blood_a(self, a):
        return self.blood_a is None or a in self.blood_a

    def _neighbors(self, a, b):
        """Candidate A/B pairings among relatives of a confirmed (a,b).
        When scoping is on, an in-law anchor (in scope but not blood) is
        terminal: it expands nowhere, so we never traverse into an in-law's
        own ancestors or siblings."""
        ta, tb = self.ta, self.tb
        if self.scope_a is not None and not self._is_blood_a(a):
            return  # in-law: terminal
        for ra, rb in ((ta.parents(a), tb.parents(b)),
                       (ta.spouses(a), tb.spouses(b)),
                       (ta.children(a), tb.children(b)),
                       (ta.siblings(a), tb.siblings(b))):
            for x in ra:
                if not self._in_scope_a(x):
                    continue
                for y in rb:
                    yield x, y

    def propagate(self):
        heap = []; seen = set(); counter = 0
        def push(a, b):
            nonlocal counter
            if a in self.matched or b in self.used_b or (a, b) in seen:
                return
            seen.add((a, b))
            s, ev = score_pair(a, b, self.ta, self.tb, self.matched)
            counter += 1
            heapq.heappush(heap, (-s, counter, a, b, ev))
        for a, b in list(self.matched.items()):
            for x, y in self._neighbors(a, b):
                push(x, y)
        while heap:
            neg, _, a, b, ev = heapq.heappop(heap)
            if a in self.matched or b in self.used_b:
                continue
            # rescore: matched-set has grown since this was enqueued
            s, ev = score_pair(a, b, self.ta, self.tb, self.matched)
            if s >= TAU_HIGH:
                self.confirm(a, b, s, ev)
                for x, y in self._neighbors(a, b):
                    push(x, y)
            elif s >= TAU_LOW:
                # defer: might gain structural support later in the fixpoint
                old = self.pending.get((a, b))
                if old is None or s > old[0]:
                    self.pending[(a, b)] = (s, ev)

    def _b_neighborhood(self, k):
        """B people within k family-hops (parent/child/spouse) of an already-
        matched B person. Confines matching to the relevant sub-tree so
        unrelated branches in a huge shared tree aren't offered as candidates."""
        seen = set(self.used_b); frontier = set(self.used_b)
        for _ in range(k):
            nxt = set()
            for b in frontier:
                nxt.update(self.tb.parents(b))
                nxt.update(self.tb.spouses(b))
                nxt.update(self.tb.children(b))
            frontier = nxt - seen
            seen |= frontier
        return seen

    def blocking_pass(self):
        """Catch fragments the flood-fill never reached. Bucket by the
        feminine-aware surname stem, then assign by GLOBAL SCORE ORDER within
        each bucket -- confirm the best pair first and skip any pair whose
        person is already taken. This stops a weak namesake (e.g. a dateless
        grandson) from grabbing a record that a strong match (the grandfather,
        with agreeing dates) should claim. The identity gate still applies.
        When scope is active, B candidates are limited to the neighborhood of
        the matched set so unrelated branches of a shared tree aren't offered."""
        def bkey(p):
            return soundex(surname_stem(p.surname))
        b_allowed = None
        if self.blood_a is not None and self.used_b:
            b_allowed = self._b_neighborhood(self.scope_hops)
        buckets = defaultdict(lambda: ([], []))
        for p in self.ta.people.values():
            if p.xid not in self.matched and self._in_scope_a(p.xid):
                buckets[bkey(p)][0].append(p.xid)
        for p in self.tb.people.values():
            if p.xid not in self.used_b and (b_allowed is None or p.xid in b_allowed):
                buckets[bkey(p)][1].append(p.xid)
        cands = []
        for (alist, blist) in buckets.values():
            for a in alist:
                for b in blist:
                    s, ev = score_pair(a, b, self.ta, self.tb, self.matched)
                    if s >= TAU_LOW:
                        cands.append((s, a, b, ev))
        cands.sort(key=lambda t: -t[0])
        for s, a, b, ev in cands:
            if a in self.matched or b in self.used_b:
                continue
            ev2 = ev + ["(blocking)"]
            if s >= TAU_HIGH:
                self.confirm(a, b, s, ev2)
            else:
                old = self.pending.get((a, b))
                if old is None or s > old[0]:
                    self.pending[(a, b)] = (s, ev2)

    def _drain_pending_high(self):
        """Confirm any deferred pair that now clears TAU_HIGH (its relatives
        got matched since it was deferred). Returns True if anything changed."""
        changed = False
        for (a, b), (s, ev) in list(self.pending.items()):
            if a in self.matched or b in self.used_b:
                del self.pending[(a, b)]; continue
            ns, nev = score_pair(a, b, self.ta, self.tb, self.matched)
            if ns >= TAU_HIGH:
                self.confirm(a, b, ns, nev)
                del self.pending[(a, b)]; changed = True
        return changed

    def _detect_b_duplicates(self):
        """A still-unmatched B record that strongly matches an already-matched
        A person is an Ancestry-side duplicate of that A person's real match --
        not a discovery. Bucket by surname stem for speed."""
        by_bucket = defaultdict(list)
        for a, b in self.matched.items():
            by_bucket[soundex(surname_stem(self.ta.people[a].surname))].append(a)
        for b in set(self.tb.people) - self.used_b:
            k = soundex(surname_stem(self.tb.people[b].surname))
            best = None
            for a in by_bucket.get(k, []):
                # attribute-only (empty matched set): a true duplicate is the
                # same IDENTITY -- same name+dates -- not merely shared family,
                # or siblings would look like duplicates of each other.
                s, _ = score_pair(a, b, self.ta, self.tb, {})
                if s >= TAU_HIGH and (best is None or s > best[0]):
                    best = (s, a)
            if best:
                self.dup_b[b] = (self.matched[best[1]], best[1])

    def run(self, use_uid=False, do_auto_seed=False):
        if use_uid:
            self.uid_pass()
        if do_auto_seed:
            self.auto_seed()
        # Phase 1: auto-confirm to a fixpoint. Prompts are deferred to
        # self.pending so people who will gain structural support (matched
        # relatives) confirm silently instead of being asked prematurely.
        for _ in range(100):
            before = len(self.matched)
            self._drain_pending_high()
            self.propagate()
            self.blocking_pass()
            if len(self.matched) == before:
                break
        # Phase 2: Ancestry-internal duplicates are not discoveries.
        self._detect_b_duplicates()
        # Phase 3: ask only about what's still ambiguous -- no structure, no
        # date, not a known duplicate. Strongest first.
        for (a, b), (s, ev) in sorted(self.pending.items(), key=lambda kv: -kv[1][0]):
            if a in self.matched or b in self.used_b or b in self.dup_b:
                continue
            ns, nev = score_pair(a, b, self.ta, self.tb, self.matched)
            if ns >= TAU_HIGH:
                self.confirm(a, b, ns, nev)
            elif ns >= TAU_LOW:
                if self._review(a, b, ns, nev) is True:
                    self.confirm(a, b, ns, nev)
        self.uncertain = [
            {"a": a, "b": b, "confidence": round(s, 3), "evidence": ev}
            for (a, b), (d, s, ev) in self.asked.items()
            if d is None and a not in self.matched and b not in self.used_b
        ]

# ------------------------------------------------------------------- output
def classify_scope_b(m: Matcher, only_b: list) -> tuple[list, list]:
    """Split only-in-B people into in-scope (a real discovery to import) vs
    out-of-scope (e.g. an in-law's ancestors/siblings we don't want).
    In-scope iff, in B, the person is parent/child/sibling of a matched BLOOD
    anchor (that link makes them blood too), or a spouse of one."""
    if m.blood_a is None:
        return list(only_b), []          # scoping disabled
    tb = m.tb
    matched_blood_b = {b for a, b in m.matched.items() if a in m.blood_a}
    in_scope, out_scope = [], []
    for b in only_b:
        blood_nb = set(tb.parents(b) + tb.children(b) + tb.siblings(b))
        spouse_nb = set(tb.spouses(b))
        if blood_nb & matched_blood_b or spouse_nb & matched_blood_b:
            in_scope.append(b)
        else:
            out_scope.append(b)
    return in_scope, out_scope

def build_diff(m: Matcher):
    ta, tb = m.ta, m.tb
    only_a = sorted(set(ta.people) - set(m.matched))
    only_b_all = sorted(set(tb.people) - m.used_b)
    in_scope_b, out_scope_b = classify_scope_b(m, only_b_all)
    dups = [{"b": b, "duplicate_of_b": kept, "matched_a": a,
             "name": f"{tb.people[b].given} {tb.people[b].surname}".strip()}
            for b, (kept, a) in sorted(m.dup_b.items())]
    dup_ids = set(m.dup_b)
    in_scope_b = [b for b in in_scope_b if b not in dup_ids]
    out_scope_b = [b for b in out_scope_b if b not in dup_ids]
    conflicts = []
    for a, b in m.matched.items():
        pa, pb = ta.people[a], tb.people[b]
        for fld, va, vb in (("birth_year", pa.birth_year, pb.birth_year),
                            ("death_year", pa.death_year, pb.death_year),
                            ("surname", pa.surname, pb.surname),
                            ("sex", pa.sex, pb.sex)):
            if va and vb and va != vb:
                conflicts.append({"a": a, "b": b, "field": fld,
                                  "a_val": va, "b_val": vb})
    matches = [{"a": a, **info} for a, info in sorted(m.evidence.items())]
    bridges = detect_bridges(m, set(in_scope_b))
    return {"matches": matches, "conflicts": conflicts,
            "only_in_a": only_a,
            "only_in_b": in_scope_b,
            "only_in_b_out_of_scope": out_scope_b,
            "duplicates_in_b": dups,
            "bridges": bridges, "uncertain": m.uncertain}

def detect_bridges(m: Matcher, only_b: set):
    """Families in B linking an already-matched (existing) person to a new
    (only-in-B) person. A new CHILD attaches to its matched parents; a new
    SPOUSE attaches to the matched other spouse -- never to siblings."""
    tb = m.tb
    b2a = {b: a for a, b in m.matched.items()}
    def label(pid):
        p = tb.people.get(pid)
        return (f"{p.given} {p.surname}".strip() or "(no name)") if p else pid
    out = []
    for fam in tb.fams.values():
        members = [x for x in ([fam.husb, fam.wife] + fam.chil) if x]
        newbies = [x for x in members if x in only_b]
        if not newbies:
            continue
        parents = [x for x in (fam.husb, fam.wife) if x]
        new_desc = []
        for x in newbies:
            if x in fam.chil:
                role = "child"; anchors = [p for p in parents if p in m.used_b]
            else:
                role = "spouse"
                other = fam.wife if x == fam.husb else fam.husb
                anchors = [other] if (other and other in m.used_b) else []
            if not anchors:
                continue                     # pure new-to-new: not a bridge
            new_desc.append({"b": x, "name": label(x), "role": role,
                             "attaches_to": [{"a": b2a[o], "b": o,
                                              "name": label(o)} for o in anchors]})
        if new_desc:
            out.append({"family_b": fam.xid, "new_people": new_desc})
    return out

def write_new_only_gedcom(m: Matcher, path: str):
    """Emit in-scope new individuals plus the families that link them, so
    relationships survive import without clutter. Guarantees around parents:
      * a kept family's parents are ALWAYS emitted when they exist in the source
        -- never a parentless family when the parents are known
      * a parent already in your tree (matched, or reached via a duplicate
        record) is a labelled STUB carrying its Gramps ID, so it merges
      * a parent not in your tree is emitted as a labelled context record you
        can review or delete
      * a family is kept only if it links >=2 people (a lone member is dropped)
      * children are emitted only when new; HUSB/WIFE are placed by SEX so a
        source that swapped the couple can't create a conflicting family"""
    tb = m.tb
    only_b_all = sorted(set(tb.people) - m.used_b)
    in_scope_b, _ = classify_scope_b(m, only_b_all)
    new_ids = set(in_scope_b) - set(m.dup_b)
    b2a = {b: a for a, b in m.matched.items()}
    dup_map = m.dup_b                         # b -> (kept_b, gramps_a)

    def gref(pid):                            # Gramps id if this B person is in
        if pid in b2a:                        # your tree (matched, or a duplicate
            return b2a[pid]                   # of a matched person)
        if pid in dup_map:
            return dup_map[pid][1]
        return None

    def sex_of(pid):                          # Gramps-side sex when in your tree
        g = gref(pid)
        if g and g in m.ta.people:
            return m.ta.people[g].sex or tb.people[pid].sex
        return tb.people[pid].sex

    # Decide which families to emit and which parents each should carry.
    #   * A child-bearing family emits ALL its known parents so no child is
    #     orphaned -- an out-of-scope parent comes along as a labelled context
    #     record. Kept when it links >=2 people.
    #   * A childless couple is worth emitting only to bridge a NEW spouse to a
    #     spouse you already have (new or in your tree); an out-of-scope spouse
    #     is never dragged in to complete a lone marriage.
    fam_keep = []                             # (fam, parents_to_emit, new_children)
    for fam in tb.fams.values():
        present = [x for x in (fam.husb, fam.wife) if x]
        new_kids = [c for c in fam.chil if c in new_ids]
        if new_kids:
            if len(present) + len(new_kids) >= 2:
                fam_keep.append((fam, present, new_kids))
        else:
            core = [x for x in present if x in new_ids or gref(x) is not None]
            if any(x in new_ids for x in core) and len(core) >= 2:
                fam_keep.append((fam, core, []))

    # Classify every parent a kept family will carry.
    stub_parents, context_parents = set(), set()
    for _, parents, _ in fam_keep:
        for p in parents:
            if p in new_ids:
                continue                      # a discovery -- full INDI below
            if gref(p) is not None:
                stub_parents.add(p)           # in your tree -> merge stub
            else:
                context_parents.add(p)        # not in your tree -> labelled

    def fam_links(pid):
        out = []
        for fam, parents, kids in fam_keep:
            if pid in parents:
                out.append(f"1 FAMS {fam.xid}")
            if pid in kids:
                out.append(f"1 FAMC {fam.xid}")
        return out

    lines = ["0 HEAD", "1 SOUR gedmatch", "1 GEDC", "2 VERS 5.5.1",
             "2 FORM LINEAGE-LINKED", "1 CHAR UTF-8"]
    for pid in sorted(new_ids):
        p = tb.people[pid]
        lines.append(f"0 {pid} INDI")
        lines.append(f"1 NAME {p.given} /{p.surname}/")
        if p.sex:
            lines.append(f"1 SEX {p.sex}")
        if p.birth_raw or p.birth_year:
            lines += ["1 BIRT", f"2 DATE {p.birth_raw or p.birth_year}"]
        if p.death_raw or p.death_year:
            lines += ["1 DEAT", f"2 DATE {p.death_raw or p.death_year}"]
        lines += fam_links(pid)
    for pid in sorted(stub_parents):
        p = tb.people[pid]; gid = gref(pid)
        dupe = " (via a duplicate record)" if pid in dup_map else ""
        lines.append(f"0 {pid} INDI")
        lines.append(f"1 NAME {p.given} /{p.surname}/")
        sx = sex_of(pid)
        if sx:
            lines.append(f"1 SEX {sx}")
        lines.append(f"1 REFN {gid}")
        lines.append(f"1 NOTE [gedmatch] already in your tree as {gid}{dupe} -- merge this stub")
        lines += fam_links(pid)
    for pid in sorted(context_parents):
        p = tb.people[pid]
        lines.append(f"0 {pid} INDI")
        lines.append(f"1 NAME {p.given} /{p.surname}/")
        if p.sex:
            lines.append(f"1 SEX {p.sex}")
        if p.birth_raw or p.birth_year:
            lines += ["1 BIRT", f"2 DATE {p.birth_raw or p.birth_year}"]
        if p.death_raw or p.death_year:
            lines += ["1 DEAT", f"2 DATE {p.death_raw or p.death_year}"]
        lines.append("1 NOTE [gedmatch] parent not in your tree / not matched -- review or delete")
        lines += fam_links(pid)
    for fam, parents, kids in fam_keep:
        males = [p for p in parents if sex_of(p) == "M"]
        females = [p for p in parents if sex_of(p) == "F"]
        if len(males) == 1 and len(females) == 1:      # normalize by sex
            husb, wife = males[0], females[0]
        else:                                          # fall back to source slots
            husb = fam.husb if fam.husb in parents else None
            wife = fam.wife if fam.wife in parents else None
            for p in parents:                          # place any leftover
                if p not in (husb, wife):
                    if husb is None: husb = p
                    elif wife is None: wife = p
        lines.append(f"0 {fam.xid} FAM")
        if husb:
            lines.append(f"1 HUSB {husb}")
        if wife:
            lines.append(f"1 WIFE {wife}")
        for c in kids:
            lines.append(f"1 CHIL {c}")
    lines.append("0 TRLR")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return len(new_ids), len(stub_parents), len(fam_keep), len(context_parents)

# --------------------------------------------------------------- review UI
def describe_relatives(tree: Tree, pid: str, maxn: int = 5) -> str:
    """Compact 'parents: ..; spouse: ..; children: ..' for a person, to give
    context when a match has no dates to judge by."""
    if pid not in tree.people:
        return ""
    def names(ids):
        out = []
        for i in ids[:maxn]:
            q = tree.people.get(i)
            if q:
                out.append(f"{q.given} {q.surname}".strip() or "(no name)")
        if len(ids) > maxn:
            out.append(f"+{len(ids) - maxn} more")
        return ", ".join(out)
    parts = []
    if tree.parents(pid): parts.append(f"parents: {names(tree.parents(pid))}")
    if tree.spouses(pid): parts.append(f"spouse: {names(tree.spouses(pid))}")
    if tree.children(pid): parts.append(f"children: {names(tree.children(pid))}")
    return "  ".join(parts)

def make_cli_review(ta, tb):
    def review(a, b, score, ev):
        pa, pb = ta.people[a], tb.people[b]
        print(f"\n? Possible match (score {score:.2f}):")
        print(f"   A {a}: {pa.given} {pa.surname} b.{pa.birth_year} d.{pa.death_year}")
        ra = describe_relatives(ta, a)
        if ra: print(f"        {ra}")
        print(f"   B {b}: {pb.given} {pb.surname} b.{pb.birth_year} d.{pb.death_year}")
        rb = describe_relatives(tb, b)
        if rb: print(f"        {rb}")
        print("   why:", "; ".join(ev))
        ans = input("   same person? [y]es/[n]o/[s]kip: ").strip().lower()
        if ans.startswith("y"): return True
        if ans.startswith("n"): return False
        return None
    return review

def auto_review(a, b, score, ev):
    return None  # non-interactive: record as uncertain, never guess

def explain_pair(m: Matcher, a: str, b: str) -> str:
    ta, tb = m.ta, m.tb
    L = [f"--- explain {a}  vs  {b} ---"]
    if a not in ta.people:
        return "\n".join(L + [f"  {a} is not in A"])
    if b not in tb.people:
        return "\n".join(L + [f"  {b} is not in B"])
    pa, pb = ta.people[a], tb.people[b]
    L.append(f"  A {a}: {pa.given} {pa.surname} b.{pa.birth_year} d.{pa.death_year}")
    ra = describe_relatives(ta, a)
    if ra: L.append(f"        {ra}")
    L.append(f"  B {b}: {pb.given} {pb.surname} b.{pb.birth_year} d.{pb.death_year}")
    rb = describe_relatives(tb, b)
    if rb: L.append(f"        {rb}")
    if m.scope_a is not None:
        L.append(f"  A in scope: {a in m.scope_a}"
                 f"  (blood kindred: {a in m.blood_a})")
        if a not in m.scope_a:
            L.append("  -> EXCLUDED: A is out of scope, so it was never considered")
    b2a = {v: k for k, v in m.matched.items()}
    if a in m.matched and m.matched[a] != b:
        o = m.matched[a]; op = tb.people.get(o)
        L.append(f"  -> A is already matched to {o} "
                 f"({op.given} {op.surname} b.{op.birth_year})" if op else o)
    if b in m.used_b and b2a.get(b) != a:
        o = b2a.get(b); op = ta.people.get(o)
        L.append(f"  -> B is already matched from {o} "
                 f"({op.given} {op.surname} b.{op.birth_year})" if op else o)
    if m.matched.get(a) == b:
        L.append("  -> these two ARE matched to each other")
    ka = soundex(surname_stem(pa.surname)); kb = soundex(surname_stem(pb.surname))
    L.append(f"  blocking bucket: A={ka} B={kb} same={ka == kb}")
    s0, _ = score_pair(a, b, ta, tb, {})
    s1, ev = score_pair(a, b, ta, tb, m.matched)
    L.append(f"  score on attributes alone: {s0:.2f}")
    L.append(f"  score with matched relatives: {s1:.2f}")
    L.append(f"  evidence: {'; '.join(ev)}")
    verdict = ("AUTO-CONFIRM" if s1 >= TAU_HIGH else
               "REVIEW (uncertain)" if s1 >= TAU_LOW else "REJECT (below floor)")
    L.append(f"  verdict for this pair at final state: {verdict}")
    return "\n".join(L)

# ------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description="Reconcile two GEDCOM files.")
    ap.add_argument("gedcom_a", help="A = your Gramps export")
    ap.add_argument("gedcom_b", help="B = the Ancestry/MyHeritage export")
    ap.add_argument("--seed", default="", help="anchor matches: Aid=Bid,Aid=Bid")
    ap.add_argument("--auto-seed", action="store_true")
    ap.add_argument("--use-uid", action="store_true", help="exact _UID pre-pass")
    ap.add_argument("--interactive", action="store_true", help="prompt on ambiguous")
    ap.add_argument("--root", default="", help="A-side id(s) of the root "
                    "person(s), comma-separated; restricts matching to blood "
                    "kindred of any root + their spouses (e.g. you + your wife)")
    ap.add_argument("--max-degree", type=int, default=0,
                    help="(reserved) cap blood distance from root; 0 = no cap")
    ap.add_argument("--scope-hops", type=int, default=2,
                    help="with --root, blocking only considers B people within "
                    "this many family-hops of the matched set (default 2); "
                    "keeps unrelated branches of a shared tree out of the prompts")
    ap.add_argument("--out-json", default="diff.json")
    ap.add_argument("--out-ged", default="new_only.ged")
    ap.add_argument("--explain", default="", help="after matching, diagnose "
                    "why pairs did/didn't match: Aid=Bid,Aid=Bid")
    args = ap.parse_args(argv)

    ta = parse_gedcom(args.gedcom_a)
    tb = parse_gedcom(args.gedcom_b)
    print(f"A: {len(ta.people)} people / {len(ta.fams)} families")
    print(f"B: {len(tb.people)} people / {len(tb.fams)} families")

    blood_a = scope_a = None
    if args.root:
        roots = [r.strip() for r in args.root.split(",") if r.strip()]
        missing = [r for r in roots if r not in ta.people]
        if missing:
            sys.exit(f"root(s) not found in A: {', '.join(missing)}")
        blood_a = set()
        for r in roots:
            blood_a |= blood_kindred(ta, r)
        scope_a = set(blood_a)
        for k in blood_a:
            scope_a.update(ta.spouses(k))
        print(f"scope: {len(blood_a)} blood kindred from {len(roots)} root(s), "
              f"{len(scope_a)} incl. spouses (of {len(ta.people)} in A)")

    review = make_cli_review(ta, tb) if args.interactive else auto_review
    m = Matcher(ta, tb, review, blood_a=blood_a, scope_a=scope_a)
    m.scope_hops = args.scope_hops
    if args.seed:
        pairs = [tuple(p.split("=")) for p in args.seed.split(",") if "=" in p]
        m.seed(pairs)
    m.run(use_uid=args.use_uid, do_auto_seed=args.auto_seed)

    diff = build_diff(m)
    with open(args.out_json, "w", encoding="utf-8") as fh:
        json.dump(diff, fh, indent=2, ensure_ascii=False)
    npeople, nanchor, nfam, nctx = write_new_only_gedcom(m, args.out_ged)

    print(f"\nmatched:      {len(m.matched)}")
    print(f"only in A:    {len(diff['only_in_a'])}")
    print(f"only in B:    {len(diff['only_in_b'])}  -> {args.out_ged} "
          f"({npeople} new indi, {nfam} fam, {nanchor} anchor stubs to merge"
          + (f", {nctx} context parents to review" if nctx else "") + ")")
    print(f"  out of scope: {len(diff['only_in_b_out_of_scope'])}  (excluded from import)")
    print(f"  duplicates:   {len(diff['duplicates_in_b'])}  (duplicate of an already-matched person (in the imported tree))")
    print(f"conflicts:    {len(diff['conflicts'])}")
    print(f"bridges:      {len(diff['bridges'])}  (new people attached to existing)")
    print(f"uncertain:    {len(diff['uncertain'])}  (need your review)")
    print(f"wrote {args.out_json}")

    if args.explain:
        print()
        for pair in args.explain.split(","):
            if "=" in pair:
                a, b = pair.split("=", 1)
                print(explain_pair(m, a.strip(), b.strip()))
                print()

if __name__ == "__main__":
    main()
