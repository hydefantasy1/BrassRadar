# BrassRadar - One-File Streamlit App (No CLI needed)
# ---------------------------------------------------
# Use this as your single deployable app on Streamlit Cloud or locally.
# - Click a button to fetch live listings (no terminal commands)
# - Browse results with filters and currency view (Native/USD/EUR)
# - Tracks Micro-Metakit & Micro-Feinmechanik across multiple eBay marketplaces

import os
import json
import time
import pathlib
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests
from requests.auth import HTTPBasicAuth
import streamlit as st

try:
    import pandas as pd
except Exception:
    pd = None

# --------------- App Config -------------------------
APP_NAME = "BrassRadar"
DATA_DIR = pathlib.Path("data")
DB_PATH = DATA_DIR / "brassradar.sqlite"
IMG_DIR = DATA_DIR / "images"
DATA_DIR.mkdir(parents=True, exist_ok=True)
IMG_DIR.mkdir(parents=True, exist_ok=True)

# Marketplaces to query each fetch
MARKETPLACES = [
    "EBAY_US",  # United States
    "EBAY_DE",  # Germany
    "EBAY_GB",  # United Kingdom
    "EBAY_FR",  # France
    "EBAY_IT",  # Italy
    "EBAY_AT",  # Austria
    "EBAY_AU",  # Australia
]

# Brands to track
BRANDS = [
    "Micro-Metakit",
    "Micro Feinmechanik",
    "Micro-Feinmechanik",
]

# eBay endpoints
EBAY_OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_BROWSE_SEARCH = "https://api.ebay.com/buy/browse/v1/item_summary/search"

# Include auctions + fixed price
BUYING_OPTIONS = ["FIXED_PRICE", "AUCTION"]
MAX_PER_PAGE = 50

# --- Quick FX (static; update as needed) ------------
# FX_RATES are units per 1 USD (1 USD = 0.92 EUR, etc.)
FX_RATES = {
    "USD": 1.00,
    "EUR": 0.92,
    "GBP": 0.78,
    "AUD": 1.48,
}

