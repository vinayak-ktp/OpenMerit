"""The fairness-audit workflow in the web app (workflow 2)."""

import json
import math
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openmerit import web_api, web_audit

RUN_ID = "20261101-120000_offline_demo_abc123"


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")


def _make_run(runs: Path, run_id: str = RUN_ID, *, created_at: str = "2026-11-01T12:00:00+00:00") -> Path:
    root = runs / run_id
    _write(root / "manifest.json", {
        "run_id": run_id,
        "created_at": created_at,
        "status": "complete",
        "total_elapsed_s": 98.4,
        "config": {
            "run_name": "offline_demo",
            "llm": {"provider": "mock", "model": "mock-rubric-v1"},
            "data": {"resumes_path": "data/sample/resumes"},
            "perturbation": {"cue_families": ["name", "pronoun"]},
            "fairness": {"attributes": ["gender"]},
        },
        "stages": {
            "score_baseline": {"sanity_check": {"verdict": "pass", "kendall_tau": 0.7}},
        },
        "warnings": [],
    })
    _write(root / "audit" / "summary.json", {
        "n_records": 120, "n_base_resumes": 4, "n_jobs": 2, "n_variants": 40,
        "n_metrics": 3, "n_failed": 1, "n_significant_after_fdr": 0,
        "by_family": {"counterfactual": {"n": 2, "failed": 1, "significant": 0}},
        "reliability": {"noise_floor_sd": 0.6, "cue_effect_to_noise_ratio": float("nan")},
        "conflicts": {"n_conflicts": 1, "guidance": "Name each disagreement."},
    })
    _write(root / "audit" / "conflicts.json", [
        {"type": "individual_vs_group", "attribute": "gender", "severity": "important",
         "explanation": "Groups look equal while individuals swing."},
    ])
    _write(root / "audit" / "metrics.csv", (
        "family,metric,attribute,value,unit,ci_low,ci_high,p_value,q_value_bh,effect_size,"
        "effect_size_name,n,threshold,passed,significant_after_fdr,interpretation\n"
        "counterfactual,counterfactual_max_swing[name],name,1.5,score_points,1.2,1.8,,,,,4,2.0,True,False,Name moved the score.\n"
        "counterfactual,decision_flip_rate[name],name,0.25,fraction_of_candidates,,,,,,,4,0.0,False,False,Decisions flipped.\n"
        "counterfactual,counterfactual_max_swing[all],all,1.6,score_points,,,,,,,4,2.0,True,False,All cues.\n"
        "group_parity,max_pairwise_score_gap[gender],gender,0.3,score_points,,,0.4,0.8,,,4,1.0,True,False,Small gap.\n"
    ))
    _write(root / "report" / "report.json", {
        "headline_findings": [{"metric": "decision_flip_rate[name]", "verdict": "FAIL", "value": 0.25}],
        "cards_summary": {"n_cards": 4, "unstable_candidates": []},
        "mitigation": {
            "ranking": ["blind_screening"],
            "guidance": "Guard utility.",
            "comparisons": {
                "blind_screening": {
                    "verdict": "recommended", "n_metrics_improved": 2, "n_metrics_worsened": 0,
                    "utility": {"rank_agreement_kendall_tau": 0.95, "top_k_overlap": 1.0},
                    "audit_summary": {"n_failed": 0},
                },
            },
        },
    })
    _write(root / "report" / "report.html", "<html>report</html>")
    (root / "report" / "cue_effects.png").write_bytes(b"\x89PNG\r\n")
    return root


class FakeProcess:
    def __init__(self) -> None:
        self.code = None
        self.terminated = False

    def poll(self):
        return self.code

    def terminate(self) -> None:
        self.terminated = True
        self.code = -15

    def wait(self, timeout=None):
        return self.code

    def kill(self) -> None:  # pragma: no cover - only used if terminate hangs
        self.code = -9


