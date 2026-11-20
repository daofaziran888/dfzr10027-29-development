"""时效检查业务规则：按各来源应交付的营业日校验到达覆盖。

判定口径（只按各来源应交付的营业日判断）：
- 已到：该来源在目标营业日有到达记录；
- 缺到：该来源有历史到达记录，但目标营业日已过交付期仍无当日记录；
- 未知：该来源没有任何到达记录，或目标营业日尚未到交付期——
  保留未知状态，不记成零事件，也不记成已完成。

上一营业日的值只用于追溯，不能冒充当前营业日的输入。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.store import store

OBSERVATIONS_TABLE = "_timeliness_observations"
RULE_MODULE = "quality_mon"

STATUS_ARRIVED = "已到"
STATUS_MISSING = "缺到"
STATUS_UNKNOWN = "未知"

REQUIRED_ARRIVAL_FIELDS = ["来源", "业务日期", "金额"]


def _parse_day(value: Any) -> date | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def list_observations() -> list[dict[str, Any]]:
    """全部时效到达记录，按来源与营业日排序，供追溯查询。"""
    rows = [dict(row) for row in store.rows(OBSERVATIONS_TABLE)]
    return sorted(rows, key=lambda row: (str(row.get("来源", "")), str(row.get("业务日期", ""))))


def latest_observation_day() -> str | None:
    """观测数据里最近的一个营业日，作为时效检查的默认目标营业日。"""
    days = [str(row.get("业务日期")) for row in store.rows(OBSERVATIONS_TABLE) if row.get("业务日期")]
    return max(days) if days else None


def _timeliness_rules() -> list[dict[str, Any]]:
    """已启用的时效性监控规则；暂停或废弃的规则不参与营业日覆盖判定。"""
    return [
        rule
        for rule in store.rows(RULE_MODULE)
        if rule.get("校验类型") == "时效性" and rule.get("status") == "已启用"
    ]


def evaluate_coverage(business_day: str) -> dict[str, Any]:
    """校验各监控来源在目标营业日的到达覆盖，生成按来源区分的清单。"""
    day = _parse_day(business_day)
    if day is None:
        raise ValueError(f"营业日「{business_day}」不是合法的 YYYY-MM-DD 日期")
    business_day = day.isoformat()
    today = date.today()

    observations = store.rows(OBSERVATIONS_TABLE)
    by_source: dict[str, list[dict[str, Any]]] = {}
    for obs in observations:
        by_source.setdefault(str(obs.get("来源")), []).append(obs)

    coverage: list[dict[str, Any]] = []
    for rule in _timeliness_rules():
        source = str(rule.get("监控对象"))
        arrivals = by_source.get(source, [])
        current = next((obs for obs in arrivals if str(obs.get("业务日期")) == business_day), None)
        earlier = [obs for obs in arrivals if str(obs.get("业务日期", "")) < business_day]
        previous = max(earlier, key=lambda obs: str(obs.get("业务日期"))) if earlier else None

        if current is not None:
            status = STATUS_ARRIVED
        elif day > today:
            # 应交付营业日还没到，无法判定，保留未知
            status = STATUS_UNKNOWN
        elif not arrivals:
            # 没有任何到达记录：保留未知，不记成零事件或已完成
            status = STATUS_UNKNOWN
        else:
            status = STATUS_MISSING

        coverage.append({
            "来源": source,
            "规则编号": rule.get("规则编号"),
            "应交付营业日": business_day,
            "状态": status,
            # 缺到/未知时金额与记录数保持 None，绝不记成 0
            "到达时间": current.get("到达时间") if current else None,
            "记录数": current.get("记录数") if current else None,
            "金额": current.get("金额") if current else None,
            # 上一营业日的值可追溯，但只是历史快照，不能冒充当前输入
            "上一营业日": (
                {
                    "业务日期": previous.get("业务日期"),
                    "到达时间": previous.get("到达时间"),
                    "金额": previous.get("金额"),
                }
                if previous
                else None
            ),
        })

    issues = [
        {
            "来源": item["来源"],
            "规则编号": item["规则编号"],
            "问题类型": STATUS_MISSING,
            "应交付营业日": business_day,
            "说明": f"{item['来源']} 未在应交付营业日 {business_day} 到达",
        }
        for item in coverage
        if item["状态"] == STATUS_MISSING
    ]

    return {
        "检查营业日": business_day,
        "检查时间": datetime.now().isoformat(),
        "来源覆盖": coverage,
        "问题清单": issues,
        "已到来源": [item["来源"] for item in coverage if item["状态"] == STATUS_ARRIVED],
        "缺到来源": [item["来源"] for item in coverage if item["状态"] == STATUS_MISSING],
        "未知来源": [item["来源"] for item in coverage if item["状态"] == STATUS_UNKNOWN],
    }


def record_arrival(values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """补录一条到达记录：必须补到正确的营业日，且不允许重复或冒充。"""
    missing = [field for field in REQUIRED_ARRIVAL_FIELDS if not str(values.get(field) or "").strip()]
    if missing:
        return None, f"缺少必填字段：{'、'.join(missing)}"

    source = str(values["来源"]).strip()
    day = _parse_day(values["业务日期"])
    if day is None:
        return None, f"业务日期「{values['业务日期']}」不是合法的 YYYY-MM-DD 日期"
    if day > date.today():
        return None, f"业务日期 {day.isoformat()} 尚未到来，不能补录未来营业日"

    try:
        amount = float(values["金额"])
    except (TypeError, ValueError):
        return None, f"金额「{values['金额']}」不是合法数值"

    table = store.rows(OBSERVATIONS_TABLE)
    for obs in table:
        if str(obs.get("来源")) == source and str(obs.get("业务日期")) == day.isoformat():
            return None, f"{source} 在营业日 {day.isoformat()} 已有到达记录，不能重复补录"

    record_count: int | None = None
    if str(values.get("记录数") or "").strip():
        try:
            record_count = int(values["记录数"])
        except (TypeError, ValueError):
            return None, f"记录数「{values['记录数']}」不是合法整数"

    entry = {
        "来源": source,
        "业务日期": day.isoformat(),
        "到达时间": str(values.get("到达时间") or datetime.now().strftime("%Y-%m-%d %H:%M")),
        "记录数": record_count,
        "金额": amount,
    }
    table.append(entry)
    return entry, ""
