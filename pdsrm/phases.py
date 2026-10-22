"""Dyad-level model calls: conversation turns, ratings, memory rewrite. (Questionnaires live in measures.py.)"""
import re
from concurrent.futures import ThreadPoolExecutor

from . import prompts
from .util import approx_tokens, parse_json_reply, strict_int, substream

NARRATION = re.compile(r"\*[^*\n]{2,80}\*")  # *smiles*, *leans back* style action text


# ---------------------------------------------------------------- conversation

def converse(client, cfg, dyad, handles, memories):
    ini, resp = dyad["initiator"], dyad["responder"]
    turns, flags = [], []
    for t in range(cfg["k"]):
        speaker = ini if t % 2 == 0 else resp
        partner = resp if speaker == ini else ini
        msgs = [{"role": "system", "content": prompts.conversation_system(
            handles[speaker], handles[partner], memories[speaker], cfg.get("tell_repeat_meetings", False))}]
        if speaker == ini:
            msgs.append({"role": "user", "content": prompts.INITIATOR_START})
        for prev in turns:
            msgs.append({"role": "assistant" if prev["agent"] == speaker else "user", "content": prev["text"]})
        res = None
        for _ in range(3):  # retry empty replies twice
            res = client.complete(msgs, cfg["max_tokens"]["conversation"], "conversation")
            if res["text"].strip():
                break
        text = res["text"].strip()
        turn = {"turn": t + 1, "agent": speaker, "handle": handles[speaker],
                "role": "initiator" if speaker == ini else "responder", "text": text,
                "snapshot": res["snapshot"], "finish_reason": res["finish_reason"], "usage": res["usage"]}
        if res["finish_reason"] == "length":
            flags.append({"dyad_id": dyad["dyad_id"], "agent": speaker, "stage": "conversation",
                          "flag": "truncated", "detail": f"turn {t + 1} hit max_tokens"})
        if not text:
            flags.append({"dyad_id": dyad["dyad_id"], "agent": speaker, "stage": "conversation",
                          "flag": "empty_message", "detail": f"turn {t + 1}"})
        elif NARRATION.search(text):
            flags.append({"dyad_id": dyad["dyad_id"], "agent": speaker, "stage": "conversation",
                          "flag": "action_narration", "detail": f"turn {t + 1}"})
        turns.append(turn)
    return turns, flags


# ---------------------------------------------------------------- ratings

def rate(client, cfg, dyad, rater, target, handles, memories, turns):
    items = [it for construct in cfg["items"].values() for it in construct]
    rng = substream(cfg["seed"], "items", dyad["dyad_id"], rater)
    rng.shuffle(items)
    lo, hi = cfg["rating_scale"]
    messages = [
        {"role": "system", "content": prompts.identity_system(handles[rater], memories[rater])},
        {"role": "user", "content": prompts.rating_prompt(
            handles[target], prompts.render_transcript(turns, rater), items, lo, hi)},
    ]
    attempts = []
    for attempt_idx in range(2):
        res = client.complete(messages, cfg["max_tokens"]["rating"], "rating", items)
        attempts.append(res)
        try:
            raw = parse_json_reply(res["text"])
            data = {}
            for k, v in raw.items():
                key = str(k).strip().lower()
                if key in data:
                    raise ValueError(f"duplicate key after normalizing: {key}")
                data[key] = v
            if set(data) != set(items):
                raise ValueError(f"wrong keys: got {sorted(data)}, expected {sorted(items)}")
            scores = {}
            for it in items:
                v = strict_int(data[it])
                if not lo <= v <= hi:
                    raise ValueError(f"score {v} out of range")
                scores[it] = v
            return {"rater": rater, "target": target, "ok": True, "accepted_attempt": attempt_idx,
                    "scores": scores, "item_order": items, "attempts": attempts}
        except (ValueError, TypeError) as e:
            res["parse_error"] = str(e)
    return {"rater": rater, "target": target, "ok": False, "accepted_attempt": None, "scores": {},
            "item_order": items, "attempts": attempts}


# ---------------------------------------------------------------- memory

def check_caps(mem, caps):
    problems = []
    tok = {"self": approx_tokens(mem["self"]), "interactions": approx_tokens(mem["interactions"]),
           "others": {h: approx_tokens(v) for h, v in mem["others"].items()}}
    if tok["self"] > caps["self"]:
        problems.append(f'"self" is about {tok["self"]} tokens, limit {caps["self"]}')
    if tok["interactions"] > caps["interactions"]:
        problems.append(f'"interactions" is about {tok["interactions"]} tokens, limit {caps["interactions"]}')
    for h, n in tok["others"].items():
        if n > caps["others_per_partner"]:
            problems.append(f'the entry for {h} is about {n} tokens, limit {caps["others_per_partner"]}')
    tok["total"] = tok["self"] + tok["interactions"] + sum(tok["others"].values())
    return problems, tok


