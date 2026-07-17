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
import argparse, json, os, re, sys
from dataclasses import dataclass, field
from collections import defaultdict, deque
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
class GDate:
    """A parsed GEDCOM date. year/month/day may be None; qual is one of
    '', 'ABT', 'EST', 'CAL', 'BEF', 'AFT' (range forms collapse to their first
    year with an approximate qualifier)."""
    year: int | None = None
    month: int | None = None
    day: int | None = None
    qual: str = ""
    raw: str = ""
    @property
    def known(self) -> bool:
        return self.year is not None
    @property
    def exact(self) -> bool:                 # a full, unqualified calendar date
        return self.day is not None and self.qual == ""

@dataclass
class Person:
    xid: str
    given: str = ""
    surname: str = ""
    sex: str = ""
    birth_year: int | None = None            # EFFECTIVE year (birth, else bapt)
    birth_raw: str = ""
    death_year: int | None = None            # EFFECTIVE year (death, else buri)
    death_raw: str = ""
    uid: str = ""
    birth: GDate = field(default_factory=GDate)
    bapt: GDate = field(default_factory=GDate)   # baptism / christening fallback
    death: GDate = field(default_factory=GDate)
    buri: GDate = field(default_factory=GDate)   # burial fallback
    raw_lines: list[str] = field(default_factory=list)  # verbatim INDI subrecord
    famc: list[str] = field(default_factory=list)   # families where child
    fams: list[str] = field(default_factory=list)   # families where spouse

    def eff_birth(self) -> GDate:
        """Best available 'born about' date: birth if recorded, else the
        baptism/christening date (Gramps-style precedence)."""
        return self.birth if self.birth.known else self.bapt
    def eff_death(self) -> GDate:
        return self.death if self.death.known else self.buri

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

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
_APPROX = {"ABT": "ABT", "ABOUT": "ABT", "CIRCA": "ABT", "CIR": "ABT", "C": "ABT",
           "EST": "EST", "ESTIMATED": "EST", "CAL": "CAL", "CALCULATED": "CAL",
           "INT": "EST", "MAYBE": "ABT", "PROB": "ABT"}
_BOUND = {"BEF": "BEF", "BEFORE": "BEF", "AFT": "AFT", "AFTER": "AFT"}
_RANGE = {"BET", "AND", "FROM", "TO"}          # ranges: take first year, mark ABT

def parse_date(val: str) -> GDate:
    """Parse a GEDCOM DATE value into a GDate. Tolerant of Gramps/Ancestry/MH
    dialects: 'ABT 1872', 'BEF 12 MAR 1900', 'MAR 1978', '1978',
    'BET 1800 AND 1810' (-> ~1800), day/month/year in any of the usual orders."""
    raw = (val or "").strip()
    if not raw:
        return GDate(raw="")
    qual = ""
    year = month = day = None
    saw_range = False
    for t in raw.upper().replace(",", " ").split():
        if t in _APPROX and not qual:
            qual = _APPROX[t]; continue
        if t in _BOUND and not qual:
            qual = _BOUND[t]; continue
        if t in _RANGE:
            if t in ("BET", "FROM"):
                saw_range = True
            elif t in ("AND", "TO"):          # stop at the second bound
                break
            continue
        if t in _MONTHS and month is None:
            month = _MONTHS[t]; continue
        if t.isdigit():
            n = int(t)
            if len(t) == 4 and year is None:
                year = n
            elif n <= 31 and day is None:
                day = n
            elif year is None:
                year = n
    if saw_range and not qual:
        qual = "ABT"
    return GDate(year, month, day, qual, raw)

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
                cur.raw_lines.append(line)    # verbatim subrecord for re-emit
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
                elif tag == "DATE" and parent in ("BIRT", "DEAT", "BAPM",
                                                  "CHR", "BURI"):
                    gd = parse_date(val)
                    slot = {"BIRT": "birth", "DEAT": "death", "BAPM": "bapt",
                            "CHR": "bapt", "BURI": "buri"}[parent]
                    if not getattr(cur, slot).raw:   # first date for this event
                        setattr(cur, slot, gd)
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
    for p in people.values():                # effective year: birth else bapt,
        eb, ed = p.eff_birth(), p.eff_death() # death else burial (Gramps-style)
        p.birth_year, p.birth_raw = eb.year, eb.raw
        p.death_year, p.death_raw = ed.year, ed.raw
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

