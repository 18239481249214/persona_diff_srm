"""Persona differentiation SRM runner.

  python run.py --config config.json --dry-run     free test with fake replies
  python run.py --config config.json               live run
  python run.py --assemble runs\\<run_name>         rebuild the CSVs from a run folder
  python audit.py runs\\<run_name>                  independent integrity check (run after every collection)
"""
import argparse
import json
import sys
from pathlib import Path

from pdsrm.assemble import assemble
from pdsrm.client import MockClient, OpenAICompatClient
from pdsrm.runner import Runner


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="path to config JSON")
    ap.add_argument("--dry-run", action="store_true", help="use fake replies; no API calls, no cost")
    ap.add_argument("--workers", type=int, help="override parallel workers")
    ap.add_argument("--assemble", metavar="RUN_DIR", help="only rebuild output files for an existing run")
    args = ap.parse_args()

    if args.assemble:
        assemble(args.assemble)
        return
    if not args.config:
        ap.error("--config is required")

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if args.workers:
        cfg["workers"] = args.workers
    if args.dry_run:
        cfg["run_name"] = cfg["run_name"] + "_dryrun"
        client = MockClient(cfg["seed"])
    else:
        client = OpenAICompatClient(cfg["model"])

    run_dir = Path(cfg.get("runs_dir", "runs")) / cfg["run_name"]
    try:
        Runner(cfg, client, run_dir, args.dry_run).run()
    except KeyboardInterrupt:
        print("\nStopped. Finished rounds are saved; run the same command again to resume.")
        sys.exit(1)
    assemble(run_dir)
    print(f"Done. Outputs are in {run_dir}")
    print(f"Next, audit it:  python audit.py {run_dir}")


if __name__ == "__main__":
    main()
