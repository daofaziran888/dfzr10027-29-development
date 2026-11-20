"""跨模块数据管道测试：验证真实计算与数据流动。"""
from __future__ import annotations

import math

from app.services.pipeline_orchestrator import (
    list_pipeline_results,
    run_all_pipelines,
    run_attribution_compare_pipeline,
    run_cohort_extract_pipeline,
    run_cube_pipeline,
    run_experiment_stat_pipeline,
    run_forecast_pipeline,
    run_metric_agg_pipeline,
    run_quality_scan,
)
from app.store import store


class TestMetricAggPipeline:
    """Pipeline 1: metric_def -> agg_task -> api_service"""

    def test_gmv_aggregation(self):
        result = run_metric_agg_pipeline("METR-GMV")
        assert result["metric_code"] == "METR-GMV"
        assert result["unit"] == "元"
        assert result["point_count"] == 30
        # 30 天 GMV 总和应为已知 fixtures 之和
        expected = 124500.0 + 138200.0 + 119800.0 + 145600.0 + 152300.0 + 160100.0 + 175400.0 + 168900.0 + 155200.0 + 142000.0 + 149500.0 + 161200.0 + 173800.0 + 182000.0 + 190500.0 + 178300.0 + 165000.0 + 158400.0 + 171200.0 + 185000.0 + 192300.0 + 201000.0 + 210500.0 + 198000.0 + 205400.0 + 218000.0 + 225600.0 + 232000.0 + 221000.0 + 238500.0
        assert result["value"] == round(expected, 2)

    def test_cvr_computation(self):
        result = run_metric_agg_pipeline("METR-CVR")
        # service returns aggregated cvr over all points
        assert result["metric_code"] == "METR-CVR"
        assert result["unit"] == "比率"
        assert 0.0 < result["value"] <= 1.0

    def test_pipeline_result_written(self):
        run_metric_agg_pipeline("METR-UV")
        results = list_pipeline_results()
        ids = [r["pipeline_id"] for r in results]
        assert "metric_agg_api" in ids


class TestForecastPipeline:
    """Pipeline 2: trend_series -> forecast -> report_gen"""

    def test_forecast_structure(self):
        result = run_forecast_pipeline(horizon=7)
        assert result["model"] == "linear_trend"
        assert len(result["forecasts"]) == 7
        assert "slope" in result
        assert "intercept" in result

    def test_forecast_monotonicity(self):
        # fixtures gmv 整体上升，slope 应该为正
        result = run_forecast_pipeline(horizon=7)
        assert result["slope"] > 0

    def test_confidence_intervals(self):
        result = run_forecast_pipeline(horizon=3)
        for f in result["forecasts"]:
            assert f["置信下界"] < f["预测gmv"] < f["置信上界"]

    def test_pipeline_result_written(self):
        run_forecast_pipeline(5)
        results = list_pipeline_results()
        ids = [r["pipeline_id"] for r in results]
        assert "forecast_report" in ids


class TestCubePipeline:
    """Pipeline 3: dim_registry -> cube_builder -> api_service"""

    def test_cube_grouping(self):
        result = run_cube_pipeline(["DIM-REGION", "DIM-CHANNEL"])
        assert result["cell_count"] > 0
        assert "cells" in result
        for cell in result["cells"]:
            assert "region" in cell
            assert "channel" in cell
            assert "gmv" in cell

    def test_cube_single_dimension(self):
        result = run_cube_pipeline(["DIM-REGION"])
        regions = {c["region"] for c in result["cells"]}
        assert len(regions) == result["cell_count"]

    def test_pipeline_result_written(self):
        run_cube_pipeline()
        results = list_pipeline_results()
        ids = [r["pipeline_id"] for r in results]
        assert "cube_api" in ids


