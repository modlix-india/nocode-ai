"""Keyword Metric Performance Evaluator.

Evaluates keyword performance metrics against predefined thresholds to classify
keywords into 'good', 'poor', or 'top' performers.
"""

from dataclasses import dataclass, field
from heapq import nlargest
from statistics import median
from typing import Any, Dict, List, Tuple

import logging

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MetricEvaluatorConfig:
    ctr_threshold: float = 2.0
    quality_score_threshold: int = 4
    cpl_multiplier: float = 1.5
    min_clicks_for_conversions: int = 15
    conversion_rate_threshold: float = 1.0
    critical_click_threshold: int = 50
    critical_cost_threshold: float = 2000.0
    default_max_cpl: float = 2000.0
    default_min_cpl: float = 50.0
    top_performer_percentage: float = 0.2
    performance_weights: Dict[str, float] = field(
        default_factory=lambda: {"efficiency": 0.40, "impressions": 0.30, "conversions": 0.30}
    )


KEYWORD_CONFIG = MetricEvaluatorConfig()



class MetricPerformanceEvaluator:
    def __init__(self, config: MetricEvaluatorConfig = KEYWORD_CONFIG):
        self.config = config

    def evaluate(self, entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Classify entries with strength: good, poor."""
        results: List[Dict[str, Any]] = []
        with_data: List[Dict[str, Any]] = []

        for entry in entries:
            if entry.get("metrics", {}).get("impressions", 0) == 0:
                results.append(
                    {
                        **entry,
                        "strength": "poor",
                        "is_critical": True,
                        "reason": "No impressions - not generating traffic",
                    }
                )
            else:
                with_data.append(entry)

        if not with_data:
            return results

        median_cpl = self._calculate_median_cpl(with_data)

        for entry in with_data:
            issues, is_critical = self._identify_performance_issues(entry, median_cpl)
            if len(issues) >= 2 or is_critical:
                results.append(
                    {
                        **entry,
                        "strength": "poor",
                        "is_critical": is_critical,
                        "reason": "; ".join(issues),
                    }
                )
            else:
                results.append({**entry, "strength": "good"})

        logger.info(
            "performance_evaluation total=%d good=%d",
            len(results),
            sum(1 for e in results if e["strength"] == "good"),
        )
        return results

    def mark_top_performers(self, entries: List[Dict[str, Any]]) -> None:
        """Upgrade top good entries strength to 'top'."""
        good = [e for e in entries if e.get("strength") == "good"]
        if not good:
            return
        top_set = set(map(id, self._rank_top_entries(good)))
        for e in entries:
            if id(e) in top_set:
                e["strength"] = "top"

    def _calculate_median_cpl(self, entries: List[Dict[str, Any]]) -> float:
        valid = [e["metrics"].get("cpl") for e in entries if e["metrics"].get("cpl") is not None]
        return median(valid) if valid else self.config.default_max_cpl

    def _identify_performance_issues(
        self, entry: Dict[str, Any], median_cpl: float
    ) -> Tuple[List[str], bool]:
        cfg = self.config
        m = entry["metrics"]
        issues: List[str] = []
        is_critical = False
        cpl = m.get("cpl")
        conv_rate = m.get("conv_rate") or 0.0
        quality_score = entry.get("quality_score")
        has_enough_clicks = m.get("clicks", 0) >= cfg.min_clicks_for_conversions

        if m.get("conversions", 0) == 0:
            cost_critical = m.get("cost", 0) >= cfg.critical_cost_threshold
            clicks_critical = m.get("clicks", 0) >= cfg.critical_click_threshold
            if cost_critical or clicks_critical:
                parts = []
                if cost_critical:
                    parts.append(f"{m.get('cost', 0):.2f} spent")
                if clicks_critical:
                    parts.append(f"{m.get('clicks', 0)} clicks")
                issues.append(f"Critical: No conversions after {' and '.join(parts)}")
                is_critical = True
            elif has_enough_clicks:
                issues.append("No conversions despite clicks")
        elif cpl and cpl > median_cpl * cfg.cpl_multiplier:
            issues.append(f"High CPL ({cpl:.2f} vs {median_cpl:.2f} median)")

        if m.get("ctr", 0) < cfg.ctr_threshold:
            issues.append(f"Low CTR ({m.get('ctr', 0):.1f}%)")
        if m.get("clicks", 0) == 0 and m.get("impressions", 0) > 0:
            issues.append(f"No clicks after {m.get('impressions', 0)} impressions")
        if quality_score and quality_score <= cfg.quality_score_threshold:
            issues.append(f"Low quality score ({quality_score})")
        if conv_rate < cfg.conversion_rate_threshold and has_enough_clicks:
            issues.append(f"Low conversion rate ({conv_rate:.1f}%)")

        return issues, is_critical

    def _rank_top_entries(self, entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Return top N entries by performance score."""
        max_vals, min_cpl, max_cpl = self._compute_normalization_bounds(entries)
        top_count = max(2, int(len(entries) * self.config.top_performer_percentage))
        return nlargest(
            top_count,
            entries,
            key=lambda e: self._calculate_score(e, max_vals, min_cpl, max_cpl),
        )

    def _compute_normalization_bounds(
        self, entries: List[Dict[str, Any]]
    ) -> Tuple[Dict[str, float], float, float]:
        max_imp, max_ctr, max_conv, max_qs = 1.0, 1.0, 1.0, 10.0
        valid_cpls: List[float] = []
        for e in entries:
            m = e["metrics"]
            max_imp = max(max_imp, m.get("impressions", 0))
            max_ctr = max(max_ctr, m.get("ctr", 0))
            max_conv = max(max_conv, m.get("conversions", 0))
            qs = e.get("quality_score")
            if qs:
                max_qs = max(max_qs, qs)
            cpl = m.get("cpl")
            if cpl and cpl > 0:
                valid_cpls.append(cpl)

        max_vals = {
            "impressions": max_imp,
            "ctr": max_ctr,
            "conversions": max_conv,
            "quality": max_qs,
        }
        cfg = self.config
        min_cpl = min(valid_cpls) if valid_cpls else cfg.default_min_cpl
        max_cpl = (
            min(max(valid_cpls), cfg.default_max_cpl * 2)
            if valid_cpls
            else cfg.default_max_cpl
        )
        return max_vals, min_cpl, max_cpl

    def _calculate_score(
        self, entry: Dict[str, Any], max_vals: Dict[str, float], min_cpl: float, max_cpl: float
    ) -> float:
        w = self.config.performance_weights
        m = entry["metrics"]
        cpl = m.get("cpl")
        qs = entry.get("quality_score")

        imp_score = (m.get("impressions", 0) / max_vals["impressions"]) * 100
        ctr_score = (m.get("ctr", 0) / max_vals["ctr"]) * 100
        conv_score = (m.get("conversions", 0) / max_vals["conversions"]) * 100
        qs_score = (qs / max_vals["quality"] * 100) if qs else 50
        cpl_score = (
            (1 - (min(cpl, max_cpl) - min_cpl) / (max_cpl - min_cpl)) * 100
            if cpl and max_cpl > min_cpl
            else 50
        )

        efficiency = (ctr_score + qs_score + cpl_score) / 3
        return (
            efficiency * w["efficiency"]
            + imp_score * w["impressions"]
            + conv_score * w["conversions"]
        )
