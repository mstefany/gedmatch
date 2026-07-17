import pytest
import os
from gedmatch import (
    split_name, extract_year, norm, surname_stem, first_token,
    jaro, jaro_winkler, soundex, canon_given,
    parse_gedcom, Tree, Person, Family,
    given_sim, year_score, attribute_score, _build_given_map
)

def test_split_name():
    assert split_name("John /Doe/") == ("John", "Doe")
    assert split_name("Jane/Smith/ ") == ("Jane", "Smith")
    assert split_name("No Slashes") == ("No Slashes", "")

def test_extract_year():
    assert extract_year("12 MAY 1990") == 1990
    assert extract_year("Abt 1850") == 1850
    assert extract_year("Unknown") is None

def test_norm():
    assert norm("Ján") == "jan"
    assert norm("O'Connor") == "oconnor"
    assert norm("Černý") == "cerny"

def test_surname_stem():
    assert surname_stem("Nováková") == "novak"
    assert surname_stem("Smith") == "smith"

def test_first_token():
    assert first_token("John Michael") == "john"
    assert first_token("") == ""

def test_soundex():
    assert soundex("Smith") == "S530"
    assert soundex("Smythe") == "S530"
    assert soundex("Washington") == "W252"
    assert soundex("") == ""

def test_canon_given():
    assert canon_given("Ján") == "jan"
    assert canon_given("Janko") == "jan"
    assert canon_given("John") == "jan" # Since 'jan john johnny' is in groups

def test_string_metrics():
    assert jaro("martha", "marhta") > 0.9
    assert jaro_winkler("martha", "marhta") > 0.9
    assert jaro_winkler("dixon", "dicksonx") > 0.7

def test_year_score():
    assert year_score(1990, 1990) == 1.0
    assert year_score(1990, 1991) == 0.95
    assert year_score(1990, 1992) == 0.85
    assert year_score(1990, 1995) == 0.6
    assert year_score(1990, 2000) == 0.3
    assert year_score(1990, 2010) == 0.0
    assert year_score(None, 1990) is None

def test_parse_gedcom(tmp_path):
    gedcom_data = """0 HEAD
1 SOUR TEST
0 @I1@ INDI
1 NAME John /Doe/
1 SEX M
1 BIRT
2 DATE 10 JAN 1900
1 FAMS @F1@
0 @I2@ INDI
1 NAME Jane /Smith/
1 SEX F
1 FAMS @F1@
0 @I3@ INDI
1 NAME Baby /Doe/
1 FAMC @F1@
0 @F1@ FAM
1 HUSB @I1@
1 WIFE @I2@
1 CHIL @I3@
0 TRLR
"""
    p = tmp_path / "test.ged"
    p.write_text(gedcom_data, encoding="utf-8")
    
    tree = parse_gedcom(str(p))
    assert len(tree.people) == 3
    assert len(tree.fams) == 1
    
    p1 = tree.people["@I1@"]
    assert p1.given == "John"
    assert p1.surname == "Doe"
    assert p1.sex == "M"
    assert p1.birth_year == 1900
    
    p3 = tree.people["@I3@"]
    assert p3.famc == ["@F1@"]
    
    assert tree.parents("@I3@") == ["@I1@", "@I2@"]
    assert tree.spouses("@I1@") == ["@I2@"]
    assert tree.children("@I1@") == ["@I3@"]

def test_attribute_score():
    ta = Tree(people={"A": Person(xid="A", given="John", surname="Doe", birth_year=1900)}, fams={})
    tb = Tree(people={"B": Person(xid="B", given="John", surname="Doe", birth_year=1900)}, fams={})
    score, ev = attribute_score(ta.people["A"], tb.people["B"], ta, tb)
    assert score > 0.9
