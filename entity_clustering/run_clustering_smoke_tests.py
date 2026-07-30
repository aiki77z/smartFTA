from __future__ import annotations

from cluster_entities import Mention, cluster_mentions


def make_mention(
    mention_id: str,
    chunk_id: str,
    name: str,
    neighbor_tokens: set[str] | None = None,
    entity_type: str = "故障事件",
) -> Mention:
    return Mention(
        mention_id=mention_id,
        local_entity_id=mention_id.rsplit("#", 1)[-1],
        file_id="smoke_file",
        sample_id=chunk_id,
        chapter_id="",
        source_type="manual",
        entity_type=entity_type,
        mention=name,
        normalized_name=name,
        evidence=[{"chunk_id": chunk_id, "text_field": "text", "start": 0, "end": len(name), "text": name}],
        chunk_ids=[chunk_id],
        neighbor_tokens=neighbor_tokens or set(),
    )


def test_direction_conflict() -> None:
    mentions = [
        make_mention("smoke_file#chunk_1#E1", "chunk_1", "电网实际频率高于本地电网标准要求"),
        make_mention("smoke_file#chunk_2#E1", "chunk_2", "电网实际频率低于本地电网标准要求"),
    ]
    clusters, _mapping, _diagnostics = cluster_mentions(mentions, auto_threshold=0.90, review_threshold=0.72)
    assert len(clusters) == 2, "direction-conflicting mentions must not be merged"


def test_neighbor_boost_merge() -> None:
    left_neighbors = {
        "out:故障触发:故障事件:并网失败",
        "in:故障表征:报警码:告警2034",
        "out:故障处理:维修方法:检查电网频率",
    }
    right_neighbors = {
        "out:故障触发:故障事件:无法并网发电",
        "in:故障表征:报警码:2034告警",
        "out:故障处理:维修方法:检查电网频率设置",
    }
    mentions = [
        make_mention("smoke_file#chunk_10#E1", "chunk_10", "电网频率异常", left_neighbors),
        make_mention("smoke_file#chunk_18#E1", "chunk_18", "电网频率不符合要求", right_neighbors),
    ]
    clusters, _mapping, _diagnostics = cluster_mentions(mentions, auto_threshold=0.90, review_threshold=0.72)
    assert len(clusters) == 1, "highly consistent relation neighbors should merge compatible cross-chunk mentions"
    assert any("graph_boost=true" in reason for reason in clusters[0].merge_reasons), "merge should be caused by graph boost"


def test_semantic_boost_merge() -> None:
    mentions = [
        make_mention("smoke_file#chunk_20#E1", "chunk_20", "绝缘阻抗低", {"out:故障处理:维修方法:检查绝缘阻抗"}),
        make_mention("smoke_file#chunk_38#E1", "chunk_38", "绝缘阻抗变低", {"out:故障处理:维修方法:排查绝缘阻抗"}),
    ]
    mentions[0].embedding = [1.0, 0.0, 0.0]
    mentions[1].embedding = [0.98, 0.02, 0.0]
    clusters, _mapping, _diagnostics = cluster_mentions(mentions, auto_threshold=0.90, review_threshold=0.72)
    assert len(clusters) == 1, "high name score and high embedding score should merge compatible mentions"
    assert any("semantic_boost=true" in reason for reason in clusters[0].merge_reasons), "merge should be caused by semantic boost"


def test_repair_method_semantic_boost_merge() -> None:
    mentions = [
        make_mention("smoke_file#chunk_30#E1", "chunk_30", "联系当地电力运营商处理", entity_type="维修方法"),
        make_mention("smoke_file#chunk_31#E1", "chunk_31", "联系当地电力运营商", entity_type="维修方法"),
        make_mention("smoke_file#chunk_40#E1", "chunk_40", "取下交流输出连接器", entity_type="维修方法"),
        make_mention("smoke_file#chunk_41#E1", "chunk_41", "拆卸交流输出连接器", entity_type="维修方法"),
    ]
    mentions[0].embedding = [1.0, 0.0, 0.0]
    mentions[1].embedding = [0.97, 0.03, 0.0]
    mentions[2].embedding = [0.0, 1.0, 0.0]
    mentions[3].embedding = [0.0, 0.97, 0.03]
    clusters, _mapping, _diagnostics = cluster_mentions(mentions, auto_threshold=0.90, review_threshold=0.72)
    assert len(clusters) == 2, "repair method variants should merge by type-specific semantic boost"
    assert all(any("semantic_boost=true" in reason for reason in cluster.merge_reasons) for cluster in clusters)


def main() -> None:
    test_direction_conflict()
    test_neighbor_boost_merge()
    test_semantic_boost_merge()
    test_repair_method_semantic_boost_merge()
    print("All entity clustering smoke tests passed.")


if __name__ == "__main__":
    main()