def date_score(da: GDate, db: GDate):
    """Compare two GDates for SCORING (corroboration). None when either is
    unknown (neutral). A matching year scores full strength; when the years
    differ and either date is approximate or year-only, the gap is treated as
    half as large so a fuzzy near-miss isn't punished like two exact dates.
    (The exact-calendar-date identity signal used by the gate is separate --
    see precise_agree.)"""
    if not da.known or not db.known:
        return None
    d = abs(da.year - db.year)
    if d == 0:
        return 1.0
    fuzzy = (da.qual in ("ABT", "EST", "CAL", "BEF", "AFT") or
             db.qual in ("ABT", "EST", "CAL", "BEF", "AFT") or
             da.day is None or db.day is None)
    span = d / 2.0 if fuzzy else float(d)
    if span <= 1: return 0.95
    if span <= 2: return 0.85
    if span <= 5: return 0.6
    if span <= 10: return 0.3
    return 0.0

def precise_agree(da: GDate, db: GDate) -> bool:
    """True only for identity-grade date agreement: both full, unqualified
    calendar dates on the same day. A shared birth YEAR is common and does not
    count -- that distinction is what stops same-year strangers auto-confirming."""
    return (da.exact and db.exact and da.year == db.year
            and da.month == db.month and da.day == db.day)

def dates_hard_conflict(da: GDate, db: GDate) -> bool:
    """A real contradiction worth flagging: both dates known and reasonably
    certain (not ABT/EST/CAL/BEF/AFT) yet more than two years apart. Two
    approximate or year-only guesses that merely differ are NOT a conflict."""
    if not da.known or not db.known:
        return False
    if da.qual or db.qual:                    # any approximation -> not hard
        return False
    return abs(da.year - db.year) > 2

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
    eba, ebb = a.eff_birth(), b.eff_birth()
    eda, edb = a.eff_death(), b.eff_death()
    by = date_score(eba, ebb)
    if by is not None:
        feats.append((by, 0.30)); ev.append(f"birth {eba.year}~{ebb.year}={by:.2f}")
    dy = date_score(eda, edb)
    if dy is not None:
        feats.append((dy, 0.10)); ev.append(f"death {eda.year}~{edb.year}={dy:.2f}")
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
        overwhelming = (given_sim(a, b) >= STRONG_GIVEN
                        and by is not None and by >= 0.85
                        and dy is not None and dy >= 0.85)
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

def identity_gate(score, gs, by, dy, structural, precise=False):
    """Surname is corroborating, not identifying. To CONFIRM a match we need
    real identity evidence. In order of strength:
      * a matched relative (structural), or an exact same-day date -> identity
        established, score stands;
      * a STRONG given name plus an agreeing date (even year-only) -> stands;
        a strong name with no/contradicting date -> review (human judges);
      * a weak/moderate given name with only a same-YEAR coincidence (common!)
        and no relatives -> review, never auto-confirm (this is the case that
        used to let same-year strangers through);
      * shared surname only -> reject.
    by/dy are date_scores (None if a date is missing on either side)."""
    if structural > 0 or precise:
        return score
    present = [x for x in (by, dy) if x is not None]
    best = max(present) if present else None
    if gs >= STRONG_GIVEN:
        if best is not None and best >= 0.6:      # strong name + agreeing date
            return score
        return min(score, TAU_HIGH - 0.01)        # strong name alone -> review
    # weak/moderate given name, no structural support, no exact date:
    if best is not None and best >= 0.6:
        return min(score, TAU_HIGH - 0.01)        # only a loose date -> review
    return min(score, TAU_LOW - 0.01)             # nothing identifying -> reject