# --------------- Utils ------------------------------

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def env(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Missing environment variable: {key}")
    return val


def to_usd(amount: Optional[float], currency: Optional[str]) -> Optional[float]:
    if amount is None or not currency:
        return None
    cur = currency.upper()
    if cur not in FX_RATES:
        return None
    return round(amount / FX_RATES[cur], 2)


def usd_to_eur(amount_usd: Optional[float]) -> Optional[float]:
    if amount_usd is None:
        return None
    return round(amount_usd * FX_RATES["EUR"], 2)

# --------------- OAuth ------------------------------

def get_app_access_token(force_refresh: bool = False) -> str:
    """Application token via client-credentials; cached to disk."""
    cache = DATA_DIR / ".ebay_token_cache.json"
    if cache.exists() and not force_refresh:
        try:
            data = json.loads(cache.read_text())
            if data.get("expires_at", 0) > time.time() + 60:
                return data["access_token"]
        except Exception:
            pass

    client_id = env("EBAY_CLIENT_ID")
    client_secret = env("EBAY_CLIENT_SECRET")

    scope_list = ["https://api.ebay.com/oauth/api_scope"]
    payload = {
        "grant_type": "client_credentials",
        "scope": " ".join(scope_list),
    }

    resp = requests.post(
        EBAY_OAUTH_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=payload,
        auth=HTTPBasicAuth(client_id, client_secret),
        timeout=30,
    )
    resp.raise_for_status()
    tok = resp.json()
    access_token = tok["access_token"]
    expires_in = int(tok.get("expires_in", 7200))

    cache.write_text(json.dumps({
        "access_token": access_token,
        "expires_at": time.time() + expires_in,
        "cached_at": utc_now_iso(),
    }, indent=2))
    return access_token

# --------------- Database ---------------------------
SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS items (
  item_id TEXT PRIMARY KEY,
  title TEXT,
  brand TEXT,
  price_value REAL,
  price_currency TEXT,
  price_value_usd REAL,
  price_value_eur REAL,
  buying_options TEXT,
  condition TEXT,
  seller_username TEXT,
  marketplace TEXT,
  item_web_url TEXT,
  image_url TEXT,
  image_path TEXT,
  shipping_price REAL,
  shipping_currency TEXT,
  shipping_price_usd REAL,
  shipping_price_eur REAL,
  date_found TEXT,
  date_updated TEXT
);

CREATE TABLE IF NOT EXISTS price_history (
  item_id TEXT,
  observed_at TEXT,
  price_value REAL,
  price_currency TEXT,
  shipping_price REAL,
  shipping_currency TEXT,
  price_value_usd REAL,
  price_value_eur REAL,
  shipping_price_usd REAL,
  shipping_price_eur REAL,
  PRIMARY KEY (item_id, observed_at)
);
"""

def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH))
    con.execute("PRAGMA foreign_keys=ON;")
    return con


def init_db() -> None:
    with db() as con:
        con.executescript(SCHEMA)

# --------------- eBay Search ------------------------

def ebay_search_brand(brand: str, token: str, marketplace: str, *, limit: int = MAX_PER_PAGE) -> List[Dict]:
    params = {
        "q": brand,
        "limit": min(limit, 200),
        "sort": "NEWLY_LISTED",
    }
    filters = [f"buyingOptions:{{{','.join(BUYING_OPTIONS)}}}"]
    params["filter"] = ";".join(filters)

    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": marketplace,
        "Accept": "application/json",
    }
    r = requests.get(EBAY_BROWSE_SEARCH, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    js = r.json()
    return js.get("itemSummaries", [])

# --------------- Normalization ----------------------

def normalize_item(raw: Dict, brand_hint: str) -> Dict:
    def get(d, *keys, default=None):
        cur = d
        for k in keys:
            if cur is None:
                return default
            cur = cur.get(k)
        return cur if cur is not None else default

    price = get(raw, "price") or {}
    shipping = (get(raw, "shippingOptions") or [{}])[0].get("shippingCost", {})

    return {
        "item_id": raw.get("itemId"),
        "title": raw.get("title"),
        "brand": brand_hint,
        "price_value": float(price.get("value")) if price.get("value") else None,
        "price_currency": (price.get("currency") or "").upper() if price.get("currency") else None,
        "buying_options": ",".join(raw.get("buyingOptions", []) or []),
        "condition": raw.get("condition"),
        "seller_username": get(raw, "seller", "username"),
        "marketplace": raw.get("marketplaceId"),
        "item_web_url": raw.get("itemWebUrl"),
        "image_url": get(raw, "image", "imageUrl"),
        "shipping_price": float(shipping.get("value")) if shipping.get("value") else None,
        "shipping_currency": (shipping.get("currency") or "").upper() if shipping.get("currency") else None,
        "date_found": utc_now_iso(),
        "date_updated": utc_now_iso(),
    }

# --------------- Image download --------------------

def download_image(url: Optional[str], item_id: str) -> Optional[str]:
    if not url:
        return None
    try:
        ext = ".jpg"
        fname = DATA_DIR / "images" / f"{item_id}{ext}"
        if not fname.exists():
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            fname.write_bytes(r.content)
        return str(fname)
    except Exception:
        return None

# --------------- Upsert -----------------------------

def upsert_item(con: sqlite3.Connection, it: Dict) -> None:
    # compute normalized prices
    usd = to_usd(it.get("price_value"), it.get("price_currency"))
    eur = usd_to_eur(usd) if usd is not None else None
    ship_usd = to_usd(it.get("shipping_price"), it.get("shipping_currency"))
    ship_eur = usd_to_eur(ship_usd) if ship_usd is not None else None

    it_db = dict(it)
    it_db.update({
        "price_value_usd": usd,
        "price_value_eur": eur,
        "shipping_price_usd": ship_usd,
        "shipping_price_eur": ship_eur,
    })

    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO items (
          item_id, title, brand, price_value, price_currency, price_value_usd, price_value_eur,
          buying_options, condition, seller_username, marketplace, item_web_url, image_url, image_path,
          shipping_price, shipping_currency, shipping_price_usd, shipping_price_eur,
          date_found, date_updated
        ) VALUES (
          :item_id, :title, :brand, :price_value, :price_currency, :price_value_usd, :price_value_eur,
          :buying_options, :condition, :seller_username, :marketplace, :item_web_url, :image_url, :image_path,
          :shipping_price, :shipping_currency, :shipping_price_usd, :shipping_price_eur,
          :date_found, :date_updated
        ) ON CONFLICT(item_id) DO UPDATE SET
          title=excluded.title,
          brand=excluded.brand,
          price_value=excluded.price_value,
          price_currency=excluded.price_currency,
          price_value_usd=excluded.price_value_usd,
          price_value_eur=excluded.price_value_eur,
          buying_options=excluded.buying_options,
          condition=excluded.condition,
          seller_username=excluded.seller_username,
          marketplace=excluded.marketplace,
          item_web_url=excluded.item_web_url,
          image_url=excluded.image_url,
          image_path=excluded.image_path,
          shipping_price=excluded.shipping_price,
          shipping_currency=excluded.shipping_currency,
          shipping_price_usd=excluded.shipping_price_usd,
          shipping_price_eur=excluded.shipping_price_eur,
          date_updated=excluded.date_updated
        ;
        """,
        it_db,
    )

    cur.execute(
        """
        INSERT OR REPLACE INTO price_history (
          item_id, observed_at, price_value, price_currency, shipping_price, shipping_currency,
          price_value_usd, price_value_eur, shipping_price_usd, shipping_price_eur
        ) VALUES (
          :item_id, :observed_at, :price_value, :price_currency, :shipping_price, :shipping_currency,
          :price_value_usd, :price_value_eur, :shipping_price_usd, :shipping_price_eur
        );
        """,
        {
            "item_id": it["item_id"],
            "observed_at": utc_now_iso(),
            "price_value": it.get("price_value"),
            "price_currency": it.get("price_currency"),
            "shipping_price": it.get("shipping_price"),
            "shipping_currency": it.get("shipping_currency"),
            "price_value_usd": usd,
            "price_value_eur": eur,
            "shipping_price_usd": ship_usd,
            "shipping_price_eur": ship_eur,
        },
    )
    con.commit()