class FakeLauncher:
    def __init__(self) -> None:
        self.calls = []
        self.processes = []

    def __call__(self, args, log_path):
        self.calls.append(list(args))
        process = FakeProcess()
        self.processes.append(process)
        return process


@pytest.fixture
def runs(tmp_path):
    return tmp_path / "runs"


class FakePlanner:
    """Stands in for the dry-run subprocess; never touches a model."""

    def __init__(self, api_calls: int = 186) -> None:
        self.api_calls = api_calls
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        return {
            "provider": args[args.index("--provider") + 1],
            "model": args[args.index("--model") + 1],
            "baseline_calls": 54, "variant_calls": 168, "mitigation_calls": {},
            "total_calls": self.api_calls + 36, "served_from_cache": 36,
            "api_calls": self.api_calls, "requests_per_minute": 12,
            "estimated_minutes": round(self.api_calls / 12, 1), "n_pairs": 12, "n_variants": 144,
        }


@pytest.fixture
def llm_state():
    return {"configured": True}


@pytest.fixture
def client(runs, monkeypatch, llm_state):
    monkeypatch.setenv("OPENMERIT_OUTPUT_DIR", str(runs))
    monkeypatch.delenv("OPENMERIT_AUDIT_MAX_CALLS", raising=False)
    launcher, planner = FakeLauncher(), FakePlanner()
    app = web_api.create_app()
    app.state.audit_jobs = web_audit.AuditJobs(
        Path.cwd(), runs, launcher=launcher, planner=planner,
        llm_target=lambda: ("gemini", "gemini-3.5-flash-lite", llm_state["configured"]),
    )
    test_client = TestClient(app)
    test_client.launcher = launcher
    test_client.planner = planner
    return test_client


# --------------------------------------------------------------------------- #
# reading runs
# --------------------------------------------------------------------------- #
def test_list_runs_only_includes_finished_audits_newest_first(runs):
    _make_run(runs, RUN_ID)
    _make_run(runs, "20261102-090000_names_only_def456", created_at="2026-11-02T09:00:00+00:00")
    (runs / "20261103-090000_validate-null_aaa111").mkdir(parents=True)
    _write(runs / "20261103-090000_validate-null_aaa111" / "manifest.json", {"status": "complete"})

    listed = web_audit.list_runs(runs)

    assert [run["run_id"] for run in listed] == ["20261102-090000_names_only_def456", RUN_ID]
    assert listed[0]["provider"] == "mock"


def test_run_details_reshapes_artifacts_and_drops_nan(runs):
    _make_run(runs)

    details = web_audit.run_details(runs, RUN_ID)

    assert details["n_variants"] == 40
    assert details["n_material"] == 0
    assert details["reliability"]["cue_effect_to_noise_ratio"] is None
    assert details["cue_effects"] == [{
        "cue": "name", "swing": 1.5, "swing_ci_low": 1.2, "swing_ci_high": 1.8,
        "swing_threshold": 2.0, "swing_passed": True, "interpretation": "Name moved the score.",
        "flip_rate": 0.25, "flip_passed": False,
    }]
    assert details["group_gaps"][0]["attribute"] == "gender"
    assert details["mitigation"]["arms"][0]["arm"] == "blind_screening"
    assert details["mitigation"]["arms"][0]["rank"] == 1
    assert details["plots"] == ["cue_effects.png"]
    assert details["has_html_report"] is True
    json.dumps(details, allow_nan=False)


def test_run_details_rejects_unknown_or_unsafe_ids(runs):
    _make_run(runs)
    with pytest.raises(ValueError):
        web_audit.run_details(runs, "../etc")
    with pytest.raises(FileNotFoundError):
        web_audit.run_details(runs, "20990101-000000_missing_000000")


@pytest.mark.parametrize("name", ["../manifest.json", "manifest.json", ".hidden.png", "notes.exe"])
def test_report_file_refuses_anything_outside_the_report_folder(runs, name):
    _make_run(runs)
    _write(runs / RUN_ID / "manifest.json", {"status": "complete"})
    with pytest.raises((ValueError, FileNotFoundError)):
        web_audit.report_file(runs, RUN_ID, name)


