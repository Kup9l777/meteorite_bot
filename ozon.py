import requests
from typing import List, Dict, Any

class OzonClient:
    def __init__(self, client_id: str, api_key: str):
        self.base = "https://api-seller.ozon.ru"
        self.headers = {
            "Client-Id": str(client_id),
            "Api-Key": api_key,
            "Content-Type": "application/json"
        }

    def list_products(self, limit: int = 1000, visibility: str = "ALL") -> list[dict]:
        items = []
        last_id = ""
        while True:
            body = {"filter": {"visibility": visibility}, "last_id": last_id, "limit": limit}
            r = requests.post(f"{self.base}/v3/product/list", headers=self.headers, json=body, timeout=60)
            r.raise_for_status()
            data = r.json().get("result", {})
            chunk = data.get("items", [])
            if not chunk:
                break
            items.extend(chunk)
            last_id = data.get("last_id") or ""
            if not last_id:
                break
        return items

    def prices_by_product_ids(self, product_ids: List[int]) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for i in range(0, len(product_ids), 90):
            chunk = product_ids[i:i+90]
            r = requests.post(f"{self.base}/v4/product/info/prices", headers=self.headers, json={"product_id": chunk}, timeout=60)
            r.raise_for_status()
            for item in r.json().get("result", []):
                offer_id = item.get("offer_id") or str(item.get("product_id"))
                prices = item.get("prices", {}) or {}
                out[offer_id] = {
                    "product_id": item.get("product_id"),
                    "price": prices.get("price"),
                    "old_price": prices.get("old_price"),
                    "price_with_discount": prices.get("price_with_discount"),
                    "currency_code": prices.get("currency_code"),
                }
        return out
