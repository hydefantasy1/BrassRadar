
import os
import time
import json
from datetime import datetime, timezone
from typing import List, Dict, Optional

import requests
import streamlit as st

# ---------------- Config ----------------
st.set_page_config(page_title="BrassRadar — eBay Model Train Tracker", layout="wide")

SEARCH_TERMS = ['"Micro-Metakit"', '"Micro-Feinmechanik"']
MARKETPLACES = ["EBAY_US", "EBAY_DE", "EBAY_GB"]  # add more if desired
BUY_FILTER = "buyingOptions:{FIXED_PRICE|AUCTION}"

ALLOW = ["brass","lok","lokomotive","locomotive","zug","train","dampflok","diesel","ho","h0","h-o","model","modell","bahn"]
DENY = ["wiha","schraubendreher","screwdriver","bit set","werkzeug","tool","spanner","pliers"]

FX = {"USD":1.0,"EUR":0.92,"GBP":0.78,"AUD":1.48}  # units per USD


# ---------------- Helpers ----------------
def utc_now():
    return datetime.now(timezone.utc).isoformat()


@st.cache_data(ttl=3500)
def get_app_token(client_id: str, client_secret: str) -> str:
    """Mint an application token via client-credentials."""
    url = "https://api.ebay.com/identity/v1/oauth2/token"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {
        "grant_type":"client_credentials",
        "scope":"https://api.ebay.com/oauth/api_scope"
    }
    r = requests.post(url, headers=headers, data=data, auth=(client_id, client_secret), timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def to_usd(value: Optional[float], ccy: Optional[str]) -> Optional[float]:
    if value is None or not ccy:
        return None
    c = ccy.upper()
    if c not in FX:
        return None
    return round(value/FX[c], 2)


def relevant(title: str) -> bool:
    t = (title or "").lower()
    if any(d in t for d in DENY):
        return False
    return any(a in t for a in ALLOW)


def fetch_once(token: str) -> List[Dict]:
    url = "https://api.ebay.com/buy/browse/v1/item_summary/search"
    headers = {"Authorization": f"Bearer {token}", "Accept":"application/json"}
    results = []
    for mp in MARKETPLACES:
        for q in SEARCH_TERMS:
            params = {"q": q, "limit": 50, "sort":"NEWLY_LISTED", "filter": BUY_FILTER}
            headers_with_market = dict(headers)
            headers_with_market["X-EBAY-C-MARKETPLACE-ID"] = mp
            r = requests.get(url, params=params, headers=headers_with_market, timeout=30)
            if r.status_code != 200:
                continue
            js = r.json()
            for it in js.get("itemSummaries", []):
                title = it.get("title","")
                if not relevant(title):
                    continue
                price = (it.get("price") or {})
                ship = (it.get("shippingOptions") or [{}])[0].get("shippingCost", {})
                results.append({
                    "id": it.get("itemId"),
                    "title": title,
                    "img": (it.get("image") or {}).get("imageUrl",""),
                    "url": it.get("itemWebUrl",""),
                    "market": it.get("marketplaceId", mp),
                    "opts": ",".join(it.get("buyingOptions",[]) or []),
                    "condition": it.get("condition",""),
                    "price_value": float(price.get("value")) if price.get("value") else None,
                    "price_ccy": price.get("currency"),
                    "ship_value": float(ship.get("value")) if ship.get("value") else None,
                    "ship_ccy": ship.get("currency"),
                    "updated": utc_now(),
                })
    return results


# ---------------- UI ----------------
st.title("BrassRadar — eBay Model Train Tracker")

with st.sidebar:
    st.header("Controls")
    st.caption("Set your secrets in Streamlit Cloud: EBAY_CLIENT_ID, EBAY_CLIENT_SECRET")
    run = st.button("Fetch latest listings now", type="primary")

client_id = st.secrets.get("EBAY_CLIENT_ID")
client_secret = st.secrets.get("EBAY_CLIENT_SECRET")

if not client_id or not client_secret:
    st.error("Missing secrets: please set EBAY_CLIENT_ID and EBAY_CLIENT_SECRET in Settings → Secrets.")
else:
    token = get_app_token(client_id, client_secret)
    if run:
        data = fetch_once(token)
        if not data:
            st.info("No results found right now.")
        else:
            # Render grid 3-across
            cols_in_row = 3
            for i in range(0, len(data), cols_in_row):
                cols = st.columns(cols_in_row)
                for col, item in zip(cols, data[i:i+cols_in_row]):
                    with col:
                        with st.container(border=True):
                            if item["img"]:
                                st.image(item["img"], use_container_width=True)
                            st.markdown(f"**{item['title']}**")
                            badge = "🟢 AUCTION (live)" if "AUCTION" in item["opts"] else "💰 FIXED PRICE"
                            st.caption(f"{badge} • {item['market']} • {item['condition'] or 'Unknown'}")
                            native = f"{item['price_value']} {item['price_ccy']}" if item["price_value"] else "—"
                            usd = to_usd(item['price_value'], item['price_ccy'])
                            usd_txt = f" / {usd} USD" if usd is not None else ""
                            ship = ""
                            if item["ship_value"] is not None:
                                ship = f"  (+{item['ship_value']} {item.get('ship_ccy','')} shipping)"
                            st.write(native + usd_txt + ship)
                            if item["url"]:
                                st.link_button("View on eBay", item["url"])
