# Evaluation harness

`python -m wactorz.evalharness` compares models **per LLM call site** — the framework does not make
one kind of LLM call, it makes several with very different difficulty profiles, and a model that is
fine at intent routing may be unusable for code generation. The harness runs a benchmark against
one or more models, scores every answer automatically, and reports accuracy, latency and cost per
call site, so `LLM_OVERRIDES` can be chosen from measurements instead of guesswork.

## Usage

```bash
# built-in seed benchmark, one model
python -m wactorz.evalharness --models ollama:llama3

# several models, pinned temperature, 3 repetitions
python -m wactorz.evalharness \
  --models "ollama:qwen3:4b,ollama:gemma3:12b,anthropic:claude-sonnet-4-6" \
  --temperature 0 --repeat 3 --out results/

# a subset of call sites, from your own benchmark file
python -m wactorz.evalharness --models ollama:llama3 \
  --prompts bench.jsonl --categories intent,ha
```

Model specs use the same `provider[:model]` format as `LLM_OVERRIDES`, so anything reachable through
a configured provider works — including an OpenAI-compatible gateway (set `OPENAI_URL`), which is how
several local models can be served from one endpoint.

| Flag | Meaning |
|------|---------|
| `--models` | Comma-separated `provider[:model]` specs (required) |
| `--prompts` | Benchmark JSONL file (default: built-in seed set) |
| `--categories` | Subset of `intent,ha,actuator,planner,dynamic` |
| `--repeat` | Repetitions per case — use >1 for generation tasks, which vary |
| `--temperature` | Temperature for every model; omit to use `LLM_TEMPERATURE` |
| `--out` | Output directory (default `./eval_results`) |

## Call sites and scoring

The `intent`, `ha` and `actuator` categories run against the framework's **unmodified production
system prompts**, imported directly from the agent code, so results describe the real system rather
than a simplified benchmark. `planner` and `dynamic` use condensed variants of the production
prompts, which are assembled dynamically inside the agents and run to hundreds of lines.

| Category | Task | Scored by |
|----------|------|-----------|
| `intent` | Routing (ACTUATE / HA / PIPELINE / OTHER) | Exact label match |
| `ha` | Home Assistant action classification | Exact label match |
| `actuator` | Natural language → HA service calls | Exact action-set match — a correct action plus an unrequested one **fails** |
| `planner` | Pipeline planning | Valid JSON plan, allowed agent types, name + description present |
| `dynamic` | Agent code generation | Code compiles **and** defines the required `async` functions |

Two scoring choices are deliberate. An `expected` of `[]` for `actuator` means the model must
**refuse** — used for cases where the requested device does not exist, so over-eager substitution is
measured rather than rewarded. And extra actions count as failures, because in a smart home an
unrequested actuation is worse than none.

## Benchmark file format

One JSON object per line:

```jsonl
{"id": "intent-001", "category": "intent", "prompt": "turn on the office light", "expected": "ACTUATE"}
{"id": "ha-001", "category": "ha", "prompt": "show me all my devices", "expected": "list_devices"}
{"id": "actuator-004", "category": "actuator", "prompt": "turn off the TV\n\n[AVAILABLE HA ENTITIES:\n  light.lamp (Lamp)\n]", "expected": []}
{"id": "planner-002", "category": "planner", "prompt": "every weekday at 7:30 turn on the coffee machine (switch.coffee).", "expected": ["scheduled", "ha_actuator"]}
{"id": "dynamic-001", "category": "dynamic", "prompt": "Write an agent that subscribes to sensors/temperature ...", "expected": ["setup", "process"]}
```

`expected` is a label for `intent`/`ha`, a list of `{domain, service, entity_id}` actions for
`actuator`, a list of allowed agent types for `planner`, and a list of required function names for
`dynamic`. Malformed lines are skipped with a warning rather than aborting the run.

## Output

- `results.jsonl` — one record per (model, case, repetition): pass/fail, latency, tokens, cost,
  temperature and the raw output. Appended and flushed as the run proceeds, so an interrupted run
  keeps everything completed so far.
- `summary.csv` + a console table — accuracy, error count, mean latency and total cost per
  model × category.

Local models report `cost_usd: 0`, so the cost column isolates hosted-API spend — which is what makes
the hybrid trade-off (cheap high-frequency calls local, hard calls hosted) directly measurable.
