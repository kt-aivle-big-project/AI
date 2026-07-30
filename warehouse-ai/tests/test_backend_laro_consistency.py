from scripts.validate_backend_laro_snapshot import build_consistency_report


def test_backend_laro_consistency_report_identifies_exact_mismatches() -> None:
    report = build_consistency_report(
        warehouse_id=1,
        sql_snapshot={
            "robots": [
                {"robot_id": "1", "node_id": 150},
                {"robot_id": "2", "node_id": 151},
            ],
            "inventory": [{"node_id": 88}, {"node_id": 89}],
        },
        backend_map_nodes=[
            *[
                {
                    "node_id": value,
                    "backend_node_type": "RACK_STORAGE",
                }
                for value in range(88, 90)
            ],
            *[
                {"node_id": value, "backend_node_type": "OUTBOUND"}
                for value in range(143, 150)
            ],
            *[
                {
                    "node_id": value,
                    "backend_node_type": "CHARGING_SLOT",
                }
                for value in range(150, 160)
            ],
        ],
        graph_snapshot={
            "nodes": [
                *[{"node_id": value} for value in range(88, 90)],
                *[{"node_id": value} for value in range(143, 160)],
            ],
            "edges": [{"edge_id": "1"}],
        },
        chargers=[
            {"node_id": value}
            for value in range(150, 160)
            if value != 159
        ],
        redis_snapshot={"robots": [{"robot_id": "1"}, {"robot_id": "9"}]},
    )

    assert report["id_consistency"] == "FAIL"
    assert report["mismatches"][
        "postgres_map_node_ids_missing_in_neo4j"
    ] == []
    assert report["mismatches"]["robot_node_ids_missing_in_neo4j"] == []
    assert report["mismatches"][
        "charging_slot_node_ids_missing_charger_metadata"
    ] == [159]
    assert report["mismatches"]["postgres_robot_ids_missing_in_redis"] == [
        "2"
    ]
    assert report["mismatches"]["redis_robot_ids_missing_in_postgres"] == [
        "9"
    ]
