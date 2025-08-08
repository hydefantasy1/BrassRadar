
import os
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple
import re

import requests
import streamlit as st

# ----------------------------- Config -----------------------------
st.set_page_config(page_title="BrassRadar — eBay Model Train Tracker", layout="wide")

SEARCH_TERMS = ['"Micro-Metakit"', '"Micro-Feinmechanik"']
MARKETPLACES = ["EBAY_US", "EBAY_DE", "EBAY_GB", "EBAY_FR", "EBAY_IT", "EBAY_AT", "EBAY_AU"]
BUY_FILTER = "buyingOptions:{FIXED_PRICE|AUCTION}"
MAX_RESULTS_PER_QUERY = 300  # per marketplace and search term

ALLOW = ["brass","lok","lokomotive","locomotive","zug","train","dampflok","diesel","ho","h0","h-o","model","modell","bahn"]
DENY = ["wiha","schraubendreher","screwdriver","bit set","werkzeug","tool","spanner","pliers"]

FX = {"USD":1.0,"EUR":0.92,"GBP":0.78,"AUD":1.48}  # units per USD (update occasionally)

# ----------------------------- Helpers ----------------------------
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

@st.cache_data(ttl=3500)
def get_app_token(client_id: str, client_secret: str) -> str:
    url = "https://api.ebay.com/identity/v1/oauth2/token"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {"grant_type":"client_credentials", "scope":"https://api.ebay.com/oauth/api_scope"}
    r = requests.post(url, headers=headers, data=data, auth=(client_id, client_secret), timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]

def to_usd(value: Optional[float], ccy: Optional[str]) -> Optional[float]:
    if value is None or not ccy: return None
    c = ccy.upper()
    if c not in FX: return None
    return round(value/FX[c], 2)

def relevant(title: str) -> bool:
    t = (title or "").lower()
    if any(d in t for d in DENY): return False
    return any(a in t for a in ALLOW)

def flag_for_country(code: Optional[str]) -> str:
    if not code or len(code) != 2: return ""
    # Convert country code to regional indicator symbols
    base = 127397
    return "".join(chr(base + ord(c)) for c in code.upper())

