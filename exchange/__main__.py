import argparse
import json

from .service import Exchange


def main():
    parser = argparse.ArgumentParser(description="本地协作台账演练")
    parser.add_argument("--database", required=True)
    args = parser.parse_args()
    service = Exchange(args.database)
    try:
        service.catalog.register("flow", "机构甲", "甲地", "business", [("count", "通行计数", "integer", False)], "2026-11-17")
        service.catalog.ingest("item-1", "flow", {"count": 7}, "2026-11-17")
        service.grants.issue("g-1", "flow", "机构乙", "协作统计", "2026-12-31", "2026-11-17")
        service.policies.set("甲地", "business", "协作统计", "allow", "2026-11-17", "机构甲")
        revision = service.products.assemble("report", "通行汇总", ["item-1"], "2026-11-17", "机构甲")
        receipt = service.delivery.send("d-1", "report", revision, "机构乙", "协作统计", "2026-11-17")
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    finally:
        service.close()


if __name__ == "__main__":
    main()
