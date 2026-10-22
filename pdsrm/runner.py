"""Drives groups -> loops -> BFI-2 -> rounds -> dyads. Each finished round is an atomic folder,
so a crashed run resumes from the last complete round with identical memory state."""
import copy
import shutil
from collections import Counter
import platform
import string
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

from . import measures as measures_mod
from . import phases
from .client import provenance
from .progress import Progress, ProgressClient, StopRequested, carry_slot
from .prompts import EMPTY_MEMORY
from .schedule import build_schedule
from .util import commit_dir, design_digest, fresh_tmp, read_json, sha256_file, substream, write_json

VERSION = "0.4.0"


def make_handles(seed, group, agents):
    rng = substream(seed, "handles", group)
    alphabet = string.ascii_lowercase + string.digits
    used, out = set(), {}
    for a in agents:
        while True:
            h = "user_" + "".join(rng.choice(alphabet) for _ in range(4))
            if h not in used:
                used.add(h)
                out[a] = h
                break
    return out


class Runner:
    def __init__(self, cfg, client, run_dir, dry_run, log=print):
        self.cfg, self.dry_run = cfg, dry_run
        n = cfg["group_size"]
        rounds_per_loop = n - 1 if n % 2 == 0 else n
        self.progress = Progress(cfg["k"], cfg["n_groups"] * cfg["n_loops"] * rounds_per_loop)
        self.log = self.progress.println
        self.client = ProgressClient(client, self.progress)
        self.run_dir = Path(run_dir)
        self.parallel_groups = max(1, int(cfg.get("parallel_groups", 4)))
        self.multi = cfg["n_groups"] > 1
        self.measures = measures_mod.load_measures(cfg, allow_placeholder=dry_run)
        self.measure_fp = [m.fingerprint() for m in self.measures]
        self.prompts_path = Path(__file__).resolve().parent / "prompts.py"
        self.prompts_sha = sha256_file(self.prompts_path)
        if cfg["k"] % 2:
            raise SystemExit("k must be even.")
        if cfg["group_size"] < 2:
            raise SystemExit("group_size must be at least 2.")

    # ------------------------------------------------------------------ manifest
    def write_manifest(self):
        path = self.run_dir / "run_manifest.json"
        digest = design_digest(self.cfg, {"measures": self.measure_fp, "prompts_sha256": self.prompts_sha})
        if path.exists():
            old = read_json(path)
            if old["design_digest"] != digest:
                raise SystemExit(
                    f"{self.run_dir} was started with a different design (digest {old['design_digest']}, "
                    f"now {digest}). The config, measures, or prompt text changed. "
                    f"Use a new run_name instead of changing an existing run.")
            self.log(f"Resuming run {self.run_dir.name} (design {digest}).")
            return
        write_json(path, {
            "run_name": self.cfg["run_name"], "design_digest": digest, "config": self.cfg,
            "provenance": provenance(self.cfg["model"]), "dry_run": self.dry_run,
            "measures": self.measure_fp, "prompts_sha256": self.prompts_sha, "code_version": VERSION,
            "python": platform.python_version(),
            "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        shutil.copyfile(self.prompts_path, self.run_dir / "prompts_snapshot.py")
        self.log(f"Started run {self.run_dir.name} (design {digest}).")

    # ------------------------------------------------------------------ measures
    def measure_phase(self, folder, agents, handles, memories, group, loop, timepoint):
        if folder.exists() or not self.measures:
            return
        tmp = fresh_tmp(folder)
        jobs = [(m, a) for m in self.measures for a in agents]
        slots = [f"g{group}-{a}" for a in agents]

        def task(m, a):
            self.progress.bind(f"g{group}-{a}")
            return measures_mod.administer(self.client, self.cfg, m, a, handles[a], memories[a], group, loop, timepoint)

        self.progress.add(slots)
        try:
            with ThreadPoolExecutor(len(jobs)) as ex:
                futs = [ex.submit(task, m, a) for m, a in jobs]
                results = [f.result() for f in futs]
        finally:
            self.progress.remove(slots)
        write_json(tmp / "measures.json", results)
        commit_dir(tmp, folder)
        bad = sum(1 for r in results if not r["ok"])
        names = ", ".join(m.name for m in self.measures)
        self.log(f"{self.tag(group)}  Measures ({names}, {timepoint}) done for {len(agents)} agents"
                 + (f", {bad} FAILED" if bad else ""))

    def tag(self, g):
        return f"[g{g:02d}]" if self.multi else ""

    # ------------------------------------------------------------------ group
    def run_group(self, g):
        n = self.cfg["group_size"]
        agents = [f"A{i:02d}" for i in range(1, n + 1)]
        gdir = self.run_dir / "groups" / f"g{g:02d}"
        handles_path = gdir / "handles.json"
        if handles_path.exists():
            handles = read_json(handles_path)
        else:
            handles = make_handles(self.cfg["seed"], g, agents)
            write_json(handles_path, handles)
        memories = {a: copy.deepcopy(EMPTY_MEMORY) for a in agents}
        self.log(f"{self.tag(g)}Group {g}: agents {', '.join(f'{a}={handles[a]}' for a in agents)}")

        for loop in range(1, self.cfg["n_loops"] + 1):
            ldir = gdir / f"l{loop:02d}"
            self.log(f"{self.tag(g)} Loop {loop}")
            self.measure_phase(ldir / "measures_start", agents, handles, memories, g, loop, "start")
            schedule = build_schedule(agents, self.cfg["seed"], g, loop)
            write_json(ldir / "schedule.json", schedule)

            for rnd in schedule:
                r = rnd[0]["round"]
                rdir = ldir / f"r{r:02d}"
                if rdir.exists():
                    memories = read_json(rdir / "memories_after.json")
                    self.progress.round_done()
                    self.log(f"{self.tag(g)}  Loop {loop} round {r}: already complete, skipping")
                    continue
                tmp = fresh_tmp(rdir)
                snapshot = copy.deepcopy(memories)
                slots = [f"g{g}-d{i}" for i in range(1, len(rnd) + 1)]

                def task(d, slot):
                    self.progress.bind(slot)
                    return phases.run_dyad(self.client, self.cfg, d, handles, snapshot,
                                           wrap=lambda fn: carry_slot(self.progress, fn))

                self.progress.add(slots)
                try:
                    with ThreadPoolExecutor(len(rnd)) as ex:
                        futs = [ex.submit(task, d, sl) for d, sl in zip(rnd, slots)]
                        results = [f.result() for f in futs]
                finally:
                    self.progress.remove(slots)
                new_mem = copy.deepcopy(memories)
                for res in results:
                    for u in res["memory_updates"]:
                        new_mem[u["agent"]] = u["memory"]
                write_json(tmp / "dyads.json", results)
                write_json(tmp / "memories_after.json", new_mem)
                commit_dir(tmp, rdir)
                memories = new_mem
                nflags = sum(len(x["flags"]) for x in results)
                pairs = ", ".join(f"{d['initiator']}->{d['responder']}" for d in rnd)
                kinds = Counter(f["flag"] for x in results for f in x["flags"])
                detail = ", ".join(f"{n} {k}" for k, n in kinds.most_common())
                self.progress.round_done()
                self.log(f"{self.tag(g)}  Loop {loop} round {r} done: {pairs}"
                         + (f" (flags: {detail})" if nflags else ""))

        # Always: where each agent ends up after its last memory rewrite.
        self.measure_phase(gdir / "measures_final", agents, handles, memories, g, self.cfg["n_loops"], "final")

    def run(self):
        """Groups are independent, so up to parallel_groups of them run at once. Inside a group, rounds
        stay in order (each agent's next chat must see its updated memory) and every dyad in a round
        runs at the same time."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.write_manifest()
        self.progress.start()
        errors = []
        ex = ThreadPoolExecutor(min(self.parallel_groups, self.cfg["n_groups"]))
        futs = {ex.submit(self.run_group, g): g for g in range(1, self.cfg["n_groups"] + 1)}
        pending = set(futs)
        try:
            while pending:
                # short timeouts keep Ctrl+C responsive on Windows
                finished, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                for f in finished:
                    exc = f.exception()
                    if exc and not isinstance(exc, StopRequested):
                        errors.append((futs[f], exc))
                        self.log(f"Group {futs[f]} stopped with an error: {exc}. Rerun to resume it.")
        except KeyboardInterrupt:
            self.progress.stop_event.set()
            self.log("Stopping: letting in-flight API calls finish (up to a few minutes). "
                     "Unfinished rounds are discarded; rerun the same command to resume.")
            ex.shutdown(wait=True, cancel_futures=True)
            self.progress.stop()
            raise
        ex.shutdown(wait=True)
        self.progress.stop()
        if errors:
            raise SystemExit(f"{len(errors)} group(s) stopped early. Finished rounds are saved; "
                             "rerun the same command to resume.")