def search_page(token: str, marketplace: str, q: str, limit: int = 50, offset: int = 0) -> Dict:
    url = "https://api.ebay.com/buy/browse/v1/item_summary/search"
    headers = {"Authorization": f"Bearer {token}", "Accept":"application/json", "X-EBAY-C-MARKETPLACE-ID": marketplace}
    params = {"q": q, "limit": min(limit, 200), "offset": offset, "sort":"NEWLY_LISTED", "filter": BUY_FILTER}
    r = requests.get(url, params=params, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()

def get_item_detail(token: str, marketplace: str, item_id: str) -> Dict:
    # Get extended fields including currentBidPrice and itemEndDate when present
    url = f"https://api.ebay.com/buy/browse/v1/item/{item_id}"
    headers = {"Authorization": f"Bearer {token}", "Accept":"application/json", "X-EBAY-C-MARKETPLACE-ID": marketplace}
    params = {"fieldgroups": "EXTENDED"}
    r = requests.get(url, params=params, headers=headers, timeout=30)
    if r.status_code != 200:
        return {}
    return r.json()

def paginate_search(token: str, marketplace: str, q: str, max_results: int) -> List[Dict]:
    results = []
    offset = 0
    while len(results) < max_results:
        js = search_page(token, marketplace, q, limit=50, offset=offset)
        items = js.get("itemSummaries", []) or []
        results.extend(items)
        # Determine next offset
        total = js.get("total", 0)
        offset += 50
        if offset >= total or not items:
            break
    return results[:max_results]

def fetch_once(token: str, marketplaces: List[str], search_terms: List[str]) -> List[Dict]:
    rows = []
    for mp in marketplaces:
        for term in search_terms:
            items = paginate_search(token, mp, term, MAX_RESULTS_PER_QUERY)
            for it in items:
                title = it.get("title","")
                if not relevant(title): 
                    continue
                price = (it.get("price") or {})
                ship = (it.get("shippingOptions") or [{}])[0].get("shippingCost", {})
                country = (it.get("itemLocation") or {}).get("country")
                buying_opts = ",".join(it.get("buyingOptions",[]) or [])
                # If auction, enrich with currentBidPrice & itemEndDate
                current_bid_value = None; current_bid_ccy = None; end_dt = None
                if "AUCTION" in buying_opts:
                    detail = get_item_detail(token, mp, it.get("itemId"))
                    cb = detail.get("currentBidPrice") or {}
                    current_bid_value = cb.get("value"); current_bid_ccy = cb.get("currency")
                    end_dt = detail.get("itemEndDate")
                rows.append({
                    "id": it.get("itemId"),
                    "title": title,
                    "img": (it.get("image") or {}).get("imageUrl",""),
                    "url": it.get("itemWebUrl",""),
                    "market": mp,
                    "country": country,
                    "opts": buying_opts,
                    "condition": it.get("condition",""),
                    "price_value": float(price.get("value")) if price.get("value") else None,
                    "price_ccy": price.get("currency"),
                    "ship_value": float(ship.get("value")) if ship.get("value") else None,
                    "ship_ccy": ship.get("currency"),
                    "current_bid_value": float(current_bid_value) if current_bid_value else None,
                    "current_bid_ccy": current_bid_ccy,
                    "end_time": end_dt,
                    "updated": utc_now(),
                })
    return rows

def sort_rows(rows: List[Dict], mode: str) -> List[Dict]:
    def price_plus_shipping_usd(row: Dict) -> float:
        pv = row.get("price_value"); pc = row.get("price_ccy")
        sv = row.get("ship_value"); sc = row.get("ship_ccy")
        usd_p = to_usd(pv, pc) or 0.0
        usd_s = to_usd(sv, sc) or 0.0
        # for auctions, prefer current bid if present
        if "AUCTION" in row.get("opts","") and row.get("current_bid_value"):
            usd_p = to_usd(row["current_bid_value"], row.get("current_bid_ccy")) or usd_p
        return usd_p + usd_s

    if mode == "newly_listed":
        return sorted(rows, key=lambda r: r.get("updated",""), reverse=True)
    if mode == "ending_soon":
        def end_key(r):
            v = r.get("end_time")
            try: return datetime.fromisoformat(v.replace("Z","+00:00")) if v else datetime.max
            except Exception: return datetime.max
        return sorted(rows, key=end_key)
    if mode == "price_ship_low":
        return sorted(rows, key=price_plus_shipping_usd)
    if mode == "price_ship_high":
        return sorted(rows, key=price_plus_shipping_usd, reverse=True)
    return rows

# ----------------------------- UI -------------------------------
st.title("BrassRadar — eBay Model Train Tracker")

with st.sidebar:
    st.header("Controls")
    marketplaces = st.multiselect("Marketplaces", MARKETPLACES, default=["EBAY_US","EBAY_DE","EBAY_GB"])
    sort_mode = st.selectbox("Sort by", [
        "Best match (default)",
        "Time: ending soonest",
        "Time: newly listed",
        "Price + Shipping: lowest first",
        "Price + Shipping: highest first",
    ], index=0)
    run = st.button("Fetch latest listings now", type="primary")
    st.caption("Secrets required: EBAY_CLIENT_ID, EBAY_CLIENT_SECRET")

client_id = st.secrets.get("EBAY_CLIENT_ID")
client_secret = st.secrets.get("EBAY_CLIENT_SECRET")

if not client_id or not client_secret:
    st.error("Missing secrets: set EBAY_CLIENT_ID and EBAY_CLIENT_SECRET in Settings → Secrets.")
else:
    token = get_app_token(client_id, client_secret)
    if run:
        data = fetch_once(token, marketplaces, SEARCH_TERMS)
        # map sort labels to internal codes
        code = {
            "Best match (default)": "best",
            "Time: ending soonest": "ending_soon",
            "Time: newly listed": "newly_listed",
            "Price + Shipping: lowest first": "price_ship_low",
            "Price + Shipping: highest first": "price_ship_high",
        }[sort_mode]
        data = sort_rows(data, code)
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
                            if item.get("img"):
                                st.image(item["img"], use_container_width=True)
                            title = item.get("title","(no title)")
                            st.markdown(f"**{title}**")
                            flag = flag_for_country(item.get("country"))
                            badge = "🟢 AUCTION (live)" if "AUCTION" in item.get("opts","") else "💰 FIXED PRICE"
                            st.caption(f"{badge} • {item.get('market')} {flag} • {item.get('condition') or 'Unknown'}")
                            # price
                            native_val = item.get("current_bid_value") if "AUCTION" in item.get("opts","") and item.get("current_bid_value") else item.get("price_value")
                            native_ccy = item.get("current_bid_ccy") if "AUCTION" in item.get("opts","") and item.get("current_bid_ccy") else item.get("price_ccy")
                            native = f"{native_val} {native_ccy}" if native_val is not None else "—"
                            usd = to_usd(native_val, native_ccy)
                            usd_txt = f" / {usd} USD" if usd is not None else ""
                            ship = ""
                            if item.get("ship_value") is not None:
                                ship = f"  (+{item['ship_value']} {item.get('ship_ccy','')} shipping)"
                            st.write(native + usd_txt + ship)
                            # end time for auctions
                            if "AUCTION" in item.get("opts","") and item.get("end_time"):
                                st.caption("Ends: " + item["end_time"])
                            if item.get("url"):
                                st.link_button("View on eBay", item["url"])
            st.success(f"Fetched {len(data)} items.")
    else:
        st.info("Click the button to fetch listings.")
