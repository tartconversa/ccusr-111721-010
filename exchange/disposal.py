import json

from .audit import write

# 处置动作按资产、产品修订、交付凭据的顺序执行，保证中途失败后状态可解释。
KIND_ORDER = {"asset": 0, "product": 1, "delivery": 2}


class Disposal:
    """合作撤回后的处置：按来源约定隔离或清理内容，并保留可核验的历史凭据。"""

    def __init__(self, connection):
        self.connection = connection

    def plan(self, source_id, at):
        """推导该来源当前应处置的内容清单，不改动任何数据。"""
        source = self._withdrawn_source(source_id)
        purge_due = source["retention_until"] is None or at >= source["retention_until"]
        items = []
        assets = self.connection.execute(
            "SELECT id,state,digest FROM assets WHERE source_id=? ORDER BY id", (source_id,)
        ).fetchall()
        for asset in assets:
            if asset["state"] == "purged":
                continue
            items.append({
                "kind": "asset", "id": asset["id"],
                "action": "purge" if purge_due else "isolate",
                "details": {"digest": asset["digest"]},
            })
        products = self.connection.execute(
            "SELECT DISTINCT p.product_id,p.product_revision FROM product_inputs p "
            "JOIN assets a ON a.id=p.asset_id WHERE a.source_id=? "
            "ORDER BY p.product_id,p.product_revision",
            (source_id,),
        ).fetchall()
        for product in products:
            items.append({
                "kind": "product", "id": "%s#%s" % (product["product_id"], product["product_revision"]),
                "action": "seal",
                "details": {"product": product["product_id"], "revision": product["product_revision"]},
            })
        deliveries = self.connection.execute(
            "SELECT DISTINCT d.id,d.recipient,d.product_id,d.product_revision FROM deliveries d "
            "JOIN product_inputs p ON p.product_id=d.product_id AND p.product_revision=d.product_revision "
            "JOIN assets a ON a.id=p.asset_id WHERE a.source_id=? ORDER BY d.id",
            (source_id,),
        ).fetchall()
        for delivery in deliveries:
            items.append({
                "kind": "delivery", "id": delivery["id"], "action": "recall",
                "details": {"recipient": delivery["recipient"], "product": delivery["product_id"],
                            "revision": delivery["product_revision"]},
            })
        return items

    def execute(self, source_id, at, actor):
        """执行处置。每项内容单独提交，重复执行或中途失败后可再次调用续跑。"""
        for item in self.plan(source_id, at):
            self._upsert_item(source_id, item, at)
        applied = []
        for item in self._open_items(source_id):
            try:
                with self.connection:
                    self._apply_item(item, at, actor)
                    self._mark_item(item, "done", at)
            except Exception as exc:
                with self.connection:
                    self._mark_item(item, "failed", at, error=str(exc))
                raise RuntimeError(
                    "处置中断于 %s:%s，已记录进度，可再次执行继续" % (item["kind"], item["id"])
                ) from exc
            applied.append({"kind": item["kind"], "id": item["id"], "action": item["action"]})
        if applied:
            with self.connection:
                write(self.connection, at, actor, "disposal.execute", source_id, {"applied": applied})
        report = self.status(source_id)
        report["applied"] = applied
        return report

    def status(self, source_id):
        """核对处置进度：每份内容的动作与状态。"""
        source = self._source(source_id)
        rows = self.connection.execute(
            "SELECT kind,item_id,action,state,updated_at FROM disposal_items WHERE source_id=?",
            (source_id,),
        ).fetchall()
        items = [
            {"kind": row["kind"], "id": row["item_id"], "action": row["action"],
             "state": row["state"], "at": row["updated_at"]}
            for row in rows
        ]
        items.sort(key=lambda item: (KIND_ORDER[item["kind"]], item["id"]))
        summary = {"done": 0, "pending": 0, "failed": 0}
        for item in items:
            summary[item["state"]] += 1
        return {
            "source_id": source_id,
            "withdrawn_at": source["withdrawn_at"],
            "retention_until": source["retention_until"],
            "complete": not summary["pending"] and not summary["failed"],
            "summary": summary,
            "items": items,
        }

    def _source(self, source_id):
        row = self.connection.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if row is None:
            raise KeyError(source_id)
        return dict(row)

    def _withdrawn_source(self, source_id):
        source = self._source(source_id)
        if not source["withdrawn_at"]:
            raise ValueError("来源尚未撤回，不能处置")
        return source

    def _upsert_item(self, source_id, item, at):
        row = self.connection.execute(
            "SELECT action,state FROM disposal_items WHERE source_id=? AND kind=? AND item_id=?",
            (source_id, item["kind"], item["id"]),
        ).fetchone()
        details = json.dumps(item["details"], ensure_ascii=False, sort_keys=True)
        if row is None:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO disposal_items(source_id,kind,item_id,action,state,updated_at,details) "
                    "VALUES(?,?,?,?,'pending',?,?)",
                    (source_id, item["kind"], item["id"], item["action"], at, details),
                )
        elif row["state"] == "done" and row["action"] == "isolate" and item["action"] == "purge":
            # 保存期限届满，原先的隔离件升级为清理。
            with self.connection:
                self.connection.execute(
                    "UPDATE disposal_items SET action='purge',state='pending',updated_at=?,details=? "
                    "WHERE source_id=? AND kind=? AND item_id=?",
                    (at, details, source_id, item["kind"], item["id"]),
                )
        elif row["state"] in ("pending", "failed") and row["action"] != item["action"]:
            with self.connection:
                self.connection.execute(
                    "UPDATE disposal_items SET action=?,updated_at=?,details=? "
                    "WHERE source_id=? AND kind=? AND item_id=?",
                    (item["action"], at, details, source_id, item["kind"], item["id"]),
                )

    def _open_items(self, source_id):
        rows = self.connection.execute(
            "SELECT kind,item_id,action,details FROM disposal_items "
            "WHERE source_id=? AND state IN ('pending','failed')",
            (source_id,),
        ).fetchall()
        items = [
            {"source_id": source_id, "kind": row["kind"], "id": row["item_id"],
             "action": row["action"], "details": json.loads(row["details"])}
            for row in rows
        ]
        items.sort(key=lambda item: (KIND_ORDER[item["kind"]], item["id"]))
        return items

    def _apply_item(self, item, at, actor):
        if item["kind"] == "asset":
            if item["action"] == "purge":
                # 清理：移除内容本身，保留摘要供历史凭据核验。
                self.connection.execute(
                    "UPDATE assets SET state='purged',payload='',disposed_at=? "
                    "WHERE id=? AND state IN ('active','isolated')",
                    (at, item["id"]),
                )
                write(self.connection, at, actor, "disposal.asset.purge", item["id"], item["details"])
            else:
                # 隔离：保存义务未满，原件留存但不再参与组装与交付。
                self.connection.execute(
                    "UPDATE assets SET state='isolated',disposed_at=? WHERE id=? AND state='active'",
                    (at, item["id"]),
                )
                write(self.connection, at, actor, "disposal.asset.isolate", item["id"], item["details"])
        elif item["kind"] == "product":
            self.connection.execute(
                "UPDATE products SET state='sealed' WHERE id=? AND revision=? AND state='draft'",
                (item["details"]["product"], item["details"]["revision"]),
            )
            write(self.connection, at, actor, "disposal.product.seal",
                  item["details"]["product"], {"revision": item["details"]["revision"]})
        elif item["kind"] == "delivery":
            # 凭据只含摘要与元数据，标记撤回后继续留存。
            self.connection.execute(
                "UPDATE deliveries SET recalled_at=? WHERE id=? AND recalled_at IS NULL",
                (at, item["id"]),
            )
            write(self.connection, at, actor, "disposal.delivery.recall", item["id"], item["details"])

    def _mark_item(self, item, state, at, error=None):
        details = dict(item["details"])
        if error:
            details["error"] = error
        self.connection.execute(
            "UPDATE disposal_items SET state=?,updated_at=?,details=? "
            "WHERE source_id=? AND kind=? AND item_id=?",
            (state, at, json.dumps(details, ensure_ascii=False, sort_keys=True),
             item["source_id"], item["kind"], item["id"]),
        )
