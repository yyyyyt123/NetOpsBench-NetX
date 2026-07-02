from netopsbench.platform.observability.validation import check_observability, extract_interface_names


def test_extract_interface_names_ignores_empty_counter_identity_samples():
    csv_text = "\n".join(
        [
            ",result,table,_time,_field,_value,name,path,source",
            ",,0,2026-07-01T18:49:04Z,Ethernet100,,Ethernet100,/COUNTERS/Ethernet100,leaf1",
            ",,0,2026-07-01T18:49:04Z,in_octets,42,Ethernet0,/COUNTERS/Ethernet0,leaf1",
            ",,0,2026-07-01T18:49:04Z,out_octets,43,Ethernet4,/COUNTERS/Ethernet4,leaf1",
        ]
    )

    assert extract_interface_names(csv_text) == ["Ethernet0", "Ethernet4"]


def test_interface_observability_queries_are_scoped_by_topology_id():
    queries: list[str] = []

    def query_runner(query: str) -> str:
        queries.append(query)
        if '_measurement == "pingmesh"' in query:
            return ",result,table,_time,_value\n,,0,2026-07-01T18:49:04Z,1\n"
        if '_measurement == "interfaces"' in query:
            return "\n".join(
                [
                    ",result,table,_time,_field,_value,name,path,source,topology_id",
                    ",,0,2026-07-01T18:49:04Z,in_octets,42,Ethernet0,/COUNTERS/Ethernet0,leaf1,lab-a",
                ]
            )
        return ",result,table,_time,_value\n"

    errors = check_observability(
        query_runner,
        bucket="netopsbench",
        obs_device="leaf1",
        topology_id="lab-a",
        active_interfaces=["Ethernet0"],
    )

    assert errors == []
    interface_queries = [query for query in queries if '_measurement == "interfaces"' in query]
    assert interface_queries
    assert all('r.topology_id == "lab-a"' in query for query in interface_queries)