def test_clean_replaces_non_finite_numbers():
    assert web_audit._clean({"a": [math.inf, 1.0, {"b": math.nan}]}) == {"a": [None, 1.0, {"b": None}]}


def test_preset_config_is_always_offline(monkeypatch):
    monkeypatch.setenv("OPENMERIT_PROVIDER", "gemini")
    monkeypatch.setenv("OPENMERIT_MODEL", "gemini-3.5-flash-lite")
    root = Path(__file__).resolve().parents[1]

    for preset in web_audit.PRESETS:
        cfg = web_audit._preset_config(root, preset, "runs-test")
        assert cfg.llm.provider == "mock"
        assert cfg.llm.model == "mock-rubric-v1"
        assert cfg.output_dir == "runs-test"


# --------------------------------------------------------------------------- #
# HTTP endpoints
# --------------------------------------------------------------------------- #
def test_presets_endpoint_hides_config_paths(client):
    data = client.get("/api/audit/presets").json()

    assert {p["key"] for p in data["presets"]} == set(web_audit.PRESETS)
    assert all("config" not in p for p in data["presets"])
    assert data["llm"] == {
        "provider": "gemini", "model": "gemini-3.5-flash-lite", "configured": True,
        "requests_per_minute": 12, "max_calls": web_audit.DEFAULT_MAX_LLM_CALLS,
    }
    assert [s["key"] for s in data["stages"]][0] == "prepare_data"


def test_runs_endpoints_serve_details_and_report_files(client, runs):
    _make_run(runs)

    assert client.get("/api/audit/runs").json()["runs"][0]["run_id"] == RUN_ID
    assert client.get(f"/api/audit/runs/{RUN_ID}").json()["n_metrics"] == 3
    image = client.get(f"/api/audit/runs/{RUN_ID}/report/cue_effects.png")
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert client.get(f"/api/audit/runs/{RUN_ID}/report/report.html").status_code == 200
    assert client.get(f"/api/audit/runs/{RUN_ID}/report/..%2Fmanifest.json").status_code == 404
    assert client.get("/api/audit/runs/20990101-000000_missing_000000").status_code == 404


def test_pipeline_job_reports_stage_progress_and_blocks_a_second_job(client, runs):
    started = client.post("/api/audit/job", json={"kind": "pipeline", "preset": "offline_demo"})
    assert started.status_code == 200
    job = started.json()["job"]
    assert job["state"] == "running"
    assert job["mode"] == "offline"
    args = client.launcher.calls[0]
    assert args[:5] == ["pipeline", "--preset", "offline_demo", "--mode", "offline"]
    assert args[args.index("--provider") + 1] == "mock"
    assert args[args.index("--output-dir") + 1] == str(runs)

    assert client.post("/api/audit/job", json={"kind": "validation"}).status_code == 409

    _write(runs / job["run_id"] / "manifest.json", {"stages": {"prepare_data": {}, "generate_variants": {}}})
    stages = client.get("/api/audit/job").json()["job"]["stages"]
    assert [s["state"] for s in stages[:4]] == ["done", "done", "running", "pending"]

    client.launcher.processes[0].code = 0
    assert client.get("/api/audit/job").json()["job"]["state"] == "complete"


def test_failed_job_surfaces_the_log_tail(client, runs):
    job = client.post("/api/audit/job", json={"kind": "pipeline", "preset": "names_only"}).json()["job"]
    log = runs / ".web" / f"job-{job['job_id']}.log"
    _write(log, "Traceback\nValueError: corpus is empty")
    client.launcher.processes[0].code = 1

    status = client.get("/api/audit/job").json()["job"]
    assert status["state"] == "failed"
    assert "corpus is empty" in status["error"]


