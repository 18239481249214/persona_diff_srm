"""Independent integrity audit for a run folder.

  python audit.py runs\\<run_name>

This file deliberately imports nothing from pdsrm/. It re-derives every value in ratings.csv and
measures.csv from the raw model replies using its own parser, recomputes which group-loops are
complete, checks every SHA-256 seal, and compares cell by cell.

  FAIL = the data cannot be trusted as-is (value mismatch, missing/extra row, broken seal).
  WARN = worth a look but not corruption (e.g. the runner rejected a reply the audit could parse).
Writes audit_report.txt into the run folder. Exit code 1 if any FAIL.
"""
import csv
import hashlib
import json
import re
import sys
from collections import Counter
from itertools import permutations
from pathlib import Path

PROV = ("model", "provider", "temperature", "reasoning")


class Report:
    def __init__(self):
        self.fails, self.warns, self.info = [], [], []

    def fail(self, msg):
        self.fails.append(msg)

    def warn(self, msg):
        self.warns.append(msg)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _hook(pairs):
    seen = set()
    for k, _ in pairs:
        if k in seen:
            raise ValueError(f"duplicate key {k!r}")
        seen.add(k)
    return dict(pairs)


def extract(text):
    """Independent JSON extraction: decode the first complete object starting at the first '{'."""
    if not text or "{" not in text:
        raise ValueError("no JSON object")
    obj, _ = json.JSONDecoder(object_pairs_hook=_hook).raw_decode(text[text.index("{"):])
    if not isinstance(obj, dict):
        raise ValueError("not an object")
    return obj


def as_int(v):
    if type(v) is int:
        return v
    if type(v) is float and v == int(v):
        return int(v)
    if type(v) is str and re.fullmatch(r"-?\d+", v.strip()):
        return int(v.strip())
    raise ValueError(f"not an integer: {v!r}")


def subdirs(parent, prefix):
    return sorted(p for p in Path(parent).glob(prefix + "*") if p.is_dir() and not p.name.endswith(".tmp"))


