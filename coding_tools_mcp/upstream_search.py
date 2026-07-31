from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


DEFAULT_SEARCH_LIMIT = 5
MAX_SEARCH_LIMIT = 20
MAX_EXPANSIONS_PER_TERM = 10

FIELD_WEIGHTS: dict[str, float] = {
    "name": 5.0,
    "title": 4.0,
    "tags": 4.0,
    "alias": 3.0,
    "arguments": 2.0,
    "description": 1.0,
}

BUILTIN_SYNONYMS: dict[str, list[str]] = {
    "搜索": ["search", "find", "query", "lookup"],
    "查找": ["search", "find", "lookup"],
    "读取": ["read", "get", "fetch", "retrieve"],
    "获取": ["get", "fetch", "retrieve"],
    "创建": ["create", "add", "insert", "new"],
    "新建": ["create", "add", "new"],
    "删除": ["delete", "remove", "drop"],
    "修改": ["update", "edit", "patch", "modify"],
    "写入": ["write", "put", "save", "store"],
    "列出": ["list", "enumerate", "show"],
    "查询": ["query", "search", "find"],
    "检索": ["search", "retrieve", "fetch"],
    "下载": ["download", "fetch", "get"],
    "文件": ["file", "document"],
    "目录": ["directory", "folder", "path"],
    "提交": ["commit", "push"],
    "分支": ["branch", "checkout"],
    "合并": ["merge", "rebase"],
    "文献": ["library", "paper", "article", "reference", "literature"],
    "论文": ["paper", "article", "publication"],
    "批注": ["annotation", "highlight", "note", "comment"],
    "引用": ["citation", "reference", "cite"],
    "收藏": ["collection", "library", "bookmark"],
    "浏览器": ["browser", "web", "page"],
    "截图": ["screenshot", "capture", "snapshot"],
    "导航": ["navigate", "goto", "open"],
    "点击": ["click", "tap", "press"],
    "数据库": ["database", "db", "sql"],
    "表": ["table", "schema"],
    "部署": ["deploy", "publish", "release"],
    "服务器": ["server", "host", "instance"],
    "容器": ["container", "docker"],
}

_SEPARATOR_RE = re.compile(r"[\s._:/\\\-\[\](){}<>|,;!?@#$%^&*+=~`\"']+")
_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+|[A-Za-z0-9]+")
_CAMEL_RE = re.compile(
    r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+|[A-Z]+"
)


def _normalized_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _stable_unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def merge_synonyms(
    base: Mapping[str, Sequence[str]],
    custom: Mapping[str, Sequence[str]] | None,
) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {}
    for raw_key, raw_values in base.items():
        key = _normalized_key(str(raw_key))
        if not key:
            continue
        values = [_normalized_key(str(value)) for value in raw_values]
        merged[key] = _stable_unique(values)[:MAX_EXPANSIONS_PER_TERM]
    if custom:
        for raw_key, raw_values in custom.items():
            key = _normalized_key(str(raw_key))
            if not key:
                continue
            values = [_normalized_key(str(value)) for value in raw_values]
            combined = merged.get(key, []) + values
            merged[key] = _stable_unique(combined)[:MAX_EXPANSIONS_PER_TERM]
    return merged


class ToolTokenizer:
    """Instance-scoped tokenizer for tool names, metadata, and multilingual queries."""

    def __init__(self, synonyms: Mapping[str, Sequence[str]] | None = None) -> None:
        self.synonyms = merge_synonyms({}, synonyms or {})
        self.known_phrases = frozenset(self.synonyms)
        reverse: dict[str, list[str]] = {}
        for values in self.synonyms.values():
            group = _stable_unique(values)
            for value in group:
                reverse[value] = _stable_unique(reverse.get(value, []) + group)[
                    :MAX_EXPANSIONS_PER_TERM
                ]
        self.reverse_synonyms = reverse

    def tokenize(self, text: str) -> list[str]:
        normalized = unicodedata.normalize("NFKC", str(text))
        tokens: list[str] = []
        for part in _SEPARATOR_RE.split(normalized):
            if not part:
                continue
            for match in _RUN_RE.finditer(part):
                run = match.group(0)
                if _is_cjk_run(run):
                    tokens.extend(self._tokenize_cjk(run))
                else:
                    tokens.extend(token.casefold() for token in _CAMEL_RE.findall(run))
        return [token for token in tokens if token]

    def _tokenize_cjk(self, text: str) -> list[str]:
        if not text:
            return []
        tokens: list[str] = []
        covered = [False] * len(text)
        max_length = max((len(phrase) for phrase in self.known_phrases), default=2)
        for length in range(max_length, 1, -1):
            position = 0
            while position <= len(text) - length:
                candidate = text[position : position + length]
                if candidate in self.known_phrases and not any(
                    covered[position : position + length]
                ):
                    tokens.append(candidate)
                    covered[position : position + length] = [True] * length
                    position += length
                else:
                    position += 1
        if len(text) == 1:
            if not covered[0]:
                tokens.append(text)
            return tokens
        for position in range(len(text) - 1):
            if not (covered[position] and covered[position + 1]):
                tokens.append(text[position : position + 2])
        for position, is_covered in enumerate(covered):
            if not is_covered:
                tokens.append(text[position])
        return _stable_unique(tokens)

    def expand_query(self, tokens: Sequence[str]) -> list[str]:
        expanded: list[str] = []
        for raw_token in tokens:
            token = _normalized_key(str(raw_token))
            if not token:
                continue
            per_term = [token]
            per_term.extend(self.synonyms.get(token, ()))
            per_term.extend(
                value
                for value in self.reverse_synonyms.get(token, ())
                if value != token
            )
            expanded.extend(_stable_unique(per_term)[:MAX_EXPANSIONS_PER_TERM])
        return _stable_unique(expanded)