def test_validation_job_stores_the_latest_verdict(client, runs):
    job = client.post("/api/audit/job", json={"kind": "validation"}).json()["job"]
    args = client.launcher.calls[0]
    result_path = Path(args[args.index("--out") + 1])
    _write(result_path, {"passed": False, "n_passed": 4, "n_checks": 5, "checks": []})
    # The worker exits 1 when the auditor fails a check: a finished job, not a crash.
    client.launcher.processes[0].code = 1

    status = client.get("/api/audit/job").json()["job"]
    assert status["job_id"] == job["job_id"]
    assert status["state"] == "complete"
    assert status["validation"]["n_passed"] == 4
    assert client.get("/api/audit/validation").json()["validation"]["n_checks"] == 5


def test_cancel_terminates_the_running_job(client):
    client.post("/api/audit/job", json={"kind": "pipeline", "preset": "offline_demo"})

    cancelled = client.post("/api/audit/job/cancel").json()["job"]

    assert cancelled["state"] == "cancelled"
    assert client.launcher.processes[0].terminated
    assert client.post("/api/audit/job/cancel").status_code == 409


def test_unknown_preset_or_kind_is_rejected(client):
    assert client.post("/api/audit/job", json={"kind": "pipeline", "preset": "nope"}).status_code == 400
    assert client.post("/api/audit/job", json={"kind": "train"}).status_code == 400


# --------------------------------------------------------------------------- #
# real-LLM audits: planned, capped and confirmed
# --------------------------------------------------------------------------- #
def test_plan_endpoint_reports_the_cost_and_caches_the_dry_run(client):
    first = client.post("/api/audit/plan", json={"preset": "quick", "mode": "llm"}).json()
    again = client.post("/api/audit/plan", json={"preset": "quick", "mode": "llm"}).json()

    assert first["api_calls"] == 186
    assert first["within_limit"] is True
    assert first["model"] == "gemini-3.5-flash-lite"
    assert again == first
    assert len(client.planner.calls) == 1


def test_offline_only_designs_cannot_be_planned_for_an_llm(client):
    response = client.post("/api/audit/plan", json={"preset": "synthetic", "mode": "llm"})
    assert response.status_code == 400
    assert "offline only" in response.json()["detail"]


def test_llm_audit_requires_confirming_the_planned_calls(client):
    unconfirmed = client.post("/api/audit/job", json={"kind": "pipeline", "preset": "quick", "mode": "llm"})
    too_few = client.post(
        "/api/audit/job",
        json={"kind": "pipeline", "preset": "quick", "mode": "llm", "confirmed_calls": 100},
    )
    assert unconfirmed.status_code == 400 and too_few.status_code == 400
    assert client.launcher.calls == []

    started = client.post(
        "/api/audit/job",
        json={"kind": "pipeline", "preset": "quick", "mode": "llm", "confirmed_calls": 186},
    )
    assert started.status_code == 200
    job = started.json()["job"]
    assert job["mode"] == "llm" and job["planned_calls"] == 186
    args = client.launcher.calls[0]
    assert args[args.index("--mode") + 1] == "llm"
    assert args[args.index("--provider") + 1] == "gemini"
    assert args[args.index("--model") + 1] == "gemini-3.5-flash-lite"


def test_llm_audit_above_the_call_limit_is_refused(client, monkeypatch):
    monkeypatch.setenv("OPENMERIT_AUDIT_MAX_CALLS", "100")

    plan = client.post("/api/audit/plan", json={"preset": "quick", "mode": "llm"}).json()
    response = client.post(
        "/api/audit/job",
        json={"kind": "pipeline", "preset": "quick", "mode": "llm", "confirmed_calls": 186},
    )

    assert plan["within_limit"] is False and plan["max_calls"] == 100
    assert response.status_code == 400
    assert "OPENMERIT_AUDIT_MAX_CALLS" in response.json()["detail"]
    assert client.launcher.calls == []


