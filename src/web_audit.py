"""Fairness-audit workflow for the web app.

The candidate evaluator in :mod:`openmerit.web_api` scores one resume. This module
serves the second workflow: the population-level fairness audit. It does three
things:

* **plan** an audit before it runs: a dry run counts the exact model calls a
  design would make, so a real-LLM audit never starts without a known cost;
* **launch** audits as child processes, one at a time, in one of two modes:

  - ``llm``: score with the provider the evaluator uses. This is the audit
    that matters in production. It costs API calls, so it is capped
    (``OPENMERIT_AUDIT_MAX_CALLS``) and must be confirmed against its plan;
  - ``offline``: score with the deterministic offline scorer. Free, and used
    to check the audit tool itself, never as a verdict on a model;

* **read** finished run artifacts and reshape them into JSON for the browser.

Audits run in a separate process rather than a thread so a CPU-heavy run does
not compete with the evaluator for the server's interpreter, and so a run can
be cancelled cleanly.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

PIPELINE_STAGES = (
    ("prepare_data", "Load corpus and pair resumes with jobs"),
    ("generate_variants", "Generate controlled resume variants"),
    ("score_baseline", "Score the original resumes"),
    ("score_variants", "Score every variant"),
    ("audit", "Run the fairness metric suite"),
    ("mitigate", "Evaluate mitigation strategies"),
    ("report", "Write report, plots and model card"),
)

PRESETS: Dict[str, Dict[str, Any]] = {
    "quick": {
        "label": "Quick audit",
        "config": "configs/quick_audit.yaml",
        "corpus": "12 borderline candidates from the sample corpus",
        "description": (
            "Name swaps across gender and race, two names per group, on the candidates "
            "nearest a decision cut-off. Start here."
        ),
        "estimate_minutes": 1,
        "llm": True,
    },
    "all_cues": {
        "label": "All cues",
        "config": "configs/all_cues_audit.yaml",
        "corpus": "12 borderline candidates from the sample corpus",
        "description": (
            "All nine cue families (names, pronouns, culture, location, age, affiliations, "
            "class, education, writing style), two variants each, rotated evenly across "
            "candidates."
        ),
        "estimate_minutes": 1,
        "llm": True,
    },
    "names_only": {
        "label": "Names-only replication",
        "config": "configs/names_only.yaml",
        "corpus": "Sample corpus: 12 resumes, 6 jobs",
        "description": (
            "The Bertrand & Mullainathan (2004) name-swap design with every name in the "
            "list, on every candidate, plus the education validity probe."
        ),
        "estimate_minutes": 1,
        "llm": True,
    },
    "offline_demo": {
        "label": "Full cue sweep",
        "config": "configs/offline_demo.yaml",
        "corpus": "Sample corpus: 12 resumes, 6 jobs",
        "description": (
            "Every level of every cue, three repeats of everything and all five mitigations: "
            "the research design. About 10,400 calls on a real model, so from the browser it "
            "runs as a tool check only; use the CLI for a research-grade model audit."
        ),
        "estimate_minutes": 2,
        "llm": False,
    },
    "synthetic": {
        "label": "Controlled benchmark",
        "config": "configs/synthetic.yaml",
        "corpus": "Benchmark: 150 resumes, 24 jobs, 355 pairs",
        "description": (
            "The controlled synthetic benchmark across eight cue families. Validates the "
            "audit at scale, offline only."
        ),
        "estimate_minutes": 35,
        "llm": False,
    },
}

MODES = ("llm", "offline")
VALIDATION_ESTIMATE_MINUTES = 1
DEFAULT_MAX_LLM_CALLS = 1000
DEFAULT_LLM_RPM = 12
DEFAULT_LLM_CONCURRENCY = 4

OFFLINE_PROVIDER = "mock"
OFFLINE_MODEL = "mock-rubric-v1"

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
REPORT_MEDIA_TYPES = {
    ".png": "image/png",
    ".html": "text/html",
    ".md": "text/markdown",
    ".json": "application/json",
    ".csv": "text/csv",
}
_WEB_STATE_DIR = ".web"
_LOG_TAIL_CHARS = 1500
_PLAN_TIMEOUT_S = 240


class AuditBusyError(RuntimeError):
    """Raised when a job is requested while another one is still running."""


# --------------------------------------------------------------------------- #
# paths, settings and small readers
# --------------------------------------------------------------------------- #
def runs_dir(root: Path) -> Path:
    """Where run artifacts live; honours ``OPENMERIT_OUTPUT_DIR`` like the CLI does."""
    configured = Path(os.environ.get("OPENMERIT_OUTPUT_DIR") or "runs").expanduser()
    return configured if configured.is_absolute() else root / configured


def max_llm_calls() -> int:
    """The most API calls one browser-launched audit may make."""
    try:
        return max(1, int(os.environ.get("OPENMERIT_AUDIT_MAX_CALLS", DEFAULT_MAX_LLM_CALLS)))
    except ValueError:
        return DEFAULT_MAX_LLM_CALLS


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


def valid_run_id(run_id: str) -> bool:
    return bool(_RUN_ID.match(run_id or "")) and ".." not in run_id


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _clean(value: Any) -> Any:
    """Make artifact data JSON-safe: NaN and infinity become ``None``."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _number(text: Optional[str]) -> Optional[float]:
    if text in (None, ""):
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _flag(text: Optional[str]) -> Optional[bool]:
    if text in (None, ""):
        return None
    return str(text).strip().lower() == "true"


