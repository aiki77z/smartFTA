from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple


_STRUCTURAL_REPAIRABLE_CODES = {
    "BROKEN_LINK",
    "DUPLICATE_NODE_ID",
    "INVALID_LINK",
    "INVALID_NODE",
    "MISSING_EVENT",
    "MISSING_EVENT_FIELD",
    "MISSING_LINK_ENDPOINT",
    "MISSING_NODE_ID",
    "MISSING_NODE_NAME",
    "MISSING_NODE_TYPE",
    "NO_DOCUMENTS",
    "NO_ERROR_LEVEL",
}

_NON_REPAIRABLE_CODES = {
    "INVALID_PAYLOAD",
    "INVALID_TREE_SHAPE",
    "VALIDATOR_SERVICE_FAILED",
    "VALIDATOR_SERVICE_MISSING",
}


def _as_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def _as_severity(level: Any) -> str:
    value = str(level or "info").strip().lower()
    if value == "error":
        return "error"
    if value == "warning":
        return "warning"
    return "info"


def _extract_node_ids(raw_issue: Dict[str, Any]) -> List[str]:
    node_ids = []
    for key in ("node_ids", "node_id", "nodes"):
        for value in _as_list(raw_issue.get(key)):
            if isinstance(value, dict):
                value = value.get("id") or value.get("node_id")
            text = str(value or "").strip()
            if text and text not in node_ids:
                node_ids.append(text)
    return node_ids


def _extract_link_ids(raw_issue: Dict[str, Any]) -> List[str]:
    link_ids = []
    for key in ("link_ids", "link_id", "edge_ids", "edge_id"):
        for value in _as_list(raw_issue.get(key)):
            text = str(value or "").strip()
            if text and text not in link_ids:
                link_ids.append(text)
    return link_ids


def is_issue_repairable(issue_code: str, severity: str, raw_issue: Optional[Dict[str, Any]] = None) -> bool:
    code = str(issue_code or "").strip().upper()
    if code in _NON_REPAIRABLE_CODES:
        return False
    if str(severity or "").lower() in {"warning", "info"}:
        return True
    if code in _STRUCTURAL_REPAIRABLE_CODES:
        return True
    if raw_issue and "repairable" in raw_issue:
        return bool(raw_issue.get("repairable"))
    return False


def normalize_validation_issue(raw_issue: Dict[str, Any], index: int = 0) -> Dict[str, Any]:
    raw = raw_issue if isinstance(raw_issue, dict) else {}
    issue_code = str(raw.get("issue_code") or raw.get("code") or "VALIDATION_ISSUE").strip().upper()
    severity = _as_severity(raw.get("severity") or raw.get("level"))
    node_ids = _extract_node_ids(raw)
    link_ids = _extract_link_ids(raw)
    message = str(raw.get("message") or raw.get("detail") or issue_code).strip()
    normalized = {
        "issue_id": str(raw.get("issue_id") or f"issue_{index + 1:03d}"),
        "issue_code": issue_code,
        "severity": severity,
        "node_ids": node_ids,
        "link_ids": link_ids,
        "message": message,
        "evidence_refs": _as_list(raw.get("evidence_refs") or raw.get("evidence") or raw.get("documents")),
        "repairable": is_issue_repairable(issue_code, severity, raw),
        "source": str(raw.get("source") or "validator"),
    }
    if raw.get("node_name"):
        normalized["node_names"] = [str(raw.get("node_name"))]
    elif raw.get("node_names"):
        normalized["node_names"] = [str(item) for item in _as_list(raw.get("node_names")) if str(item or "").strip()]
    if raw.get("raw"):
        normalized["raw"] = raw.get("raw")
    return normalized


def _count_by_severity(issues: Iterable[Dict[str, Any]]) -> Tuple[int, int, int]:
    error_count = 0
    warning_count = 0
    info_count = 0
    for issue in issues:
        severity = issue.get("severity")
        if severity == "error":
            error_count += 1
        elif severity == "warning":
            warning_count += 1
        else:
            info_count += 1
    return error_count, warning_count, info_count


def normalize_validation_report(
    raw_report: Dict[str, Any],
    *,
    scope_key: str = "",
    draft_artifact_id: Optional[str] = None,
    run_id: Optional[str] = None,
    source: str = "validate_full",
) -> Dict[str, Any]:
    raw = raw_report if isinstance(raw_report, dict) else {}
    issues = [
        normalize_validation_issue(issue, index)
        for index, issue in enumerate(raw.get("issues") or [])
        if isinstance(issue, dict)
    ]
    error_count, warning_count, info_count = _count_by_severity(issues)
    report = {
        "passed": error_count == 0,
        "error_count": error_count,
        "warning_count": warning_count,
        "info_count": info_count,
        "issues": issues,
        "summary": {
            "total_issues": len(issues),
            "repairable_error_count": sum(
                1 for issue in issues if issue.get("severity") == "error" and issue.get("repairable")
            ),
            "blocking_error_count": sum(
                1 for issue in issues if issue.get("severity") == "error" and not issue.get("repairable")
            ),
        },
        "scope_key": str(scope_key or ""),
        "draft_artifact_id": draft_artifact_id,
        "run_id": run_id,
        "source": source,
    }
    if raw.get("meta"):
        report["meta"] = raw.get("meta")
    return report