def score_pair(a, b, ta, tb, matched):
    pa, pb = ta.people[a], tb.people[b]
    base, ev = attribute_score(pa, pb, ta, tb)
    sb, sev = structural_bonus(a, b, ta, tb, matched)
    raw = min(1.0, base + sb)
    eba, ebb = pa.eff_birth(), pb.eff_birth()
    eda, edb = pa.eff_death(), pb.eff_death()
    precise = precise_agree(eba, ebb) or precise_agree(eda, edb)
    gated = identity_gate(raw, given_sim(pa, pb),
                          date_score(eba, ebb), date_score(eda, edb),
                          sb, precise)
    ev = ev + sev
    if gated < raw - 1e-9:
        ev = ev + [f"gated {raw:.2f}->{gated:.2f} (weak identity: no relative, "
                   f"no strong name, no exact date)"]
    return gated, ev

# ---------------------------------------------------------------- scope
def blood_kindred(tree: Tree, root: str) -> set:
    """All consanguineal relatives of root, and NO in-laws. Two phases so a
    spouse is never traversed through:
      1. climb to every ancestor using PARENT edges only;
      2. descend to every descendant of root and of each ancestor using CHILD
         edges only -- never turning back up, which would pull in a child's
         other parent (an in-law) and then that in-law's whole family.
    Yields ancestors, descendants, siblings, cousins, aunts/uncles,
    nieces/nephews. A blood relative's spouse is added later as scope, not here;
    an in-law's own parents/siblings are correctly excluded."""
    if root not in tree.people:
        return set()
    anc = {root}; stack = [root]                 # phase 1: ancestors (up only)
    while stack:
        x = stack.pop()
        for p in tree.parents(x):
            if p not in anc:
                anc.add(p); stack.append(p)
    blood = set(anc); stack = list(anc)          # phase 2: descendants (down only)
    while stack:
        x = stack.pop()
        for c in tree.children(x):
            if c not in blood:
                blood.add(c); stack.append(c)
    return blood

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
        """A still-unmatched B record that is really another copy of an
        already-matched A person -- not a discovery. Two signals:
          * same IDENTITY on attributes alone (name + dates), scored with an
            EMPTY matched set so siblings (who share parents) aren't mistaken
            for duplicates; OR
          * same NAME plus a shared MATCHED SPOUSE. Two people with the same
            name and the same spouse are the same person -- unlike a shared
            parent (which siblings have), a shared spouse is identity-defining.
            This catches a duplicate whose birth/death date was mis-entered."""
        by_bucket = defaultdict(list)
        for a, b in self.matched.items():
            by_bucket[soundex(surname_stem(self.ta.people[a].surname))].append(a)
        for b in set(self.tb.people) - self.used_b:
            k = soundex(surname_stem(self.tb.people[b].surname))
            pb = self.tb.people[b]
            b_spouses = set(self.tb.spouses(b))
            best = None
            for a in by_bucket.get(k, []):
                s, _ = score_pair(a, b, self.ta, self.tb, {})   # attribute-only
                if s < TAU_HIGH and given_sim(self.ta.people[a], pb) >= STRONG_GIVEN:
                    a_spouse_b = {self.matched[sp] for sp in self.ta.spouses(a)
                                  if sp in self.matched}
                    if b_spouses & a_spouse_b:      # same name + same spouse
                        s = max(s, TAU_HIGH)        # -> the same person
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
_LINE_RE = re.compile(r"^(\d+)\s+(?:@[^@]+@\s+)?(\S+)(?:\s(.*))?$")
_PTR_RE = re.compile(r"^@[^@]+@$")

def full_indi_lines(p: Person, extra_note: str, links: list[str]) -> list[str]:
    """Re-emit a discovered person's FULL record from B (events, places, inline
    notes, occupation, etc.) so nothing is lost on import -- not just the
    name/dates skeleton. Two things are stripped for safety:
      * FAMS/FAMC pointers (the tool rewrites family links itself); and
      * any line whose value is a cross-record pointer (SOUR/OBJE/NOTE-@N@/
        SUBM/ASSO ...), together with its sub-lines, since those target records
        we don't emit and would dangle on import.
    Inline data (values that aren't bare @xref@ pointers) is kept verbatim."""
    out = [f"0 {p.xid} INDI"]
    skip_below = None
    for ln in p.raw_lines:
        m = _LINE_RE.match(ln)
        if not m:
            continue
        lvl = int(m.group(1)); tag = m.group(2); val = (m.group(3) or "").strip()
        if skip_below is not None:
            if lvl > skip_below:
                continue                 # inside a dropped subtree
            skip_below = None
        if tag in ("FAMC", "FAMS") or _PTR_RE.match(val):
            skip_below = lvl             # drop this line and its children
            continue
        out.append(ln)
    if extra_note:
        out.append(f"1 NOTE {extra_note}")
    out += links
    return out

