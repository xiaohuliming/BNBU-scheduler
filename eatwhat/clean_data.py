import pandas as pd
from pathlib import Path

INPUT_DIR = Path("output")
CLEAN_DIR = Path("clean_output")
CLEAN_DIR.mkdir(exist_ok=True)

shops = pd.read_csv(INPUT_DIR / "shops.csv")
dishes = pd.read_csv(INPUT_DIR / "dishes.csv")
specs = pd.read_csv(INPUT_DIR / "specs.csv")

# 去重
shops = shops.drop_duplicates(subset=["merchant_id"])
dishes = dishes.drop_duplicates(subset=["goods_id"])
specs = specs.drop_duplicates(subset=["spec_id"])

# 只保留正常上架/售卖的菜品
# 一般 status=1, sale_status=1, shelves_status=1 表示可用
dishes_available = dishes[
    (dishes["status"] == 1) &
    (dishes["sale_status"] == 1) &
    (dishes["shelves_status"] == 1)
].copy()

# 价格转数字
dishes_available["price_min"] = pd.to_numeric(
    dishes_available["price_min"],
    errors="coerce"
)

dishes_available["price_max"] = pd.to_numeric(
    dishes_available["price_max"],
    errors="coerce"
)

# 删除没有菜品名的数据
dishes_available = dishes_available.dropna(subset=["goods_name"])

# 导出
shops.to_csv(CLEAN_DIR / "shops_clean.csv", index=False, encoding="utf-8-sig")
dishes_available.to_csv(CLEAN_DIR / "dishes_clean.csv", index=False, encoding="utf-8-sig")
specs.to_csv(CLEAN_DIR / "specs_clean.csv", index=False, encoding="utf-8-sig")

print("清洗完成")
print("店铺数：", len(shops))
print("可售菜品数：", len(dishes_available))
print("规格数：", len(specs))