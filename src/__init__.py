"""OpenMerit.

An LLM-based resume screening system whose fairness audit is part of the
pipeline rather than a downstream afterthought.

The package is organised as a set of layers that each own one concern:

    openmerit.data        corpus loading, cleaning and resume/JD pairing
    openmerit.llm         provider abstraction (Gemini, mock, ...), caching, retries
    openmerit.anonymize   pre-inference redaction of identity and proxy cues
    openmerit.screening   rubric-based scoring, evidence grounding, ranking
    openmerit.perturb     counterfactual variant generation + validity checking
    openmerit.fairness    multi-family fairness metrics, statistics, conflict analysis
    openmerit.mitigate    bias-mitigation strategies and their evaluation
    openmerit.pipeline    orchestration, run manifests, reproducible artifacts
    openmerit.report      plots, HTML/Markdown reports, explanation cards, model card

Nothing in ``openmerit.fairness`` knows how a score was produced, and nothing in
``openmerit.screening`` knows how it will be audited; they communicate only through
the tidy score table defined in :mod:`openmerit.schemas`.
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = ["__version__"]
