"""数据管道接口：触发跨模块计算并查询产物。"""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.schemas import ActionResult
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
    run_timeliness_check,
)
from app.services.timeliness import list_observations, record_arrival

router = APIRouter(prefix="/api/pipeline", tags=["数据管道"])


@router.post("/run-all", response_model=ActionResult)
def trigger_all() -> ActionResult:
    """顺序执行全部 8 条跨模块数据管道。"""
    summary = run_all_pipelines()
    return ActionResult(
        ok=True,
        message="全部管道已执行",
        entry={"pipelines": list(summary.keys()), "count": len(summary)},
    )


@router.post("/metric-agg", response_model=ActionResult)
def trigger_metric_agg(
    metric_code: str | None = Query(default=None, description="指标编号，如 METR-GMV"),
) -> ActionResult:
    result = run_metric_agg_pipeline(metric_code)
    return ActionResult(ok=True, message="指标聚合管道已执行", entry=result)


@router.post("/forecast", response_model=ActionResult)
def trigger_forecast(
    horizon: int = Query(default=7, ge=1, le=30, description="预测天数"),
) -> ActionResult:
    result = run_forecast_pipeline(horizon)
    return ActionResult(ok=True, message="预测管道已执行", entry=result)


@router.post("/cube", response_model=ActionResult)
def trigger_cube(
    dim_codes: list[str] | None = Query(default=None, description="维度编号列表"),
) -> ActionResult:
    result = run_cube_pipeline(dim_codes)
    return ActionResult(ok=True, message="立方体构建管道已执行", entry=result)


@router.post("/cohort-extract", response_model=ActionResult)
def trigger_cohort_extract() -> ActionResult:
    result = run_cohort_extract_pipeline()
    return ActionResult(ok=True, message="分群提取管道已执行", entry=result)


@router.post("/experiment-stat", response_model=ActionResult)
def trigger_experiment_stat(
    experiment_code: str | None = Query(default=None, description="实验编号"),
) -> ActionResult:
    result = run_experiment_stat_pipeline(experiment_code)
    return ActionResult(ok=True, message="实验统计管道已执行", entry=result)


@router.post("/quality-scan", response_model=ActionResult)
def trigger_quality_scan() -> ActionResult:
    result = run_quality_scan()
    return ActionResult(ok=True, message="质量扫描管道已执行", entry=result)


@router.post("/timeliness-check", response_model=ActionResult)
def trigger_timeliness_check(
    business_day: str | None = Query(default=None, description="目标营业日，如 2026-08-30；缺省取观测数据里最近的营业日"),
) -> ActionResult:
    """按各来源应交付营业日校验到达覆盖，产出缺到/已到/未知清单。"""
    try:
        result = run_timeliness_check(business_day)
    except ValueError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message="时效检查管道已执行", entry=result)


@router.get("/timeliness-observations")
def get_timeliness_observations() -> dict[str, object]:
    """查询全部时效到达记录，上一营业日的值也可追溯。"""
    return {"items": list_observations()}


@router.post("/timeliness-arrivals", response_model=ActionResult)
def create_timeliness_arrival(payload: dict[str, object]) -> ActionResult:
    """补录一条到达记录：必须补到正确的营业日，重复或未来营业日会被拦下。"""
    entry, error = record_arrival(payload)
    if error:
        return ActionResult(ok=False, message=error)
    # 补录后刷新该营业日的时效检查产物，让报表取到最新结论
    run_timeliness_check(str(entry["业务日期"]))
    return ActionResult(ok=True, message="到达记录已补录，时效检查已刷新", entry=entry)


@router.post("/attribution-compare", response_model=ActionResult)
def trigger_attribution_compare(
    link_code: str | None = Query(default=None, description="链路编号"),
) -> ActionResult:
    result = run_attribution_compare_pipeline(link_code)
    return ActionResult(ok=True, message="归因对比管道已执行", entry=result)


@router.get("/results")
def get_results() -> dict[str, object]:
    """查询全部管道产物。"""
    return {"items": list_pipeline_results()}
