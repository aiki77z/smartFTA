from __future__ import annotations

from typing import Any, Dict, Optional

from database import get_version
from diff_analyzer import _diff_trees, _get_top_event_name, analyze_and_store
from tools.correction_tools import (
    build_correction_episode,
    derive_repair_patterns_from_episode,
    store_correction_episode,
)


class MemoryCurator:
    """Persist expert-confirmed correction episodes without promoting AI-only fixes."""

    name = "MemoryCurator"

    def curate_expert_save(
        self,
        *,
        tree_id: str,
        ai_version: int,
        expert_version: int,
        scope_key: str = "",
        validation_report: Optional[Dict[str, Any]] = None,
        write_legacy_corrections: bool = False,
        derive_patterns: bool = True,
        activate_expert_patterns: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        ai_ver = get_version(tree_id, ai_version)
        expert_ver = get_version(tree_id, expert_version)
        if not ai_ver or not expert_ver:
            return {
                "agent": self.name,
                "status": "skipped",
                "reason": "version_not_found",
                "episode": None,
                "legacy_corrections_written": 0,
            }

        ai_tree = ai_ver.get("tree_data") or {}
        expert_tree = expert_ver.get("tree_data") or {}
        diffs = _diff_trees(ai_tree, expert_tree)
        if not diffs:
            return {
                "agent": self.name,
                "status": "skipped",
                "reason": "no_structural_diff",
                "episode": None,
                "legacy_corrections_written": 0,
            }

        effective_scope_key = (
            scope_key
            or expert_ver.get("source_scope_key")
            or ai_ver.get("source_scope_key")
            or ""
        )
        top_event = _get_top_event_name(expert_tree) or expert_ver.get("resolved_top_event") or ai_ver.get("resolved_top_event") or ""
        episode = build_correction_episode(
            tree_id=tree_id,
            ai_version=ai_version,
            expert_version=expert_version,
            ai_tree_data=ai_tree,
            expert_tree_data=expert_tree,
            diffs=diffs,
            scope_key=effective_scope_key,
            top_event=top_event,
            validation_report=validation_report,
            status="expert_confirmed",
            source="expert_save",
            metadata=metadata,
        )
        stored_episode = store_correction_episode(episode)
        patterns = []
        if derive_patterns:
            patterns = derive_repair_patterns_from_episode(
                stored_episode,
                activate_expert_confirmed=activate_expert_patterns,
            )
        legacy_count = 0
        if write_legacy_corrections:
            legacy_count = analyze_and_store(tree_id, ai_version, expert_version)
        return {
            "agent": self.name,
            "status": "stored",
            "episode": stored_episode,
            "repair_patterns": patterns,
            "legacy_corrections_written": legacy_count,
        }


def curate_expert_save(*args, **kwargs) -> Dict[str, Any]:
    return MemoryCurator().curate_expert_save(*args, **kwargs)
