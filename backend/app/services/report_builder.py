"""日报生成：关联质量检查产物，按时效数据选择本期可以生成的章节。

规则：
- 日报按各来源覆盖出独立章节；数据未到的来源保留缺失/未知章节，
  让读者知道本期还缺什么，而不是静默省略；
- 缺到或未知来源的金额保持未知（None），不记成零事件；
- 上一营业日的值只在说明里可追溯，不能冒充当前营业日输入；
- 跨来源合计只使用同一营业日的完整数据：任一来源未到齐备，合计保持未知。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.services.pipeline_orchestrator import run_timeliness_check
from app.services.timeliness import STATUS_ARRIVED, STATUS_MISSING

# 日报的两份输入来源：预测输入与切片数据
DAILY_REPORT_INPUTS: list[dict[str, str]] = [
    {"来源": "forecast_report", "章节": "预测章节"},
    {"来源": "segmentation_slice", "章节": "切片章节"},
]

_VERSIONS_FIELD = "报告版本"


def _report_business_day(entry: dict[str, Any]) -> str:
    """从数据范围里取最后一个日期作为本期营业日，如 2026-08-01至2026-08-30。"""
    days = re.findall(r"\d{4}-\d{2}-\d{2}", str(entry.get("数据范围") or ""))
    if not days:
        raise ValueError(f"报表 {entry.get('报表编号')} 的数据范围里没有可识别的营业日")
    return days[-1]


def build_report_version(entry: dict[str, Any]) -> dict[str, Any]:
    """为日报构建一个生成版本：先刷新时效检查产物，再按覆盖选择章节。"""
    business_day = _report_business_day(entry)
    # 关联质量检查产物：每次生成都重新校验该营业日的时效覆盖，
    # 保证补录到达后再次提交能拿到最新结论
    timeliness = run_timeliness_check(business_day)
    coverage = {item["来源"]: item for item in timeliness["来源覆盖"]}

    sections: list[dict[str, Any]] = []
    for spec in DAILY_REPORT_INPUTS:
        source = spec["来源"]
        cov = coverage.get(source)
        section: dict[str, Any] = {
            "章节": spec["章节"],
            "来源": source,
            "业务日期": business_day,
        }
        if cov is None:
            section["状态"] = "未知"
            section["金额"] = None
            section["说明"] = "该来源不在时效监控范围内，覆盖状态未知"
        elif cov["状态"] == STATUS_ARRIVED:
            section["状态"] = "已生成"
            section["金额"] = cov["金额"]
            section["说明"] = f"{source} 已于 {cov['到达时间']} 准时到达"
        elif cov["状态"] == STATUS_MISSING:
            section["状态"] = "缺到"
            section["金额"] = None
            previous = cov.get("上一营业日")
            note = f"{source} 未在应交付营业日 {business_day} 到达"
            if previous:
                note += (
                    f"；上一营业日 {previous['业务日期']} 的值 {previous['金额']} 可追溯，"
                    "但不能冒充本期输入"
                )
            section["说明"] = note
        else:
            section["状态"] = "未知"
            section["金额"] = None
            section["说明"] = f"{source} 没有任何到达记录，本期覆盖未知"
        sections.append(section)

    # 跨来源合计：只允许同一营业日且全部来源准时到达的完整数据
    complete = all(
        section["状态"] == "已生成" and section["业务日期"] == business_day
        for section in sections
    )
    total_amount = round(sum(float(section["金额"]) for section in sections), 2) if complete else None
    missing_sources = [section["来源"] for section in sections if section["状态"] != "已生成"]

    versions = entry.setdefault(_VERSIONS_FIELD, [])
    version = {
        "版本": f"v{len(versions) + 1}",
        "业务日期": business_day,
        "生成时间": datetime.now().isoformat(),
        "章节": sections,
        "缺失来源": missing_sources,
        "合计金额": total_amount,
        "合计状态": "完整" if complete else "待数据齐备",
        "时效检查时间": timeliness["检查时间"],
    }
    versions.append(version)
    return version
