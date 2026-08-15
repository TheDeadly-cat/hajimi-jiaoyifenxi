from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.store import StudioStore
from backend.templates import (
    ROOM_TEMPLATES,
    member_template_catalog,
    room_template_catalog,
)


ROOM_MEMBER_PREVIEW_FIELDS = {
    "name",
    "identity",
    "responsibilities",
    "boundaries",
    "stance",
    "workflow_stage",
    "capabilities",
    "avatar_color",
}


class RoomTemplateCatalogTests(unittest.TestCase):
    def test_catalog_includes_member_counts_and_previews(self) -> None:
        catalog = {item["id"]: item for item in room_template_catalog()}

        storage = catalog["us_storage_committee"]
        self.assertEqual(storage["member_count"], 12)
        self.assertEqual(len(storage["member_preview"]), 12)

        general = catalog["open_collaboration"]
        self.assertEqual(general["member_count"], 4)
        self.assertEqual(len(general["member_preview"]), 4)
        self.assertEqual(
            [member["name"] for member in general["member_preview"]],
            [
                member["name"]
                for member in ROOM_TEMPLATES["open_collaboration"]["members"]
            ],
        )

        football = catalog["football_research"]
        self.assertEqual(football["member_count"], 6)
        self.assertEqual(
            football["capability_pack_ids"],
            ["football_research_readonly", "structured_turn_contract_v1"],
        )
        self.assertTrue(all(
            "投注" in member["boundaries"]
            or "胜率" in member["boundaries"]
            or "官方事实" in member["boundaries"]
            or "截止" in member["boundaries"]
            for member in football["member_preview"]
        ))

        stock = catalog["stock_research"]
        self.assertEqual(stock["member_count"], 6)
        self.assertEqual(
            stock["capability_pack_ids"],
            ["stock_research_readonly", "structured_turn_contract_v1"],
        )
        self.assertTrue(all(
            "不" in member["boundaries"]
            for member in stock["member_preview"]
        ))

    def test_member_preview_uses_only_public_identity_fields(self) -> None:
        for template in room_template_catalog():
            for member in template["member_preview"]:
                self.assertEqual(set(member), ROOM_MEMBER_PREVIEW_FIELDS)
                self.assertNotIn("provider", member)
                self.assertNotIn("model", member)
                self.assertNotIn("instructions", member)
                self.assertNotIn("api_key", member)

    def test_catalog_and_previews_do_not_share_template_references(self) -> None:
        catalog = {item["id"]: item for item in room_template_catalog()}
        general = catalog["open_collaboration"]
        source = ROOM_TEMPLATES["open_collaboration"]
        source_capabilities = list(source["capabilities"])
        source_member_capabilities = list(source["members"][0]["capabilities"])

        self.assertIsNot(general["capabilities"], source["capabilities"])
        self.assertIsNot(
            general["member_preview"][0]["capabilities"],
            source["members"][0]["capabilities"],
        )
        general["capabilities"].append("catalog_mutation")
        general["member_preview"][0]["capabilities"].append("preview_mutation")
        general["member_preview"][0]["name"] = "mutated"

        self.assertEqual(source["capabilities"], source_capabilities)
        self.assertEqual(
            source["members"][0]["capabilities"], source_member_capabilities
        )
        self.assertNotEqual(source["members"][0]["name"], "mutated")

        fresh_general = next(
            item
            for item in room_template_catalog()
            if item["id"] == "open_collaboration"
        )
        self.assertNotIn("catalog_mutation", fresh_general["capabilities"])
        self.assertNotIn(
            "preview_mutation",
            fresh_general["member_preview"][0]["capabilities"],
        )
        self.assertNotEqual(fresh_general["member_preview"][0]["name"], "mutated")


class MemberTemplateCatalogTests(unittest.TestCase):
    def test_catalog_is_unique_safe_and_model_independent(self) -> None:
        catalog = member_template_catalog()

        self.assertGreaterEqual(len(catalog), 12)
        self.assertEqual(len({item["id"] for item in catalog}), len(catalog))
        self.assertTrue(
            {
                "战略主持人",
                "基本面分析师",
                "风险经理",
                "数据质量官",
            }.issubset({item["name"] for item in catalog})
        )
        for item in catalog:
            self.assertNotIn("provider", item)
            self.assertNotIn("model", item)
            self.assertNotIn("api_key", item)
            self.assertIsInstance(item["capabilities"], list)

    def test_catalog_returns_deep_copies(self) -> None:
        catalog = member_template_catalog()
        source_name = ROOM_TEMPLATES["open_collaboration"]["members"][0]["name"]
        source_capabilities = list(
            ROOM_TEMPLATES["open_collaboration"]["members"][0]["capabilities"]
        )

        catalog[0]["name"] = "被调用方修改"
        catalog[0]["capabilities"].append("mutated")

        self.assertEqual(
            ROOM_TEMPLATES["open_collaboration"]["members"][0]["name"],
            source_name,
        )
        self.assertEqual(
            ROOM_TEMPLATES["open_collaboration"]["members"][0]["capabilities"],
            source_capabilities,
        )

    def test_bootstrap_exposes_catalog_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StudioStore(Path(temp_dir) / "member-templates.sqlite3")
            bootstrap = store.bootstrap("room_plan")

        self.assertEqual(
            bootstrap["member_templates"],
            member_template_catalog(),
        )
        self.assertNotIn("providers", bootstrap)


if __name__ == "__main__":
    unittest.main()
