"""Small shared helpers."""
import hashlib
import json
import math
import os
import random
import re
import shutil
from pathlib import Path


def substream(seed, name, *parts):
    """Independent, reproducible RNG for a named purpose (e.g. 'schedule', group, loop)."""
    key = ":".join([str(seed), name] + [str(p) for p in parts])
    h = int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:8], "big")
    return random.Random(h)


def approx_tokens(text):
    """Model-agnostic token estimate (~4 characters per token). Used only for memory caps."""
    return math.ceil(len(text or "") / 4)


def _reject_duplicates(pairs):
    keys = [k for k, _ in pairs]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    if dupes:
        raise ValueError(f"duplicate keys in reply: {dupes}")
    return dict(pairs)


def parse_json_reply(text):
    """Pull the JSON object out of a model reply (tolerates ``` fences and stray prose).
    Duplicate keys are an error, not silently overwritten."""
    if not text or not text.strip():
        raise ValueError("empty reply")
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found")
    obj = json.loads(t[start:end + 1], object_pairs_hook=_reject_duplicates)
    if not isinstance(obj, dict):
        raise ValueError("reply is not a JSON object")
    return obj


def strict_int(v):
    """Accept 4, 4.0 or "4". Reject 4.5, "4.5", "four", true. Never rounds or truncates."""
    if isinstance(v, bool):
        raise ValueError(f"not an integer: {v!r}")
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and re.fullmatch(r"\s*-?\d+\s*", v):
        return int(v)
    raise ValueError(f"not an integer: {v!r}")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


HASH_FILE = "sha256.json"


def hash_folder(folder):
    folder = Path(folder)
    return {p.name: sha256_file(p) for p in sorted(folder.iterdir())
            if p.is_file() and p.name != HASH_FILE}


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def commit_dir(tmp_dir, final_dir):
    """Seal a finished temp folder with SHA-256 hashes of its files, then promote it atomically.
    A folder only exists under its final name once fully written and hashed."""
    tmp_dir, final_dir = Path(tmp_dir), Path(final_dir)
    write_json(tmp_dir / HASH_FILE, hash_folder(tmp_dir))
    if final_dir.exists():
        shutil.rmtree(final_dir)
    os.replace(tmp_dir, final_dir)


def fresh_tmp(final_dir):
    tmp = Path(str(final_dir) + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    return tmp


def design_digest(cfg, extra=None):
    """Hash of every setting that changes the design (not run name, workers, or key location),
    plus fingerprints of the measure definitions and item files."""
    design = {k: v for k, v in cfg.items() if k not in ("run_name", "workers", "runs_dir", "parallel_groups")}
    model = dict(design.get("model", {}))
    model.pop("api_key_env", None)
    model.pop("request_timeout_s", None)
    design["model"] = model
    if extra is not None:
        design["_measures"] = extra
    blob = json.dumps(design, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]
