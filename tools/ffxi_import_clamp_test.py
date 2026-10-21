#!/usr/bin/env python3
"""The import's server-side ceilings: a dump cannot claim more than retail allows.

    python tools/ffxi_import_clamp_test.py

The polexport dump is made on the player's machine and the self-serve import
endpoint takes it from them, so lsb/ffxi_import_core.build_statements clamps
gil, job levels, skills, craft ranks, mission rank and item stacks before any
of it becomes SQL. These checks drive build_statements with a hand-made dump
and read the numbers back out of the statements. No database, no network.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "lsb"))
import ffxi_import_core as core  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


def dump(**over):
    d = {"format": "polexport-1", "name": "Clampy", "nation": 0,
         "main_job": 1, "sub_job": 0, "jobs": {"1": 50}, "gil": 1234,
         "containers": {}, "skills": {}, "crafts": {}, "rank": 3,
         "rank_points": 100}
    d.update(over)
    return d


def find(stmts, pattern):
    for s in stmts:
        m = re.search(pattern, s)
        if m:
            return m
    return None


# sane values pass through untouched
plan = core.build_statements(dump())
stmts = plan["core"] + plan["best"]
chk("gil within the cap is kept", find(stmts, r"quantity = (\d+)").group(1), "1234")
chk("a level within the cap is kept", find(stmts, r"war = (\d+)").group(1), "50")
chk("rank within the cap is kept",
    find(stmts, r"rank_sandoria = (\d+), rank_points = (\d+)").groups(), ("3", "100"))

# the ceilings
plan = core.build_statements(dump(gil=10 ** 12, jobs={"1": 999, "7": 150},
                                  rank=99, rank_points=10 ** 9,
                                  skills={"1": 9999}, crafts={"0": {"skill": 9999, "rank": 99}},
                                  containers={"inventory": {"id": 0, "size": 30,
                                                            "items": [[1, 4096, 100000, ""]]}}))
stmts = plan["core"] + plan["best"]
chk("gil is clamped to the retail cap",
    find(stmts, r"quantity = (\d+) WHERE charid = @charid AND location = 0").group(1),
    str(core.GIL_MAX))
chk("a job level is clamped to 99", find(stmts, r"war = (\d+)").group(1),
    str(core.JOB_LEVEL_MAX))
chk("...every job, not just the first", find(stmts, r"pld = (\d+)").group(1),
    str(core.JOB_LEVEL_MAX))
chk("genkai never exceeds 99", find(stmts, r"genkai = (\d+)").group(1), "99")
chk("mission rank and rank points are clamped",
    find(stmts, r"rank_sandoria = (\d+), rank_points = (\d+)").groups(),
    (str(core.NATION_RANK_MAX), str(core.RANK_POINTS_MAX)))
chk("a combat skill is clamped (tenths)",
    find(stmts, r"\(@charid, 1, (\d+), (\d+)\)").groups(),
    (str(core.SKILL_MAX * 10), "0"))
chk("a craft skill and its rank are clamped",
    find(stmts, r"\(@charid, 48, (\d+), (\d+)\)").groups(),
    (str(core.SKILL_MAX * 10), str(core.CRAFT_RANK_MAX)))
chk("an item stack is clamped to 99",
    find(stmts, r"\(@charid, 0, 1, 4096, (\d+), ''\)").group(1),
    str(core.ITEM_STACK_MAX))

# the floor: a negative figure is not a credit
plan = core.build_statements(dump(gil=-5, rank=-1, skills={"1": -40},
                                  crafts={"0": {"skill": -1, "rank": -3}}))
stmts = plan["core"] + plan["best"]
chk("negative gil writes nothing", find(stmts, r"quantity = ") is None, True)
chk("a negative rank writes nothing", find(stmts, r"rank_points") is None, True)
chk("negative skills floor at zero",
    find(stmts, r"\(@charid, 1, (\d+), (\d+)\)").groups(), ("0", "0"))
chk("negative craft rank floors at zero",
    find(stmts, r"\(@charid, 48, (\d+), (\d+)\)").groups(), ("0", "0"))

print("%s: %d failure(s)" % (os.path.basename(__file__), bad))
sys.exit(1 if bad else 0)
