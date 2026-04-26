import json
import sqlite3
from pathlib import Path

import pandas as pd


DB_PATH = "food_25doer.db"
OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

STATIC_BASE = "https://static.25doer.com"


def safe_json_loads(value, default=None):
    if default is None:
        default = None

    if value is None:
        return default

    if isinstance(value, (dict, list)):
        return value

    if not isinstance(value, str):
        return default

    value = value.strip()

    if not value:
        return default

    try:
        return json.loads(value)
    except Exception:
        return default


def normalize_url(path):
    if not path:
        return ""

    if isinstance(path, list):
        if not path:
            return ""
        path = path[0]

    if not isinstance(path, str):
        return ""

    path = path.strip()

    if not path:
        return ""

    if path.startswith("["):
        parsed = safe_json_loads(path, default=[])
        if isinstance(parsed, list) and parsed:
            path = parsed[0]

    if path.startswith("http://") or path.startswith("https://"):
        return path

    if path.startswith("/"):
        return STATIC_BASE + path

    return STATIC_BASE + "/" + path


def get_price_min(goods):
    values = goods.get("price_interval")

    if isinstance(values, list) and values:
        try:
            return float(values[0])
        except Exception:
            return None

    try:
        return float(goods.get("estimate_price_interval"))
    except Exception:
        return None


def get_price_max(goods):
    values = goods.get("price_interval")

    if isinstance(values, list) and len(values) >= 2:
        try:
            return float(values[1])
        except Exception:
            return None

    return get_price_min(goods)


def parse_shop(raw):
    return {
        "team_id": raw.get("team_id"),
        "merchant_id": raw.get("merchant_id"),
        "merchant_name": raw.get("merchant_name"),
        "photo": normalize_url(raw.get("photo")),
        "address": raw.get("address"),
        "tag": raw.get("tag"),
        "distance": raw.get("distance"),
        "business_status": raw.get("business_status"),
        "is_business": raw.get("is_business"),
        "is_business_pre": raw.get("is_business_pre"),
        "business_time": raw.get("business_time"),
        "business_time_json": json.dumps(
            safe_json_loads(raw.get("business_time"), default={}),
            ensure_ascii=False
        ),
        "takeout_floor_price": raw.get("takeout_floor_price"),
        "takeout_delivery_price": raw.get("takeout_delivery_price"),
        "extra_packing_price": raw.get("extra_packing_price"),
        "meal_time": raw.get("meal_time"),
        "takeout_meal_time": raw.get("takeout_meal_time"),
        "merchant_score": raw.get("merchant_score"),
        "comment_slogan": raw.get("comment_slogan"),
        "per_capita_price": raw.get("per_capita_price"),
        "goods_num": raw.get("goods_num"),
        "sales": raw.get("sales"),
        "total_sales": raw.get("total_sales"),
        "takeout_switch": raw.get("takeout_switch"),
        "takeout_show_index_switch": raw.get("takeout_show_index_switch"),
        "is_brand": raw.get("is_brand"),
        "is_new": raw.get("is_new"),
    }


def parse_category(category):
    return {
        "merchant_id": category.get("merchant_id"),
        "classify_id": category.get("classify_id"),
        "parent_id": category.get("parent_id"),
        "classify_name": category.get("name"),
        "sort": category.get("sort"),
        "status": category.get("status"),
        "enable_status": category.get("enable_status"),
        "classify_type_tag": category.get("classify_type_tag"),
        "goods_count": len(category.get("goodss") or []),
        "create_time": category.get("create_time"),
        "update_time": category.get("update_time"),
    }