def _cue_of(metric: str) -> str:
    match = re.search(r"\[(.*?)\]", metric)
    return match.group(1) if match else ""


def _read_metrics(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return [
        {
            "family": row.get("family", ""),
            "metric": row.get("metric", ""),
            "attribute": row.get("attribute", ""),
            "value": _number(row.get("value")),
            "unit": row.get("unit", ""),
            "ci_low": _number(row.get("ci_low")),
            "ci_high": _number(row.get("ci_high")),
            "q_value": _number(row.get("q_value_bh")),
            "threshold": _number(row.get("threshold")),
            "n": _number(row.get("n")),
            "passed": _flag(row.get("passed")),
            "significant": _flag(row.get("significant_after_fdr")),
            "interpretation": row.get("interpretation", ""),
        }
        for row in rows
    ]


def _last_progress(run_dir: Path) -> Optional[Dict[str, Any]]:
    """The latest "scoring progress" event from a run's structured log."""
    path = run_dir / "logs" / "run.jsonl"
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 65536))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if '"scoring progress"' not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        return {"arm": event.get("arm"), "done": event.get("done"), "total": event.get("total")}
    return None


# --------------------------------------------------------------------------- #
# reading finished runs
# --------------------------------------------------------------------------- #
def _run_overview(run_dir: Path) -> Optional[Dict[str, Any]]:
    manifest = _read_json(run_dir / "manifest.json")
    summary = _read_json(run_dir / "audit" / "summary.json")
    if not isinstance(manifest, dict) or not isinstance(summary, dict):
        return None
    config = manifest.get("config") or {}
    llm = config.get("llm") or {}
    data = config.get("data") or {}
    conflicts = summary.get("conflicts") or {}
    run_name = config.get("run_name") or run_dir.name
    return {
        "run_id": run_dir.name,
        "run_name": run_name,
        "preset_label": PRESETS.get(run_name, {}).get("label"),
        "mode": "offline" if llm.get("provider") == OFFLINE_PROVIDER else "llm",
        "created_at": manifest.get("created_at"),
        "status": manifest.get("status", "incomplete"),
        "provider": llm.get("provider"),
        "model": llm.get("model"),
        "corpus": data.get("resumes_path") or data.get("corpus"),
        "n_records": summary.get("n_records"),
        "n_metrics": summary.get("n_metrics"),
        "n_failed": summary.get("n_failed"),
        "n_significant_after_fdr": summary.get("n_significant_after_fdr"),
        "n_conflicts": conflicts.get("n_conflicts"),
    }


def list_runs(directory: Path) -> List[Dict[str, Any]]:
    """Every run with a finished audit, newest first."""
    if not directory.is_dir():
        return []
    runs = []
    for child in directory.iterdir():
        if child.is_dir() and valid_run_id(child.name):
            overview = _run_overview(child)
            if overview is not None:
                runs.append(overview)
    runs.sort(key=lambda run: (run.get("created_at") or "", run["run_id"]), reverse=True)
    return _clean(runs)


