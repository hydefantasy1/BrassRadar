
import streamlit as st
import requests
from datetime import datetime
import pandas as pd

# Streamlit page config
st.set_page_config(page_title="BrassRadar", layout="wide")

# Title
st.title("BrassRadar — eBay Model Train Tracker")

# Search parameters
SEARCH_TERMS = ['"Micro-Metakit"', '"Micro-Feinmechanik"']
EBAY_APP_ID = st.secrets.get("EBAY_CLIENT_ID", None)

if not EBAY_APP_ID:
    st.warning("Please set your EBAY_CLIENT_ID in Streamlit Cloud secrets.")
else:
    st.sidebar.header("Controls")
    if st.sidebar.button("Fetch latest listings now"):
        all_results = []
        for term in SEARCH_TERMS:
            url = "https://api.ebay.com/buy/browse/v1/item_summary/search"
            params = {
                "q": term,
                "limit": 50,
                "filter": "buyingOptions:{FIXED_PRICE|AUCTION}"
            }
            headers = {"Authorization": f"Bearer {st.secrets['EBAY_OAUTH_TOKEN']}"}
            r = requests.get(url, params=params, headers=headers)
            if r.status_code == 200:
                data = r.json()
                for item in data.get("itemSummaries", []):
                    title = item.get("title", "")
                    category = item.get("categoryPath", "").lower()
                    if "tool" in title.lower() or "schraubendreher" in title.lower():
                        continue  # skip irrelevant items
                    price_info = item["price"]
                    shipping = item.get("shippingOptions", [{}])[0].get("shippingCost", {"value": "0"})
                    all_results.append({
                        "title": title,
                        "price_native": f"{price_info['value']} {price_info['currency']}", 
                        "price_usd": float(price_info['value']) * (1.1 if price_info['currency'] == 'EUR' else 1),
                        "shipping": f"{shipping['value']} {shipping.get('currency', '')}",
                        "condition": item.get("condition", ""),
                        "marketplace": item.get("itemWebUrl", ""),
                        "buyingOption": item.get("buyingOptions", []),
                        "image": item.get("image", {}).get("imageUrl", ""),
                        "updated": datetime.utcnow()
                    })
        
        # Display results in eBay-style grid
        if all_results:
            df = pd.DataFrame(all_results)
            for _, row in df.iterrows():
                with st.container():
                    cols = st.columns([1, 3])
                    with cols[0]:
                        st.image(row["image"], use_container_width=True)
                    with cols[1]:
                        st.markdown(f"**{row['title']}**")
                        st.write(f"{row['price_native']}  |  {round(row['price_usd'], 2)} USD")
                        st.write(f"Condition: {row['condition']}")
                        st.write(f"Shipping: {row['shipping']}")
                        badge = "🟢 Live Auction" if "AUCTION" in row["buyingOption"] else "💰 Fixed Price"
                        st.write(badge)
                        st.write(f"[View on eBay]({row['marketplace']})")
        else:
            st.info("No results found.")

