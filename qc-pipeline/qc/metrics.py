"""Accuracy metrics per document and pooled.

acc_T: accuracy over every flagged fact (a census, no sampling error).
acc_R: accuracy over the systematic π-sample of all facts (unbiased for the document).
miss: errors(R ∩ U) / |R ∩ U|, what the triggers fail to flag.
Strict scoring counts a `minor` contradiction as an error; lenient does not. Unverifiable
absence facts are left out of every denominator and reported as a count.
"""

from __future__ import annotations

import math
import warnings

import numpy as np
from scipy.stats import beta

ERRORS = {"contradicted", "not_found"}
CORRECT = {"supported", "consistent"}


def clopper_pearson(errors_or_successes: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    x = errors_or_successes
    if n == 0:
        return (math.nan, math.nan)
    low = 0.0 if x == 0 else float(beta.ppf(alpha / 2, x, n - x + 1))
    high = 1.0 if x == n else float(beta.ppf(1 - alpha / 2, x + 1, n - x))
    return low, high


def is_error(verdict: dict, strict: bool) -> bool | None:
    """True/False for a scored verdict, None when it does not count (unverifiable)."""
    if verdict["verdict"] == "unverifiable":
        return None
    if verdict["verdict"] == "contradicted" and verdict.get("minor") and not strict:
        return False
    return verdict["verdict"] in ERRORS


def accuracy(ids, verdicts: dict[str, dict], strict: bool) -> dict:
    scored = [is_error(verdicts[i], strict) for i in ids if i in verdicts]
    scored = [s for s in scored if s is not None]
    n, errors = len(scored), sum(scored)
    low, high = clopper_pearson(n - errors, n)
    return {"n": n, "errors": errors, "acc": (n - errors) / n if n else math.nan, "ci": [low, high]}


def doc_metrics(facts: list[dict], verdicts: dict[str, dict], strict: bool = True) -> dict:
    ids = [f["fact_id"] for f in facts]
    T = [f["fact_id"] for f in facts if f["in_T"]]
    R = [f["fact_id"] for f in facts if f["in_R"]]
    RU = [f["fact_id"] for f in facts if f["in_R"] and not f["in_T"]]
    U = [i for i in ids if i not in set(T)]
    acc_T, acc_R, acc_RU = (accuracy(group, verdicts, strict) for group in (T, R, RU))
    miss = {"n": acc_RU["n"], "errors": acc_RU["errors"],
            "rate": acc_RU["errors"] / acc_RU["n"] if acc_RU["n"] else math.nan,
            "ci": list(clopper_pearson(acc_RU["errors"], acc_RU["n"]))}
    lowvar = math.nan
    if ids and acc_T["n"] and acc_RU["n"]:
        lowvar = (len(T) * acc_T["acc"] + len(U) * acc_RU["acc"]) / len(ids)

    cited_total = cited_support = 0
    for fact in facts:
        verdict = verdicts.get(fact["fact_id"])
        # JEV-accepted verdicts name no supporting pages, so they cannot enter citation precision.
        if (verdict and fact["status"] == "present" and fact["cited_pages"] and verdict["verdict"] != "unverifiable"
                and verdict.get("judge_stage") != "jev"):
            cited_total += len(fact["cited_pages"])
            cited_support += len(set(verdict.get("supporting_pages") or []) & set(fact["cited_pages"]))
    absent = [f["fact_id"] for f in facts if f["status"] == "absent" and f["fact_id"] in verdicts]
    absent_scored = [is_error(verdicts[i], strict) for i in absent]
    absent_scored = [s for s in absent_scored if s is not None]

    claims: dict[int, list[bool]] = {}
    claims_T: set[int] = set()
    claims_R: set[int] = set()
    for fact in facts:
        verdict = verdicts.get(fact["fact_id"])
        if verdict is None:
            continue
        error = is_error(verdict, strict)
        if error is None:
            continue
        claims.setdefault(fact["claim_id"], []).append(error)
        if fact["in_T"]:
            claims_T.add(fact["claim_id"])
        if fact["in_R"]:
            claims_R.add(fact["claim_id"])
    claim_errors = {c for c, errs in claims.items() if any(errs)}

    return {
        "N": len(ids), "T": len(T), "R": len(R), "RU": len(RU), "U": len(U),
        "acc_T": acc_T, "acc_R": acc_R, "acc_RU": acc_RU, "miss": miss, "acc_lowvar": lowvar,
        "citation_precision": cited_support / cited_total if cited_total else math.nan,
        "absence_errors": sum(absent_scored), "absence_n": len(absent_scored),
        "absence_error_rate": sum(absent_scored) / len(absent_scored) if absent_scored else math.nan,
        "unverifiable": sum(1 for v in verdicts.values() if v["verdict"] == "unverifiable"),
        "needs_review": sum(1 for v in verdicts.values() if v.get("needs_review")),
        "jev_accepted": sum(1 for v in verdicts.values() if v.get("judge_stage") == "jev"),
        # Descriptive only: a claim is wrong if any judged fact in it is. Claim-level
        # random accuracy is not a design-based estimate, because compound claims are
        # more likely to have a fact in R.
        "claims_judged": len(claims), "claims_with_error": len(claim_errors),
        "claims_T_with_error": len(claim_errors & claims_T), "claims_T": len(claims_T),
        "claims_R_with_error": len(claim_errors & claims_R), "claims_R": len(claims_R),
    }


def pooled(rows: list[dict], strict: bool = True) -> dict:
    """Pooled error rate over R with a random intercept per document (binomial GLMM).

    rows: one per judged fact in R, with keys doc_id, section, verdict (dict).
    Uses statsmodels' variational-Bayes binomial mixed GLM; the fixed intercept gives the
    error rate for a typical document. Falls back to the plain pooled proportion when there
    are fewer than two documents or the model does not fit.
    """
    scored = [(r["doc_id"], r["section"], is_error(r["verdict"], strict)) for r in rows]
    scored = [(d, s, e) for d, s, e in scored if e is not None]
    out: dict = {"n": len(scored), "docs": len({d for d, _, _ in scored})}
    if not scored:
        return out
    errors = sum(e for _, _, e in scored)
    out["pooled_proportion"] = {"errors": errors, "acc": 1 - errors / len(scored),
                                "ci": [1 - x for x in reversed(clopper_pearson(errors, len(scored)))]}
    by_section: dict[str, list[bool]] = {}
    for _, section, error in scored:
        by_section.setdefault(section, []).append(error)
    out["by_section"] = {
        section: {"n": len(v), "errors": sum(v), "acc": 1 - sum(v) / len(v),
                  "ci": [1 - x for x in reversed(clopper_pearson(sum(v), len(v)))]}
        for section, v in by_section.items()
    }
    docs = sorted({d for d, _, _ in scored})
    if len(docs) < 2 or errors in (0, len(scored)):
        out["glmm"] = None
        return out
    try:
        from statsmodels.genmod.bayes_mixed_glm import BinomialBayesMixedGLM

        y = np.array([float(e) for _, _, e in scored])
        X = np.ones((len(y), 1))
        Z = np.array([[1.0 if d == doc else 0.0 for doc in docs] for d, _, _ in scored])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            fit = BinomialBayesMixedGLM(y, X, Z, ident=np.zeros(len(docs), dtype=int)).fit_vb()
        mean, sd = float(fit.fe_mean[0]), float(fit.fe_sd[0])
        expit = lambda v: 1 / (1 + math.exp(-v))  # noqa: E731
        out["glmm"] = {
            "error_rate": expit(mean),
            "acc": 1 - expit(mean),
            "acc_ci": [1 - expit(mean + 1.96 * sd), 1 - expit(mean - 1.96 * sd)],
            "doc_sd": float(math.exp(fit.vcp_mean[0])),
        }
    except Exception as exc:  # pragma: no cover - reported, not fatal
        out["glmm"] = {"error": str(exc)}
    return out
