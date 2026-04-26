import json
import time
import sqlite3
import requests
from urllib.parse import urlencode


BASE_URL = "https://api.25doer.com"

TEAM_ID = 1269
USER_TAG = "113.520645,22.350104"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, */*",
}


def request_json(path, params=None):
    url = BASE_URL + path

    for attempt in range(3):
        try:
            response = requests.get(
                url,
                params=params,
                headers=HEADERS,
                timeout=15
            )

            print("GET", response.url)
            response.raise_for_status()

            return response.json()

        except Exception as e:
            print(f"请求失败，第 {attempt + 1} 次：{e}")
            time.sleep(2)

    raise RuntimeError(f"请求失败：{url}")


def init_db():
    conn = sqlite3.connect("food_25doer.db")
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS merchants (
        merchant_id TEXT PRIMARY KEY,
        merchant_name TEXT,
        raw_json TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS goods_raw (
        merchant_id TEXT PRIMARY KEY,
        raw_json TEXT
    )
    """)

    conn.commit()
    return conn


def find_merchant_list(data):
    """
    尽量兼容不同 JSON 结构。
    目标是从返回数据里找到店铺列表。
    """

    if isinstance(data, dict):
        for key in ["list", "data", "records", "merchant_list", "merchantList"]:
            value = data.get(key)

            if isinstance(value, list):
                return value

            if isinstance(value, dict):
                result = find_merchant_list(value)
                if result:
                    return result

        for value in data.values():
            result = find_merchant_list(value)
            if result:
                return result

    return []


def get_merchant_id(merchant):
    return (
        merchant.get("merchant_id")
        or merchant.get("merchantId")
        or merchant.get("id")
    )


def get_merchant_name(merchant):
    return (
        merchant.get("merchant_name")
        or merchant.get("merchantName")
        or merchant.get("name")
        or merchant.get("shop_name")
        or merchant.get("shopName")
        or merchant.get("title")
    )


def save_merchant(conn, merchant):
    merchant_id = str(get_merchant_id(merchant))
    merchant_name = get_merchant_name(merchant)

    cur = conn.cursor()
    cur.execute("""
    INSERT OR REPLACE INTO merchants (
        merchant_id, merchant_name, raw_json
    )
    VALUES (?, ?, ?)
    """, (
        merchant_id,
        merchant_name,
        json.dumps(merchant, ensure_ascii=False)
    ))

    conn.commit()


def save_goods_raw(conn, merchant_id, data):
    cur = conn.cursor()
    cur.execute("""
    INSERT OR REPLACE INTO goods_raw (
        merchant_id, raw_json
    )
    VALUES (?, ?)
    """, (
        str(merchant_id),
        json.dumps(data, ensure_ascii=False)
    ))

    conn.commit()


def fetch_merchant_list(page=1, size=10):
    params = {
        "team_id": TEAM_ID,
        "is_takeout_index": 1,
        "page": page,
        "size": size,
        "user_tag": USER_TAG,
        "is_group_buy_index": 1,
        "order": 0,
        "filter_type": 0,
    }

    return request_json(
        "/api/user.Merchant/getMerchantList",
        params=params
    )


def fetch_all_classify_goods(merchant_id):
    params = {
        "merchant_id": merchant_id,
        "goods_id": "",
        "team_id": TEAM_ID,
    }

    return request_json(
        "/api/user.Goods/getNewAllClassifyGoods",
        params=params
    )


def crawl_merchants():
    conn = init_db()

    page = 1
    all_merchants = []

    while True:
        print(f"\n正在爬取店铺列表 page={page}")

        data = fetch_merchant_list(page=page, size=10)
        merchants = find_merchant_list(data)

        if not merchants:
            print("没有更多店铺，店铺列表爬取结束。")
            break

        print(f"本页发现 {len(merchants)} 个店铺")

        for merchant in merchants:
            merchant_id = get_merchant_id(merchant)
            merchant_name = get_merchant_name(merchant)

            if not merchant_id:
                print("跳过一个没有 merchant_id 的店铺：", merchant)
                continue

            print(f"店铺：{merchant_name} / merchant_id={merchant_id}")

            save_merchant(conn, merchant)
            all_merchants.append({
                "merchant_id": str(merchant_id),
                "merchant_name": merchant_name
            })

        page += 1
        time.sleep(1)

    conn.close()
    return all_merchants


def crawl_goods(all_merchants):
    conn = init_db()

    for index, merchant in enumerate(all_merchants, start=1):
        merchant_id = merchant["merchant_id"]
        merchant_name = merchant["merchant_name"]

        print(f"\n[{index}/{len(all_merchants)}] 正在爬取菜品：{merchant_name} / {merchant_id}")

        try:
            data = fetch_all_classify_goods(merchant_id)
            save_goods_raw(conn, merchant_id, data)
            print("菜品数据已保存")

        except Exception as e:
            print(f"爬取失败：merchant_id={merchant_id}, error={e}")

        time.sleep(1.2)

    conn.close()


def main():
    merchants = crawl_merchants()

    print(f"\n共发现 {len(merchants)} 个店铺，开始爬取菜品。")

    crawl_goods(merchants)

    print("\n全部完成，数据保存在 food_25doer.db")


if __name__ == "__main__":
    main()