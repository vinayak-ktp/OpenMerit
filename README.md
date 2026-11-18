# OpenMerit

**Generative AI Powered Resume Screening and Candidate Evaluation with Fairness
Assessment**

This project has two connected but separate workflows:

1. **Candidate evaluator** - upload one resume and compare it with one job
   description. A hosted LLM (Gemini by default, OpenAI also supported) provides an
   evidence-backed score when configured; the
   deterministic local scorer is used as a fallback or as a second opinion.
2. **Fairness research pipeline** - create controlled resume variants, measure
   score and decision effects, evaluate mitigation strategies, and produce
   reproducible reports for system-level research.

The candidate evaluator is decision support for human review. It does not
automatically hire, advance, reject, or contact a candidate. A single resume
cannot establish statistical fairness, so fairness auditing remains a
population-level benchmark workflow.

## Contents

- [Quick start](#quick-start-web-evaluator)
- [Choosing an LLM provider](#choosing-an-llm-provider-optional)
- [Using the evaluator](#using-the-evaluator)
- [Fairness audit in the web app](#fairness-audit-in-the-web-app)
- [Fairness research workflow](#fairness-research-workflow)
- [Common commands](#common-commands)
- [Troubleshooting](#troubleshooting)
- [Repository layout](#repository-layout)

## Requirements

- Python 3.9 or newer
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- An API key for a supported LLM provider (optional; only for LLM-backed scoring)

Works on macOS, Linux, and Windows. Commands below use a POSIX shell; on
Windows PowerShell use `Copy-Item` instead of `cp`.

## Quick start: web evaluator

```bash
uv sync --extra web --extra gemini   # web app + Gemini SDK (the default provider)
uv run openmerit doctor                    # optional: verify the environment
uv run openmerit web
```

Open <http://127.0.0.1:8000>. No API key is needed to try it: without one, the
app falls back to the deterministic offline scorer and labels results as such.

The page accepts TXT, Markdown, PDF, and DOCX resumes up to 5 MB. Files are
processed in memory and are not saved.

## Choosing an LLM provider (optional)

The evaluator scores with one hosted LLM and falls back to the offline scorer if
it fails. Pick the provider with `OPENMERIT_PROVIDER`; each needs its SDK extra and key.

| Provider | `OPENMERIT_PROVIDER` | Install | Key | Default model |
| --- | --- | --- | --- | --- |
| Google Gemini | `gemini` (default) | `uv sync --extra web --extra gemini` | `GEMINI_API_KEY` ([get one](https://aistudio.google.com/apikey)) | `gemini-3.5-flash-lite` |
| OpenAI | `openai` | `uv sync --extra web --extra openai` | `OPENAI_API_KEY` | `gpt-4o-mini` |
| OpenAI-compatible (Ollama, vLLM, OpenRouter, ...) | `openai` | same as OpenAI | `OPENAI_API_KEY` + `OPENAI_BASE_URL` | set `OPENMERIT_MODEL` |

1. Copy the example env file and fill in the key for your provider:

   ```bash
   cp .env.example .env
   ```

   ```text
   OPENMERIT_PROVIDER=openai
   OPENAI_API_KEY=your_key_here
   ```

2. Restart `uv run openmerit web`. The header badge shows the active provider.

If the provider is unavailable, rate-limited, or returns an unusable response,
the result is clearly labelled as an offline fallback with the reason. API keys
stay on the server and are never sent to the browser.

**Score stability.** Each evaluation calls the provider 3 times and reports the
median, because hosted models vary slightly between identical calls. Results are
also remembered in server memory (never on disk), so submitting the same resume
and job description again returns the same score until the server restarts. One
evaluation therefore uses 3 API requests; on a free tier of about 12 requests per
minute that is roughly 4 evaluations per minute.

Optional settings (environment variables or `.env`):

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENMERIT_PROVIDER` | `gemini` | LLM provider: `gemini` or `openai` |
| `OPENMERIT_MODEL` | per provider (see above) | Model name for that provider |
| `GEMINI_API_KEY` | none | Key for `gemini` |
| `OPENAI_API_KEY` | none | Key for `openai` |
| `OPENAI_BASE_URL` | OpenAI | Point `openai` at a compatible endpoint |
| `OPENMERIT_AUDIT_MAX_CALLS` | `1000` | Most API calls one audit started from the browser may make |
| `OPENMERIT_RPM` | `12` | Requests per minute for browser-started LLM audits; sets pacing and time estimates |
| `OPENMERIT_WEB_HOST` | `127.0.0.1` | Bind address |
| `OPENMERIT_WEB_PORT` | `8000` | Port |

Adding another provider is one file plus one line; see
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#adding-an-llm-provider).

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `google-genai is not installed` (or `openai`) | Install the provider's extra: `uv sync --extra web --extra gemini` (or `--extra openai`, or `--all-extras`). Plain `uv sync` skips optional extras. |
| `Web support is not installed` | Run `uv sync --extra web`. |
| Results say "Offline fallback used" | The provider's API key is missing from `.env`, or it is rate-limited. The reason is shown in the result. |
| Offline score is far from the LLM score | Expected to differ: the offline scorer is a keyword-overlap baseline. List requirements as `Requirements: Python, AWS, ...` or as bullets to help it. |
| Same inputs, slightly different score after editing the job text | Any edit triggers fresh LLM calls; only identical inputs are remembered. |
| Anything else | Run `uv run openmerit doctor` for a provider and environment check. |

## Using the evaluator

1. Choose a resume file.
2. Paste a job description with its responsibilities and requirements.
3. Select **Evaluate resume**.
4. Review the overall score, confidence, summary, assessment dimensions,
   supporting resume evidence, and missing requirements.
5. Optionally select **Get offline second opinion**. This makes no additional
   LLM request and compares the deterministic local score with the primary
   result.

The offline comparison is a scoring comparison, not a fairness certification.
Differences should be reviewed by a person and are not proof that either score
is correct.

## Fairness audit in the web app

`uv run openmerit web` serves both workflows. The **Fairness audit** tab has two modes:

- **LLM audit** runs the audit with the same provider and model as the
  evaluator. This is the audit that matters for a deployment. Every design shows
  its exact number of API calls and its expected time before anything starts,
  and a run begins only after you confirm that number. Designs above
  `OPENMERIT_AUDIT_MAX_CALLS` are refused in the browser.
- **Tool check** runs the same audit on the offline scorer. It is free and says
  nothing about a real model: it checks that the audit stays quiet when there is
  no bias. **Validate the auditor** also injects a known bias and checks that the
  audit recovers it.

| Design | Calls on a real LLM | Use it for |
| --- | --- | --- |
| Quick audit (`configs/quick_audit.yaml`) | 186 | Names across gender and race. Start here. |
| All cues (`configs/all_cues_audit.yaml`) | 244 | All nine cue families |
| Names-only replication (`configs/names_only.yaml`) | about 1,440 | Every name in the list |
| Full cue sweep (`configs/offline_demo.yaml`) | about 10,400 | Research precision; tool check only in the browser |

The two lean designs audit only the candidates nearest a decision cut-off,
repeat only each original resume to measure noise, keep two variants per cue
(eight names, two per gender and race group) rotated evenly across candidates,
and leave prompt-level mitigations for a follow-up once a bias is found.
[`docs/METHODOLOGY.md`](docs/METHODOLOGY.md) §10 explains why each saving keeps
the evidence. Re-running an unchanged design is served from the response cache
and costs nothing.

Read a result from **Significant and above threshold**: effects that survive the
multiple-testing correction *and* are large enough to matter. A run can be
cancelled at any point, and runs started from the CLI appear in the same results
view.

## Fairness research workflow

The research pipeline uses the controlled benchmark in
[`data/benchmark/`](data/benchmark/). It tests whether changing irrelevant
identity or proxy cues changes outcomes while preserving job-related content.
The audit includes counterfactual, group, allocational, ranking, rationale,
calibration, intersectional, validity, and reliability analyses. Mitigation
strategies can be compared in the same reproducible run.

Run the complete offline demo:

```bash
uv sync
uv run openmerit pipeline --config configs/offline_demo.yaml
```

Run the controlled benchmark:

```bash
uv run openmerit pipeline --config configs/synthetic.yaml --provider mock
```

Validate the audit instrument itself:

```bash
uv run openmerit validate-auditor --provider mock
```

The validation should report **5/5 checks passed**. It verifies that the
measurement system stays quiet under a no-bias null and recovers a known
injected effect with the expected direction and magnitude.

Explore completed research runs:

```bash
uv run openmerit dashboard
```

The Streamlit dashboard is for research artifacts and is intentionally separate
from the one-resume candidate evaluator.

Before spending API quota, inspect a planned run. The dry run prints the total
number of model calls, including mitigation arms that re-score variants:

```bash
uv run openmerit pipeline --config configs/quick_audit.yaml --provider gemini --dry-run
```

## Common commands

```bash
uv sync --all-extras                 # install every optional capability
uv run openmerit doctor                    # check environment and provider readiness
uv run openmerit web                       # start the candidate evaluator
uv run openmerit dashboard                 # start the research report explorer
uv run openmerit validate-auditor --provider mock
uv run pytest                        # run the full test suite
uv run ruff check src tests scripts  # lint Python
node --check web/assets/app.js       # check frontend JavaScript syntax
```

Equivalent Make targets are available:

```bash
make setup
make web
make demo
make validate
make test
make check
```

## Repository layout

The source files live directly under `src/` but are installed as the `openmerit`
package through this intentional setuptools mapping:

```toml
package-dir = { "openmerit" = "src" }
```

This keeps the requested repository layout while avoiding ambiguous top-level
imports such as `config`, `data`, and `pipeline`.

```text
configs/                    Reproducible evaluator and research configurations
data/benchmark/             Controlled synthetic fairness benchmark
docs/                       Architecture, data, and methodology documentation
scripts/                    Corpus and benchmark generation utilities
src/web_api.py              FastAPI backend for the candidate evaluator (median-of-3 LLM scoring)
src/web_audit.py            Plans, launches and reads fairness audits for the web app
src/screening/              Resume scoring, prompts, evidence, and rubric logic
src/perturb/                Controlled cue and counterfactual generation
src/fairness/               System-level fairness metrics and audit suite
src/mitigate/               Bias mitigation strategies and comparisons
src/pipeline/               Reproducible research pipeline orchestration
web/                        Static candidate-facing frontend
tests/                      Automated tests
runs/                       Generated research artifacts (ignored by Git)
```

## Design boundaries and limitations

- Scores are job-related decision support, not employment decisions.
- LLM output can be wrong, incomplete, or inconsistent.
- The local scorer is a deterministic baseline, not an independent truth
  source and not the fairness auditor.
- Fairness findings depend on the benchmark, cue design, sample size, model,
  prompt, decoding settings, and statistical thresholds.
- A population-level audit cannot certify an individual candidate result.
- Real-model research runs can consume API quota. Use `--dry-run`, a small
  corpus, and the response cache before larger experiments.
- Uploaded documents are processed in memory by the web endpoint; deployment
  operators should still apply normal server, network, and credential controls.

For the research rationale, data decisions, and metric definitions, see
[`docs/METHODOLOGY.md`](docs/METHODOLOGY.md),
[`docs/DATA.md`](docs/DATA.md), and
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Development

Run the checks used before a release:

```bash
uv run ruff check src tests scripts
node --check web/assets/app.js
uv run pytest
uv run openmerit validate-auditor --provider mock
```

Do not commit `.env`, API keys, `runs/`, caches, virtual environments, or
generated packaging artifacts. The repository `.gitignore` excludes them.

## License

MIT. See the project metadata in [`pyproject.toml`](pyproject.toml).
