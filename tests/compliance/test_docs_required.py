from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


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
            "docs/competitive-analysis.md",
            "docs/runtime-contract-v0.2.md",
            "docs/remote-mcp.md",
            "docs/admin-api.md",
            "docs/admin-webui.md",
            "docs/chat-persistence.md",
            "docs/migration-v0.1-to-v0.2.2.md",
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

    def test_docs_contain_required_operational_topics(self) -> None:
        expectations = {
            "README.md": ["Quickstart", "Safety Boundary", "Dogfood", "SWE-bench"],
            "SECURITY.md": ["Linux Landlock", "Environment Scrubbing", "Session Lifecycle"],
            "COMPLIANCE.md": ["make compliance", "required_tools", "not_measured"],
            "BENCHMARK.md": ["make dogfood-smoke", "make benchmark-latency", "PREFLIGHT_ONLY", "swebench-official-attempt"],
            "docs/ci-and-tests.md": ["make ci", "workflow", "swebench-lite"],
            "docs/dogfood.md": ["MCP-Only Rule", "view_image", "Direct filesystem/shell bypass"],
            "docs/swe-bench.md": ["Official attempt report", "BLOCKED", "sympy__sympy-12419"],
            "docs/troubleshooting.md": ["SANDBOX_UNAVAILABLE", "MCP-Protocol-Version"],
            "docs/permission-modes.md": ["safe", "trusted", "dangerous"],
            "docs/exec-command-recipes.md": ["MAVEN_USER_HOME", "npm_config_cache", "GOCACHE", "CARGO_HOME"],
            "docs/troubleshooting-exec.md": ["DEV_NULL_DENIED", "DNS_RESOLUTION_FAILED", "OUTPUT_TRUNCATED"],
            "docs/security-boundary.md": ["Landlock", "external container or VM"],
            "docs/docker.md": ["permission-mode trusted", "permission_mode=dangerous", "mvn -version"],
            "docs/competitive-analysis.md": ["Claude Code", "Aider", "OpenHands", "Cline"],
        }
        for rel_path, needles in expectations.items():
            text = (ROOT / rel_path).read_text(encoding="utf-8")
            for needle in needles:
                with self.subTest(path=rel_path, needle=needle):
                    self.assertIn(needle, text)


    def test_integrated_v022_docs_cover_final_boundaries(self) -> None:
        expectations = {
            "README.md": [
                "Integrated v0.2.2 architecture",
                "refresh_token",
                "Admin WebUI",
                "migration-v0.1-to-v0.2.2.md",
            ],
            "README.zh-CN.md": [
                "集成后的 v0.2.2 架构",
                "持久化 OAuth",
                "升级与回滚",
            ],
            "docs/runtime-contract-v0.2.md": [
                "Session identity and Workspace binding",
                "listChanged: false",
                "UPSTREAM_TOOL_COLLISION",
                "refresh_token",
                "legacy_tool_profile_ignored",
            ],
            "docs/remote-mcp.md": [
                "CODING_TOOLS_MCP_SECRETS_KEY",
                "oauth.sqlite3",
                "oauth_client_workspace_bindings",
                "restart_required",
            ],
            "docs/admin-webui.md": [
                "stale HTTP 409",
                "no upstream start, stop, reload",
                "textContent",
            ],
            "docs/chat-persistence.md": [
                "workspace_id",
                "summary",
                "telemetry",
            ],
            "docs/migration-v0.1-to-v0.2.2.md": [
                "legacy_tool_profile_ignored",
                "server-settings.json",
                "oauth.sqlite3",
                "oauth-secrets.json",
                "full snapshot rollback",
                "Refresh rotation",
                "original refresh token remains retryable",
            ],
            "docs/telemetry.md": [
                "chat/transcript content",
                "Workspace IDs, OAuth Agent/Client",
                "CODING_TOOLS_MCP_TELEMETRY=off",
            ],
        }
        for rel_path, needles in expectations.items():
            text = (ROOT / rel_path).read_text(encoding="utf-8")
            for needle in needles:
                with self.subTest(path=rel_path, needle=needle):
                    self.assertIn(needle, text)

    def test_v01_profile_doc_is_history_not_current_contract(self) -> None:
        self.assertFalse((ROOT / "docs/profile-v0.1.md").exists())
        contract = (ROOT / "docs/runtime-contract-v0.2.md").read_text(encoding="utf-8")
        self.assertIn("There are no\ntool profiles", contract)
        self.assertNotIn("--tool-profile", contract)

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