def parse_dish(goods, category, merchant_name=""):
    return {
        "merchant_id": goods.get("merchant_id"),
        "merchant_name": merchant_name,
        "classify_id": goods.get("classify_id") or category.get("classify_id"),
        "classify_name": goods.get("classify_name") or category.get("name"),
        "goods_id": goods.get("goods_id"),
        "goods_name": goods.get("name"),
        "image": normalize_url(goods.get("image")),
        "intro": goods.get("intro"),
        "describe": goods.get("describe"),
        "packing_fee": goods.get("packing_fee"),
        "price_min": get_price_min(goods),
        "price_max": get_price_max(goods),
        "price_interval": json.dumps(goods.get("price_interval"), ensure_ascii=False),
        "origin_price_interval": json.dumps(goods.get("origin_price_interval"), ensure_ascii=False),
        "estimate_show_price": (goods.get("estimate_price_list") or {}).get("goods_show_price"),
        "stock": goods.get("stock"),
        "stock_status": goods.get("stock_status"),
        "sale_status": goods.get("sale_status"),
        "shelves_status": goods.get("shelves_status"),
        "status": goods.get("status"),
        "sales": goods.get("sales"),
        "virtual_sales": goods.get("virtual_sales"),
        "month_sales": goods.get("month_sales"),
        "total_sales": goods.get("total_sales"),
        "minimum_purchases": goods.get("minimum_purchases"),
        "many_spec": goods.get("many_spec"),
        "attr": goods.get("attr"),
        "sort": goods.get("sort"),
        "create_time": goods.get("create_time"),
        "update_time": goods.get("update_time"),
    }


def parse_specs(goods):
    rows = []
    specs = goods.get("spec") or []

    for spec_group in specs:
        group = spec_group.get("goods_spec_classify") or {}
        options = spec_group.get("goods_spec") or []

        for option in options:
            rows.append({
                "merchant_id": goods.get("merchant_id"),
                "goods_id": goods.get("goods_id"),
                "goods_name": goods.get("name"),
                "spec_classify_id": group.get("spec_classify_id"),
                "spec_group_name": group.get("name"),
                "spec_group_min_select": group.get("min_select"),
                "spec_group_max_select": group.get("max_select"),
                "spec_id": option.get("spec_id"),
                "spec_name": option.get("name"),
                "price": option.get("price"),
                "origin_price": option.get("origin_price"),
                "status": option.get("status"),
                "create_time": option.get("create_time"),
                "update_time": option.get("update_time"),
            })

    return rows


def main():
    conn = sqlite3.connect(DB_PATH)

    merchant_rows = conn.execute(
        "SELECT merchant_id, merchant_name, raw_json FROM merchants"
    ).fetchall()

    goods_rows = conn.execute(
        "SELECT merchant_id, raw_json FROM goods_raw"
    ).fetchall()

    conn.close()

    shops = []
    merchant_name_map = {}

    for merchant_id, merchant_name, raw_json in merchant_rows:
        raw = safe_json_loads(raw_json, default={})
        shops.append(parse_shop(raw))
        merchant_name_map[str(merchant_id)] = merchant_name

    categories = []
    dishes = []
    specs = []

    for merchant_id, raw_json in goods_rows:
        raw = safe_json_loads(raw_json, default={})
        category_list = raw.get("data") or []

        if not isinstance(category_list, list):
            continue

        for category in category_list:
            categories.append(parse_category(category))

            for goods in category.get("goodss") or []:
                merchant_name = merchant_name_map.get(str(goods.get("merchant_id")), "")
                dishes.append(parse_dish(goods, category, merchant_name))
                specs.extend(parse_specs(goods))

    pd.DataFrame(shops).to_csv(
        OUTPUT_DIR / "shops.csv",
        index=False,
        encoding="utf-8-sig"
    )

    pd.DataFrame(categories).to_csv(
        OUTPUT_DIR / "categories.csv",
        index=False,
        encoding="utf-8-sig"
    )

    pd.DataFrame(dishes).to_csv(
        OUTPUT_DIR / "dishes.csv",
        index=False,
        encoding="utf-8-sig"
    )

    pd.DataFrame(specs).to_csv(
        OUTPUT_DIR / "specs.csv",
        index=False,
        encoding="utf-8-sig"
    )

    print("导出完成")
    print(f"店铺数：{len(shops)}")
    print(f"分类数：{len(categories)}")
    print(f"菜品数：{len(dishes)}")
    print(f"规格数：{len(specs)}")
    print("文件位置：output/")


if __name__ == "__main__":
    main()