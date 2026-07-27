"""Statistical analysis of multiple simulation replications.

Given a list of replication result dicts (each a flat mapping of metric name ->
scalar value), `ReplicationAnalyzer.analyze_replications` auto-extracts every
numeric metric and, for each, computes central tendency, variability,
confidence intervals (t-distribution), percentiles, a normality test, and
outlier bounds. `format_industry_summary` renders the classic
``Mean +/- Half-Width (95%) [n=...]`` report.

Callers here pass *flat* dicts: `run_replications` flattens each nested
run_simulation result (nodes/edges -> per-stat scalars) before handing them in,
so `_extract_metrics` can pick up every numeric leaf by its top-level key. Keys
beginning with `_` are treated as metadata and skipped.
"""

from __future__ import annotations

import math
import statistics
from typing import Any, Dict, List

import numpy as np
from scipy import stats


def _sanitize(obj: Any) -> Any:
    """Recursively replace non-finite floats (inf/-inf/nan) with None.

    A coefficient of variation is `inf` when the mean is 0, and Shapiro-Wilk can
    return a `nan` statistic for a (near-)constant metric. `inf`/`nan` are not
    valid strict JSON, so an MCP client may reject the payload — and `nan`
    breaks `==` (nan != nan). Mapping them to None keeps the result strict-JSON
    and round-trippable, which is the contract every other tool here honors.
    """
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def _num(value: Any) -> str:
    """Format a number to 4 dp, or "n/a" for a non-number (e.g. sanitized None)."""
    return f"{value:.4f}" if isinstance(value, (int, float)) else "n/a"


def _pct(value: Any) -> str:
    """Format a number as a percentage, or "n/a" for a non-number."""
    return f"{value:.2%}" if isinstance(value, (int, float)) else "n/a"


