from app.repo_intelligence.indexer import (
    ExtractedFile, RepositoryIndexer, _extract_python, _extract_sql, _graph_edges,
)
from app.tools.code_search import CodeSearchTool
from tests.phase11.test_repository_index import _embedding, indexed_repo


def test_parser_builds_only_proven_call_sql_and_contains_edges():
    source = (
        "def save_order(db, order_id, total):\n"
        "    audit(order_id)\n"
        "    return db.execute('UPDATE orders SET total = ? WHERE id = ?', total)\n"
    )
    item = ExtractedFile(file={"path": "orders.py"})
    _extract_python(source, "orders.py", "abc123", item)
    _extract_sql(source, "orders.py", "abc123", item)

    edges = _graph_edges(item)

    assert {"CONTAINS", "CALLS", "CALLED_BY", "REFERENCES", "WRITES"} <= {
        edge["relation_kind"] for edge in edges
    }
    assert any(edge["target_node"] == "table:orders" for edge in edges)
    assert any(edge["target_node"] == "column:orders.total" for edge in edges)
    assert all(edge["evidence_kind"] == "PROVEN" for edge in edges)


def test_graph_traversal_is_source_verified(indexed_repo):
    repo_tool, _git, _remote, full_name = indexed_repo
    RepositoryIndexer(repo_tool, embedder=_embedding).ensure(full_name, "graph-subtask")
    events = []
    search = CodeSearchTool(repo_tool, events.append, embedder=_embedding)

    callers = search.get_callers(full_name, "graph-subtask", "load_order")
    callees = search.get_callees(full_name, "graph-subtask", "checkout")
    reads = search.get_reads_writes(full_name, "graph-subtask", "orders")

    assert callers[0]["relation"] == "CALLED_BY"
    assert callers[0]["verified"] and "load_order" in callers[0]["text"]
    assert callees[0]["relation"] == "CALLS"
    assert callees[0]["verified"] and "load_order" in callees[0]["text"]
    assert reads[0]["relation"] == "READS"
    assert reads[0]["verified"] and "orders" in reads[0]["text"]
    assert all(item["provenance"]["evidence_kind"] == "PROVEN"
               for item in callers + callees + reads)
    assert any(event.get("tool") == "get_reads_writes_graph" for event in events)


def test_incremental_graph_drops_changed_file_edges(indexed_repo):
    repo_tool, git, remote, full_name = indexed_repo
    indexer = RepositoryIndexer(repo_tool, embedder=_embedding)
    indexer.ensure(full_name, "graph-before")
    (remote / "orders.py").write_text("def checkout():\n    return None\n")
    git("add", "orders.py")
    git("commit", "-m", "remove old graph edges")
    indexer.ensure(full_name, "graph-after")

    search = CodeSearchTool(repo_tool, embedder=_embedding)
    assert search.get_callers(full_name, "graph-after", "load_order") == []
    assert search.get_reads_writes(full_name, "graph-after", "orders") == []
