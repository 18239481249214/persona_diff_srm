"""Swappable self-report measures. Each measure is one JSON definition plus one items CSV.
The runner never knows which questionnaire it is giving; everything specific lives in the files."""
import csv
from pathlib import Path

from . import prompts
from .util import parse_json_reply, sha256_file, strict_int, substream

ROOT = Path(__file__).resolve().parent.parent
REQUIRED = ("name", "instructions", "scale", "items_file", "expected_items")


def _resolve(p):
    p = Path(p)
    return p if p.is_absolute() else ROOT / p


class Measure:
    def __init__(self, definition_path, allow_placeholder):
        import json
        self.definition_path = _resolve(definition_path)
        with open(self.definition_path, "r", encoding="utf-8") as f:
            d = json.load(f)
        missing = [k for k in REQUIRED if k not in d]
        if missing:
            raise SystemExit(f"{self.definition_path} is missing: {', '.join(missing)}")
        self.name = d["name"]
        self.instructions = d["instructions"]
        self.item_stem = d.get("item_stem", "")
        self.scale_min = int(d["scale"]["min"])
        self.scale_max = int(d["scale"]["max"])
        self.labels = {int(k): v for k, v in d["scale"].get("labels", {}).items()}
        self.items_file = _resolve(d["items_file"])
        self.expected = int(d["expected_items"])
        self.randomize = bool(d.get("randomize_order", True))
        self.max_tokens = d.get("max_tokens")
        self.definition = d
        self.items, self.placeholder = self._load_items(allow_placeholder)

    def _load_items(self, allow_placeholder):
        def placeholder():
            return [(f"{self.name}_{i:02d}", f"(placeholder item {i})") for i in range(1, self.expected + 1)], True

        if not self.items_file.exists():
            if allow_placeholder:
                return placeholder()
            raise SystemExit(f"Item file for measure '{self.name}' not found: {self.items_file}")
        items = []
        with open(self.items_file, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                iid, text = (row.get("item_id") or "").strip(), (row.get("text") or "").strip()
                if iid and text:
                    items.append((iid, text))
        ids = [i for i, _ in items]
        if len(ids) != len(set(ids)):
            raise SystemExit(f"{self.items_file} has duplicate item_id values.")
        if len(items) != self.expected:
            if allow_placeholder and not items:
                return placeholder()
            raise SystemExit(f"{self.items_file} has {len(items)} filled items; "
                             f"measure '{self.name}' expects {self.expected}.")
        return items, False

    def fingerprint(self):
        return {"name": self.name, "definition": self.definition,
                "items_sha256": sha256_file(self.items_file) if self.items_file.exists() else None,
                "placeholder": self.placeholder}


def load_measures(cfg, allow_placeholder):
    measures = [Measure(p, allow_placeholder) for p in cfg.get("measures", [])]
    names = [m.name for m in measures]
    if len(names) != len(set(names)):
        raise SystemExit("Two measures share the same name.")
    return measures


def administer(client, cfg, measure, agent, handle, mem, group, loop, timepoint):
    order = list(measure.items)
    if measure.randomize:
        substream(cfg["seed"], "measure_items", measure.name, group, loop, timepoint, agent).shuffle(order)
    numbered = [(pos, text) for pos, (_, text) in enumerate(order, start=1)]
    messages = prompts.measure_messages(handle, mem, prompts.measure_prompt(measure, numbered))
    expect = [str(i) for i in range(1, len(order) + 1)]
    max_tokens = measure.max_tokens or cfg["max_tokens"]["measure"]
    base = {"measure": measure.name, "agent": agent, "group": group, "loop": loop, "timepoint": timepoint,
            "order": [iid for iid, _ in order]}
    attempts = []
    for attempt_idx in range(2):
        res = client.complete(messages, max_tokens, "measure", expect)
        attempts.append(res)
        try:
            data = parse_json_reply(res["text"])
            keys = {str(k).strip() for k in data}
            if keys != set(expect):
                missing = sorted(set(expect) - keys, key=int)
                extra = sorted(keys - set(expect))
                raise ValueError(f"wrong keys (missing {missing[:5]}, extra {extra[:5]})")
            clean = {str(k).strip(): v for k, v in data.items()}
            responses = []
            for pos, iid in enumerate(base["order"], start=1):
                v = strict_int(clean[str(pos)])
                if not measure.scale_min <= v <= measure.scale_max:
                    raise ValueError(f"score {v} out of range for position {pos}")
                responses.append({"item_id": iid, "displayed_position": pos, "score": v})
            return {**base, "ok": True, "accepted_attempt": attempt_idx, "responses": responses,
                    "attempts": attempts}
        except (ValueError, TypeError) as e:
            res["parse_error"] = str(e)
    return {**base, "ok": False, "accepted_attempt": None, "responses": [], "attempts": attempts}
