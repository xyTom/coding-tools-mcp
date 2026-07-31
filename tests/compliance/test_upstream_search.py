from __future__ import annotations

import unittest
from collections.abc import Mapping

from coding_tools_mcp.upstream_search import (
    BUILTIN_SYNONYMS,
    MAX_EXPANSIONS_PER_TERM,
    MAX_SEARCH_LIMIT,
    BM25Field,
    CatalogSearchIndex,
    SearchBackend,
    ToolSearchFilters,
    ToolTokenizer,
    UpstreamToolCatalogEntry,
    merge_synonyms,
)


def entry(
    public_name: str,
    *,
    title: str = "",
    description: str = "",
    tags: tuple[str, ...] = (),
    arguments: tuple[str, ...] = (),
    risk: str = "mutating",
    digest: str = "0" * 32,
) -> UpstreamToolCatalogEntry:
    alias, _, remote_name = public_name.partition("__")
    return UpstreamToolCatalogEntry(
        public_name=public_name,
        server_alias=alias,
        remote_name=remote_name,
        title=title,
        description=description,
        tags=tags,
        argument_names=arguments,
        effective_risk=risk,
        public_schema_digest=digest,
    )


def build_index(
    entries: Mapping[str, UpstreamToolCatalogEntry],
    *,
    custom_synonyms: dict[str, list[str]] | None = None,
) -> CatalogSearchIndex:
    index = CatalogSearchIndex(custom_synonyms)
    index.build(entries)
    return index


class ToolTokenizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tokenizer = ToolTokenizer(merge_synonyms(BUILTIN_SYNONYMS, None))

    def test_camel_case_snake_case_and_nfkc(self) -> None:
        self.assertEqual(self.tokenizer.tokenize("searchLibrary"), ["search", "library"])
        self.assertEqual(
            self.tokenizer.tokenize("snake_case_name"),
            ["snake", "case", "name"],
        )
        self.assertEqual(self.tokenizer.tokenize("Ｓｅａｒｃｈ-Library"), ["search", "library"])

    def test_cjk_known_phrases_and_query_expansion(self) -> None:
        tokens = self.tokenizer.tokenize("搜索文献")
        self.assertIn("搜索", tokens)
        self.assertIn("文献", tokens)
        expanded = self.tokenizer.expand_query(tokens)
        self.assertIn("search", expanded)
        self.assertIn("library", expanded)
        self.assertEqual(len(expanded), len(set(expanded)))

    def test_english_reverse_expansion_is_stable_and_bounded(self) -> None:
        first = self.tokenizer.expand_query(["search"])
        second = self.tokenizer.expand_query(["search"])
        self.assertEqual(first, second)
        self.assertEqual(first[0], "search")
        self.assertLessEqual(len(first), MAX_EXPANSIONS_PER_TERM)
        self.assertIn("find", first)

    def test_custom_synonyms_merge_without_global_or_instance_pollution(self) -> None:
        custom = {"核磁": ["nmr", "spectroscopy"]}
        merged = merge_synonyms(BUILTIN_SYNONYMS, custom)
        self.assertNotIn("核磁", BUILTIN_SYNONYMS)
        first = ToolTokenizer(merged)
        second = ToolTokenizer(merge_synonyms(BUILTIN_SYNONYMS, None))
        self.assertIn("核磁", first.known_phrases)
        self.assertNotIn("核磁", second.known_phrases)
        self.assertIn("nmr", first.expand_query(first.tokenize("核磁")))
        self.assertNotIn("nmr", second.expand_query(second.tokenize("核磁")))

    def test_merge_synonyms_deduplicates_and_caps_each_term(self) -> None:
        merged = merge_synonyms(
            {"搜索": ["search", "find"]},
            {"搜索": ["find"] + [f"term-{index}" for index in range(20)]},
        )
        self.assertEqual(merged["搜索"][:2], ["search", "find"])
        self.assertEqual(len(merged["搜索"]), MAX_EXPANSIONS_PER_TERM)


class BM25FieldTests(unittest.TestCase):
    def test_repeated_query_token_does_not_double_score(self) -> None:
        field = BM25Field("name", 5)
        field.add_document("tool", ["search", "library"])
        field.finalize()
        self.assertEqual(
            field.score("tool", ["search"]),
            field.score("tool", ["search", "search", "search"]),
        )

    def test_field_weights_rank_name_above_title_above_description(self) -> None:
        entries = {
            "a__needle_tool": entry("a__needle_tool", title="other", description="other"),
            "b__other": entry("b__other", title="needle", description="other"),
            "c__other": entry("c__other", title="other", description="needle"),
        }
        results = build_index(entries).search("needle", ToolSearchFilters(limit=10))
        self.assertEqual(
            [result.tool_id for result in results],
            ["a__needle_tool", "b__other", "c__other"],
        )


class CatalogSearchIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.entries = {
            "zotero__search_library": entry(
                "zotero__search_library",
                title="Search Library",
                description="Search papers and references in a literature library.",
                tags=("research", "papers"),
                arguments=("query", "limit"),
                risk="readonly",
                digest="1" * 32,
            ),
            "zotero__get_item": entry(
                "zotero__get_item",
                title="Get Item",
                description="Retrieve one bibliographic item.",
                tags=("research",),
                arguments=("itemKey",),
                risk="readonly",
                digest="2" * 32,
            ),
            "github__search_library": entry(
                "github__search_library",
                title="Search Repository Library",
                description="Search source repositories.",
                tags=("code",),
                arguments=("query",),
                risk="readonly",
                digest="3" * 32,
            ),
            "github__create_issue": entry(
                "github__create_issue",
                title="Create Issue",
                description="Create a remote issue.",
                tags=("code", "write"),
                arguments=("title", "body"),
                risk="mutating",
                digest="4" * 32,
            ),
        }
        self.index = build_index(self.entries)

    def test_implements_search_backend_protocol(self) -> None:
        self.assertIsInstance(self.index, SearchBackend)

    def test_exact_public_alias_remote_unique_remote_and_unique_prefix(self) -> None:
        self.assertEqual(
            self.index.search("zotero__get_item")[0].tool_id,
            "zotero__get_item",
        )
        self.assertEqual(
            self.index.search("zotero/get_item")[0].tool_id,
            "zotero__get_item",
        )
        self.assertEqual(self.index.search("get_item")[0].tool_id, "zotero__get_item")
        self.assertEqual(
            self.index.search("github__create")[0].tool_id,
            "github__create_issue",
        )

    def test_duplicate_remote_name_does_not_use_unique_fast_path(self) -> None:
        results = self.index.search("search_library", ToolSearchFilters(limit=10))
        result_ids = {result.tool_id for result in results}
        self.assertIn("github__search_library", result_ids)
        self.assertIn("zotero__search_library", result_ids)
        self.assertTrue(all(result.score != 10_000.0 for result in results))

    def test_structural_filters_and_nfkc_name_prefix(self) -> None:
        results = self.index.search(
            "search",
            ToolSearchFilters(
                server="ＺＯＴＥＲＯ",
                read_only=True,
                tags=("research",),
                name_prefix="ＺＯＴＥＲＯ＿＿ＳＥＡＲＣＨ",
                limit=10,
            ),
        )
        self.assertEqual([result.tool_id for result in results], ["zotero__search_library"])
        mutating = self.index.search(
            "issue",
            ToolSearchFilters(read_only=False, tags=("write",), limit=10),
        )
        self.assertEqual([result.tool_id for result in mutating], ["github__create_issue"])

    def test_min_score_limit_and_deterministic_tie_sort(self) -> None:
        self.assertEqual(self.index.search("search", ToolSearchFilters(min_score=10_000)), [])
        limited = self.index.search("search", ToolSearchFilters(limit=1))
        self.assertEqual(len(limited), 1)
        clamped = self.index.search("search", ToolSearchFilters(limit=10_000))
        self.assertLessEqual(len(clamped), MAX_SEARCH_LIMIT)

        tied_entries = {
            "b__same": entry("b__same", description="needle"),
            "a__same": entry("a__same", description="needle"),
        }
        tied = build_index(tied_entries).search("needle", ToolSearchFilters(limit=10))
        self.assertEqual([result.tool_id for result in tied], ["a__same", "b__same"])

    def test_default_limit_is_five_and_hard_limit_is_twenty(self) -> None:
        entries = {
            f"server__tool_{index}": entry(
                f"server__tool_{index}",
                description="sharedneedle",
                risk="readonly",
            )
            for index in range(30)
        }
        index = build_index(entries)
        self.assertEqual(len(index.search("sharedneedle")), 5)
        self.assertEqual(
            len(index.search("sharedneedle", ToolSearchFilters(limit=999))),
            MAX_SEARCH_LIMIT,
        )

    def test_chinese_search_literature_matches_english_tool(self) -> None:
        results = self.index.search("搜索文献", ToolSearchFilters(limit=10))
        self.assertEqual(results[0].tool_id, "zotero__search_library")

    def test_custom_chinese_synonym_matches_nmr_metadata(self) -> None:
        index = build_index(
            {
                "lab__analyze_spectrum": entry(
                    "lab__analyze_spectrum",
                    title="NMR Spectrum Analysis",
                    description="Analyze nuclear magnetic resonance spectra.",
                    risk="readonly",
                )
            },
            custom_synonyms={"核磁": ["nmr"]},
        )
        self.assertEqual(index.search("核磁")[0].tool_id, "lab__analyze_spectrum")

    def test_results_are_compact_and_do_not_contain_schema(self) -> None:
        result = self.index.search("zotero__get_item")[0]
        self.assertEqual(result.public_schema_digest, "2" * 32)
        self.assertFalse(hasattr(result, "input_schema"))
        self.assertFalse(hasattr(result, "public_definition"))
        self.assertLessEqual(len(result.description), 200)

    def test_empty_index_and_unbuilt_index(self) -> None:
        unbuilt = CatalogSearchIndex()
        with self.assertRaises(RuntimeError):
            unbuilt.search("anything")
        empty = CatalogSearchIndex()
        empty.build({})
        self.assertEqual(empty.search("anything"), [])

    def test_large_catalog_fixtures_are_bounded_and_find_the_target(self) -> None:
        for size in (50, 200, 500):
            with self.subTest(size=size):
                entries = {
                    f"server{index % 5}__tool_{index}": entry(
                        f"server{index % 5}__tool_{index}",
                        title=f"Routine helper {index}",
                        description="Generic catalog fixture entry.",
                        tags=("fixture",),
                        risk="readonly",
                    )
                    for index in range(size)
                }
                target_name = f"server{size % 5}__needle_search_library_{size}"
                entries[target_name] = entry(
                    target_name,
                    title="Needle Search Library",
                    description="Unique target for large index validation.",
                    tags=("fixture", "target"),
                    risk="readonly",
                )
                results = build_index(entries).search(
                    "needle search library",
                    ToolSearchFilters(limit=5),
                )
                self.assertLessEqual(len(results), 5)
                self.assertEqual(results[0].tool_id, target_name)


if __name__ == "__main__":
    unittest.main()