def _is_cjk_run(value: str) -> bool:
    return bool(value) and all(
        "\u3400" <= char <= "\u4dbf" or "\u4e00" <= char <= "\u9fff"
        for char in value
    )


@dataclass(frozen=True)
class UpstreamToolCatalogEntry:
    public_name: str
    server_alias: str
    remote_name: str
    title: str = ""
    description: str = ""
    tags: tuple[str, ...] = ()
    argument_names: tuple[str, ...] = ()
    effective_risk: str = "mutating"
    public_schema_digest: str = ""

    @property
    def tool_id(self) -> str:
        return self.public_name


@dataclass(frozen=True)
class ToolSearchFilters:
    server: str | None = None
    risk: str | None = None
    read_only: bool | None = None
    tags: tuple[str, ...] = ()
    name_prefix: str | None = None
    min_score: float = 0.0
    limit: int = DEFAULT_SEARCH_LIMIT

    def bounded_limit(self) -> int:
        return max(1, min(int(self.limit), MAX_SEARCH_LIMIT))


@dataclass(frozen=True)
class ToolSearchResult:
    tool_id: str
    public_name: str
    server_alias: str
    remote_name: str
    title: str
    description: str
    tags: tuple[str, ...]
    effective_risk: str
    public_schema_digest: str
    score: float

    @classmethod
    def from_entry(
        cls,
        entry: UpstreamToolCatalogEntry,
        *,
        score: float,
    ) -> "ToolSearchResult":
        return cls(
            tool_id=entry.tool_id,
            public_name=entry.public_name,
            server_alias=entry.server_alias,
            remote_name=entry.remote_name,
            title=entry.title,
            description=entry.description[:200],
            tags=tuple(entry.tags),
            effective_risk=entry.effective_risk,
            public_schema_digest=entry.public_schema_digest,
            score=score,
        )


@runtime_checkable
class SearchBackend(Protocol):
    def search(
        self,
        query: str,
        filters: ToolSearchFilters | None = None,
    ) -> list[ToolSearchResult]: ...


class BM25Field:
    def __init__(
        self,
        name: str,
        weight: float,
        *,
        k1: float = 1.2,
        b: float = 0.75,
    ) -> None:
        self.name = name
        self.weight = float(weight)
        self.k1 = float(k1)
        self.b = float(b)
        self._documents: dict[str, Counter[str]] = {}
        self._lengths: dict[str, int] = {}
        self._document_frequency: Counter[str] = Counter()
        self._average_length = 0.0
        self._finalized = False

    def add_document(self, doc_id: str, tokens: Sequence[str]) -> None:
        if self._finalized:
            raise RuntimeError("BM25Field is already finalized.")
        counts = Counter(token for token in tokens if token)
        self._documents[doc_id] = counts
        length = sum(counts.values())
        self._lengths[doc_id] = length
        self._document_frequency.update(counts.keys())

    def finalize(self) -> None:
        document_count = len(self._documents)
        self._average_length = (
            sum(self._lengths.values()) / document_count if document_count else 0.0
        )
        self._finalized = True

    def score(self, doc_id: str, query_tokens: Sequence[str]) -> float:
        if not self._finalized:
            raise RuntimeError("BM25Field must be finalized before scoring.")
        counts = self._documents.get(doc_id)
        if not counts:
            return 0.0
        document_count = len(self._documents)
        if document_count == 0:
            return 0.0
        document_length = self._lengths.get(doc_id, 0)
        average_length = self._average_length or 1.0
        score = 0.0
        for token in _stable_unique(query_tokens):
            frequency = counts.get(token, 0)
            if frequency <= 0:
                continue
            document_frequency = self._document_frequency.get(token, 0)
            inverse_document_frequency = math.log(
                1.0
                + (document_count - document_frequency + 0.5)
                / (document_frequency + 0.5)
            )
            denominator = frequency + self.k1 * (
                1.0 - self.b + self.b * document_length / average_length
            )
            score += inverse_document_frequency * (
                frequency * (self.k1 + 1.0) / denominator
            )
        return score * self.weight


