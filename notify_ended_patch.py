
# --- only the notify_ended() function needs to be swapped into your file ---
def notify_ended(item: dict):
    # Build strings without nested f-strings to avoid any parsing quirks
    title = "BrassRadar: Auction ended"
    title_line = item.get("title", "(no title)")
    final_line = "Final (observed) price: {} {}".format(item.get("final_price"), item.get("final_currency"))
    url_line = item.get("item_web_url", "") or ""
    body = "{}\n{}\n{}".format(title_line, final_line, url_line)

    # ntfy push
    topic = st.secrets.get("NTFY_TOPIC")
    base = st.secrets.get("NTFY_URL", "https://ntfy.sh")
    if topic:
        try:
            url = "{}/{}".format(base.rstrip("/"), topic)
            requests.post(url, data=body.encode("utf-8"), headers={"Title": title}, timeout=10)
        except Exception:
            pass

    # email (SMTP)
    smtp_host = st.secrets.get("SMTP_HOST")
    smtp_user = st.secrets.get("SMTP_USER")
    smtp_pass = st.secrets.get("SMTP_PASS")
    from_addr = st.secrets.get("SMTP_FROM")
    to_addr = st.secrets.get("SMTP_TO")
    if smtp_host and smtp_user and smtp_pass and from_addr and to_addr:
        try:
            from email.mime.text import MIMEText
            import smtplib
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = title
            msg["From"] = from_addr
            msg["To"] = to_addr
            with smtplib.SMTP_SSL(smtp_host, 465, timeout=15) as s:
                s.login(smtp_user, smtp_pass)
                s.sendmail(from_addr, [to_addr], msg.as_string())
        except Exception:
            pass
