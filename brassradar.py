
import os
import io
import csv
import zipfile
import sqlite3
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional

import requests
import streamlit as st
import pandas as pd

# ----------------------------- Config -----------------------------
st.set_page_config(page_title="BrassRadar — eBay Model Train Tracker (Pro)", layout="wide")

SEARCH_TERMS = ['"Micro-Metakit"', '"Micro-Feinmechanik"']
MARKETPLACES_DEFAULT = ["EBAY_US", "EBAY_DE", "EBAY_GB", "EBAY_FR", "EBAY_IT", "EBAY_AT", "EBAY_AU"]
BUY_FILTER = "buyingOptions:{FIXED_PRICE|AUCTION}"
MAX_RESULTS_PER_QUERY = 600  # per marketplace & term

# Category IDs (mostly global across sites) — feel free to tweak
# 19119: Model Railroads & Trains; 122604: Locomotives; 122591: Freight Cars; 122595: Passenger Cars
DEFAULT_CATEGORY_IDS = ["19119","122604","122591","122595"]

ALLOW = ["brass","lok","lokomotive","locomotive","zug","train","dampflok","diesel","ho","h0","h-o","model","modell","bahn"]
DENY = ["wiha","schraubendreher","screwdriver","bit set","werkzeug","tool","spanner","pliers"]

FX = {"USD":1.0,"EUR":0.92,"GBP":0.78,"AUD":1.48}  # units per USD

DATA_DIR = "/mount/data" if os.path.exists("/mount/data") else "data"
DB_PATH = os.path.join(DATA_DIR, "brassradar.sqlite")
IMG_DIR = os.path.join(DATA_DIR, "images")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(IMG_DIR, exist_ok=True)

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
    base = 127397
    return "".join(chr(base + ord(c)) for c in code.upper())

