import argparse
import json

from .service import Exchange


def main():
    parser = argparse.ArgumentParser(description="本地协作台账演练")
    parser.add_argument("--database", required=True)
    args = parser.parse_args()
    service = Exchange(args.database)
    try:
        service.catalog.register("flow", "机构甲", "甲地", "business", [("count", "通行计数", "integer", False)],
                                 "2026-11-17", retention_until="2026-12-31")
        service.catalog.ingest("item-1", "flow", {"count": 7}, "2026-11-17")
        service.grants.issue("g-1", "flow", "机构乙", "协作统计", "2026-12-31", "2026-11-17")
        service.policies.set("甲地", "business", "协作统计", "allow", "2026-11-17", "机构甲")
        revision = service.products.assemble("report", "通行汇总", ["item-1"], "2026-11-17", "机构甲")
        receipt = service.delivery.send("d-1", "report", revision, "机构乙", "协作统计", "2026-11-17")
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        service.catalog.withdraw("flow", "2026-11-18", "机构甲")
        # 保存期限未满：原件隔离留存，产品修订封存，交付凭据标记撤回。
        print(json.dumps(service.disposal.execute("flow", "2026-11-18", "运维"), ensure_ascii=False, sort_keys=True))
        # 保存期限届满后再次执行：隔离件升级为清理，凭据仍可核验。
        print(json.dumps(service.disposal.execute("flow", "2027-01-01", "运维"), ensure_ascii=False, sort_keys=True))
    finally:
        service.close()


if __name__ == "__main__":
    main()