# --------------- Fetch ------------------------------

def fetch_once() -> int:
    init_db()
    token = get_app_access_token()
    total = 0
    with db() as con:
        for marketplace in MARKETPLACES:
            for brand in BRANDS:
                items = ebay_search_brand(brand, token, marketplace)
                for raw in items:
                    it = normalize_item(raw, brand)
                    it["marketplace"] = marketplace
                    it["image_path"] = download_image(it.get("image_url"), it["item_id"]) or None
                    upsert_item(con, it)
                    total += 1
    return total

# --------------- Streamlit UI -----------------------

def ensure_credentials_from_secrets():
    """If running on Streamlit Cloud, read secrets and set env automatically."""
    try:
        if "EBAY_CLIENT_ID" in st.secrets and "EBAY_CLIENT_SECRET" in st.secrets:
            os.environ["EBAY_CLIENT_ID"] = st.secrets["EBAY_CLIENT_ID"]
            os.environ["EBAY_CLIENT_SECRET"] = st.secrets["EBAY_CLIENT_SECRET"]
    except Exception:
        pass


def main():
    st.set_page_config(page_title=f"{APP_NAME}", layout="wide")
    st.title(f"{APP_NAME}: Micro-Metakit & Micro-Feinmechanik Tracker")

    ensure_credentials_from_secrets()

    with st.sidebar:
        st.header("Controls")
        if st.button("Fetch latest listings now", type="primary"):
            try:
                n = fetch_once()
                st.success(f"Fetched/updated {n} items at {utc_now_iso()}")
            except Exception as e:
                st.error(f"Fetch failed: {e}")
        st.caption("Tip: On Streamlit Cloud, add EBAY_CLIENT_ID / EBAY_CLIENT_SECRET in Settings -> Secrets.")

    if not DB_PATH.exists():
        st.info("No database yet. Click \"Fetch latest listings now\".", icon="🛈")
        return

    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row

    brand = st.multiselect("Brands", BRANDS, default=BRANDS)
    buying = st.multiselect("Buying options", ["AUCTION", "FIXED_PRICE"], default=["AUCTION", "FIXED_PRICE"])
    mkt = st.multiselect("Marketplaces", MARKETPLACES, default=MARKETPLACES)
    display_ccy = st.radio("Display currency", ["Native", "USD", "EUR"], horizontal=True)

    q = "SELECT * FROM items WHERE 1=1"
    params = []
    if brand:
        q += " AND brand IN (%s)" % (",".join(["?"] * len(brand)))
        params += brand
    if buying:
        like_clause = " OR ".join(["buying_options LIKE ?" for _ in buying])
        q += f" AND ({like_clause})"
        params += [f"%{b}%" for b in buying]
    if mkt:
        q += " AND marketplace IN (%s)" % (",".join(["?"] * len(mkt)))
        params += mkt
    q += " ORDER BY date_updated DESC"

    rows = con.execute(q, params).fetchall()

    c1, c2, c3 = st.columns(3)
    c1.metric("Tracked items", len(rows))
    cnt = con.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    c2.metric("Price observations", cnt)
    c3.write("DB:", str(DB_PATH))

    for row in rows:
        with st.container(border=True):
            col1, col2 = st.columns([1, 3])

            # Image
            img = row["image_path"]
            if img and pathlib.Path(img).exists():
                col1.image(img, use_column_width=True)
            else:
                col1.write("(no image)")

            # Title / meta
            col2.subheader(row["title"] or "(no title)")
            col2.caption(f"Brand: {row['brand']} • {row['condition'] or 'Unknown'} • {row['buying_options']} • {row['marketplace']}")

            # Price display
            native_txt = (
                f"{row['price_value']} {row['price_currency']}" if row["price_value"] is not None else "-"
            )
            if display_ccy == "USD" and row["price_value_usd"] is not None:
                price_txt = f"{row['price_value_usd']} USD"
            elif display_ccy == "EUR" and row["price_value_eur"] is not None:
                price_txt = f"{row['price_value_eur']} EUR"
            else:
                price_txt = native_txt

            # Shipping display
            ship_txt = ""
            if display_ccy == "USD" and row["shipping_price_usd"] is not None:
                ship_txt = f" (+{row['shipping_price_usd']} USD shipping)"
            elif display_ccy == "EUR" and row["shipping_price_eur"] is not None:
                ship_txt = f" (+{row['shipping_price_eur']} EUR shipping)"
            elif row["shipping_price"] is not None:
                ship_txt = f" (+{row['shipping_price']} {row['shipping_currency']} shipping)"

            col2.write(price_txt + ship_txt)
            if row["item_web_url"]:
                col2.link_button("View on eBay", row["item_web_url"])  # streamlit >=1.31
            col2.caption(f"Seller: {row['seller_username'] or '-'}  •  Updated: {row['date_updated']}")

            # Tiny price history chart (optional if pandas available)
            if pd is not None:
                hist = pd.read_sql_query(
                    "SELECT observed_at, price_value FROM price_history WHERE item_id=? ORDER BY observed_at",
                    con,
                    params=(row["item_id"],),
                )
                if not hist.empty:
                    st.line_chart(hist.set_index("observed_at")["price_value"], use_container_width=True)


if __name__ == "__main__":
    main()