def search_page(token: str, marketplace: str, q: str, category_ids: List[str], limit: int = 50, offset: int = 0) -> Dict:
    url = "https://api.ebay.com/buy/browse/v1/item_summary/search"
    headers = {"Authorization": f"Bearer {token}", "Accept":"application/json", "X-EBAY-C-MARKETPLACE-ID": marketplace}
    params = {
        "q": q,
        "limit": min(limit, 200),
        "offset": offset,
        "sort":"NEWLY_LISTED",
        "filter": BUY_FILTER,
        "category_ids": ",".join(category_ids) if category_ids else None
    }
    # remove None params
    params = {k:v for k,v in params.items() if v is not None}
    r = requests.get(url, params=params, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()

def get_item_detail(token: str, marketplace: str, item_id: str) -> Dict:
    url = f"https://api.ebay.com/buy/browse/v1/item/{item_id}"
    headers = {"Authorization": f"Bearer {token}", "Accept":"application/json", "X-EBAY-C-MARKETPLACE-ID": marketplace}
    params = {"fieldgroups": "EXTENDED"}
    r = requests.get(url, params=params, headers=headers, timeout=30)
    if r.status_code != 200:
        return {}
    return r.json()

def paginate_search(token: str, marketplace: str, q: str, category_ids: List[str], max_results: int) -> List[Dict]:
    results = []
    offset = 0
    while len(results) < max_results:
        js = search_page(token, marketplace, q, category_ids, limit=100, offset=offset)
        items = js.get("itemSummaries", []) or []
        results.extend(items)
        total = js.get("total", 0) or 0
        offset += 100
        if offset >= total or not items:
            break
    return results[:max_results]

def download_image(url: Optional[str], item_id: str) -> Optional[str]:
    if not url: return None
    fn = os.path.join(IMG_DIR, f"{item_id}.jpg")
    if os.path.exists(fn): return fn
    try:
        r = requests.get(url, timeout=30)
        if r.status_code == 200:
            with open(fn, "wb") as f:
                f.write(r.content)
            return fn
    except Exception:
        pass
    return None

# ----------------------------- DB -------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  item_id TEXT PRIMARY KEY,
  title TEXT,
  brand TEXT,
  marketplace TEXT,
  country TEXT,
  buying_options TEXT,
  is_auction INTEGER,
  condition TEXT,
  item_web_url TEXT,
  image_url TEXT,
  image_path TEXT,
  price_value REAL,
  price_ccy TEXT,
  ship_value REAL,
  ship_ccy TEXT,
  current_bid_value REAL,
  current_bid_ccy TEXT,
  end_time TEXT,
  price_usd REAL,
  ship_usd REAL,
  status TEXT,
  date_found TEXT,
  date_updated TEXT
);
CREATE TABLE IF NOT EXISTS price_history (
  item_id TEXT,
  observed_at TEXT,
  price_value REAL,
  price_ccy TEXT,
  current_bid_value REAL,
  current_bid_ccy TEXT,
  PRIMARY KEY (item_id, observed_at)
);
CREATE TABLE IF NOT EXISTS watchlist (
  item_id TEXT PRIMARY KEY,
  marketplace TEXT,
  end_time TEXT,
  added_at TEXT,
  last_checked TEXT,
  status TEXT, -- WATCHING, ENDED
  final_price REAL,
  final_currency TEXT
);
"""

def db():
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL;")
    return con

def init_db():
    with db() as con:
        con.executescript(SCHEMA)

def upsert_item(con, row: Dict):
    con.execute("""
        INSERT INTO items (item_id, title, brand, marketplace, country, buying_options, is_auction, condition,
                           item_web_url, image_url, image_path, price_value, price_ccy, ship_value, ship_ccy,
                           current_bid_value, current_bid_ccy, end_time, price_usd, ship_usd, status,
                           date_found, date_updated)
        VALUES (:item_id,:title,:brand,:marketplace,:country,:buying_options,:is_auction,:condition,
                :item_web_url,:image_url,:image_path,:price_value,:price_ccy,:ship_value,:ship_ccy,
                :current_bid_value,:current_bid_ccy,:end_time,:price_usd,:ship_usd,:status,
                :date_found,:date_updated)
        ON CONFLICT(item_id) DO UPDATE SET
            title=excluded.title,
            brand=excluded.brand,
            marketplace=excluded.marketplace,
            country=excluded.country,
            buying_options=excluded.buying_options,
            is_auction=excluded.is_auction,
            condition=excluded.condition,
            item_web_url=excluded.item_web_url,
            image_url=excluded.image_url,
            image_path=excluded.image_path,
            price_value=excluded.price_value,
            price_ccy=excluded.price_ccy,
            ship_value=excluded.ship_value,
            ship_ccy=excluded.ship_ccy,
            current_bid_value=excluded.current_bid_value,
            current_bid_ccy=excluded.current_bid_ccy,
            end_time=excluded.end_time,
            price_usd=excluded.price_usd,
            ship_usd=excluded.ship_usd,
            status=excluded.status,
            date_updated=excluded.date_updated
    """, row)
    con.execute("""
        INSERT OR REPLACE INTO price_history (item_id, observed_at, price_value, price_ccy, current_bid_value, current_bid_ccy)
        VALUES (:item_id, :observed_at, :price_value, :price_ccy, :current_bid_value, :current_bid_ccy)
    """, {
        "item_id": row["item_id"],
        "observed_at": utc_now(),
        "price_value": row.get("price_value"),
        "price_ccy": row.get("price_ccy"),
        "current_bid_value": row.get("current_bid_value"),
        "current_bid_ccy": row.get("current_bid_ccy"),
    })

def mark_ended(con, active_ids: set):
    cur = con.execute("SELECT item_id FROM items WHERE status='ACTIVE'")
    for (iid,) in cur.fetchall():
        if iid not in active_ids:
            con.execute("UPDATE items SET status='ENDED', date_updated=? WHERE item_id=?", (utc_now(), iid))

def add_to_watchlist(con, item_id: str, marketplace: str, end_time: Optional[str]):
    con.execute("""
        INSERT INTO watchlist (item_id, marketplace, end_time, added_at, last_checked, status, final_price, final_currency)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(item_id) DO NOTHING
    """, (item_id, marketplace, end_time, utc_now(), None, "WATCHING", None, None))

# -------------------- Notifications (ntfy + SMTP) -----------------
def send_ntfy(topic: str, title: str, message: str, base_url: str = "https://ntfy.sh"):
    try:
        url = f"{base_url.rstrip('/')}/{topic}"
        requests.post(url, data=message.encode("utf-8"), headers={"Title": title}, timeout=10)
    except Exception:
        pass

def send_email(smtp_host: str, smtp_user: str, smtp_pass: str, from_addr: str, to_addr: str, subject: str, body: str):
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to_addr
        with smtplib.SMTP_SSL(smtp_host, 465, timeout=15) as s:
            s.login(smtp_user, smtp_pass)
            s.sendmail(from_addr, [to_addr], msg.as_string())
    except Exception:
        pass

def notify_ended(item: Dict):
    title = "BrassRadar: Auction ended"
    body = f"{item.get('title','(no title)')}
Final (observed) price: {item.get('final_price')} {item.get('final_currency')}
{item.get('item_web_url','')}"
    # ntfy

    topic = st.secrets.get("NTFY_TOPIC")
    base = st.secrets.get("NTFY_URL", "https://ntfy.sh")
    if topic:
        send_ntfy(topic, title, body, base_url=base)
    # email

    smtp_host = st.secrets.get("SMTP_HOST")
    smtp_user = st.secrets.get("SMTP_USER")
    smtp_pass = st.secrets.get("SMTP_PASS")
    from_addr = st.secrets.get("SMTP_FROM")
    to_addr = st.secrets.get("SMTP_TO")
    if smtp_host and smtp_user and smtp_pass and from_addr and to_addr:
        send_email(smtp_host, smtp_user, smtp_pass, from_addr, to_addr, title, body)

def check_watchlist(token: str, window_minutes: int = 15) -> int:
    updated = 0
    with db() as con:
        rows = con.execute("SELECT item_id, marketplace, end_time, status FROM watchlist WHERE status='WATCHING'").fetchall()
        for item_id, mp, end_time, status in rows:
            try:
                # only poll when near/past end

                if end_time:

                    try:

                        end_dt = datetime.fromisoformat(end_time.replace("Z","+00:00"))

                    except Exception:

                        end_dt = None

                else:

                    end_dt = None

                if end_dt and end_dt > datetime.now(timezone.utc) + timedelta(minutes=window_minutes):

                    continue

                detail = get_item_detail(token, mp, item_id)

                cb = detail.get("currentBidPrice") or {}

                bid_v = cb.get("value"); bid_c = cb.get("currency")

                if not bid_v:

                    row = con.execute("SELECT status, current_bid_value, current_bid_ccy, item_web_url, title FROM items WHERE item_id=?", (item_id,)).fetchone()

                    if row:

                        status_db, last_bid_v, last_bid_c, url, title = row

                        if status_db == "ENDED" or last_bid_v is not None:

                            con.execute("UPDATE watchlist SET status='ENDED', final_price=?, final_currency=?, last_checked=? WHERE item_id=?",

                                        (last_bid_v, last_bid_c, utc_now(), item_id))

                            updated += 1

                            notify_ended({"title": title, "final_price": last_bid_v, "final_currency": last_bid_c, "item_web_url": url})

                    else:

                        con.execute("UPDATE watchlist SET status='ENDED', last_checked=? WHERE item_id=?", (utc_now(), item_id))

                        updated += 1

                else:

                    con.execute("UPDATE watchlist SET last_checked=? WHERE item_id=?", (utc_now(), item_id))

            except Exception:

                continue

        con.commit()

    return updated

def load_items(marketplaces: List[str]) -> List[Dict]:
    with db() as con:
        q = "SELECT * FROM items WHERE marketplace IN ({}) ORDER BY date_updated DESC".format(",".join(["?"]*len(marketplaces)))
        rows = [dict(zip([c[0] for c in con.execute("PRAGMA table_info(items)")], r)) for r in con.execute(q, marketplaces).fetchall()]
    return rows

# ----------------------------- Fetch -----------------------------
def fetch_and_store(token: str, marketplaces: List[str], search_terms: List[str], category_ids: List[str], save_images: bool) -> int:
    init_db()
    total = 0
    seen = set()
    with db() as con:
        for mp in marketplaces:
            for term in search_terms:
                items = paginate_search(token, mp, term, category_ids, MAX_RESULTS_PER_QUERY)
                for it in items:
                    title = it.get("title","")
                    if not relevant(title):
                        continue
                    price = (it.get("price") or {})
                    ship = (it.get("shippingOptions") or [{}])[0].get("shippingCost", {})
                    country = (it.get("itemLocation") or {}).get("country")
                    opts = ",".join(it.get("buyingOptions",[]) or [])
                    is_auction = 1 if "AUCTION" in opts else 0
                    # enrich for auctions
                    bid_v = None; bid_c = None; end_dt = None
                    if is_auction:
                        detail = get_item_detail(token, mp, it.get("itemId"))
                        cb = detail.get("currentBidPrice") or {}
                        bid_v = float(cb.get("value")) if cb.get("value") else None
                        bid_c = cb.get("currency")
                        end_dt = detail.get("itemEndDate")
                    native_v = bid_v if bid_v is not None else (float(price.get("value")) if price.get("value") else None)
                    native_c = bid_c if bid_c else price.get("currency")
                    image_url = (it.get("image") or {}).get("imageUrl","")
                    image_path = download_image(image_url, it.get("itemId")) if save_images else None
                    row = {
                        "item_id": it.get("itemId"),
                        "title": title,
                        "brand": "Micro-Metakit" if "metakit" in title.lower() else "Micro-Feinmechanik",
                        "marketplace": mp,
                        "country": country,
                        "buying_options": opts,
                        "is_auction": is_auction,
                        "condition": it.get("condition",""),
                        "item_web_url": it.get("itemWebUrl",""),
                        "image_url": image_url,
                        "image_path": image_path,
                        "price_value": native_v,
                        "price_ccy": native_c,
                        "ship_value": float(ship.get("value")) if ship.get("value") else None,
                        "ship_ccy": ship.get("currency"),
                        "current_bid_value": bid_v,
                        "current_bid_ccy": bid_c,
                        "end_time": end_dt,
                        "price_usd": to_usd(native_v, native_c),
                        "ship_usd": to_usd(float(ship.get("value")) if ship.get("value") else None, ship.get("currency")),
                        "status": "ACTIVE",
                        "date_found": utc_now(),
                        "date_updated": utc_now(),
                    }
                    upsert_item(con, row)
                    seen.add(row["item_id"])
                    total += 1
        mark_ended(con, seen)
        con.commit()
    return total

# ----------------------------- UI -------------------------------
st.title("BrassRadar — eBay Model Train Tracker (Pro)")

with st.sidebar:
    st.header("Controls")
    marketplaces = st.multiselect("Marketplaces", MARKETPLACES_DEFAULT, default=MARKETPLACES_DEFAULT[:3])
    category_ids = st.text_input("Category IDs (comma-separated)", value=",".join(DEFAULT_CATEGORY_IDS)).split(",")
    category_ids = [c.strip() for c in category_ids if c.strip()]
    save_images = st.toggle("Save listing images to storage", value=True)
    sort_mode = st.selectbox("Sort by", [
        "Newest updates",
        "Time: ending soonest",
        "Price + Shipping: lowest first (USD)",
        "Price + Shipping: highest first (USD)",
    ], index=0)
    do_fetch = st.button("Fetch latest listings now", type="primary")
    do_check = st.button("Check watched auctions now")
    st.caption("Secrets required: EBAY_CLIENT_ID, EBAY_CLIENT_SECRET. Optional: NTFY_TOPIC/NTFY_URL or SMTP_* for alerts.")

# Secrets
client_id = st.secrets.get("EBAY_CLIENT_ID")
client_secret = st.secrets.get("EBAY_CLIENT_SECRET")

if not client_id or not client_secret:
    st.error("Missing secrets: set EBAY_CLIENT_ID and EBAY_CLIENT_SECRET in Settings → Secrets.")
else:
    token = get_app_token(client_id, client_secret)
    if do_fetch:
        n = fetch_and_store(token, marketplaces, SEARCH_TERMS, category_ids, save_images)
        st.success(f"Fetched/updated {n} items. Stored in database.")
    if do_check:
        updated = check_watchlist(token, window_minutes=15)
        st.info(f"Checked watchlist. Updated {updated} records.")

# Load from DB
init_db()
with sqlite3.connect(DB_PATH) as con:
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM items WHERE marketplace IN ({}) ORDER BY date_updated DESC".format(",".join(["?"]*len(marketplaces))), marketplaces).fetchall()]
    watched = {r["item_id"]: dict(r) for r in con.execute("SELECT * FROM watchlist").fetchall()}

def price_ship_usd(r):
    return (r.get("price_usd") or 0.0) + (r.get("ship_usd") or 0.0)

# Export buttons
def df_items(rows): 
    return pd.DataFrame(rows)[[
        "item_id","title","brand","marketplace","country","buying_options","is_auction","condition",
        "price_value","price_ccy","price_usd","ship_value","ship_ccy","ship_usd",
        "current_bid_value","current_bid_ccy","end_time","status","item_web_url","image_url","image_path","date_updated"
    ]]
def df_watchlist():
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        wl = [dict(r) for r in c.execute("SELECT * FROM watchlist").fetchall()]
    return pd.DataFrame(wl)

c1, c2, c3 = st.columns(3)
with c1:
    if rows:
        csv_items = df_items(rows).to_csv(index=False).encode("utf-8")
        st.download_button("Download items CSV", csv_items, file_name="brassradar_items.csv", mime="text/csv")
with c2:
    wl_df = df_watchlist()
    if not wl_df.empty:
        csv_wl = wl_df.to_csv(index=False).encode("utf-8")
        st.download_button("Download watchlist CSV", csv_wl, file_name="brassradar_watchlist.csv", mime="text/csv")
with c3:
    # Zip images

    if any(r.get("image_path") for r in rows):
        mem = io.BytesIO()
        with zipfile.ZipFile(mem, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for r in rows:
                p = r.get("image_path")
                if p and os.path.exists(p):
                    zf.write(p, arcname=os.path.basename(p))
        mem.seek(0)
        st.download_button("Download images ZIP", mem, file_name="brassradar_images.zip", mime="application/zip")

# sort locally

if sort_mode == "Newest updates":
    rows = sorted(rows, key=lambda r: r.get("date_updated",""), reverse=True)
elif sort_mode == "Time: ending soonest":
    def end_key(r):
        v = r.get("end_time")
        try:
            return datetime.fromisoformat(v.replace("Z","+00:00")) if v else datetime.max
        except Exception:
            return datetime.max
    rows = sorted(rows, key=end_key)
elif sort_mode == "Price + Shipping: lowest first (USD)":
    rows = sorted(rows, key=price_ship_usd)
else:
    rows = sorted(rows, key=price_ship_usd, reverse=True)

# render grid with watch + chart

cols_per_row = 3
for i in range(0, len(rows), cols_per_row):
    cols = st.columns(cols_per_row)
    for col, r in zip(cols, rows[i:i+cols_per_row]):
        with col:
            with st.container(border=True):
                img_src = r.get("image_path") if r.get("image_path") and os.path.exists(r["image_path"]) else r.get("image_url")
                if img_src:
                    st.image(img_src, use_container_width=True)
                st.markdown(f"**{r.get('title','(no title)')}**")
                flag = (r.get("country") or "")
                try:
                    if len(flag) == 2:
                        base = 127397
                        flag = ''.join(chr(base + ord(c)) for c in flag.upper())
                except Exception:
                    pass
                badge = "🟢 AUCTION (live)" if r.get("is_auction") else "💰 FIXED PRICE"
                if r.get("status") == "ENDED":
                    badge = "⚪ AUCTION — ended" if r.get("is_auction") else "⛔ ENDED"
                st.caption(f"{badge} • {r.get('marketplace')} {flag} • {r.get('condition') or 'Unknown'}")
                native_val = r.get("price_value")
                native_ccy = r.get("price_ccy")
                native = f"{native_val} {native_ccy}" if native_val is not None else "—"
                usd = r.get("price_usd")
                usd_txt = f" / {usd} USD" if usd is not None else ""
                ship = ""
                if r.get("ship_value") is not None:
                    ship = f"  (+{r['ship_value']} {r.get('ship_ccy','')} shipping)"
                st.write(native + usd_txt + ship)
                if r.get("end_time") and r.get("is_auction"):
                    st.caption("Ends: " + r["end_time"])

                c1, c2 = st.columns([1,1])
                with c1:
                    if r.get("item_web_url"):
                        st.link_button("View on eBay", r["item_web_url"])
                with c2:
                    watched_flag = "✅ Watching" if r["item_id"] in watched and watched[r["item_id"]]["status"] == "WATCHING" else "Watch"
                    if st.button(watched_flag, key=f"watch_{r['item_id']}", disabled=watched_flag.startswith("✅")):
                        with sqlite3.connect(DB_PATH) as conw:
                            add_to_watchlist(conw, r["item_id"], r["marketplace"], r.get("end_time"))
                            st.experimental_rerun()

                # mini chart from price_history

                with sqlite3.connect(DB_PATH) as conh:
                    conh.row_factory = sqlite3.Row
                    ph = conh.execute("SELECT observed_at, COALESCE(current_bid_value, price_value) AS v FROM price_history WHERE item_id=? ORDER BY observed_at",
                                      (r["item_id"],)).fetchall()
                    if ph:
                        df = pd.DataFrame(ph, columns=["observed_at","v"]).set_index("observed_at")
                        st.line_chart(df, use_container_width=True)