class ReplicationAnalyzer:
    """Analyzes multiple simulation replications with industry-standard statistics."""

    def __init__(self, confidence_levels: List[float] | None = None):
        """Initialize the replication analyzer.

        Args:
            confidence_levels: confidence levels to calculate. Defaults to
                90%, 95%, 99% when omitted (a fresh list per instance — never a
                shared mutable default).
        """
        self.confidence_levels = (
            [0.90, 0.95, 0.99] if confidence_levels is None else confidence_levels
        )

    def analyze_replications(
        self, replications: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Perform comprehensive statistical analysis on multiple replications.

        Args:
            replications: individual replication results (flat metric dicts).

        Returns:
            Per-metric statistics, plus `_replication_summary` and
            `_individual_replications` bookkeeping keys.

        Raises:
            ValueError: if fewer than two replications are provided (you cannot
                do statistics on a single point).
        """
        if not replications:
            raise ValueError("No replications provided for analysis")

        if len(replications) < 2:
            raise ValueError("At least 2 replications required for statistical analysis")

        # Extract all numeric metrics from replications
        metrics = self._extract_metrics(replications)

        # Perform statistical analysis for each metric
        analysis_results: Dict[str, Any] = {}
        for metric_name, values in metrics.items():
            if len(values) >= 2:  # Need at least 2 values for statistics
                analysis_results[metric_name] = self._analyze_metric(
                    metric_name, values
                )

        # Add overall replication summary
        analysis_results["_replication_summary"] = {
            "total_replications": len(replications),
            "successful_replications": len(
                [r for r in replications if "_metadata" not in r or "error" not in r]
            ),
            "metrics_analyzed": len(analysis_results),
            "confidence_levels": self.confidence_levels,
        }

        # Add individual replication data for transparency
        analysis_results["_individual_replications"] = replications

        # Map inf/nan -> None so the whole payload is strict-JSON-valid.
        return _sanitize(analysis_results)

    def _extract_metrics(
        self, replications: List[Dict[str, Any]]
    ) -> Dict[str, List[float]]:
        """Extract all numeric metrics from replications.

        Metadata keys (leading `_`) and non-numeric values are skipped. `bool`
        is an `int` subclass, so it is excluded explicitly — a True/False flag
        is not a metric to average.
        """
        metrics: Dict[str, List[float]] = {}

        for replication in replications:
            for key, value in replication.items():
                # Skip metadata and non-numeric values (bool is an int subclass).
                if (
                    key.startswith("_")
                    or isinstance(value, bool)
                    or not isinstance(value, (int, float))
                ):
                    continue

                metrics.setdefault(key, []).append(float(value))

        return metrics

    def _analyze_metric(
        self, metric_name: str, values: List[float]
    ) -> Dict[str, Any]:
        """Perform comprehensive statistical analysis on a single metric."""
        n = len(values)

        # Basic statistics
        mean_val = statistics.mean(values)
        median_val = statistics.median(values)
        std_dev = statistics.stdev(values) if n > 1 else 0.0
        variance = statistics.variance(values) if n > 1 else 0.0

        # Range and percentiles
        min_val = min(values)
        max_val = max(values)
        range_val = max_val - min_val

        # Calculate percentiles
        percentiles: Dict[str, float] = {}
        for p in [5, 10, 25, 75, 90, 95]:
            percentiles[f"p{p}"] = float(np.percentile(values, p))

        # Coefficient of variation (only meaningful for a non-zero mean)
        cv = (std_dev / abs(mean_val)) if mean_val != 0 else float("inf")

        # Standard error of mean
        se_mean = std_dev / math.sqrt(n) if n > 1 else 0.0

        # Confidence intervals (t-distribution for small samples)
        confidence_intervals: Dict[str, Any] = {}
        for conf_level in self.confidence_levels:
            if n > 1:
                alpha = 1 - conf_level
                t_critical = stats.t.ppf(1 - alpha / 2, n - 1)
                margin_of_error = t_critical * se_mean

                confidence_intervals[f"ci_{int(conf_level * 100)}"] = {
                    "lower": mean_val - margin_of_error,
                    "upper": mean_val + margin_of_error,
                    "half_width": margin_of_error,
                    "relative_precision": (
                        (margin_of_error / abs(mean_val))
                        if mean_val != 0
                        else float("inf")
                    ),
                }

        # Normality test (Shapiro-Wilk for 3 <= n <= 50, otherwise skip).
        # Zero-variance data has no distribution to test — Shapiro returns a nan
        # statistic there and warns; report that instead of running it.
        normality_test = None
        if 3 <= n <= 50 and std_dev == 0.0:
            normality_test = {
                "test": "Shapiro-Wilk",
                "skipped": "constant data (zero variance)",
            }
        elif 3 <= n <= 50:
            try:
                stat, p_value = stats.shapiro(values)
                normality_test = {
                    "test": "Shapiro-Wilk",
                    "statistic": float(stat),
                    "p_value": float(p_value),
                    # bool(...) so it's a native bool, not a numpy bool_ (the
                    # latter isn't JSON-serializable through the MCP layer).
                    "is_normal": bool(p_value > 0.05),
                    "interpretation": (
                        "Normal distribution"
                        if p_value > 0.05
                        else "Non-normal distribution"
                    ),
                }
            except Exception:
                normality_test = {"test": "Shapiro-Wilk", "error": "Test failed"}

        # Outlier detection (IQR method)
        q1 = percentiles["p25"]
        q3 = percentiles["p75"]
        iqr = q3 - q1
        outlier_bounds = {
            "lower": q1 - 1.5 * iqr,
            "upper": q3 + 1.5 * iqr,
        }
        outliers = [
            v
            for v in values
            if v < outlier_bounds["lower"] or v > outlier_bounds["upper"]
        ]

        return {
            # Central tendency
            "mean": mean_val,
            "median": median_val,
            "mode": (
                statistics.mode(values) if len(set(values)) < len(values) else None
            ),
            # Variability
            "std_dev": std_dev,
            "variance": variance,
            "coefficient_of_variation": cv,
            "range": range_val,
            "min": min_val,
            "max": max_val,
            "iqr": iqr,
            # Percentiles
            "percentiles": percentiles,
            # Confidence intervals
            "confidence_intervals": confidence_intervals,
            # Sample statistics
            "sample_size": n,
            "degrees_of_freedom": n - 1,
            "standard_error": se_mean,
            # Distribution analysis
            "normality_test": normality_test,
            "outliers": {
                "count": len(outliers),
                "values": outliers,
                "bounds": outlier_bounds,
            },
        }

    def format_industry_summary(self, analysis: Dict[str, Any]) -> str:
        """Format results in industry-standard reporting format."""
        summary_lines: List[str] = []
        summary_lines.append("SIMULATION REPLICATION ANALYSIS SUMMARY")
        summary_lines.append("=" * 50)

        repl_summary = analysis.get("_replication_summary", {})
        summary_lines.append(
            f"Total Replications: {repl_summary.get('total_replications', 'N/A')}"
        )
        summary_lines.append(
            f"Successful Runs: {repl_summary.get('successful_replications', 'N/A')}"
        )
        summary_lines.append("")

        # Format each metric
        for metric_name, metric_data in analysis.items():
            if metric_name.startswith("_"):
                continue

            summary_lines.append(f"{metric_name.replace('_', ' ').title()}:")

            # Industry standard format: Mean +/- Half-Width (CI%) [n=replications]
            # Any stat may be None here (analyze_replications maps inf/nan ->
            # None); `_num`/`_pct` degrade those to "n/a" instead of crashing.
            mean = metric_data.get("mean", 0)
            ci_95 = metric_data.get("confidence_intervals", {}).get("ci_95", {})
            half_width = ci_95.get("half_width", 0)
            sample_size = metric_data.get("sample_size", 0)

            if isinstance(half_width, (int, float)) and half_width > 0:
                summary_lines.append(
                    f"  {_num(mean)} +/- {_num(half_width)} (95%) [n={sample_size}]"
                )
            else:
                summary_lines.append(f"  {_num(mean)} [n={sample_size}]")

            # Additional statistics
            std_dev = metric_data.get("std_dev", 0)
            cv = metric_data.get("coefficient_of_variation", 0)
            min_val = metric_data.get("min", 0)
            max_val = metric_data.get("max", 0)

            summary_lines.append(f"  Std Dev: {_num(std_dev)}, CV: {_pct(cv)}")
            summary_lines.append(f"  Range: [{_num(min_val)}, {_num(max_val)}]")

            # Relative precision (None once inf was sanitized away -> skip it)
            rel_precision = ci_95.get("relative_precision", 0)
            if isinstance(rel_precision, (int, float)):
                summary_lines.append(
                    f"  Relative Precision: +/-{_pct(rel_precision)}"
                )

            summary_lines.append("")

        return "\n".join(summary_lines)


def create_replication_analyzer() -> ReplicationAnalyzer:
    """Factory function to create a replication analyzer with default settings."""
    return ReplicationAnalyzer()
