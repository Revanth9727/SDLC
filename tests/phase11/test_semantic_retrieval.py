from sqlalchemy import select

from app.db.connection import SessionLocal
from app.db.models import RepositoryCodeChunk
from app.repo_intelligence.indexer import RepositoryIndexer
from app.tools.code_search import CodeSearchTool
from tests.phase11.test_repository_index import _embedding, indexed_repo


def test_semantic_meaning_finds_code_without_exact_keyword(indexed_repo):
    repo_tool, _git, _remote, full_name = indexed_repo
    indexer = RepositoryIndexer(repo_tool, embedder=_embedding)
    snapshot = indexer.ensure(full_name, "semantic-subtask")
    events = []
    search = CodeSearchTool(repo_tool, events.append, embedder=_embedding)

    hits = search.search_semantic(
        full_name, "semantic-subtask", "complete a customer purchase", limit=3
    )

    assert hits[0]["path"] == "orders.py"
    assert "checkout" in hits[0]["text"]
    assert hits[0]["snapshot_id"] == str(snapshot.id)
    assert any(event.get("tool") == "search_semantic" for event in events)


def test_hybrid_fuses_deduplicates_and_reranks_without_llm(indexed_repo):
    repo_tool, _git, _remote, full_name = indexed_repo
    RepositoryIndexer(repo_tool, embedder=_embedding).ensure(full_name, "hybrid-subtask")
    events = []
    search = CodeSearchTool(repo_tool, events.append, embedder=_embedding)

    hits = search.search_hybrid(
        full_name, "hybrid-subtask", "complete a customer purchase",
        exact_queries=["checkout"], limit=5,
    )

    assert hits[0]["path"] == "orders.py"
    assert set(hits[0]["sources"]) == {"lexical", "semantic"}
    assert len({(hit["path"], hit["start_line"], hit["end_line"]) for hit in hits}) == len(hits)
    event = next(event for event in events if event.get("tool") == "search_hybrid")
    assert event["output"]["reranker"] == "weighted_v1"
    assert event["output"]["llm_calls"] == 0


def test_equal_semantic_scores_use_stable_symbol_path_and_chunk_tiebreak(indexed_repo):
    repo_tool, _git, _remote, full_name = indexed_repo
    vector = [1.0] + [0.0] * 1535
    snapshot = RepositoryIndexer(repo_tool, embedder=lambda _text: vector).ensure(
        full_name, "tie-subtask"
    )
    with SessionLocal() as db:
        chunks = list(db.scalars(select(RepositoryCodeChunk).where(
            RepositoryCodeChunk.snapshot_id == snapshot.id
        )))
        for chunk in chunks:
            chunk.embedding = vector
        db.commit()

    search = CodeSearchTool(repo_tool, embedder=lambda _text: vector)
    first = search.search_semantic(full_name, "tie-subtask", "checkout", limit=5)
    second = search.search_semantic(full_name, "tie-subtask", "checkout", limit=5)

    assert first == second
    assert "checkout" in first[0]["label"].lower()


def test_incremental_reembeds_only_changed_file(indexed_repo):
    repo_tool, git, remote, full_name = indexed_repo
    embedded = []

    def tracking_embed(text):
        embedded.append(text)
        return _embedding(text)

    indexer = RepositoryIndexer(repo_tool, embedder=tracking_embed)
    first = indexer.ensure(full_name, "cold-subtask")
    cold_count = len(embedded)
    with SessionLocal() as db:
        first_unchanged = db.scalar(select(RepositoryCodeChunk).where(
            RepositoryCodeChunk.snapshot_id == first.id,
            RepositoryCodeChunk.source_file == "unchanged.py",
        ))
        unchanged_hash = first_unchanged.content_hash

    (remote / "orders.py").write_text(
        (remote / "orders.py").read_text() + "\ndef refund():\n    return True\n"
    )
    git("add", "orders.py")
    git("commit", "-m", "change orders")
    second = indexer.ensure(full_name, "incremental-subtask")

    assert len(embedded) > cold_count
    assert all("orders.py" in prompt for prompt in embedded[cold_count:])
    with SessionLocal() as db:
        second_unchanged = db.scalar(select(RepositoryCodeChunk).where(
            RepositoryCodeChunk.snapshot_id == second.id,
            RepositoryCodeChunk.source_file == "unchanged.py",
        ))
        assert second_unchanged.content_hash == unchanged_hash
