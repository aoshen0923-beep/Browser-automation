from quizpilot.kb import KB
from quizpilot.textutil import chunk, fts_query, tokens


def test_tokens_bigrams_and_ids():
    toks = tokens("儿童口罩 GB/T 38880-2020")
    assert "儿童" in toks and "口罩" in toks and "童口" in toks
    assert "38880-2020" in toks and "38880" in toks


def test_fts_query_drops_stopwords():
    assert '"下列"' not in fts_query("下列哪些是口罩")
    assert fts_query("   ") == ""


def test_chunk_respects_size():
    parts = chunk("句子。" * 1000, size=300, overlap=50)
    assert all(len(p) <= 300 for p in parts)
    assert len(parts) > 5


def test_search_finds_chinese_text():
    with KB(":memory:") as kb:
        kb.add_document("std-1", [(2, "本标准主要起草人：高尚荣、李桂梅、李建全。")], title="GB/T 38880-2020 儿童口罩技术规范", module="11")
        kb.add_document("other", [(1, "图书馆众筹研究的现状与展望")], title="论文")
        hits = kb.search("儿童口罩技术规范的起草人有谁")
        assert hits and hits[0].source == "std-1"
        assert hits[0].page == 2
        assert "李桂梅" in hits[0].text
        assert kb.search("起草人", module="12") == []


def test_title_matches_every_chunk():
    with KB(":memory:") as kb:
        kb.add_document("book", [(1, "alpha"), (82, "two figures here")], title="Big Data and Global Trade Law")
        pages = {h.page for h in kb.search("Big Data and Global Trade Law", k=10)}
        assert pages == {1, 82}


def test_readding_replaces_document():
    with KB(":memory:") as kb:
        kb.add_document("u", [(None, "旧内容苹果")])
        kb.add_document("u", [(None, "新内容香蕉")])
        assert kb.stats() == {"docs": 1, "chunks": 1}
        assert kb.search("苹果") == []
        assert kb.search("香蕉")[0].source == "u"
        kb.remove("u")
        assert kb.stats() == {"docs": 0, "chunks": 0}