def apply_ignore(tree: Tree, patterns: set, side: str = "") -> set:
    """Drop placeholder / to-be-ignored people and scrub every reference to
    them, so they never match, fall in scope, or reach new_only.ged. Runs
    BEFORE anything else. Two kinds of pattern: a record id ('@I5@'; prefix
    'A:' or 'B:' to target only that tree, since the trees' id spaces can
    collide) drops exactly that person, and any other token drops everyone
    whose given name matches it (case- and diacritic-insensitive). Returns
    the set of dropped ids. Used for records like 'Private', 'Living', or
    unnamed child stubs the other tree shouldn't import."""
    if not patterns:
        return set()
    ids, names = set(), set()
    for p in patterns:
        tgt, sep, rest = p.partition(":")
        if sep and tgt.upper() in ("A", "B"):
            if tgt.upper() != side:
                continue
            p = rest
        if p.startswith("@") and p.endswith("@") and len(p) > 2:
            ids.add(p)
        elif p:
            names.add(p)
    pats = {norm(p) for p in names if norm(p)}
    drop = {pid for pid, per in tree.people.items()
            if pid in ids
            or first_token(per.given) in pats or norm(per.given) in pats}
    for pid in drop:
        del tree.people[pid]
    for fam in tree.fams.values():
        if fam.husb in drop: fam.husb = ""
        if fam.wife in drop: fam.wife = ""
        fam.chil = [c for c in fam.chil if c not in drop]
    for per in tree.people.values():
        per.famc = [f for f in per.famc if f in tree.fams]
        per.fams = [f for f in per.fams if f in tree.fams]
    return drop

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

def discovery_info(m: Matcher, roots_a: list) -> dict:
    """For every in-scope discovery, work out (a) its graph distance from the
    root person in B and (b) how it attaches to your EXISTING tree -- the
    nearest matched neighbour and the relationship to it (a new 'child',
    'parent', 'spouse' or 'sibling' of that existing person). Distance is how
    many relationship hops separate the discovery from you; attachment is what
    lets us phase the import (children vs ancestors) and label who each person
    is. Returns {b_id: {dist, rel, anchor_b, anchor_a, anchor_name, name}}."""
    tb = m.tb
    b2a = {b: a for a, b in m.matched.items()}
    only_b_all = sorted(set(tb.people) - m.used_b)
    in_scope, _ = classify_scope_b(m, only_b_all)
    in_scope = set(in_scope) - set(m.dup_b)

    # BFS distance in B from the root person(s)' B-image, through all edges.
    root_b = [m.matched[r] for r in roots_a if r in m.matched]
    dist = {b: 0 for b in root_b}
    dq = deque(root_b)
    while dq:
        x = dq.popleft()
        for nb in (tb.parents(x) + tb.children(x)
                   + tb.spouses(x) + tb.siblings(x)):
            if nb not in dist:
                dist[nb] = dist[x] + 1
                dq.append(nb)

    rank = {"child": 0, "parent": 0, "sibling": 1, "spouse": 2}
    info = {}
    for b in in_scope:
        cands = []
        for rel, nbs in (("child", tb.parents(b)),    # a matched parent  -> b is its child
                         ("parent", tb.children(b)),   # a matched child   -> b is its parent
                         ("spouse", tb.spouses(b)),
                         ("sibling", tb.siblings(b))):
            for nb in nbs:
                if nb in m.used_b:
                    cands.append((dist.get(nb, 10**9), rank[rel], rel, nb))
        cands.sort()
        p = tb.people[b]
        rec = {"dist": dist.get(b), "name": f"{p.given} {p.surname}".strip(),
               "rel": None, "anchor_b": None, "anchor_a": None, "anchor_name": None}
        if cands:
            _, _, rel, nb = cands[0]
            an = tb.people[nb]
            rec.update(rel=rel, anchor_b=nb, anchor_a=b2a[nb],
                       anchor_name=f"{an.given} {an.surname}".strip())
        info[b] = rec
    return info