class CatalogSearchIndex(SearchBackend):
    def __init__(
        self,
        synonyms: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        merged = merge_synonyms(BUILTIN_SYNONYMS, synonyms)
        self._tokenizer = ToolTokenizer(merged)
        self._entries: dict[str, UpstreamToolCatalogEntry] = {}
        self._fields: dict[str, BM25Field] = {}
        self._built = False

    @property
    def tokenizer(self) -> ToolTokenizer:
        return self._tokenizer

    def build(self, entries: Mapping[str, UpstreamToolCatalogEntry]) -> None:
        self._entries = dict(entries)
        self._fields = {
            name: BM25Field(name=name, weight=weight)
            for name, weight in FIELD_WEIGHTS.items()
        }
        tokenize = self._tokenizer.tokenize
        for doc_id, entry in self._entries.items():
            self._fields["name"].add_document(doc_id, tokenize(entry.remote_name))
            self._fields["title"].add_document(doc_id, tokenize(entry.title))
            self._fields["tags"].add_document(
                doc_id,
                [token for tag in entry.tags for token in tokenize(tag)],
            )
            self._fields["alias"].add_document(doc_id, tokenize(entry.server_alias))
            self._fields["arguments"].add_document(
                doc_id,
                [
                    token
                    for argument_name in entry.argument_names
                    for token in tokenize(argument_name)
                ],
            )
            self._fields["description"].add_document(
                doc_id,
                tokenize(entry.description),
            )
        for field in self._fields.values():
            field.finalize()
        self._built = True

    def search(
        self,
        query: str,
        filters: ToolSearchFilters | None = None,
    ) -> list[ToolSearchResult]:
        if not self._built:
            raise RuntimeError("CatalogSearchIndex must be built before searching.")
        active_filters = filters or ToolSearchFilters()
        candidates = [
            entry for entry in self._entries.values() if _matches_filters(entry, active_filters)
        ]
        if not candidates:
            return []
        normalized_query = _normalized_key(query)
        fast = self._fast_match(normalized_query, candidates)
        if fast is not None:
            return [ToolSearchResult.from_entry(fast, score=10_000.0)][
                : active_filters.bounded_limit()
            ]
        raw_tokens = self._tokenizer.tokenize(query)
        expanded_tokens = self._tokenizer.expand_query(raw_tokens)
        if not expanded_tokens:
            return []
        scored: list[tuple[UpstreamToolCatalogEntry, float]] = []
        for entry in candidates:
            score = sum(
                field.score(entry.tool_id, expanded_tokens)
                for field in self._fields.values()
            )
            if score >= float(active_filters.min_score) and score > 0.0:
                scored.append((entry, score))
        scored.sort(key=lambda item: (-item[1], item[0].tool_id))
        return [
            ToolSearchResult.from_entry(entry, score=score)
            for entry, score in scored[: active_filters.bounded_limit()]
        ]

    @staticmethod
    def _fast_match(
        normalized_query: str,
        candidates: Sequence[UpstreamToolCatalogEntry],
    ) -> UpstreamToolCatalogEntry | None:
        if not normalized_query:
            return None
        exact_public = [
            entry
            for entry in candidates
            if _normalized_key(entry.public_name) == normalized_query
        ]
        if exact_public:
            return sorted(exact_public, key=lambda entry: entry.tool_id)[0]

        alias_remote = [
            entry
            for entry in candidates
            if normalized_query
            in {
                _normalized_key(f"{entry.server_alias}/{entry.remote_name}"),
                _normalized_key(f"{entry.server_alias}:{entry.remote_name}"),
                _normalized_key(f"{entry.server_alias}__{entry.remote_name}"),
            }
        ]
        if alias_remote:
            return sorted(alias_remote, key=lambda entry: entry.tool_id)[0]

        exact_remote = [
            entry
            for entry in candidates
            if _normalized_key(entry.remote_name) == normalized_query
        ]
        if len(exact_remote) == 1:
            return exact_remote[0]

        prefix = [
            entry
            for entry in candidates
            if _normalized_key(entry.public_name).startswith(normalized_query)
        ]
        if len(prefix) == 1:
            return prefix[0]
        return None


def _matches_filters(
    entry: UpstreamToolCatalogEntry,
    filters: ToolSearchFilters,
) -> bool:
    if filters.server is not None and _normalized_key(entry.server_alias) != _normalized_key(
        filters.server
    ):
        return False
    if filters.risk is not None and entry.effective_risk != filters.risk:
        return False
    if filters.read_only is True and entry.effective_risk != "readonly":
        return False
    if filters.read_only is False and entry.effective_risk == "readonly":
        return False
    required_tags = {_normalized_key(tag) for tag in filters.tags if _normalized_key(tag)}
    entry_tags = {_normalized_key(tag) for tag in entry.tags if _normalized_key(tag)}
    if required_tags and not required_tags.issubset(entry_tags):
        return False
    if filters.name_prefix is not None and not _normalized_key(entry.public_name).startswith(
        _normalized_key(filters.name_prefix)
    ):
        return False
    return True
