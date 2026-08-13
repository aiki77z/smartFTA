from agents.repair_agent import RepairAgent
from agents.verify_agent import VerifyAgent
from tools.correction_tools import build_tree_diff_summary, derive_repair_patterns_from_episode
from tools.repair_patch_tools import apply_repair_patch, build_repair_patch_from_validation


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


def _basic_event_missing_event_tree():
    return {
        "nodeList": [
            {"id": "top", "name": "顶事件", "type": "top_event", "event": None},
            {
                "id": "a",
                "name": "原因A",
                "type": "basic_event",
                "event": None,
            },
            {
                "id": "b",
                "name": "原因B",
                "type": "basic_event",
                "event": {"id": "b", "name": "原因B"},
            },
        ],
        "linkList": [
            {"type": "link", "sourceId": "a", "targetId": "top", "isCondition": False},
        ],
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


def test_repair_patch_applies_without_mutating_source_tree():
    tree = _basic_event_missing_event_tree()
    report = VerifyAgent().run({"artifact_id": "draft_2", "content": tree}, skip_semantic=True)["payload"]
    patch = build_repair_patch_from_validation(tree, report, base_artifact_id="draft_2")
    assert patch["operations"]
    applied = apply_repair_patch(tree, patch)
    assert applied["changed"]
    assert tree["nodeList"][1].get("event") is None
    assert applied["tree_data"]["nodeList"][1]["event"]["id"] == "a"


def test_repair_agent_can_create_repaired_draft_payload():
    tree = _basic_event_missing_event_tree()
    report = VerifyAgent().run({"artifact_id": "draft_3", "content": tree}, skip_semantic=True)["payload"]
    result = RepairAgent().run(
        {"artifact_id": "draft_3", "content": tree},
        report,
        apply_patch=True,
        revalidate=True,
    )
    patch = result["payload"]
    assert patch["status"] == "patched"
    assert patch["repaired_tree"]["nodeList"][1]["event"]["id"] == "a"
    assert result["validation_report"] is not None
    assert result["draft_tree_artifact"]["payload"]["tree_data"]["nodeList"][1]["event"]["id"] == "a"


def test_ai_only_episode_does_not_create_active_patterns():
    stored = []

    def fake_upsert(pattern):
        stored.append(pattern)
        return pattern

    import tools.correction_tools as correction_tools

    original = correction_tools.upsert_repair_pattern
    correction_tools.upsert_repair_pattern = fake_upsert
    try:
        patterns = derive_repair_patterns_from_episode(
            {
                "episode_id": "ce_1",
                "status": "pending",
                "scope_key": "scope_a",
                "diffs": [{"type": "gate_changed", "node_name": "顶事件", "to_gate": "AND"}],
            }
        )
    finally:
        correction_tools.upsert_repair_pattern = original
    assert patterns
    assert stored[0]["status"] == "pending"


def test_repair_patch_edge_ops_can_resolve_node_names():
    tree = _basic_event_missing_event_tree()
    patch = {
        "operations": [
            {
                "op": "add_edge",
                "from_node": "原因B",
                "to_node": "原因A",
                "reason_issue_code": "MISSING_EDGE",
                "source": "repair_patterns",
            }
        ]
    }
    applied = apply_repair_patch(tree, patch)
    assert applied["changed"]
    assert any(
        link.get("sourceId") == "b" and link.get("targetId") == "a"
        for link in applied["tree_data"]["linkList"]
    )


def test_repair_patch_removes_broken_link_by_index():
    tree = _basic_event_missing_event_tree()
    tree["linkList"].append({"type": "link", "sourceId": "missing", "targetId": "top"})
    report = {
        "issues": [
            {
                "issue_id": "issue_001",
                "issue_code": "BROKEN_LINK",
                "severity": "error",
                "node_ids": [],
                "link_ids": [],
                "repairable": True,
                "message": "broken link",
            }
        ]
    }
    patch = build_repair_patch_from_validation(tree, report, base_artifact_id="draft_4")
    applied = apply_repair_patch(tree, patch)
    assert applied["changed"]
    assert all(link.get("sourceId") != "missing" for link in applied["tree_data"]["linkList"])


def test_repair_patch_duplicate_node_id_updates_links():
    tree = {
        "nodeList": [
            {"id": "top", "name": "顶事件", "type": "top_event", "event": None},
            {"id": "dup", "name": "原因A", "type": "basic_event", "event": {"id": "a", "name": "原因A"}},
            {"id": "dup", "name": "原因B", "type": "basic_event", "event": {"id": "b", "name": "原因B"}},
        ],
        "linkList": [
            {"type": "link", "sourceId": "dup", "targetId": "top"},
        ],
    }
    report = VerifyAgent().run({"artifact_id": "draft_5", "content": tree}, skip_semantic=True)["payload"]
    patch = build_repair_patch_from_validation(tree, report, base_artifact_id="draft_5")
    applied = apply_repair_patch(tree, patch)
    ids = [node.get("id") for node in applied["tree_data"]["nodeList"]]
    assert len(ids) == len(set(ids))
