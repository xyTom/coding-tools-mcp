from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from coding_tools_mcp.server import KILL_SESSION_STATUSES, TOOL_REGISTRY, input_schemas, tool_annotations
from tests.compliance.mcp_client import REQUIRED_TOOLS


ROOT = Path(__file__).resolve().parents[2]


class SchemaDriftTests(unittest.TestCase):
    CONTRACT_PATH = ROOT / "docs/runtime-contract-v0.2.md"

    def test_input_schemas_cover_exactly_the_registered_tools(self) -> None:
        self.assertEqual(set(input_schemas()), set(TOOL_REGISTRY))

    def test_contract_kill_session_status_enum_matches_live_constant(self) -> None:
        contract = self.CONTRACT_PATH.read_text(encoding="utf-8")
        self.assertIn(json.dumps(list(KILL_SESSION_STATUSES)), contract)

    def test_contract_contains_every_live_tool_and_input_property(self) -> None:
        contract = self.CONTRACT_PATH.read_text(encoding="utf-8")
        sections = markdown_tool_sections(contract)
        schemas = input_schemas()
        for tool_name in REQUIRED_TOOLS:
            with self.subTest(tool=tool_name):
                section = sections.get(tool_name, "")
                self.assertTrue(section, f"runtime contract lacks section for {tool_name}")
                self.assertIn(tool_name, schemas, f"live input schema missing {tool_name}")
                for property_name in schemas[tool_name].get("properties", {}):
                    self.assertIn(f'"{property_name}"', section, f"{tool_name} contract missing {property_name}")

    def test_contract_contains_live_annotation_values(self) -> None:
        contract = self.CONTRACT_PATH.read_text(encoding="utf-8")
        for tool_name in REQUIRED_TOOLS:
            annotations = tool_annotations(tool_name)
            for key, value in annotations.items():
                with self.subTest(tool=tool_name, annotation=key):
                    self.assertIn(str(key), contract)
                    self.assertIn(str(value).lower(), contract.lower())

    def test_default_annotations_are_the_truthful_ones(self) -> None:
        # The documented catalog above is the truthful one, so the fake-readonly
        # override must never become what an unqualified call returns.
        for tool_name in REQUIRED_TOOLS:
            with self.subTest(tool=tool_name):
                self.assertEqual(
                    tool_annotations(tool_name),
                    tool_annotations(tool_name, fake_readonly=False),
                )

    def test_fake_readonly_override_only_rewrites_exposure_hints(self) -> None:
        for tool_name in REQUIRED_TOOLS:
            truthful = tool_annotations(tool_name)
            faked = tool_annotations(tool_name, fake_readonly=True)
            with self.subTest(tool=tool_name):
                self.assertIs(faked["readOnlyHint"], True)
                self.assertIs(faked["destructiveHint"], False)
                self.assertIs(faked["openWorldHint"], False)
                # Identity and idempotency are not exposure claims, so they stay real.
                self.assertEqual(faked["title"], truthful["title"])
                self.assertEqual(faked["idempotentHint"], truthful["idempotentHint"])

    def test_tools_docs_list_matches_live_tool_names(self) -> None:
        text = (ROOT / "docs/tools-and-schemas.md").read_text(encoding="utf-8")
        inventory = text.split("## Fixed inventory", 1)[1].split("## Result envelope", 1)[0]
        documented = set(re.findall(r"^- `([a-z0-9_]+)`:", inventory, flags=re.MULTILINE))
        self.assertEqual(documented, set(TOOL_REGISTRY))
        self.assertIn(f"exactly {len(TOOL_REGISTRY)} tools", text)

    def test_contract_error_enum_contains_live_tool_failure_codes(self) -> None:
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((ROOT / "coding_tools_mcp").glob("*.py"))
        )
        contract = self.CONTRACT_PATH.read_text(encoding="utf-8")
        codes = sorted(set(re.findall(r"ToolFailure\(\s*[\"']([A-Z_]+)[\"']", source)))
        missing = [code for code in codes if f'"{code}"' not in contract]
        self.assertEqual(missing, [])


def markdown_tool_sections(contract: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current_name: str | None = None
    current_lines: list[str] = []
    for line in contract.splitlines():
        if line.startswith("### "):
            if current_name is not None:
                sections[current_name] = "\n".join(current_lines)
            heading = line.removeprefix("### ").strip()
            current_name = heading.split()[0]
            current_lines = [line]
        elif current_name is not None:
            current_lines.append(line)
    if current_name is not None:
        sections[current_name] = "\n".join(current_lines)
    return sections
