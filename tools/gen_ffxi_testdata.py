#!/usr/bin/env python3
"""Build lsb/ffxi_testdata.json, the data the Characters page's test scenarios use.

  - zones: id -> name, from LSB's data/enums/zone.yaml;
  - lines: every zone line, from LSB's data/zones/*/zone.yaml: the zone it is in, the zone it leads
    to and where it puts the character (x, y, z, facing 0-255). "Arrive in B from A" uses the line
    in A that leads to B;
  - heavy: arrival cutscenes whose event script loads another zone's model (event op 0x34) or reloads
    the zone's own (0x35), the steps that run the 2016 PS2 client out of heap ("449a64"). Taken from a
    static sweep of the scripts behind the zone-in events LSB's Lua starts (a CSV: zone, zone id,
    cutscene, Lua file, line, conditions, present, instructions, hits). The sweep itself is not part
    of this repository; without --sweep the list already in the JSON is kept.

  python tools/gen_ffxi_testdata.py --lsb PATH_TO_LSB_TREE [--sweep zonein_scan.csv]
"""
import argparse
import csv
import json
import math
import os
import re
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, os.pardir, "lsb", "ffxi_testdata.json")
_HIT = re.compile(r"0x(34|35)@[0-9a-f]+:\d+->0x([0-9a-f]+)")


def zones(lsb):
    with open(os.path.join(lsb, "data", "enums", "zone.yaml"), encoding="utf-8") as fh:
        values = yaml.safe_load(fh)["values"]
    return {name: int(zid) for name, zid in values.items()}


def facing(radians):
    return int(round(radians / (2 * math.pi) * 256)) % 256


def lines(lsb, ids):
    out = []
    root = os.path.join(lsb, "data", "zones")
    for zdir in sorted(os.listdir(root)):
        path = os.path.join(root, zdir, "zone.yaml")
        if zdir not in ids or not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        for key, line in (doc.get("zonelines") or {}).items():
            to, at = line.get("to"), line.get("at") or []
            if to not in ids or len(at) < 3:
                continue
            out.append({"from": ids[zdir], "to": ids[to], "line": key, "x": round(float(at[0]), 3),
                        "y": round(float(at[1]), 3), "z": round(float(at[2]), 3),
                        "rot": facing(float(at[3])) if len(at) > 3 else 0})
    return out


def heavy(sweep):
    out = []
    with open(sweep, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            hits = _HIT.findall(row.get("hits") or "")
            if not hits or not row.get("zid") or not row.get("csid"):
                continue
            out.append({"zone": int(row["zid"]), "csid": int(row["csid"]), "file": row["file"],
                        "conds": (row.get("conds") or "").replace(" ( ", "(").replace(" ) ", ")").strip(),
                        "loads": [int(z, 16) for op, z in hits if op == "34"],
                        "reloads": sum(1 for op, _ in hits if op == "35")})
    out.sort(key=lambda r: (r["zone"], r["csid"]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lsb", required=True)
    ap.add_argument("--sweep")
    a = ap.parse_args()
    ids = zones(a.lsb)
    data = {"zones": {str(v): k for k, v in sorted(ids.items(), key=lambda kv: kv[1])},
            "lines": lines(a.lsb, ids)}
    if a.sweep:
        data["heavy"] = heavy(a.sweep)
    elif os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as fh:
            data["heavy"] = json.load(fh).get("heavy", [])
    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, separators=(",", ":"), sort_keys=True)
        fh.write("\n")
    print("%d zones, %d zone lines, %d heavy cutscenes -> %s" % (len(data["zones"]), len(data["lines"]),
                                                                 len(data.get("heavy", [])), os.path.normpath(OUT)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