def passes_phase(rec: dict, max_dist: int, grow: str) -> bool:
    """Phase filter for a discovery given its info record: distance cap from
    root and growth direction (down = new descendants, up = new ancestors)."""
    if max_dist > 0 and (rec.get("dist") is None or rec["dist"] > max_dist):
        return False
    if grow == "down" and rec.get("rel") != "child":
        return False
    if grow == "up" and rec.get("rel") != "parent":
        return False
    return True

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
        if dates_hard_conflict(pa.eff_birth(), pb.eff_birth()):
            conflicts.append({"a": a, "b": b, "field": "birth_year",
                              "a_val": pa.birth_year, "b_val": pb.birth_year})
        if dates_hard_conflict(pa.eff_death(), pb.eff_death()):
            conflicts.append({"a": a, "b": b, "field": "death_year",
                              "a_val": pa.death_year, "b_val": pb.death_year})
        for fld, va, vb in (("surname", pa.surname, pb.surname),
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
            "discovery_info": getattr(m, "disc_info", {}),
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

def write_new_only_gedcom(m: Matcher, path: str,
                          max_root_distance: int = 0, grow: str = "both"):
    """Emit in-scope new individuals plus the families that link them to your
    EXISTING tree, so relationships survive import without clutter:
      * a family is emitted only if it holds an in-tree member (matched, or a
        duplicate of a matched person) AND a new discovery -- a discovery's
        far-side relatives (an ancestor's own parents, an in-law's birth
        family) are NOT dragged in
      * in-tree members appear as merge STUBs carrying their Gramps ID: parents
        always, and a matched child only when it's the sole anchor for new
        siblings
      * out-of-scope members are dropped (no pointer dangles, no non-relatives)
      * HUSB/WIFE are placed by SEX so a source that swapped the couple can't
        create a conflicting family
    Phasing: max_root_distance caps how far from you a discovery may be; grow
    ('down'=new children, 'up'=new ancestors, 'both') limits by attachment. Each
    discovery is stamped with a NOTE giving its distance and attachment."""
    tb = m.tb
    only_b_all = sorted(set(tb.people) - m.used_b)
    in_scope_b, _ = classify_scope_b(m, only_b_all)
    new_ids = set(in_scope_b) - set(m.dup_b)
    info = getattr(m, "disc_info", {})
    if max_root_distance > 0 or grow != "both":   # apply the phase filter
        new_ids = {b for b in new_ids
                   if passes_phase(info.get(b, {}), max_root_distance, grow)}
    b2a = {b: a for a, b in m.matched.items()}
    dup_map = m.dup_b                         # b -> (kept_b, gramps_a)

    def note_for(pid):                        # distance + attachment label
        rec = info.get(pid)
        if not rec:
            return None
        bits = []
        if rec.get("dist") is not None:
            d = rec["dist"]
            bits.append(f"{d} step{'' if d == 1 else 's'} from root")
        if rec.get("rel"):
            bits.append(f"{rec['rel']} of {rec['anchor_name']} "
                        f"[{rec['anchor_a']} in your tree]")
        return "[gedmatch] " + "; ".join(bits) if bits else None

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

    # Keep a family only when it is worth importing as a unit:
    #   * it links a discovery to your EXISTING tree -- has an in-tree member
    #     (matched, or a duplicate of a matched person) plus a new discovery; OR
    #   * it is a self-contained NEW sub-family -- two or more discoveries
    #     (e.g. a newly found parent and child who are both new).
    # Either way it must link >=2 emitted people. Out-of-scope members are
    # always dropped, never dragged in -- so a lone discovery's far-side
    # relatives (a discovered ancestor's own parents, an in-law's birth family)
    # don't come along. In-tree members appear as merge stubs: parents always,
    # and a matched child only when it is the sole anchor for new siblings.
    fam_keep = []            # (fam, parents_to_emit, children_to_emit)
    stub_ids = set()
    for fam in tb.fams.values():
        in_tree_parents = [x for x in (fam.husb, fam.wife) if x and gref(x)]
        new_parents     = [x for x in (fam.husb, fam.wife) if x in new_ids]
        new_children    = [c for c in fam.chil if c in new_ids]
        in_tree_children= [c for c in fam.chil if gref(c)]
        child_stubs = in_tree_children if (new_children and not in_tree_parents) else []
        parents_to_emit  = in_tree_parents + new_parents
        children_to_emit = new_children + child_stubs
        has_in_tree = bool(in_tree_parents or child_stubs)
        n_new = len(new_parents) + len(new_children)
        n_emit = len(parents_to_emit) + len(children_to_emit)
        if n_new >= 1 and n_emit >= 2 and (has_in_tree or n_new >= 2):
            fam_keep.append((fam, parents_to_emit, children_to_emit))
            stub_ids.update(in_tree_parents)
            stub_ids.update(child_stubs)
    stub_ids -= new_ids

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
        lines += full_indi_lines(tb.people[pid], note_for(pid), fam_links(pid))
    for pid in sorted(stub_ids):
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
    return len(new_ids), len(stub_ids), len(fam_keep), 0

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

def load_answers(path: str) -> dict:
    """Load a saved decision file, tolerating a missing/corrupt file."""
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as e:
            print(f"(could not read {path}: {e}; starting fresh)", file=sys.stderr)
    return {}

def make_cached_review(inner, ta, tb, store: dict, path: str, record: bool):
    """Wrap a review callback so prior answers replay instead of re-prompting.
    A decision is keyed by the id pair AND a fingerprint of both people
    (name + birth/death years); a stored answer is reused only when that
    fingerprint still matches, so an answer can never be misapplied if an id is
    later reused for someone else. New answers are saved immediately (atomic
    write) so an interrupted session keeps what you've already answered. With
    --answers but no --interactive, cached pairs replay and everything else
    stays uncertain (no prompts)."""
    def fp(tree, xid):
        p = tree.people.get(xid)
        return "|".join([p.given or "", p.surname or "",
                         str(p.birth_year or ""), str(p.death_year or "")]) if p else ""
    def save():
        if not path:
            return
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(store, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    stats = {"recalled": 0, "asked": 0}
    word = {True: "yes", False: "no", None: "skip"}
    def review(a, b, score, ev):
        key = f"{a}={b}"
        sig = fp(ta, a) + "  #  " + fp(tb, b)
        rec = store.get(key)
        # Only reuse a definitive yes/no. A 'skip' means "ask me again", so a
        # stored None (from this or an older file) is treated as not-yet-decided.
        if rec and rec.get("sig") == sig and rec.get("decision") is not None:
            d = rec["decision"]
            print(f"  [recalled: {word[d]}]  {fp(ta, a)}  =?  {fp(tb, b)}")
            stats["recalled"] += 1
            return d
        d = inner(a, b, score, ev)
        if record and d is not None:              # persist decisions, not skips
            store[key] = {"decision": d, "sig": sig, "a": a, "b": b,
                          "a_name": fp(ta, a), "b_name": fp(tb, b)}
            save()
            stats["asked"] += 1
        return d
    review.stats = stats
    return review

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
    ap.add_argument("--max-root-distance", type=int, default=0,
                    help="phased import: emit only discoveries within this many "
                    "relationship hops of the root person (0 = no cap). Import "
                    "the inner ring, merge, re-run to widen.")
    ap.add_argument("--grow", choices=["both", "down", "up"], default="both",
                    help="phased import by attachment: 'down' = only new children "
                    "of your people, 'up' = only new parents/ancestors, "
                    "'both' = all (default)")
    ap.add_argument("--answers", default="", help="path to a decision file: "
                    "replay saved --interactive answers instead of re-prompting "
                    "(and save new ones). Lets you answer the ambiguous pairs "
                    "once and re-run freely with different --grow/--max-root-distance.")
    ap.add_argument("--ignore", default="Private,Living",
                    help="comma-separated patterns to ignore entirely "
                    "(default 'Private,Living'). A record id like '@I5@' drops "
                    "exactly that person (prefix 'A:@I5@'/'B:@X5@' to target "
                    "one tree); any other token drops people whose given name "
                    "matches it. Applied to BOTH trees before matching/scope/"
                    "emission, so placeholder or hidden-living records never "
                    "reach new_only.ged. Pass '' to disable.")
    ap.add_argument("--out-json", default="diff.json")
    ap.add_argument("--out-ged", default="new_only.ged")
    ap.add_argument("--explain", default="", help="after matching, diagnose "
                    "why pairs did/didn't match: Aid=Bid,Aid=Bid")
    args = ap.parse_args(argv)

    ta = parse_gedcom(args.gedcom_a)
    tb = parse_gedcom(args.gedcom_b)
    print(f"A: {len(ta.people)} people / {len(ta.fams)} families")
    print(f"B: {len(tb.people)} people / {len(tb.fams)} families")

    ign = {p.strip() for p in args.ignore.split(",") if p.strip()}
    if ign:
        da, db = apply_ignore(ta, ign, "A"), apply_ignore(tb, ign, "B")
        if da or db:
            print(f"ignored: {len(da)} in A, {len(db)} in B "
                  f"(matching {sorted(ign)})")

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
    if args.answers:
        store = load_answers(args.answers)
        review = make_cached_review(review, ta, tb, store, args.answers,
                                    record=args.interactive)
    m = Matcher(ta, tb, review, blood_a=blood_a, scope_a=scope_a)
    m.scope_hops = args.scope_hops
    if args.seed:
        pairs = [tuple(p.split("=")) for p in args.seed.split(",") if "=" in p]
        m.seed(pairs)
    m.run(use_uid=args.use_uid, do_auto_seed=args.auto_seed)

    roots_a = [r.strip() for r in args.root.split(",") if r.strip()]
    m.disc_info = discovery_info(m, roots_a)

    diff = build_diff(m)
    with open(args.out_json, "w", encoding="utf-8") as fh:
        json.dump(diff, fh, indent=2, ensure_ascii=False)
    npeople, nanchor, nfam, nctx = write_new_only_gedcom(
        m, args.out_ged, max_root_distance=args.max_root_distance, grow=args.grow)

    phase = (args.max_root_distance > 0) or (args.grow != "both")
    print(f"\nmatched:      {len(m.matched)}")
    print(f"only in A:    {len(diff['only_in_a'])}")
    total_b = len(diff['only_in_b'])
    if phase:
        lim = []
        if args.max_root_distance > 0:
            lim.append(f"within {args.max_root_distance} of root")
        if args.grow != "both":
            lim.append(f"grow={args.grow}")
        print(f"only in B:    {total_b} in scope; this phase ({', '.join(lim)}) "
              f"-> {args.out_ged} ({npeople} new indi, {nfam} fam, "
              f"{nanchor} anchor stubs"
              + (f", {nctx} context parents" if nctx else "") + ")")
    else:
        print(f"only in B:    {total_b}  -> {args.out_ged} "
              f"({npeople} new indi, {nfam} fam, {nanchor} anchor stubs to merge"
              + (f", {nctx} context parents to review" if nctx else "") + ")")
    print(f"  out of scope: {len(diff['only_in_b_out_of_scope'])}  (excluded from import)")
    print(f"  duplicates:   {len(diff['duplicates_in_b'])}  (duplicate of an already-matched person (in the imported tree))")
    print(f"conflicts:    {len(diff['conflicts'])}")
    print(f"bridges:      {len(diff['bridges'])}  (new people attached to existing)")
    print(f"uncertain:    {len(diff['uncertain'])}  (need your review)")
    if args.answers:
        st = getattr(review, "stats", {"recalled": 0, "asked": 0})
        print(f"answers:      {st['recalled']} recalled"
              + (f", {st['asked']} new" if st['asked'] else "")
              + f"  ({args.answers})")
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
