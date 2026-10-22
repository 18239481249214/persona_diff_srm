# persona differentiation srm (v0.4.0)

This runner implements the "persona differentiation srm" design spec. Blank LLM agents complete one or more self-report measures, then run a round robin of anonymous chats. After each chat they rate their partner on warmth and competence and rewrite their own capped memory. Every model-facing prompt is in `pdsrm/prompts.py`.

## Files

| Path | Purpose |
| --- | --- |
| `run.py` | Runs a collection |
| `audit.py` | Independent integrity check; imports nothing from `pdsrm/` |
| `config.json` | Design parameters: group_size, n_groups, k, n_loops, caps, model, measures |
| `measures/` | One JSON definition per questionnaire (`bfi2.json`, plus `TEMPLATE.json` to copy) |
| `data/` | Item files for the measures |
| `pdsrm/` | Runner code: schedule, phases, measures, runner, assemble |

## Measures are swappable

Each questionnaire is one definition file in `measures/` plus one items CSV (columns `item_id,text`). The definition holds the instructions, the optional item stem, the scale with its anchor labels, the item file path, the expected item count, and whether to shuffle item order. `config.json` lists which measures to give:

```
"measures": ["measures/bfi2.json"]
```

To use a different instrument, copy `measures/TEMPLATE.json`, fill it in, create its items CSV, and list it in `config.json`. Several measures can be listed; each is given as its own call at the start of every loop and once after the last loop (always). The measures and their item files are part of the design digest, so changing them requires a new `run_name`.

### BFI-2 items

The BFI-2 (Soto & John, 2017) is free for non-commercial research but is not bundled here. Fill `data/bfi2_items.csv` with the 60 phrases from the official form, in the official order (bfi01 = item 1), as the part that completes "I am someone who...". Use `data/bfi2_items_TEMPLATE.csv` as the starting file if needed. Quote any phrase containing a comma: `bfi01,"Is outgoing, sociable"`. A dry run works with placeholders; a live run will not start until all 60 rows are filled.

## Data integrity

- **Strict parsing.** A reply is rejected, never repaired, if it has a non-integer (4.5), an out-of-range value, a missing, extra or duplicate key, or non-text memory fields. Rejected replies are retried once. Every retry and failure is logged in `flags.csv`.
- **Sealed folders.** Every finished round and every measure administration is written to a temporary folder, hashed (SHA-256, stored in `sha256.json`), and only then renamed into place.
- **Completeness gates.** A group-loop's ratings enter `ratings.csv` only if all n(n−1) directed ratings are valid, and a measure administration enters `measures.csv` only if every item is present. Anything downstream of an incomplete loop is quarantined too, and every exclusion is listed in `quarantine.csv` with its reason.
- **Prompt text is part of the design.** The hash of `pdsrm/prompts.py` is in the design digest and a copy is saved as `prompts_snapshot.py` in each run folder, so a run can never mix prompt versions.
- **Output seal.** `outputs_sha256.json` records hashes of the assembled files.
- **Independent audit.** `audit.py` re-parses every raw reply with its own parser and recomputes completeness and quarantine. It verifies every seal and compares `ratings.csv` and `measures.csv` with the raw data cell by cell: score, snapshot, round, role, item position and provenance. It writes `audit_report.txt` and ends with PASS or FAIL.

## Setup (PowerShell, from this folder)

```
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:DEEPSEEK_API_KEY="your-key"
```

## Parallelism

Groups are independent, so `parallel_groups` of them (default 4) run at the same time. Inside a group, rounds stay in order because each agent's next chat must see its updated memory. Every dyad in a round runs simultaneously, and inside each dyad the two ratings, and then the two memory rewrites, run in parallel. Chat turns are sequential by nature. With an even group size (6), every agent is in a chat in every round. `parallel_groups` is a runtime setting and does not change the design digest.

While running, a live line shows each active dyad's step and how long its current API call has waited, or, with many groups, a summary with the slowest call. Finished rounds print with their flag types. Ctrl+C stops after in-flight calls finish; rerun to resume.

## Run, then audit

```
python run.py --config config.json --dry-run
python run.py --config config.json
python audit.py runs\pilot_n6_k10_l3_g2
```

If a run stops, run the same command again; it resumes from the last sealed round. To change the design, change `run_name`.

## Outputs (runs\<run_name>\)

| Path | Content |
| --- | --- |
| `loop_01\` ... `loop_NN\` | One folder per loop, each laid out exactly like a single-loop run: `ratings.csv`, `measures.csv`, `transcripts.jsonl`, `memory_snapshots.jsonl`, `schedule.csv`, `flags.csv`, `quarantine.csv`. `measures.csv` holds the questionnaire taken at the start of that loop; the last loop's also holds the final one (timepoint `final`). |
| `combined\` | The same seven files with every loop stacked, same columns |
| `<measure>_over_time.csv` | e.g. `bfi2_over_time.csv`: one row per agent per administration (1 = before any chats, 2 = after loop 1, ...), items as columns |
| `outputs_sha256.json` | Hashes of every output file |
| `audit_report.txt` | Written by `audit.py` |
| `prompts_snapshot.py` | The exact prompt text used |
| `groups\` | Sealed raw data, including every raw model reply and its reasoning |

The final questionnaire is always given after the last loop. Every row in every file carries `group` and `loop`, and agent IDs (A01, A02, ...) are stable across loops. The audit checks that each loop folder is exactly the combined data filtered to that loop and that the over-time file matches the questionnaire data cell by cell.
