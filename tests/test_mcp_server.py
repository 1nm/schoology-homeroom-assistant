"""The MCP server must keep answering when the semantic backend is unusable."""

import pytest


@pytest.fixture
def archive(tmp_path, monkeypatch):
    import mcp_server

    posts = tmp_path / "posts"
    posts.mkdir()
    # tf-idf needs a few documents before a term scores above zero.
    (posts / "2026-09-01-1.md").write_text("Homework: sign the HW diary.", encoding="utf-8")
    (posts / "2026-09-08-2.md").write_text("Field trip permission slips.", encoding="utf-8")
    (posts / "2026-09-15-3.md").write_text("Photos from the science fair.", encoding="utf-8")
    (tmp_path / "materials").mkdir()
    monkeypatch.setattr(mcp_server, "DATA_DIR", tmp_path)
    return mcp_server


class BrokenIndex:
    def search(self, query, k):
        raise RuntimeError("Missing credentials")


def test_hybrid_search_falls_back_to_keywords(archive, monkeypatch):
    monkeypatch.setattr(archive, "_semantic_index", lambda: BrokenIndex())
    hits = archive.search("homework", limit=3)
    assert hits, "keyword results must survive a broken embedding backend"
    assert all("error" not in h for h in hits)
    assert "diary" in hits[0]["passage"].lower() or "homework" in hits[0]["passage"].lower()


def test_semantic_only_reports_the_failure(archive, monkeypatch):
    monkeypatch.setattr(archive, "_semantic_index", lambda: BrokenIndex())
    hits = archive.search("homework", limit=3, mode="semantic")
    assert hits == [{"error": "Semantic search is unavailable: Missing credentials"}]