def supplement_structural_issues(tree_data: Dict[str, Any], raw_report: Dict[str, Any]) -> Dict[str, Any]:
    """Add stable local structural issues that external validators may collapse."""

    report = dict(raw_report or {})
    issues = list(report.get("issues") or [])
    existing = {
        (
            str(issue.get("code") or issue.get("issue_code") or "").upper(),
            tuple(_as_list(issue.get("node_id") or issue.get("node_ids") or issue.get("node_name"))),
            tuple(_as_list(issue.get("link_id") or issue.get("link_ids"))),
        )
        for issue in issues
        if isinstance(issue, dict)
    }

    def add_issue(issue: Dict[str, Any]) -> None:
        key = (
            str(issue.get("code") or "").upper(),
            tuple(_as_list(issue.get("node_id") or issue.get("node_ids") or issue.get("node_name"))),
            tuple(_as_list(issue.get("link_id") or issue.get("link_ids"))),
        )
        if key not in existing:
            existing.add(key)
            issues.append(issue)

    if not isinstance(tree_data, dict):
        report["issues"] = issues
        return _recount_raw_report(report)

    node_list = tree_data.get("nodeList") or []
    link_list = tree_data.get("linkList") or []
    if not isinstance(node_list, list) or not isinstance(link_list, list):
        report["issues"] = issues
        return _recount_raw_report(report)

    seen_node_ids = set()
    all_node_ids = set()
    for index, node in enumerate(node_list):
        if not isinstance(node, dict):
            add_issue(
                {
                    "level": "ERROR",
                    "code": "INVALID_NODE",
                    "message": "nodeList contains a non-object node",
                    "node_id": f"node_index_{index}",
                    "source": "c_structural_precheck",
                }
            )
            continue
        node_id = str(node.get("id") or "").strip()
        if not node_id:
            add_issue(
                {
                    "level": "ERROR",
                    "code": "MISSING_NODE_ID",
                    "message": "node is missing id",
                    "node_id": f"node_index_{index}",
                    "node_name": str(node.get("name") or ""),
                    "source": "c_structural_precheck",
                }
            )
            continue
        if node_id in seen_node_ids:
            add_issue(
                {
                    "level": "ERROR",
                    "code": "DUPLICATE_NODE_ID",
                    "message": f"duplicate node id: {node_id}",
                    "node_id": node_id,
                    "node_name": str(node.get("name") or ""),
                    "source": "c_structural_precheck",
                }
            )
        seen_node_ids.add(node_id)
        all_node_ids.add(node_id)

    for index, link in enumerate(link_list):
        if not isinstance(link, dict):
            add_issue(
                {
                    "level": "ERROR",
                    "code": "INVALID_LINK",
                    "message": "linkList contains a non-object link",
                    "link_id": f"link_index_{index}",
                    "source": "c_structural_precheck",
                }
            )
            continue
        source_id = str(link.get("sourceId") or "").strip()
        target_id = str(link.get("targetId") or "").strip()
        if not source_id or not target_id:
            add_issue(
                {
                    "level": "ERROR",
                    "code": "MISSING_LINK_ENDPOINT",
                    "message": "link is missing sourceId or targetId",
                    "link_id": str(link.get("id") or f"link_index_{index}"),
                    "source": "c_structural_precheck",
                }
            )
            continue
        if source_id not in all_node_ids or target_id not in all_node_ids:
            add_issue(
                {
                    "level": "ERROR",
                    "code": "BROKEN_LINK",
                    "message": "link points to a missing node",
                    "link_id": str(link.get("id") or f"link_index_{index}"),
                    "source": "c_structural_precheck",
                }
            )

    report["issues"] = issues
    return _recount_raw_report(report)


def _recount_raw_report(report: Dict[str, Any]) -> Dict[str, Any]:
    issues = [issue for issue in (report.get("issues") or []) if isinstance(issue, dict)]
    error_count = sum(1 for issue in issues if _as_severity(issue.get("level") or issue.get("severity")) == "error")
    warning_count = sum(1 for issue in issues if _as_severity(issue.get("level") or issue.get("severity")) == "warning")
    info_count = len(issues) - error_count - warning_count
    report["issues"] = issues
    report["error_count"] = error_count
    report["warning_count"] = warning_count
    report["info_count"] = info_count
    report["passed"] = error_count == 0
    return report
