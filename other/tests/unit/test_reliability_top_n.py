from other.scripts import reliability_top_n


class FakeMappings:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class FakeConnection:
    def __init__(self):
        self.calls = []

    def execute(self, statement, parameters):
        self.calls.append((str(statement), parameters))
        return FakeMappings([{"rank": len(self.calls)}])


def test_collect_top_n_queries_every_sanitized_category(monkeypatch):
    connection = FakeConnection()
    monkeypatch.setattr(reliability_top_n.time, "time", lambda: 10.0)

    snapshot = reliability_top_n.collect_top_n(connection, 7)

    assert snapshot["generated_at"] == 10.0
    assert snapshot["limit"] == 7
    assert set(snapshot["categories"]) == set(reliability_top_n.QUERY_SPECS)
    assert len(connection.calls) == len(reliability_top_n.QUERY_SPECS)
    assert all(parameters == {"limit": 7} for _sql, parameters in connection.calls)
    combined_sql = "\n".join(sql for sql, _parameters in connection.calls).lower()
    assert "chatmessage" in combined_sql
    assert "pg_stat_activity" in combined_sql
    assert "chatrun" in combined_sql
    assert "agentdispatchtask" in combined_sql
    assert "content" not in combined_sql
    assert "query," not in combined_sql