def test_llm_audit_needs_an_api_key(client, llm_state):
    llm_state["configured"] = False
    response = client.post(
        "/api/audit/job",
        json={"kind": "pipeline", "preset": "quick", "mode": "llm", "confirmed_calls": 186},
    )
    assert response.status_code == 400
    assert "API key" in response.json()["detail"]


def test_llm_preset_config_targets_the_evaluator_model_with_the_cache_on(monkeypatch):
    monkeypatch.setenv("OPENMERIT_RPM", "30")
    root = Path(__file__).resolve().parents[1]

    cfg = web_audit._preset_config(
        root, "quick", "runs-test", mode="llm", provider="gemini", model="gemini-3.5-flash-lite"
    )

    assert (cfg.llm.provider, cfg.llm.model) == ("gemini", "gemini-3.5-flash-lite")
    assert cfg.llm.requests_per_minute == 30
    assert cfg.llm.cache_enabled is True
    assert cfg.llm.repeat_scope == "baseline"
    assert cfg.perturbation.focus_pairs == 12
    assert cfg.perturbation.levels_per_family == 8
    assert cfg.mitigation.strategies == [
        "counterfactual_marginalization", "sensitivity_gated_review", "cue_effect_debiasing",
    ]
    assert cfg.perturbation.rotate_levels is True
    assert cfg.run_name == "quick"
    with pytest.raises(ValueError):
        web_audit._preset_config(root, "quick", mode="llm", provider="mock", model="mock-rubric-v1")


def test_job_status_reports_scoring_progress_from_the_run_log(client, runs):
    job = client.post("/api/audit/job", json={"kind": "pipeline", "preset": "names_only"}).json()["job"]
    _write(runs / job["run_id"] / "logs" / "run.jsonl", "\n".join([
        json.dumps({"msg": "run started"}),
        json.dumps({"msg": "scoring progress", "arm": "baseline", "done": 40, "total": 168}),
        json.dumps({"msg": "scoring progress", "arm": "baseline", "done": 80, "total": 168}),
    ]))

    progress = client.get("/api/audit/job").json()["job"]["progress"]

    assert progress == {"arm": "baseline", "done": 80, "total": 168}


def test_runs_are_labelled_by_what_they_audited(runs):
    _make_run(runs)
    llm_run = _make_run(runs, "20261103-090000_quick_bbb222", created_at="2026-11-03T09:00:00+00:00")
    manifest = json.loads((llm_run / "manifest.json").read_text())
    manifest["config"]["run_name"] = "quick"
    manifest["config"]["llm"] = {"provider": "gemini", "model": "gemini-3.5-flash-lite"}
    manifest["warnings"] = ["Focused design: only the 12 of 18 pairs were perturbed."]
    _write(llm_run / "manifest.json", manifest)

    listed = {run["run_id"]: run for run in web_audit.list_runs(runs)}
    details = web_audit.run_details(runs, "20261103-090000_quick_bbb222")

    assert listed[RUN_ID]["mode"] == "offline"
    assert listed["20261103-090000_quick_bbb222"]["mode"] == "llm"
    assert listed["20261103-090000_quick_bbb222"]["preset_label"] == "Quick audit"
    assert details["design_notes"] and details["warnings"] == []


def test_every_llm_preset_uses_the_cost_controls():
    """Browser-launchable LLM designs must stay cheap; the research designs stay offline."""
    root = Path(__file__).resolve().parents[1]
    for key in ("quick", "all_cues"):
        cfg = web_audit._preset_config(root, key, mode="llm", provider="gemini", model="m")
        assert cfg.llm.repeat_scope == "baseline"
        assert cfg.perturbation.focus_pairs
        assert not {"blind_screening", "debias_prompt"} & set(cfg.mitigation.strategies)
    assert not web_audit.PRESETS["offline_demo"]["llm"]
    assert not web_audit.PRESETS["synthetic"]["llm"]
