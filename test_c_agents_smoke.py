from agents.verify_agent import VerifyAgent
from tools.correction_tools import build_tree_diff_summary


def _sample_tree():
    return {
        "nodeList": [
            {"id": "n1", "name": "顶事件", "type": "top_event", "gate": "OR", "event": None},
            {
                "id": "n2",
                "name": "原因A",
                "type": "basic_event",
                "gate": None,
                "event": {"id": "E2", "name": "原因A", "documents": []},
            },
        ],
        "linkList": [{"type": "link", "sourceId": "n2", "targetId": "n1", "isCondition": False}],
    }


def test_verify_agent_normalizes_validation_report():
    result = VerifyAgent().run({"artifact_id": "draft_1", "content": _sample_tree()}, skip_semantic=True)
    report = result["payload"]
    assert report["draft_artifact_id"] == "draft_1"
    assert report["error_count"] >= 1
    assert report["summary"]["repairable_error_count"] >= 1
    assert report["summary"]["blocking_error_count"] == 0
    assert all("issue_code" in issue and "severity" in issue for issue in report["issues"])


def test_tree_diff_summary_counts_by_type():
    summary = build_tree_diff_summary(
        [
            {"type": "node_added", "node_name": "A"},
            {"type": "gate_changed", "node_name": "B"},
            {"type": "gate_changed", "node_name": "B"},
        ]
    )
    assert summary["change_count"] == 3
    assert summary["by_type"]["gate_changed"] == 2
    assert "A" in summary["touched_nodes"]