def _parse_memory(text):
    data = parse_json_reply(text)
    others = data.get("others") or {}
    if not isinstance(others, dict):
        raise ValueError("'others' is not an object")
    extra = set(data) - {"self", "others", "interactions"}
    if extra:
        raise ValueError(f"unexpected memory sections: {sorted(extra)}")
    for key in ("self", "interactions"):
        if data.get(key) is not None and not isinstance(data[key], str):
            raise ValueError(f"'{key}' is not text")
    for h, v in others.items():
        if not isinstance(v, str):
            raise ValueError(f"entry for {h} is not text")
    return {"self": data.get("self") or "", "others": {str(h): v for h, v in others.items()},
            "interactions": data.get("interactions") or ""}


def update_memory(client, cfg, dyad, agent, partner, handles, memories, turns):
    caps = cfg["memory_caps"]
    old = memories[agent]
    known = sorted(set(old["others"]) | {handles[partner]})
    messages = [
        {"role": "system", "content": prompts.identity_system(handles[agent], old)},
        {"role": "user", "content": prompts.memory_prompt(
            handles[partner], prompts.render_transcript(turns, agent), caps)},
    ]
    attempts, flags = [], []
    mem, text = None, None
    for _ in range(2):
        res = client.complete(messages, cfg["max_tokens"]["memory"], "memory", known)
        attempts.append(res)
        try:
            mem, text = _parse_memory(res["text"]), res["text"]
            break
        except (ValueError, TypeError, AttributeError) as e:
            res["parse_error"] = str(e)
    if mem is None:
        flags.append({"dyad_id": dyad["dyad_id"], "agent": agent, "stage": "memory",
                      "flag": "memory_parse_failed", "detail": "kept previous memory"})
        _, tok = check_caps(old, caps)
        return {"agent": agent, "ok": False, "memory": old, "tokens": tok, "condensed": False,
                "over_cap": False, "attempts": attempts}, flags

    problems, tok = check_caps(mem, caps)
    condensed = False
    if problems:
        condensed = True
        msgs2 = messages + [{"role": "assistant", "content": text},
                            {"role": "user", "content": prompts.condense_prompt(problems)}]
        res = client.complete(msgs2, cfg["max_tokens"]["memory"], "memory", known)
        attempts.append(res)
        try:
            mem2 = _parse_memory(res["text"])
            mem = mem2
            problems, tok = check_caps(mem, caps)
        except (ValueError, TypeError, AttributeError) as e:
            res["parse_error"] = str(e)
        if problems:
            flags.append({"dyad_id": dyad["dyad_id"], "agent": agent, "stage": "memory",
                          "flag": "over_cap", "detail": "; ".join(problems)})

    if any(a.get("finish_reason") == "length" for a in attempts):
        flags.append({"dyad_id": dyad["dyad_id"], "agent": agent, "stage": "memory",
                      "flag": "truncated", "detail": "a memory reply hit max_tokens"})
    unknown = [h for h in mem["others"] if h not in handles.values()]
    if unknown:
        flags.append({"dyad_id": dyad["dyad_id"], "agent": agent, "stage": "memory",
                      "flag": "unknown_handle_in_others", "detail": ", ".join(unknown)})
    return {"agent": agent, "ok": True, "memory": mem, "tokens": tok, "condensed": condensed,
            "over_cap": bool(problems), "attempts": attempts}, flags


# ---------------------------------------------------------------- whole dyad

def run_dyad(client, cfg, dyad, handles, memories, wrap=lambda f: f):
    """Chat turns are sequential by nature. The two ratings run in parallel, then the two memory
    rewrites run in parallel; neither agent sees the other's rating or new memory, so order is irrelevant.
    `wrap` lets the progress display follow the work into helper threads."""
    ini, resp = dyad["initiator"], dyad["responder"]
    turns, flags = converse(client, cfg, dyad, handles, memories)
    with ThreadPoolExecutor(2) as ex:
        r1 = ex.submit(wrap(rate), client, cfg, dyad, ini, resp, handles, memories, turns)
        r2 = ex.submit(wrap(rate), client, cfg, dyad, resp, ini, handles, memories, turns)
        ratings = [r1.result(), r2.result()]
    for r in ratings:
        if not r["ok"]:
            flags.append({"dyad_id": dyad["dyad_id"], "agent": r["rater"], "stage": "rating",
                          "flag": "rating_parse_failed", "detail": f"target {r['target']}"})
    with ThreadPoolExecutor(2) as ex:
        m1 = ex.submit(wrap(update_memory), client, cfg, dyad, ini, resp, handles, memories, turns)
        m2 = ex.submit(wrap(update_memory), client, cfg, dyad, resp, ini, handles, memories, turns)
        done = [m1.result(), m2.result()]
    updates = []
    for u, f in done:
        updates.append(u)
        flags.extend(f)
    return {"dyad": dyad, "handles": {ini: handles[ini], resp: handles[resp]},
            "turns": turns, "ratings": ratings, "memory_updates": updates, "flags": flags}
