"""Turn the sealed per-round folders into analysis files. Safe to re-run any time."""
import csv
import json
from pathlib import Path

from .util import read_json, sha256_file, write_json

RATING_FIELDS = ["group", "loop", "round", "dyad_id", "rater", "target", "rater_role", "construct", "item",
                 "score", "model", "provider", "temperature", "reasoning", "snapshot"]
MEASURE_FIELDS = ["measure", "group", "loop", "agent", "handle", "timepoint", "item_id", "displayed_position",
                  "score", "model", "provider", "temperature", "reasoning", "snapshot"]
OVER_TIME_META = ["group", "agent", "handle", "administration", "when", "loop", "timepoint"]


def _write_csv(path, rows, fields):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _write(path, rows, fields):
    if fields is None:
        _write_jsonl(path, rows)
    else:
        _write_csv(path, rows, fields)


def over_time_rows(measure_rows, n_loops):
    """One row per agent per administration, items as columns, built only from measures.csv rows."""
    by_measure = {}
    for r in measure_rows:
        by_measure.setdefault(r["measure"], []).append(r)
    out = {}
    for name, rows in by_measure.items():
        items = sorted({r["item_id"] for r in rows})
        admins = {}
        for r in rows:
            admin = int(r["loop"]) if r["timepoint"] == "start" else n_loops + 1
            key = (int(r["group"]), r["agent"], admin)
            if key not in admins:
                admins[key] = {"group": r["group"], "agent": r["agent"], "handle": r["handle"],
                               "administration": admin,
                               "when": "before any chats" if admin == 1 else f"after loop {admin - 1}",
                               "loop": r["loop"], "timepoint": r["timepoint"]}
            admins[key][r["item_id"]] = r["score"]
        out[name] = ([admins[k] for k in sorted(admins)], OVER_TIME_META + items)
    return out


def write_layout(run_dir, cfg, files):
    """combined/ holds every loop; loop_NN/ holds the same files with the same columns for one loop;
    <measure>_over_time.csv holds every questionnaire administration, one row per agent per time.
    In the last loop's folder, measures.csv holds both its start and the final questionnaire."""
    n_loops = cfg["n_loops"]
    written = []
    (run_dir / "combined").mkdir(exist_ok=True)
    for name, (rows, fields) in files.items():
        _write(run_dir / "combined" / name, rows, fields)
        written.append(f"combined/{name}")
    for loop in range(1, n_loops + 1):
        ldir = run_dir / f"loop_{loop:02d}"
        ldir.mkdir(exist_ok=True)
        for name, (rows, fields) in files.items():
            _write(ldir / name, [r for r in rows if int(r["loop"]) == loop], fields)
            written.append(f"loop_{loop:02d}/{name}")
    for name, (rows, fields) in over_time_rows(files["measures.csv"][0], n_loops).items():
        _write_csv(run_dir / f"{name}_over_time.csv", rows, fields)
        written.append(f"{name}_over_time.csv")
    write_json(run_dir / "outputs_sha256.json", {f: sha256_file(run_dir / f) for f in written})


def _dirs(parent, prefix):
    return sorted(p for p in Path(parent).glob(prefix + "*") if p.is_dir() and not p.name.endswith(".tmp"))