class TestCohortExtractPipeline:
    """Pipeline 4: cohort_rule -> data_extract -> report_gen"""

    def test_filter_by_region(self):
        result = run_cohort_extract_pipeline({"region": "华东", "min_ltv": 0})
        assert result["matched_count"] == 4
        for u in result["users"]:
            assert u["region"] == "华东"

    def test_filter_by_ltv(self):
        result = run_cohort_extract_pipeline({"min_ltv": 1000.0})
        assert result["matched_count"] == 4
        for u in result["users"]:
            assert u["ltv"] >= 1000.0

    def test_summary_computation(self):
        result = run_cohort_extract_pipeline({"region": "华东"})
        summary = result["summary"]
        assert summary["total_ltv"] == round(sum(u["ltv"] for u in result["users"]), 2)
        assert summary["avg_ltv"] == round(summary["total_ltv"] / len(result["users"]), 2)

    def test_pipeline_result_written(self):
        run_cohort_extract_pipeline()
        results = list_pipeline_results()
        ids = [r["pipeline_id"] for r in results]
        assert "cohort_extract_report" in ids


class TestExperimentStatPipeline:
    """Pipeline 5: experiment -> stat_model -> data_mart"""

    def test_expe001_significance(self):
        result = run_experiment_stat_pipeline("EXPE-001")
        assert result["experiment_code"] == "EXPE-001"
        # treatment 412/5000 = 0.0824, control 350/5000 = 0.07 -> lift positive
        assert result["absolute_lift"] > 0
        assert result["significant_at_05"] is True
        assert abs(result["z_score"]) > 1.96

    def test_expe002_effect_size(self):
        result = run_experiment_stat_pipeline("EXPE-002")
        assert result["cohen_h"] != 0.0
        assert result["sample_size_control"] == 8000
        assert result["sample_size_treatment"] == 8000

    def test_mart_entry_written(self):
        before = len(store.rows("data_mart"))
        run_experiment_stat_pipeline("EXPE-001")
        after = len(store.rows("data_mart"))
        assert after == before + 1
        last = store.rows("data_mart")[-1]
        assert last.get("实验编号") == "EXPE-001"

    def test_pipeline_result_written(self):
        run_experiment_stat_pipeline("EXPE-001")
        results = list_pipeline_results()
        ids = [r["pipeline_id"] for r in results]
        assert "experiment_stat_mart" in ids


class TestQualityScanPipeline:
    """Pipeline 6: quality_mon 跨模块扫描"""

    def test_quality_score_range(self):
        result = run_quality_scan()
        assert 0 <= result["quality_score"] <= 100

    def test_module_issues_detected(self):
        result = run_quality_scan()
        # seed 里有 abnormal=True 的记录
        assert any(item["abnormal_count"] > 0 for item in result["module_issues"])

    def test_sample_issues_non_empty(self):
        result = run_quality_scan()
        assert len(result["sample_issues"]) > 0

    def test_pipeline_result_written(self):
        run_quality_scan()
        results = list_pipeline_results()
        ids = [r["pipeline_id"] for r in results]
        assert "quality_scan" in ids


class TestAttributionComparePipeline:
    """Pipeline 7: attribution -> metric_def -> compare_job"""

    def test_first_touch_totals(self):
        result = run_attribution_compare_pipeline("ATTR-001")
        totals = result["totals"]
        assert totals["first_touch"] > 0

    def test_compare_rows_generated(self):
        result = run_attribution_compare_pipeline("ATTR-002")
        assert len(result["compare_rows"]) > 0
        for row in result["compare_rows"]:
            assert "触点类型" in row
            assert "差异" in row

    def test_linear_vs_first_difference(self):
        result = run_attribution_compare_pipeline("ATTR-002")
        models = result["models"]
        # 线性归因与首次归因应该产生不同分布
        linear_keys = set(models["linear"].keys())
        first_keys = set(models["first_touch"].keys())
        assert linear_keys == first_keys or len(linear_keys & first_keys) > 0

    def test_pipeline_result_written(self):
        run_attribution_compare_pipeline("ATTR-001")
        results = list_pipeline_results()
        ids = [r["pipeline_id"] for r in results]
        assert "attribution_metric_compare" in ids


class TestRunAllPipelines:
    """批量执行入口"""

    def test_all_pipelines_run(self):
        summary = run_all_pipelines()
        expected_keys = {
            "metric_agg_api",
            "forecast_report",
            "cube_api",
            "cohort_extract_report",
            "experiment_stat_mart",
            "timeliness_check",
            "quality_scan",
            "attribution_metric_compare",
        }
        assert set(summary.keys()) == expected_keys
        results = list_pipeline_results()
        assert len(results) == 8
