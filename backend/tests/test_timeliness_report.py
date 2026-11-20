"""时效检查与日报生成：营业日覆盖、缺到/未知语义、补录后重生成的版本对比。

场景：报告营业日 2026-08-30，预测输入准时到达，切片数据当日无记录（迟到）；
补录到正确营业日后再次提交生成，新旧版本的业务日与金额必须可追溯对比。
"""
from __future__ import annotations

import pytest

from app.services.pipeline_orchestrator import (
    list_pipeline_results,
    run_quality_scan,
    run_timeliness_check,
)
from app.services.report_gen import ReportGenService
from app.services.timeliness import (
    evaluate_coverage,
    list_observations,
    record_arrival,
)

BUSINESS_DAY = "2026-08-30"
PREV_DAY = "2026-08-29"
FORECAST_AMOUNT = 1345000.0
SLICE_PREV_AMOUNT = 375000.0
SLICE_BACKFILL_AMOUNT = 382000.0


def _coverage_by_source(result: dict) -> dict[str, dict]:
    return {item["来源"]: item for item in result["来源覆盖"]}


class TestTimelinessCheck:
    """从监控规则对应的时效观测数据校验营业日覆盖"""

    def test_forecast_arrived_slice_missing(self):
        result = run_timeliness_check(BUSINESS_DAY)
        coverage = _coverage_by_source(result)
        assert coverage["forecast_report"]["状态"] == "已到"
        assert coverage["forecast_report"]["金额"] == FORECAST_AMOUNT
        assert coverage["segmentation_slice"]["状态"] == "缺到"

    def test_issue_list_distinguishes_sources(self):
        result = run_timeliness_check(BUSINESS_DAY)
        assert result["已到来源"] == ["forecast_report"]
        assert result["缺到来源"] == ["segmentation_slice"]
        assert result["未知来源"] == ["experiment_daily"]
        assert [issue["来源"] for issue in result["问题清单"]] == ["segmentation_slice"]
        assert result["问题清单"][0]["问题类型"] == "缺到"
        assert result["问题清单"][0]["应交付营业日"] == BUSINESS_DAY

    def test_missing_keeps_unknown_amount_not_zero(self):
        # 没有当日到达记录时金额与记录数保持未知，不能记成零事件或已完成
        result = run_timeliness_check(BUSINESS_DAY)
        coverage = _coverage_by_source(result)
        slice_cov = coverage["segmentation_slice"]
        assert slice_cov["金额"] is None
        assert slice_cov["记录数"] is None
        assert slice_cov["状态"] not in ("已完成", "已到")
        unknown_cov = coverage["experiment_daily"]
        assert unknown_cov["状态"] == "未知"
        assert unknown_cov["金额"] is None
        assert unknown_cov["记录数"] is None

    def test_previous_day_traceable_but_not_current(self):
        # 上一营业日的值可追溯，但不能冒充当前营业日输入
        result = run_timeliness_check(BUSINESS_DAY)
        coverage = _coverage_by_source(result)
        previous = coverage["segmentation_slice"]["上一营业日"]
        assert previous is not None
        assert previous["业务日期"] == PREV_DAY
        assert previous["金额"] == SLICE_PREV_AMOUNT
        # 当前营业日依旧缺到，上一营业日的值没有被当成当日输入
        assert coverage["segmentation_slice"]["状态"] == "缺到"
        assert coverage["segmentation_slice"]["金额"] is None

    def test_default_day_uses_latest_observation(self):
        result = run_timeliness_check()
        assert result["检查营业日"] == BUSINESS_DAY

    def test_invalid_day_rejected(self):
        with pytest.raises(ValueError):
            evaluate_coverage("2026/08/30")

    def test_pipeline_result_written(self):
        run_timeliness_check(BUSINESS_DAY)
        ids = [r["pipeline_id"] for r in list_pipeline_results()]
        assert "timeliness_check" in ids

    def test_quality_scan_includes_timeliness(self):
        result = run_quality_scan()
        assert result["timeliness_checked_day"] == BUSINESS_DAY
        assert any(issue["来源"] == "segmentation_slice" for issue in result["timeliness_issues"])
        assert "experiment_daily" in result["timeliness_unknown_sources"]


