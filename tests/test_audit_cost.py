"""Cost controls for auditing a real LLM: fewer calls, same evidence."""

import copy

from openmerit.pipeline import run_pipeline
from openmerit.pipeline.run import focus_pairs, planned_calls, start_run
from openmerit.schemas import Pair


def _cheap(cfg, *, focus=None):
    cfg = copy.deepcopy(cfg)
    cfg.perturbation.cue_families = ["name", "education_prestige"]
    cfg.perturbation.levels_per_family = 4
    cfg.llm.self_consistency_samples = 3
    cfg.llm.mock_noise_sd = 1.5
    cfg.perturbation.focus_pairs = focus
    return cfg


class _Record:
    def __init__(self, job_id, resume_id, score):
        self.job_id = job_id
        self.base_resume_id = resume_id
        self.overall_score = score
        self.parse_ok = True


# --------------------------------------------------------------------------- #
# focusing on borderline candidates
# --------------------------------------------------------------------------- #
def test_focus_keeps_the_pairs_nearest_a_decision_threshold(cfg):
    cfg = copy.deepcopy(cfg)
    cfg.perturbation.focus_pairs = 2
    state = start_run(cfg)
    state.pairs = [
        Pair(pair_id="far-low", job_id="J1", resume_id="R1", pairing_rationale=""),
        Pair(pair_id="near-advance", job_id="J1", resume_id="R2", pairing_rationale=""),
        Pair(pair_id="far-high", job_id="J1", resume_id="R3", pairing_rationale=""),
        Pair(pair_id="near-borderline", job_id="J1", resume_id="R4", pairing_rationale=""),
    ]
    state.records_by_arm["baseline_unperturbed"] = [
        _Record("J1", "R1", 20.0), _Record("J1", "R2", 71.0),
        _Record("J1", "R3", 95.0), _Record("J1", "R4", 53.0),
    ]

    focus_pairs(state)

    assert [p.pair_id for p in state.pairs] == ["near-advance", "near-borderline"]
    assert any(w.startswith("Focused design:") for w in state.warnings)
    assert state.manifest.data["stages"]["focus_pairs"]["n_pairs_kept"] == 2


def test_focus_is_a_no_op_when_not_configured(cfg):
    state = start_run(cfg)
    state.pairs = [Pair(pair_id="p", job_id="J1", resume_id="R1", pairing_rationale="")]
    focus_pairs(state)
    assert len(state.pairs) == 1 and not state.warnings


def test_cheap_design_still_recovers_an_injected_bias(cfg):
    """The cost cuts must not cost the audit its sensitivity."""
    cfg = _cheap(cfg, focus=10)
    cfg.llm.repeat_scope = "baseline"
    cfg.llm.mock_bias = {"gender": {"female": -3.0}}
    stages = ["prepare_data", "generate_variants", "score_baseline", "score_variants", "audit"]

    state = run_pipeline(cfg, stages=stages)

    gaps = [m for m in state.audit_report.metrics if m.name == "score_gap[gender:female_vs_male]"]
    assert gaps and gaps[0].value < -2.0
    assert gaps[0].significant
    assert len(state.pairs) == 10


# --------------------------------------------------------------------------- #
# the plan
# --------------------------------------------------------------------------- #
def test_dry_run_plan_counts_re_scoring_mitigation_arms(cfg):
    cfg = _cheap(cfg)
    cfg.mitigation.strategies = ["blind_screening", "counterfactual_marginalization"]

    state = run_pipeline(cfg, dry_run=True)
    plan = state.manifest.data["planned_calls"]

    assert plan["mitigation_calls"] == {"blind_screening": plan["variant_calls"]}
    assert plan["total_calls"] == plan["baseline_calls"] + 2 * plan["variant_calls"]
    # The original resumes are scored for the sanity gate and again in the
    # variant set; the second pass is served from cache.
    assert plan["api_calls"] == plan["total_calls"] - plan["served_from_cache"]
    assert plan == planned_calls(state)


def test_dry_run_plan_reflects_the_focused_design(cfg):
    full = run_pipeline(_cheap(cfg), dry_run=True).manifest.data["planned_calls"]
    focused = run_pipeline(_cheap(cfg, focus=4), dry_run=True).manifest.data["planned_calls"]

    assert focused["variant_calls"] < full["variant_calls"]
    assert focused["baseline_calls"] == full["baseline_calls"]