def assemble(run_dir, log=print):
    run_dir = Path(run_dir)
    man = read_json(run_dir / "run_manifest.json")
    cfg, prov = man["config"], man["provenance"]
    expected_items = {m["name"]: m["definition"]["expected_items"] for m in man.get("measures", [])}
    item_construct = {it: c for c, its in cfg["items"].items() for it in its}
    n = cfg["group_size"]
    expected_rows = n * (n - 1) * len(item_construct)

    ratings, measure_rows, schedule_rows, flags, quarantine, summary = [], [], [], [], [], []
    transcripts, memories = [], []

    for gdir in _dirs(run_dir / "groups", "g"):
        g = int(gdir.name[1:])
        handles = read_json(gdir / "handles.json")
        poisoned = False  # once a loop is incomplete, every later loop in the group depends on it

        def add_measures(folder, upstream_bad):
            if not (folder / "measures.json").exists():
                return
            for res in read_json(folder / "measures.json"):
                scope = f"measure:{res['measure']}:{res['agent']}:{res['timepoint']}"
                if res["ok"] and res["accepted_attempt"] > 0:
                    flags.append({"group": g, "loop": res["loop"], "dyad_id": "", "agent": res["agent"],
                                  "stage": "measure", "flag": "accepted_on_retry",
                                  "detail": f"{res['measure']}: {res['attempts'][0].get('parse_error', '')}"})
                vals = {r["score"] for r in res["responses"]}
                if res["ok"] and len(vals) == 1:
                    flags.append({"group": g, "loop": res["loop"], "dyad_id": "", "agent": res["agent"],
                                  "stage": "measure", "flag": "straightline",
                                  "detail": f"{res['measure']} {res['timepoint']}: all {len(res['responses'])} "
                                            f"items = {vals.pop()}"})
                if not res["ok"]:
                    flags.append({"group": g, "loop": res["loop"], "dyad_id": "", "agent": res["agent"],
                                  "stage": "measure", "flag": "measure_parse_failed",
                                  "detail": f"{res['measure']} {res['timepoint']}"})
                if upstream_bad:
                    quarantine.append({"group": g, "loop": res["loop"], "scope": scope,
                                       "reason": "earlier loop incomplete"})
                    continue
                want = expected_items.get(res["measure"])
                if len(res["responses"]) != want:
                    quarantine.append({"group": g, "loop": res["loop"], "scope": scope,
                                       "reason": f"{len(res['responses'])} of {want} items"})
                    continue
                snap = res["attempts"][res["accepted_attempt"]]["snapshot"]
                for r in res["responses"]:
                    measure_rows.append({"measure": res["measure"], "group": g, "loop": res["loop"],
                                         "agent": res["agent"], "handle": handles[res["agent"]],
                                         "timepoint": res["timepoint"], "item_id": r["item_id"],
                                         "displayed_position": r["displayed_position"], "score": r["score"],
                                         **prov, "snapshot": snap})

        for ldir in _dirs(gdir, "l"):
            loop = int(ldir.name[1:])
            add_measures(ldir / "measures_start", poisoned)
            loop_ratings = []
            if (ldir / "schedule.json").exists():
                for rnd in read_json(ldir / "schedule.json"):
                    for d in rnd:
                        schedule_rows.append({k: d[k] for k in ("group", "loop", "round", "dyad_id",
                                                                "initiator", "responder")})
            for rdir in _dirs(ldir, "r"):
                for res in read_json(rdir / "dyads.json"):
                    d = res["dyad"]
                    transcripts.append({"group": g, "loop": loop, "round": d["round"], "dyad_id": d["dyad_id"],
                                        "initiator": d["initiator"], "responder": d["responder"],
                                        "handles": res["handles"], "turns": res["turns"]})
                    for f in res["flags"]:
                        flags.append({"group": g, "loop": loop, **f})
                    for rt in res["ratings"]:
                        if rt["ok"] and rt["accepted_attempt"] > 0:
                            flags.append({"group": g, "loop": loop, "dyad_id": d["dyad_id"], "agent": rt["rater"],
                                          "stage": "rating", "flag": "accepted_on_retry",
                                          "detail": rt["attempts"][0].get("parse_error", "")})
                        if not rt["ok"]:
                            continue
                        snap = rt["attempts"][rt["accepted_attempt"]]["snapshot"]
                        role = "initiator" if rt["rater"] == d["initiator"] else "responder"
                        for item in rt["item_order"]:
                            loop_ratings.append({
                                "group": g, "loop": loop, "round": d["round"], "dyad_id": d["dyad_id"],
                                "rater": rt["rater"], "target": rt["target"], "rater_role": role,
                                "construct": item_construct[item], "item": item, "score": rt["scores"][item],
                                **prov, "snapshot": snap})
                    for u in res["memory_updates"]:
                        partner = d["responder"] if u["agent"] == d["initiator"] else d["initiator"]
                        memories.append({"group": g, "loop": loop, "round": d["round"], "dyad_id": d["dyad_id"],
                                         "agent": u["agent"], "handle": handles[u["agent"]], "partner": partner,
                                         "ok": u["ok"], "condensed": u["condensed"], "over_cap": u["over_cap"],
                                         "tokens": u["tokens"], "memory": u["memory"]})
            complete = len(loop_ratings) == expected_rows
            bad = poisoned or not complete
            if bad:
                quarantine.append({"group": g, "loop": loop, "scope": "ratings",
                                   "reason": "earlier loop incomplete" if poisoned
                                   else f"{len(loop_ratings)} of {expected_rows} rating rows"})
                poisoned = True
            else:
                ratings.extend(loop_ratings)
            summary.append(f"  group {g} loop {loop}: {len(loop_ratings)}/{expected_rows} rating rows"
                           + (" (QUARANTINED)" if bad else ""))
        add_measures(gdir / "measures_final", poisoned)

    write_layout(run_dir, cfg, {
        "ratings.csv": (ratings, RATING_FIELDS),
        "measures.csv": (measure_rows, MEASURE_FIELDS),
        "schedule.csv": (schedule_rows, ["group", "loop", "round", "dyad_id", "initiator", "responder"]),
        "flags.csv": (flags, ["group", "loop", "dyad_id", "agent", "stage", "flag", "detail"]),
        "quarantine.csv": (quarantine, ["group", "loop", "scope", "reason"]),
        "transcripts.jsonl": (transcripts, None),
        "memory_snapshots.jsonl": (memories, None),
    })

    log(f"Assembled outputs: combined/, loop_01/ to loop_{cfg['n_loops']:02d}/, plus an over-time file per questionnaire")
    for line in summary:
        log(line)
    log(f"  ratings.csv: {len(ratings)} rows | measures.csv: {len(measure_rows)} rows | "
        f"flags: {len(flags)} | quarantined: {len(quarantine)}")
    if flags:
        from collections import Counter
        kinds = Counter(f["flag"] for f in flags)
        log("  flags by type: " + ", ".join(f"{n} {k}" for k, n in kinds.most_common()))