def read_csv(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


# ------------------------------------------------------------------ checks

def check_seals(run, rep):
    sealed = 0
    for p in (run / "groups").rglob("*"):
        if p.is_dir() and p.name.endswith(".tmp"):
            rep.warn(f"leftover unfinished folder (ignored by the runner): {p.relative_to(run)}")
    for gdir in subdirs(run / "groups", "g"):
        folders = [gdir / "measures_final"]
        for ldir in subdirs(gdir, "l"):
            folders += [ldir / "measures_start"] + subdirs(ldir, "r")
        for folder in folders:
            if not folder.exists():
                continue
            seal = folder / "sha256.json"
            if not seal.exists():
                rep.fail(f"unsealed folder: {folder.relative_to(run)}")
                continue
            recorded = load(seal)
            actual = {p.name: sha(p) for p in folder.iterdir() if p.is_file() and p.name != "sha256.json"}
            if recorded != actual:
                changed = sorted(set(recorded) ^ set(actual) |
                                 {k for k in recorded if k in actual and recorded[k] != actual[k]})
                rep.fail(f"seal broken in {folder.relative_to(run)}: {changed}")
            sealed += 1
    rep.info.append(f"sealed folders verified: {sealed}")


def check_schedule(ldir, n, rep):
    sched = load(ldir / "schedule.json")
    pairs = [frozenset((d["initiator"], d["responder"])) for rnd in sched for d in rnd]
    if len(pairs) != n * (n - 1) // 2 or len(set(pairs)) != len(pairs):
        rep.fail(f"{ldir}: schedule is not a complete round robin")
    for rnd in sched:
        r = rnd[0]["round"]
        rdir = ldir / f"r{r:02d}"
        if (rdir / "dyads.json").exists():
            got = sorted(x["dyad"]["dyad_id"] for x in load(rdir / "dyads.json"))
            want = sorted(d["dyad_id"] for d in rnd)
            if got != want:
                rep.fail(f"{rdir}: dyads on disk {got} do not match schedule {want}")


def expected_ratings(run, cfg, rep):
    items_cfg = {it: c for c, its in cfg["items"].items() for it in its}
    lo, hi = cfg["rating_scale"]
    n = cfg["group_size"]
    expected, counted = {}, 0
    for gdir in subdirs(run / "groups", "g"):
        g = int(gdir.name[1:])
        agents = list(load(gdir / "handles.json"))
        poisoned = False
        for ldir in subdirs(gdir, "l"):
            loop = int(ldir.name[1:])
            check_schedule(ldir, n, rep)
            rows, directed = {}, Counter()
            for rdir in subdirs(ldir, "r"):
                for res in load(rdir / "dyads.json"):
                    d = res["dyad"]
                    for rt in res["ratings"]:
                        counted += 1
                        where = f"{d['dyad_id']} {rt['rater']}->{rt['target']}"
                        for i, att in enumerate(rt["attempts"]):
                            parsed = None
                            try:
                                raw = extract(att["text"])
                                norm = {}
                                for k, v in raw.items():
                                    kk = str(k).strip().lower()
                                    if kk in norm:
                                        raise ValueError("duplicate key after normalizing")
                                    norm[kk] = v
                                if set(norm) != set(items_cfg):
                                    raise ValueError(f"keys {sorted(norm)}")
                                parsed = {k: as_int(v) for k, v in norm.items()}
                                if any(not lo <= v <= hi for v in parsed.values()):
                                    raise ValueError("out of range")
                            except (ValueError, TypeError):
                                parsed = None
                            if rt["ok"] and i == rt["accepted_attempt"]:
                                if parsed is None:
                                    rep.fail(f"{where}: runner accepted a reply the audit cannot parse")
                                elif parsed != rt["scores"]:
                                    rep.fail(f"{where}: stored scores {rt['scores']} != raw reply {parsed}")
                                else:
                                    directed[(rt["rater"], rt["target"])] += 1
                                    role = "initiator" if rt["rater"] == d["initiator"] else "responder"
                                    for it, v in parsed.items():
                                        rows[(g, loop, d["dyad_id"], rt["rater"], rt["target"], it)] = {
                                            "score": v, "round": d["round"], "rater_role": role,
                                            "construct": items_cfg[it], "snapshot": att["snapshot"]}
                            elif parsed is not None:
                                rep.warn(f"{where}: attempt {i + 1} was rejected by the runner but parses cleanly")
            want_pairs = set(permutations(agents, 2))
            complete = set(directed) == want_pairs and all(v == 1 for v in directed.values())
            if complete and not poisoned:
                expected.update(rows)
            else:
                poisoned = True
    rep.info.append(f"directed ratings re-parsed from raw replies: {counted}")
    return expected


def expected_measures(run, man, rep):
    defs = {m["name"]: m for m in man.get("measures", [])}
    for name, m in defs.items():
        items_file = Path(m["definition"]["items_file"])
        if not items_file.is_absolute():
            items_file = Path(__file__).resolve().parent / items_file
        if m.get("items_sha256") and items_file.exists() and sha(items_file) != m["items_sha256"]:
            rep.warn(f"item file for '{name}' has changed since the run started: {items_file}")
    expected = {}
    ratings_poison = {}  # (g) -> first bad loop, recomputed from ratings completeness
    n = man["config"]["group_size"]
    k_items = sum(len(v) for v in man["config"]["items"].values())
    for gdir in subdirs(run / "groups", "g"):
        g = int(gdir.name[1:])
        first_bad = None
        for ldir in subdirs(gdir, "l"):
            loop = int(ldir.name[1:])
            count = 0
            for rdir in subdirs(ldir, "r"):
                for res in load(rdir / "dyads.json"):
                    count += sum(1 for rt in res["ratings"] if rt["ok"])
            if count != n * (n - 1) and first_bad is None:
                first_bad = loop
        ratings_poison[g] = first_bad
        folders = [(ldir / "measures_start", int(ldir.name[1:])) for ldir in subdirs(gdir, "l")]
        folders.append((gdir / "measures_final", None))
        for folder, loop in folders:
            if not (folder / "measures.json").exists():
                continue
            # start measures depend on loops before this one; the final one depends on all loops
            upstream_bad = first_bad is not None and (loop is None or first_bad < loop)
            for res in load(folder / "measures.json"):
                name = res["measure"]
                where = f"g{g} {name} {res['agent']} {res['timepoint']} loop {res['loop']}"
                want_n = defs[name]["definition"]["expected_items"] if name in defs else None
                if want_n is None:
                    rep.fail(f"{where}: measure not declared in run_manifest.json")
                    continue
                order = res["order"]
                if len(order) != want_n or len(set(order)) != want_n:
                    rep.fail(f"{where}: displayed item order is not {want_n} distinct items")
                    continue
                for i, att in enumerate(res["attempts"]):
                    try:
                        raw = extract(att["text"])
                        keys = {str(k).strip() for k in raw}
                        if keys != {str(p) for p in range(1, want_n + 1)}:
                            raise ValueError("keys")
                        clean = {str(k).strip(): as_int(v) for k, v in raw.items()}
                        sc = defs[name]["definition"]["scale"]
                        if any(not int(sc["min"]) <= v <= int(sc["max"]) for v in clean.values()):
                            raise ValueError("range")
                        parsed = clean
                    except (ValueError, TypeError):
                        parsed = None
                    if res["ok"] and i == res["accepted_attempt"]:
                        if parsed is None:
                            rep.fail(f"{where}: runner accepted a reply the audit cannot parse")
                            break
                        stored = {str(r["displayed_position"]): (r["item_id"], r["score"]) for r in res["responses"]}
                        derived = {str(p): (order[p - 1], parsed[str(p)]) for p in range(1, want_n + 1)}
                        if stored != derived:
                            rep.fail(f"{where}: stored responses do not match the raw reply")
                            break
                        if not upstream_bad:
                            for p, (iid, v) in derived.items():
                                expected[(name, g, res["loop"], res["agent"], res["timepoint"], iid)] = {
                                    "score": v, "displayed_position": int(p), "snapshot": att["snapshot"]}
                    elif parsed is not None:
                        rep.warn(f"{where}: attempt {i + 1} was rejected by the runner but parses cleanly")
    return expected


def compare(rows, key_fields, expected, extra_checks, prov, label, rep):
    seen = {}
    for i, row in enumerate(rows, start=2):
        key = tuple(int(row[f]) if f in ("group", "loop") else row[f] for f in key_fields)
        if key in seen:
            rep.fail(f"{label} line {i}: duplicate row {key}")
            continue
        seen[key] = row
        for p in PROV:
            if row[p] != str(prov[p]):
                rep.fail(f"{label} line {i}: {p} is {row[p]!r}, manifest says {prov[p]!r}")
        if key not in expected:
            rep.fail(f"{label} line {i}: row has no valid raw source {key}")
            continue
        want = expected[key]
        if int(row["score"]) != want["score"]:
            rep.fail(f"{label} line {i}: score {row['score']} != raw {want['score']}")
        if row["snapshot"] != want["snapshot"]:
            rep.fail(f"{label} line {i}: snapshot {row['snapshot']!r} != raw {want['snapshot']!r}")
        for col in extra_checks:
            if str(row[col]) != str(want[col]):
                rep.fail(f"{label} line {i}: {col} {row[col]!r} != raw {want[col]!r}")
    for key in expected:
        if key not in seen:
            rep.fail(f"{label}: valid raw data missing from the CSV {key}")
    rep.info.append(f"{label}: {len(rows)} rows checked against {len(expected)} raw-derived values")


META = {"group", "agent", "handle", "administration", "when", "loop", "timepoint"}
LAYOUT_FILES = ["ratings.csv", "measures.csv", "schedule.csv", "flags.csv", "quarantine.csv",
                "transcripts.jsonl", "memory_snapshots.jsonl"]


def read_any(path):
    if path.suffix == ".csv":
        return read_csv(path)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def check_layout(run, man, rep):
    """Every loop_NN file must be exactly the combined file filtered to that loop, there must be exactly
    n_loops loop folders, and each over-time file must match combined/measures.csv cell by cell."""
    n_loops = man["config"]["n_loops"]
    found = sorted(p.name for p in run.glob("loop_*") if p.is_dir())
    want = [f"loop_{i:02d}" for i in range(1, n_loops + 1)]
    if found != want:
        rep.fail(f"loop folders are {found}, expected {want}")
    for name in LAYOUT_FILES:
        combined = read_any(run / "combined" / name)
        total = 0
        for i in range(1, n_loops + 1):
            path = run / f"loop_{i:02d}" / name
            if not path.exists():
                rep.fail(f"missing {path.relative_to(run)}")
                continue
            part = read_any(path)
            total += len(part)
            if part != [r for r in combined if int(r["loop"]) == i]:
                rep.fail(f"{path.relative_to(run)} does not match combined/{name} filtered to loop {i}")
        if total != len(combined):
            rep.fail(f"combined/{name} has {len(combined)} rows but the loop folders hold {total}")
    # over-time files, rebuilt independently from combined/measures.csv
    rows = read_csv(run / "combined" / "measures.csv")
    for mname in sorted({r["measure"] for r in rows}):
        path = run / f"{mname}_over_time.csv"
        if not path.exists():
            rep.fail(f"missing {path.name}")
            continue
        want_cells = {}
        for r in rows:
            if r["measure"] != mname:
                continue
            admin = int(r["loop"]) if r["timepoint"] == "start" else n_loops + 1
            want_cells[(r["group"], r["agent"], admin, r["item_id"])] = r["score"]
        got_cells, admins = {}, set()
        for i, r in enumerate(read_csv(path), start=2):
            admin = int(r["administration"])
            if (r["group"], r["agent"], admin) in admins:
                rep.fail(f"{path.name} line {i}: duplicate administration")
            admins.add((r["group"], r["agent"], admin))
            expected_when = "before any chats" if admin == 1 else f"after loop {admin - 1}"
            if r["when"] != expected_when:
                rep.fail(f"{path.name} line {i}: 'when' is {r['when']!r}, expected {expected_when!r}")
            for col, v in r.items():
                if col not in META and v != "":
                    got_cells[(r["group"], r["agent"], admin, col)] = v
        if got_cells != want_cells:
            diff = len(set(got_cells.items()) ^ set(want_cells.items()))
            rep.fail(f"{path.name} disagrees with combined/measures.csv in {diff} cells")
        rep.info.append(f"{path.name}: {len(admins)} administrations checked cell by cell")
    rep.info.append(f"loop folders verified against combined/: {len(found)}")


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    run = Path(sys.argv[1])
    rep = Report()
    man = load(run / "run_manifest.json")
    cfg, prov = man["config"], man["provenance"]

    check_seals(run, rep)

    hashes_path = run / "outputs_sha256.json"
    if hashes_path.exists():
        for name, h in load(hashes_path).items():
            if not (run / name).exists() or sha(run / name) != h:
                rep.fail(f"{name} changed after assembly (re-run assemble, then audit again)")
    else:
        rep.fail("outputs_sha256.json missing: run assembly first")

    exp_r = expected_ratings(run, cfg, rep)
    compare(read_csv(run / "combined" / "ratings.csv"), ("group", "loop", "dyad_id", "rater", "target", "item"),
            exp_r, ("round", "rater_role", "construct"), prov, "ratings.csv", rep)

    exp_m = expected_measures(run, man, rep)
    compare(read_csv(run / "combined" / "measures.csv"), ("measure", "group", "loop", "agent", "timepoint", "item_id"),
            exp_m, ("displayed_position",), prov, "measures.csv", rep)

    check_layout(run, man, rep)

    status = "FAIL" if rep.fails else "PASS"
    lines = [f"AUDIT {status}: {run}", f"design digest {man['design_digest']}", ""]
    lines += [f"  {x}" for x in rep.info]
    lines += ["", f"FAIL ({len(rep.fails)})"] + [f"  {x}" for x in rep.fails[:200]]
    lines += ["", f"WARN ({len(rep.warns)})"] + [f"  {x}" for x in rep.warns[:200]]
    text = "\n".join(lines)
    (run / "audit_report.txt").write_text(text + "\n", encoding="utf-8")
    print(text)
    sys.exit(1 if rep.fails else 0)


if __name__ == "__main__":
    main()
