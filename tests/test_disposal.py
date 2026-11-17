import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from exchange import Exchange


class DisposalFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = Exchange(Path(self.temp.name) / "exchange.db")
        catalog = self.service.catalog
        catalog.register("flow-a", "机构甲", "甲地", "restricted",
                         [("count", "通行计数", "integer", False)], "2026-11-01",
                         retention_until="2026-12-31")
        catalog.register("flow-b", "机构乙", "乙地", "business",
                         [("note", "备注", "text", False)], "2026-11-01")
        catalog.ingest("a-1", "flow-a", {"count": 8}, "2026-11-02")
        catalog.ingest("b-1", "flow-b", {"note": "常态"}, "2026-11-02")
        self.service.grants.issue("g-a", "flow-a", "机构丙", "协作统计", "2027-06-30", "2026-11-03")
        self.service.grants.issue("g-b", "flow-b", "机构丙", "协作统计", "2027-06-30", "2026-11-03")
        self.service.policies.set("甲地", "restricted", "协作统计", "allow", "2026-11-03", "机构甲")
        self.service.policies.set("乙地", "business", "协作统计", "allow", "2026-11-03", "机构乙")
        self.mixed = self.service.products.assemble("mixed", "混用产品", ["a-1", "b-1"], "2026-11-04", "机构甲")
        self.solo = self.service.products.assemble("solo", "单方产品", ["b-1"], "2026-11-04", "机构乙")
        self.service.delivery.send("d-mixed", "mixed", self.mixed, "机构丙", "协作统计", "2026-11-05")
        self.service.delivery.send("d-solo", "solo", self.solo, "机构丙", "协作统计", "2026-11-05")

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def delivery_row(self, delivery_id):
        return dict(self.service.connection.execute(
            "SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone())

    def withdraw_and_dispose(self, at="2026-11-10"):
        self.service.catalog.withdraw("flow-a", at, "机构甲")
        return self.service.disposal.execute("flow-a", at, "运维")

    def test_withdrawal_isolates_without_touching_other_source(self):
        report = self.withdraw_and_dispose()
        self.assertTrue(report["complete"])
        # 保存义务未满：原件隔离留存而非删除。
        asset = self.service.catalog.get("a-1")
        self.assertEqual(asset["state"], "isolated")
        self.assertIn("8", asset["payload"])
        # 另一家来源仍有效：其资产、单方产品与交付不受连带影响。
        self.assertEqual(self.service.catalog.get("b-1")["state"], "active")
        self.assertIsNone(self.delivery_row("d-solo")["recalled_at"])
        receipt = self.service.delivery.send("d-3", "solo", self.solo, "机构丙", "协作统计", "2026-11-11")
        self.assertEqual(receipt["product_id"], "solo")
        # 混用产品修订封存，含受限内容的交付凭据标记撤回但留存。
        with self.assertRaises(ValueError):
            self.service.delivery.send("d-2", "mixed", self.mixed, "机构丙", "协作统计", "2026-11-11")
        recalled = self.delivery_row("d-mixed")
        self.assertEqual(recalled["recalled_at"], "2026-11-10")
        self.assertTrue(recalled["input_digest"])
        # 隔离内容不能再进入新产品。
        with self.assertRaises(ValueError):
            self.service.products.assemble("new", "新产品", ["a-1"], "2026-11-12", "机构甲")

    def test_purge_after_retention_keeps_verifiable_receipt(self):
        self.withdraw_and_dispose()
        before = self.delivery_row("d-mixed")["input_digest"]
        report = self.service.disposal.execute("flow-a", "2027-01-01", "运维")
        self.assertTrue(report["complete"])
        asset = self.service.catalog.get("a-1")
        self.assertEqual(asset["state"], "purged")
        self.assertEqual(asset["payload"], "")
        # 摘要保留，历史交付凭据仍可核验且不含受限内容。
        digests = [self.service.catalog.get(key)["digest"] for key in ("a-1", "b-1")]
        expected = hashlib.sha256("|".join(digests).encode()).hexdigest()
        self.assertEqual(self.delivery_row("d-mixed")["input_digest"], expected)
        self.assertEqual(self.delivery_row("d-mixed")["input_digest"], before)

    def test_repeated_execution_is_idempotent(self):
        first = self.withdraw_and_dispose()
        second = self.service.disposal.execute("flow-a", "2026-11-10", "运维")
        self.assertEqual(second["applied"], [])
        self.assertTrue(second["complete"])
        self.assertEqual(first["summary"]["done"], second["summary"]["done"])
        self.assertEqual(self.service.catalog.get("a-1")["state"], "isolated")

    def test_failed_run_resumes_from_recorded_progress(self):
        self.service.catalog.withdraw("flow-a", "2026-11-10", "机构甲")
        disposal = self.service.disposal
        original = disposal._apply_item

        def fail_on_product(item, at, actor):
            if item["kind"] == "product":
                raise RuntimeError("模拟中断")
            return original(item, at, actor)

        with patch.object(disposal, "_apply_item", side_effect=fail_on_product):
            with self.assertRaises(RuntimeError):
                disposal.execute("flow-a", "2026-11-10", "运维")
        states = {(item["kind"], item["state"]) for item in disposal.status("flow-a")["items"]}
        self.assertIn(("asset", "done"), states)
        self.assertIn(("product", "failed"), states)
        self.assertIn(("delivery", "pending"), states)
        report = disposal.execute("flow-a", "2026-11-10", "运维")
        self.assertTrue(report["complete"])
        self.assertEqual(self.service.catalog.get("a-1")["state"], "isolated")
        self.assertEqual(self.delivery_row("d-mixed")["recalled_at"], "2026-11-10")

    def test_disposal_requires_withdrawal(self):
        with self.assertRaises(ValueError):
            self.service.disposal.execute("flow-a", "2026-11-10", "运维")

    def test_disposal_writes_audit_trail(self):
        self.withdraw_and_dispose()
        asset_actions = [entry["action"] for entry in self.service.history("a-1")]
        self.assertIn("disposal.asset.isolate", asset_actions)
        delivery_actions = [entry["action"] for entry in self.service.history("d-mixed")]
        self.assertIn("disposal.delivery.recall", delivery_actions)
        source_actions = [entry["action"] for entry in self.service.history("flow-a")]
        self.assertIn("source.withdraw", source_actions)
        self.assertIn("disposal.execute", source_actions)


if __name__ == "__main__":
    unittest.main()