def _mitigation_arms(mitigation: Dict[str, Any]) -> List[Dict[str, Any]]:
    arms = []
    ranking = list(mitigation.get("ranking") or [])
    for name, comparison in (mitigation.get("comparisons") or {}).items():
        utility = comparison.get("utility") or {}
        discrimination = utility.get("discrimination_retained") or {}
        summary = comparison.get("audit_summary") or {}
        arms.append(
            {
                "arm": name,
                "rank": ranking.index(name) + 1 if name in ranking else None,
                "verdict": comparison.get("verdict"),
                "verdict_reasons": comparison.get("verdict_reasons") or [],
                "utility_acceptable": comparison.get("utility_acceptable"),
                "n_metrics_improved": comparison.get("n_metrics_improved"),
                "n_metrics_worsened": comparison.get("n_metrics_worsened"),
                "n_failed": summary.get("n_failed"),
                "rank_agreement": utility.get("rank_agreement_kendall_tau"),
                "top_k_overlap": utility.get("top_k_overlap"),
                "score_dispersion_ratio": utility.get("score_dispersion_ratio"),
                "mean_score_shift": utility.get("mean_score_shift"),
                "coverage": utility.get("coverage"),
                "n_routed_to_human": utility.get("n_routed_to_human"),
                "discrimination_tau": discrimination.get("kendall_tau_vs_expected_fit"),
            }
        )
    arms.sort(key=lambda arm: (arm["rank"] is None, arm["rank"] or 0, arm["arm"]))
    return arms