class TestRecordArrival:
    """补录到达记录：必须补到正确的营业日"""

    def test_backfill_makes_slice_arrived(self):
        entry, error = record_arrival({
            "来源": "segmentation_slice",
            "业务日期": BUSINESS_DAY,
            "到达时间": "2026-08-31 02:10",
            "记录数": 5,
            "金额": SLICE_BACKFILL_AMOUNT,
        })
        assert error == ""
        assert entry["业务日期"] == BUSINESS_DAY
        coverage = _coverage_by_source(evaluate_coverage(BUSINESS_DAY))
        assert coverage["segmentation_slice"]["状态"] == "已到"
        assert coverage["segmentation_slice"]["金额"] == SLICE_BACKFILL_AMOUNT

    def test_duplicate_arrival_rejected(self):
        entry, error = record_arrival({
            "来源": "forecast_report",
            "业务日期": BUSINESS_DAY,
            "金额": 1.0,
        })
        assert entry is None
        assert "已有到达记录" in error

    def test_future_day_rejected(self):
        entry, error = record_arrival({
            "来源": "segmentation_slice",
            "业务日期": "2999-01-01",
            "金额": 1.0,
        })
        assert entry is None
        assert "未来营业日" in error

    def test_missing_fields_rejected(self):
        entry, error = record_arrival({"来源": "segmentation_slice"})
        assert entry is None
        assert "业务日期" in error and "金额" in error

    def test_observations_traceable(self):
        record_arrival({
            "来源": "segmentation_slice",
            "业务日期": BUSINESS_DAY,
            "金额": SLICE_BACKFILL_AMOUNT,
        })
        days = [obs["业务日期"] for obs in list_observations() if obs["来源"] == "segmentation_slice"]
        # 上一营业日记录仍在，新补录的当日记录也可追溯
        assert PREV_DAY in days
        assert BUSINESS_DAY in days


class TestDailyReportGeneration:
    """报表生成关联时效检查产物，按覆盖选择章节并留存版本"""

    def _service(self) -> ReportGenService:
        return ReportGenService()

    def test_first_submit_generates_partial_version(self):
        service = self._service()
        entry, message = service.run_action(1, "提交生成")
        assert entry is not None
        versions = entry["报告版本"]
        assert len(versions) == 1
        version = versions[0]
        assert version["版本"] == "v1"
        assert version["业务日期"] == BUSINESS_DAY
        sections = {s["章节"]: s for s in version["章节"]}
        # 预测章节按自身覆盖正常生成
        assert sections["预测章节"]["状态"] == "已生成"
        assert sections["预测章节"]["金额"] == FORECAST_AMOUNT
        # 切片迟到：章节保留为缺到，金额未知而不是零
        assert sections["切片章节"]["状态"] == "缺到"
        assert sections["切片章节"]["金额"] is None
        assert "可追溯" in sections["切片章节"]["说明"]
        # 读者能看到本期还缺什么
        assert version["缺失来源"] == ["segmentation_slice"]
        # 跨来源合计只能用同一营业日的完整数据：切片未到，合计保持未知
        assert version["合计金额"] is None
        assert version["合计状态"] == "待数据齐备"
        assert "segmentation_slice" in message

    def test_cross_source_total_needs_same_complete_day(self):
        # 上一营业日切片值存在，但不能与当日预测拼出合计
        service = self._service()
        entry, _ = service.run_action(1, "提交生成")
        version = entry["报告版本"][-1]
        assert version["合计金额"] is None

    def test_resubmit_after_backfill_produces_complete_version(self):
        service = self._service()
        entry, _ = service.run_action(1, "提交生成")
        old_version = entry["报告版本"][-1]

        # 补到正确的营业日后再次提交生成
        _, error = record_arrival({
            "来源": "segmentation_slice",
            "业务日期": BUSINESS_DAY,
            "到达时间": "2026-08-31 02:10",
            "记录数": 5,
            "金额": SLICE_BACKFILL_AMOUNT,
        })
        assert error == ""
        entry, message = service.run_action(1, "提交生成")

        versions = entry["报告版本"]
        assert len(versions) == 2
        new_version = versions[-1]
        assert new_version["版本"] == "v2"
        # 新旧版本是同一业务日，金额口径可对比
        assert new_version["业务日期"] == old_version["业务日期"] == BUSINESS_DAY
        assert new_version["缺失来源"] == []
        assert new_version["合计状态"] == "完整"
        assert new_version["合计金额"] == round(FORECAST_AMOUNT + SLICE_BACKFILL_AMOUNT, 2)
        sections = {s["章节"]: s for s in new_version["章节"]}
        assert sections["切片章节"]["状态"] == "已生成"
        assert sections["切片章节"]["金额"] == SLICE_BACKFILL_AMOUNT
        # 旧版本保留原貌：缺到、合计未知，可追溯
        assert old_version["合计金额"] is None
        assert old_version["缺失来源"] == ["segmentation_slice"]
        assert new_version["合计金额"] != old_version["合计金额"]

    def test_non_daily_report_skips_section_building(self):
        service = self._service()
        entry, message = service.run_action(3, "提交生成")  # REPO-EXPERIMENT 实验报告
        assert entry is not None
        assert "报告版本" not in entry
        assert message == "分析报表已提交生成"
