"""报表生成业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services.report_builder import build_report_version
from app.store import store

MODULE = "report_gen"
LIST_FIELDS = ["报表编号", "报表类型", "数据范围", "包含图表", "生成周期", "输出格式", "分发渠道", "下次生成"]
REQUIRED_FIELDS = ["报表编号", "报表类型", "数据范围"]
STATUS_ORDER = ["草稿", "待生成", "已生成", "已停用"]
ACTION_RULES = {"提交生成": "待生成", "确认分发": "已生成", "停用报表": "已停用"}
NEGATIVE_ACTIONS = ["停用报表"]


class ReportGenService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("报表编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"分析报表 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于报表生成可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        message = f"分析报表已{action}"
        if action == "提交生成" and entry.get("报表类型") == "日报":
            # 日报提交生成时关联时效检查产物，按各来源覆盖选择本期章节
            version = build_report_version(entry)
            missing = version["缺失来源"]
            if missing:
                message += (
                    f"；本期业务日 {version['业务日期']}，缺到来源：{'、'.join(missing)}，"
                    "跨来源合计待数据齐备"
                )
            else:
                message += (
                    f"；本期业务日 {version['业务日期']}，章节齐备，"
                    f"合计金额 {version['合计金额']}"
                )
        return entry, message