def _cue_effects(metrics: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One row per cue family: mean score swing and decision flip rate."""
    by_cue: Dict[str, Dict[str, Any]] = {}
    for metric in metrics:
        if metric["family"] != "counterfactual":
            continue
        cue = _cue_of(metric["metric"])
        if not cue or cue == "all":
            continue
        row = by_cue.setdefault(cue, {"cue": cue})
        if metric["metric"].startswith("counterfactual_max_swing"):
            row.update(
                swing=metric["value"],
                swing_ci_low=metric["ci_low"],
                swing_ci_high=metric["ci_high"],
                swing_threshold=metric["threshold"],
                swing_passed=metric["passed"],
                interpretation=metric["interpretation"],
            )
        elif metric["metric"].startswith("decision_flip_rate"):
            row.update(flip_rate=metric["value"], flip_passed=metric["passed"])
    return sorted(by_cue.values(), key=lambda row: -(row.get("swing") or 0.0))


def _group_gaps(metrics: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for metric in metrics:
        if metric["family"] == "group_parity" and metric["metric"].startswith(
            "max_pairwise_score_gap"
        ):
            rows.append(
                {
                    "attribute": metric["attribute"] or _cue_of(metric["metric"]),
                    "gap": metric["value"],
                    "ci_low": metric["ci_low"],
                    "ci_high": metric["ci_high"],
                    "q_value": metric["q_value"],
                    "passed": metric["passed"],
                    "significant": metric["significant"],
                    "interpretation": metric["interpretation"],
                }
            )
    return sorted(rows, key=lambda row: -(row.get("gap") or 0.0))


def run_details(directory: Path, run_id: str) -> Dict[str, Any]:
    """Everything the audit view shows for one finished run."""
    if not valid_run_id(run_id):
        raise ValueError("Unknown run id.")
    run_dir = directory / run_id
    overview = _run_overview(run_dir) if run_dir.is_dir() else None
    if overview is None:
        raise FileNotFoundError(f"No finished audit found for run {run_id!r}.")

    manifest = _read_json(run_dir / "manifest.json") or {}
    summary = _read_json(run_dir / "audit" / "summary.json") or {}
    report = _read_json(run_dir / "report" / "report.json") or {}
    conflicts = _read_json(run_dir / "audit" / "conflicts.json") or []
    metrics = _read_metrics(run_dir / "audit" / "metrics.csv")
    stages = manifest.get("stages") or {}
    config = manifest.get("config") or {}
    variants_stage = stages.get("generate_variants") or {}
    focus = stages.get("focus_pairs") or {}
    mitigation = report.get("mitigation") or {}
    report_dir = run_dir / "report"
    warnings = manifest.get("warnings") or summary.get("warnings") or []
    usage = (
        ((stages.get("score_variants") or {}).get("telemetry") or {}).get("client") or {}
    )

    return _clean(
        {
            **overview,
            "elapsed_s": manifest.get("total_elapsed_s"),
            # The focused-design note is a description of the design, not a
            # problem with the run, so it is reported separately.
            "warnings": [w for w in warnings if not str(w).startswith("Focused design:")],
            "design_notes": [w for w in warnings if str(w).startswith("Focused design:")],
            "cue_families": (config.get("perturbation") or {}).get("cue_families") or [],
            "attributes": (config.get("fairness") or {}).get("attributes") or [],
            "n_base_resumes": summary.get("n_base_resumes"),
            "n_jobs": summary.get("n_jobs"),
            "n_variants": summary.get("n_variants") or variants_stage.get("n_variants"),
            "n_focus_pairs": focus.get("n_pairs_kept"),
            # Statistically significant AND past the metric's practical threshold:
            # with low-noise scoring a 0.01-point gap can be significant, and
            # significance alone says nothing about whether it matters.
            "n_material": sum(1 for m in metrics if m["significant"] and m["passed"] is False),
            "api_calls": usage.get("api_calls"),
            "by_family": summary.get("by_family") or {},
            "skipped_families": summary.get("skipped_families") or [],
            "reliability": summary.get("reliability") or {},
            "sanity_check": (stages.get("score_baseline") or {}).get("sanity_check"),
            "headline_findings": report.get("headline_findings") or [],
            "cue_effects": _cue_effects(metrics),
            "group_gaps": _group_gaps(metrics),
            "metrics": metrics,
            "conflicts": conflicts if isinstance(conflicts, list) else [],
            "conflict_guidance": (summary.get("conflicts") or {}).get("guidance"),
            "cards_summary": report.get("cards_summary"),
            "mitigation": {
                "arms": _mitigation_arms(mitigation),
                "guidance": mitigation.get("guidance"),
            },
            "plots": sorted(p.name for p in report_dir.glob("*.png")) if report_dir.is_dir() else [],
            "has_html_report": (report_dir / "report.html").exists(),
        }
    )


def report_file(directory: Path, run_id: str, filename: str) -> Path:
    """Resolve a file inside a run's ``report/`` folder, refusing anything else."""
    if not valid_run_id(run_id):
        raise ValueError("Unknown run id.")
    if not filename or "/" in filename or "\\" in filename or filename.startswith("."):
        raise ValueError("Unknown report file.")
    if Path(filename).suffix.lower() not in REPORT_MEDIA_TYPES:
        raise ValueError("Unknown report file.")
    report_dir = (directory / run_id / "report").resolve()
    path = (report_dir / filename).resolve()
    if path.parent != report_dir or not path.is_file():
        raise FileNotFoundError(filename)
    return path


def latest_validation(directory: Path) -> Optional[Dict[str, Any]]:
    data = _read_json(directory / _WEB_STATE_DIR / "latest_validation.json")
    return _clean(data) if isinstance(data, dict) else None


# --------------------------------------------------------------------------- #
# launching jobs
# --------------------------------------------------------------------------- #
#: Returns ``(provider, model, api_key_configured)`` for the evaluator's LLM.
LLMTarget = Callable[[], Tuple[str, str, bool]]


@dataclass
class AuditJob:
    job_id: str
    kind: str  # "pipeline" | "validation"
    process: Any
    started_at: float
    log_path: Path
    mode: str = "offline"
    preset: Optional[str] = None
    run_id: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    planned_calls: Optional[int] = None
    result_path: Optional[Path] = None
    finished_at: Optional[float] = None
    cancelled: bool = False


class AuditJobs:
    """Plans audits and runs at most one audit job at a time."""

    def __init__(
        self,
        root: Path,
        directory: Optional[Path] = None,
        launcher: Any = None,
        llm_target: Optional[LLMTarget] = None,
        planner: Any = None,
    ) -> None:
        self.root = root
        self.directory = directory or runs_dir(root)
        self._launch = launcher or self._spawn
        self._plan = planner or self._dry_run
        self._llm_target = llm_target or (lambda: ("gemini", "gemini-3.5-flash-lite", False))
        self._lock = threading.Lock()
        self._job: Optional[AuditJob] = None
        self._plans: Dict[Tuple[str, ...], Dict[str, Any]] = {}

    # -- settings shown to the browser ---------------------------------- #
    def llm_settings(self) -> Dict[str, Any]:
        provider, model, configured = self._llm_target()
        return {
            "provider": provider,
            "model": model,
            "configured": configured,
            "requests_per_minute": _int_env("OPENMERIT_RPM", DEFAULT_LLM_RPM),
            "max_calls": max_llm_calls(),
        }

    # -- planning --------------------------------------------------------- #
    def _dry_run(self, args: List[str]) -> Dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="openmerit-plan-") as scratch:
            out = Path(scratch) / "plan.json"
            completed = subprocess.run(
                [sys.executable, "-m", "openmerit.web_audit", "plan", *args,
                 "--output-dir", scratch, "--out", str(out)],
                cwd=str(self.root), capture_output=True, text=True, timeout=_PLAN_TIMEOUT_S,
            )
            plan = _read_json(out)
            if completed.returncode != 0 or not isinstance(plan, dict):
                tail = (completed.stderr or completed.stdout or "")[-_LOG_TAIL_CHARS:].strip()
                raise RuntimeError(f"Could not plan this audit. {tail}")
            return plan

    def plan(self, preset: str, mode: str) -> Dict[str, Any]:
        """Exact call count and time for a design, from a dry run (no model calls)."""
        if preset not in PRESETS:
            raise ValueError(f"Unknown audit preset {preset!r}.")
        if mode not in MODES:
            raise ValueError(f"Unknown audit mode {mode!r}.")
        if mode == "llm" and not PRESETS[preset]["llm"]:
            raise ValueError(f"{PRESETS[preset]['label']} runs offline only.")
        provider, model, _ = self._llm_target() if mode == "llm" else (OFFLINE_PROVIDER, OFFLINE_MODEL, True)
        rpm = _int_env("OPENMERIT_RPM", DEFAULT_LLM_RPM)
        key = (preset, mode, provider, model, str(rpm))
        if key not in self._plans:
            args = ["--preset", preset, "--mode", mode, "--provider", provider, "--model", model]
            self._plans[key] = self._plan(args)
        plan = dict(self._plans[key])
        plan["max_calls"] = max_llm_calls()
        plan["within_limit"] = mode == "offline" or plan.get("api_calls", 0) <= plan["max_calls"]
        return _clean(plan)

    # -- launching -------------------------------------------------------- #
    def _spawn(self, args: List[str], log_path: Path) -> Any:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as log:
            return subprocess.Popen(
                [sys.executable, "-m", "openmerit.web_audit", *args],
                cwd=str(self.root),
                stdout=log,
                stderr=subprocess.STDOUT,
            )

    def _state_dir(self) -> Path:
        return self.directory / _WEB_STATE_DIR

    def _start(self, kind: str, args: List[str], **fields: Any) -> Dict[str, Any]:
        with self._lock:
            if self._job is not None and self._running(self._job):
                raise AuditBusyError("An audit job is already running. Wait for it or cancel it.")
            job_id = uuid.uuid4().hex[:12]
            log_path = self._state_dir() / f"job-{job_id}.log"
            if kind == "validation":
                fields["result_path"] = self._state_dir() / f"job-{job_id}.json"
                args = [*args, "--out", str(fields["result_path"])]
            process = self._launch([*args, "--output-dir", str(self.directory)], log_path)
            self._job = AuditJob(
                job_id=job_id, kind=kind, process=process, started_at=time.time(),
                log_path=log_path, **fields,
            )
        return self.status()

    def start_pipeline(
        self, preset: str, mode: str = "offline", confirmed_calls: Optional[int] = None
    ) -> Dict[str, Any]:
        if preset not in PRESETS:
            raise ValueError(f"Unknown audit preset {preset!r}.")
        if mode not in MODES:
            raise ValueError(f"Unknown audit mode {mode!r}.")
        provider, model = OFFLINE_PROVIDER, OFFLINE_MODEL
        planned: Optional[int] = None
        if mode == "llm":
            provider, model, configured = self._llm_target()
            if not configured:
                raise ValueError(
                    f"No API key is configured for {provider}. Add it to .env and restart the server."
                )
            plan = self.plan(preset, mode)
            planned = int(plan.get("api_calls") or 0)
            if not plan["within_limit"]:
                raise ValueError(
                    f"This design needs about {planned:,} calls, above the {plan['max_calls']:,}-call "
                    "limit for audits started from the browser. Choose a smaller design, raise "
                    "OPENMERIT_AUDIT_MAX_CALLS, or run it from the CLI."
                )
            if confirmed_calls is None or int(confirmed_calls) < planned:
                raise ValueError("Confirm the planned number of calls before starting a real-LLM audit.")
        run_id = new_web_run_id(self.root, preset, mode, provider, model)
        return self._start(
            "pipeline",
            ["pipeline", "--preset", preset, "--mode", mode, "--provider", provider,
             "--model", model, "--run-id", run_id],
            mode=mode, preset=preset, run_id=run_id, provider=provider, model=model,
            planned_calls=planned,
        )

    def start_validation(self) -> Dict[str, Any]:
        return self._start("validation", ["validate"], provider=OFFLINE_PROVIDER, model=OFFLINE_MODEL)

    def cancel(self) -> Dict[str, Any]:
        with self._lock:
            job = self._job
            if job is None or not self._running(job):
                raise ValueError("No audit job is running.")
            job.cancelled = True
            job.process.terminate()
        try:
            job.process.wait(timeout=10)
        except Exception:  # pragma: no cover - a stuck child is killed outright
            job.process.kill()
        return self.status()

    # -- reporting -------------------------------------------------------- #
    @staticmethod
    def _running(job: AuditJob) -> bool:
        return job.process.poll() is None

    def _log_tail(self, job: AuditJob) -> str:
        try:
            text = job.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return text[-_LOG_TAIL_CHARS:].strip()

    def _stages(self, job: AuditJob, job_state: str) -> List[Dict[str, Any]]:
        """Per-stage state. Only a running job has a running stage: once a job is
        cancelled or fails, the stage it was on is marked where it stopped."""
        manifest = _read_json(self.directory / str(job.run_id) / "manifest.json") or {}
        done = set((manifest.get("stages") or {}).keys())
        current = {"running": "running", "cancelled": "stopped", "failed": "stopped"}.get(
            job_state, "pending"
        )
        stages, current_found = [], False
        for key, label in PIPELINE_STAGES:
            if key in done:
                state = "done"
            elif not current_found:
                state, current_found = current, True
            else:
                state = "pending"
            stages.append({"key": key, "label": label, "state": state})
        return stages

    def status(self) -> Dict[str, Any]:
        with self._lock:
            job = self._job
            if job is None:
                return {"job": None}
            code = job.process.poll()
            if code is not None and job.finished_at is None:
                job.finished_at = time.time()
                if job.kind == "validation" and code in (0, 1) and not job.cancelled:
                    self._store_validation(job)
            if code is None:
                state = "running"
            elif job.cancelled:
                state = "cancelled"
            elif code == 0 or (job.kind == "validation" and code == 1 and self._has_result(job)):
                # The validation worker exits 1 when the auditor fails a check; that
                # is a finished job with a failing verdict, not a crashed one.
                state = "complete"
            else:
                state = "failed"
            end = job.finished_at or time.time()
            preset = PRESETS.get(job.preset or "", {})
            payload: Dict[str, Any] = {
                "job_id": job.job_id,
                "kind": job.kind,
                "mode": job.mode,
                "state": state,
                "preset": job.preset,
                "preset_label": preset.get("label"),
                "run_id": job.run_id,
                "provider": job.provider,
                "model": job.model,
                "planned_calls": job.planned_calls,
                "elapsed_s": round(end - job.started_at, 1),
                "estimate_minutes": (
                    VALIDATION_ESTIMATE_MINUTES if job.kind == "validation"
                    else self._estimate_minutes(job)
                ),
            }
            if job.kind == "pipeline":
                payload["stages"] = self._stages(job, state)
                payload["progress"] = _last_progress(self.directory / str(job.run_id))
            if state == "failed":
                payload["error"] = self._log_tail(job) or f"The audit process exited with code {code}."
            if job.kind == "validation" and state == "complete":
                payload["validation"] = latest_validation(self.directory)
            return {"job": _clean(payload)}

    def _estimate_minutes(self, job: AuditJob) -> Optional[float]:
        if job.mode == "llm" and job.planned_calls:
            return round(job.planned_calls / _int_env("OPENMERIT_RPM", DEFAULT_LLM_RPM), 1)
        return PRESETS.get(job.preset or "", {}).get("estimate_minutes")

    def _has_result(self, job: AuditJob) -> bool:
        return job.result_path is not None and job.result_path.exists()

    def _store_validation(self, job: AuditJob) -> None:
        if not self._has_result(job):
            return
        verdict = _read_json(job.result_path)
        if isinstance(verdict, dict):
            verdict["validated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            target = self._state_dir() / "latest_validation.json"
            target.write_text(json.dumps(_clean(verdict), indent=2), encoding="utf-8")


def new_web_run_id(root: Path, preset: str, mode: str, provider: str, model: str) -> str:
    from .pipeline.artifacts import new_run_id

    return new_run_id(_preset_config(root, preset, mode=mode, provider=provider, model=model))


# --------------------------------------------------------------------------- #
# configuration of a browser-launched run
# --------------------------------------------------------------------------- #
def _offline(cfg: Any) -> Any:
    cfg.llm.provider = OFFLINE_PROVIDER
    cfg.llm.model = OFFLINE_MODEL
    cfg.llm.requests_per_minute = 10**6
    return cfg


def _preset_config(
    root: Path,
    preset: str,
    output_dir: Optional[str] = None,
    *,
    mode: str = "offline",
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> Any:
    from .config import Config

    # Environment overrides are deliberately not applied wholesale: OPENMERIT_PROVIDER
    # selects the evaluator's model and must never turn an offline tool check
    # into a paid run. The real-LLM mode takes its provider explicitly instead.
    cfg = Config.load(root / PRESETS[preset]["config"], apply_env=False)
    cfg.run_name = preset
    if mode == "llm":
        if not provider or provider == OFFLINE_PROVIDER or not model:
            raise ValueError("A real-LLM audit needs a hosted provider and model.")
        cfg.llm.provider = provider
        cfg.llm.model = model
        cfg.llm.requests_per_minute = _int_env("OPENMERIT_RPM", DEFAULT_LLM_RPM)
        cfg.llm.max_concurrency = _int_env("OPENMERIT_CONCURRENCY", DEFAULT_LLM_CONCURRENCY)
        # Re-running an unchanged design must cost nothing.
        cfg.llm.cache_enabled = True
    else:
        _offline(cfg)
    if output_dir:
        cfg.output_dir = output_dir
    cfg.validate()
    return cfg


# --------------------------------------------------------------------------- #
# worker entry point (runs in the child process)
# --------------------------------------------------------------------------- #
def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(_clean(payload), indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _worker(argv: Optional[Sequence[str]] = None) -> int:
    from .config import Config, load_dotenv_if_present
    from .logging_setup import setup_logging
    from .utils.io import project_root

    parser = argparse.ArgumentParser(prog="python -m openmerit.web_audit")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("pipeline", "plan"):
        command = sub.add_parser(name)
        command.add_argument("--preset", required=True, choices=sorted(PRESETS))
        command.add_argument("--mode", required=True, choices=MODES)
        command.add_argument("--provider", required=True)
        command.add_argument("--model", required=True)
        command.add_argument("--output-dir", required=True)
        if name == "pipeline":
            command.add_argument("--run-id", required=True)
        else:
            command.add_argument("--out", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--out", required=True)
    validate.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)

    root = project_root()
    load_dotenv_if_present()
    if args.command in ("pipeline", "plan"):
        from .pipeline.run import run_pipeline

        cfg = _preset_config(
            root, args.preset, args.output_dir,
            mode=args.mode, provider=args.provider, model=args.model,
        )
        setup_logging(cfg.log_level)
        if args.command == "plan":
            state = run_pipeline(cfg, dry_run=True)
            plan = dict(state.manifest.data.get("planned_calls") or {})
            plan.update(
                preset=args.preset,
                mode=args.mode,
                n_pairs=len(state.pairs),
                n_variants=len(state.variants),
            )
            _write_json(Path(args.out), plan)
            return 0
        run_pipeline(cfg, run_id=args.run_id)
        return 0

    from .validate_auditor import validate_auditor

    cfg = _offline(Config.load(apply_env=False))
    cfg.output_dir = args.output_dir
    setup_logging(cfg.log_level)
    verdict = validate_auditor(cfg)
    _write_json(Path(args.out), verdict)
    return 0 if verdict.get("passed") else 1


if __name__ == "__main__":
    sys.exit(_worker())
