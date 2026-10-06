from __future__ import annotations

import re
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[2]


def markdown_anchors(text: str) -> set[str]:
    """Read GitHub heading slugs and explicit compatibility anchors."""

    anchors = set(re.findall(r'<a\s+id="([^"]+)"\s*>', text))
    counts: dict[str, int] = {}
    fence: str | None = None
    for line in text.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            if fence is None:
                fence = marker.group(1)[0]
            elif fence == marker.group(1)[0]:
                fence = None
            continue
        if fence is not None:
            continue
        heading = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if heading is None:
            continue
        slug = re.sub(r"[^\w\- ]", "", heading.group(1).lower()).replace(" ", "-")
        count = counts.get(slug, 0)
        counts[slug] = count + 1
        anchors.add(f"{slug}-{count}" if count else slug)
    return anchors


class RequiredDocsTests(unittest.TestCase):
    def test_required_operator_docs_exist(self) -> None:
        required_paths = [
            "README.md",
            "SECURITY.md",
            "COMPLIANCE.md",
            "BENCHMARK.md",
            "docs/quickstart.md",
            "docs/mcp-client-config.md",
            "docs/tools-and-schemas.md",
            "docs/permission-modes.md",
            "docs/exec-command-recipes.md",
            "docs/troubleshooting-exec.md",
            "docs/security-boundary.md",
            "docs/docker.md",
            "docs/ci-and-tests.md",
            "docs/dogfood.md",
            "docs/swe-bench.md",
            "docs/limitations.md",
            "docs/telemetry.md",
            "docs/troubleshooting.md",
            "docs/local-tool-events.md",
            "docs/competitive-analysis.md",
            "docs/runtime-contract-v0.3.md",
            "docs/migration-0.3.md",
            "docs/runtime-contract-v0.2.md",
            "Dockerfile",
            ".dockerignore",
            "docker-compose.yml",
            "scripts/mcp_smoke.py",
            ".devcontainer/devcontainer.json",
        ]
        missing = [path for path in required_paths if not (ROOT / path).is_file()]
        self.assertEqual(missing, [])

    def test_required_evidence_artifacts_exist(self) -> None:
        required_paths = [
            "reports/compliance/latest.json",
            "reports/compliance/latest.md",
            "reports/dogfood/coding-tools-dogfood.json",
            "reports/dogfood/coding-tools-dogfood.md",
            "docs/dogfood/coding-tools-dogfood-transcript.json",
            "reports/benchmark/swebench-regression.json",
            "reports/benchmark/swebench-regression.md",
            "reports/benchmark/swebench-official-attempt.json",
            "reports/benchmark/swebench-official-attempt.md",
            "reports/benchmark/mcp-latency.json",
            "reports/benchmark/mcp-latency.md",
        ]
        missing = [path for path in required_paths if not (ROOT / path).is_file()]
        self.assertEqual(missing, [])

    def test_docs_contain_required_operational_topics_and_migration_pointers(self) -> None:
        expectations = {
            "README.md": ["Quickstart", "Safety Boundary", "Dogfood", "SWE-bench"],
            "SECURITY.md": ["Linux Landlock", "Environment Scrubbing", "Command Lifecycle"],
            "COMPLIANCE.md": ["make compliance", "required_tools", "not_measured"],
            "BENCHMARK.md": ["make dogfood-smoke", "make benchmark-latency", "PREFLIGHT_ONLY", "swebench-official-attempt"],
            "docs/ci-and-tests.md": ["make ci", "workflow", "swebench-lite"],
            "docs/dogfood.md": ["MCP-Only Rule", "view_image", "Direct filesystem/shell bypass"],
            "docs/swe-bench.md": ["Official attempt report", "BLOCKED", "sympy__sympy-12419"],
            "docs/permission-modes.md": ["safe", "trusted", "dangerous"],
            "docs/security-boundary.md": ["Landlock", "external container or VM"],
            "docs/competitive-analysis.md": ["Claude Code", "Aider", "OpenHands", "Cline"],
            "docs/local-tool-events.md": [
                "CODING_TOOLS_MCP_EVENT_LOG_DIR", "journal.lock", "0700", "0600",
                "tool_call_started", "tool_call_finished", "not fsynced",
                "unmatched start", "disable recording", "does not grant MCP file tools access",
            ],
            "docs/quickstart.md": ["coding-tools-mcp/docs", "content/docs/getting-started/index.mdx"],
            "docs/mcp-client-config.md": ["coding-tools-mcp/docs", "content/docs/clients/index.mdx"],
            "docs/remote-mcp.md": ["coding-tools-mcp/docs", "content/docs/guides/remote-access.mdx"],
            "docs/docker.md": ["coding-tools-mcp/docs", "content/docs/guides/docker-sandbox.mdx"],
            "docs/embedding.md": ["coding-tools-mcp/docs", "content/docs/guides/embedding.mdx"],
            "docs/exec-command-recipes.md": ["coding-tools-mcp/docs", "content/docs/guides/exec-command-recipes.mdx"],
            "docs/troubleshooting.md": ["coding-tools-mcp/docs", "content/docs/troubleshooting/index.mdx"],
            "docs/troubleshooting-exec.md": ["coding-tools-mcp/docs", "content/docs/troubleshooting/execution.mdx"],
            "docs/migration-0.3.md": ["coding-tools-mcp/docs", "content/docs/migrations/0.3.mdx"],
            "docs/migration-0.5.md": ["coding-tools-mcp/docs", "content/docs/migrations/0.5.mdx"],
        }
        for rel_path, needles in expectations.items():
            text = (ROOT / rel_path).read_text(encoding="utf-8")
            text = " ".join(text.split())
            for needle in needles:
                with self.subTest(path=rel_path, needle=needle):
                    self.assertIn(needle, text)

    def test_migration_pointers_preserve_current_runtime_guidance(self) -> None:
        expectations = {
            "docs/troubleshooting.md": ["### Durable local tool events", "local-tool-events.md"],
            "docs/exec-command-recipes.md": [
                "runtime-contract-v0.3.md#command-lifecycle", "keep_stdin_open",
                "tools-and-schemas.md#command-and-output-behavior",
            ],
            "docs/migration-0.5.md": [
                "take precedence", "line_numbers: true", "REVISION_MISMATCH",
                "mixed or bare-CR", "tools-and-schemas.md#locating-a-hunk",
                "runtime-contract-v0.3.md#repeated-failure-advice", "no longer produces `REPEATED_CALL_BLOCKED`",
            ],
            "docs/runtime-contract-v0.3.md": [
                "Repeated failures never prevent a tool handler from running",
                '"line_numbers"', "every untouched", "line: 2, start_line: 2",
            ],
            "docs/tools-and-schemas.md": [
                "schema default", "punctuation-only lines", "keep_stdin_open",
            ],
            "docs/ci-and-tests.md": ["https://github.com/coding-tools-mcp/desktop#development"],
        }
        for rel_path, needles in expectations.items():
            text = (ROOT / rel_path).read_text(encoding="utf-8")
            text = " ".join(text.split())
            for needle in needles:
                with self.subTest(path=rel_path, needle=needle):
                    self.assertIn(needle, text)

    def test_local_operator_document_links_resolve(self) -> None:
        documents = sorted(ROOT.glob("*.md")) + sorted((ROOT / "docs").rglob("*.md"))
        failures = []
        for source in documents:
            text = source.read_text(encoding="utf-8")
            for match in re.finditer(r"\[[^\]\n]*\]\(([^)\n]*)\)", text):
                destination = match.group(1).split(' "', 1)[0].strip("<>")
                parsed = urlsplit(destination)
                if parsed.scheme or parsed.netloc:
                    continue
                target = (source.parent / unquote(parsed.path)).resolve() if parsed.path else source
                line = text.count("\n", 0, match.start()) + 1
                location = f"{source.relative_to(ROOT)}:{line} -> {destination}"
                if not target.exists():
                    failures.append(location)
                elif parsed.fragment and target.suffix == ".md":
                    anchors = markdown_anchors(target.read_text(encoding="utf-8"))
                    if unquote(parsed.fragment) not in anchors:
                        failures.append(location)
        self.assertEqual(failures, [])

    def test_ci_workflows_include_required_gates(self) -> None:
        compliance = (ROOT / ".github/workflows/compliance.yml").read_text(encoding="utf-8")
        for needle in (
            "make lint",
            "make typecheck",
            "make test",
            "make test-protocol",
            "make test-integration",
            "make check-npm-launcher",
            "make dogfood-smoke",
            "make benchmark-latency",
            "make benchmark-smoke",
            "make compliance",
            "actions/upload-artifact",
        ):
            with self.subTest(workflow="compliance", needle=needle):
                self.assertIn(needle, compliance)

        swebench = (ROOT / ".github/workflows/swebench-lite.yml").read_text(encoding="utf-8")
        for needle in ("workflow_dispatch", "--install-swebench", "--run-evaluation", "reports/benchmark/**"):
            with self.subTest(workflow="swebench-lite", needle=needle):
                self.assertIn(needle, swebench)

        docker_image = (ROOT / ".github/workflows/docker-image.yml").read_text(encoding="utf-8")
        for needle in ("docker/build-push-action", "ghcr.io", "coding-tools-mcp-sandbox"):
            with self.subTest(workflow="docker-image", needle=needle):
                self.assertIn(needle, docker_image)

        docker_smoke = (ROOT / ".github/workflows/docker-smoke.yml").read_text(encoding="utf-8")
        for needle in ("docker build", "scripts/mcp_smoke.py"):
            with self.subTest(workflow="docker-smoke", needle=needle):
                self.assertIn(needle, docker_smoke)

        release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        for needle in (
            'tags: ["v*"]',
            "scripts/check_release_versions.py",
            "./.github/workflows/compliance.yml",
            "./.github/workflows/real-workloads.yml",
            "./.github/workflows/swebench-lite.yml",
            "Verify wheel contents and installation",
            "pypa/gh-action-pypi-publish",
            "npm@11.18.0",
            "npm publish ./dist/*.tgz --access public --provenance",
            "gh release create",
        ):
            with self.subTest(workflow="release", needle=needle):
                self.assertIn(needle, release)

        final_audit = (ROOT / ".github/workflows/final-audit.yml").read_text(encoding="utf-8")
        for needle in (
            "actions/setup-python@v6.2.0",
            'expected_ref="refs/tags/$RELEASE_TAG"',
            "git rev-list",
            "scripts/check_release_versions.py",
            "Validate referenced runs",
        ):
            with self.subTest(workflow="final-audit", needle=needle):
                self.assertIn(needle, final_audit)

        smoke_script = (ROOT / "scripts/mcp_smoke.py").read_text(encoding="utf-8")
        for needle in ("server_info", "exec_command"):
            with self.subTest(target="mcp_smoke", needle=needle):
                self.assertIn(needle, smoke_script)

        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        for needle in (
            "ARG JAVA_VERSION",
            "JAVA_HOME",
            "CODING_TOOLS_MCP_EXEC_ALLOW_ROOTS",
            "/etc/maven",
            "CODING_TOOLS_MCP_GENERATE_AUTH_TOKEN",
        ):
            with self.subTest(target="Dockerfile", needle=needle):
                self.assertIn(needle, dockerfile)
