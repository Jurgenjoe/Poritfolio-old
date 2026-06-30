#!/usr/bin/env python3
"""
ตรวจเมล Dime ใบยืนยันการซื้อขายใหม่ทุกวัน, ถอดรหัส PDF, parse ข้อมูลธุรกรรม,
แล้วเขียนลง Supabase ตาราง pending_imports (รอผู้ใช้กดยืนยันในเว็บ)

รันโดย GitHub Actions วันละครั้ง (08:00 น. ไทย)
PDF จะถูกลบทิ้งทันทีหลัง parse เสร็จ ไม่เก็บไว้ใน repo หรือที่ใดถาวร
"""
import os
import re
import sys
import json
import base64
import datetime
import urllib.request

import pikepdf
import pdfplumber

# ===== อ่านค่าจาก environment (มาจาก GitHub Secrets) =====
GMAIL_CLIENT_ID     = os.environ["GMAIL_CLIENT_ID"]
GMAIL_CLIENT_SECRET = os.environ["GMAIL_CLIENT_SECRET"]
GMAIL_REFRESH_TOKEN = os.environ["GMAIL_REFRESH_TOKEN"]
DIME_PDF_PASSWORD   = os.environ["DIME_PDF_PASSWORD"]
SUPABASE_URL        = os.environ["SUPABASE_URL"]
SUPABASE_KEY        = os.environ["SUPABASE_KEY"]

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"


def get_access_token():
    """แลก refresh token เป็น access token ใหม่ (อายุสั้น ใช้ครั้งเดียวจบ)"""
    data = urllib.parse.urlencode({
        "client_id": GMAIL_CLIENT_ID,
        "client_secret": GMAIL_CLIENT_SECRET,
        "refresh_token": GMAIL_REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())["access_token"]


def gmail_get(path, token, params=None):
    url = f"{GMAIL_API}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def search_dime_messages(token):
    """ค้นหาเมลจาก Dime ในรอบ 2 วันล่าสุด (กันเคส workflow ไม่ได้รันเมื่อวาน)"""
    query = 'from:no-reply@dime.co.th subject:"ใบยืนยันการซื้อขาย" newer_than:2d'
    result = gmail_get("messages", token, {"q": query, "maxResults": 20})
    return result.get("messages", [])


def get_message_detail(msg_id, token):
    return gmail_get(f"messages/{msg_id}", token, {"format": "full"})


def find_pdf_attachment(message):
    """หา attachment PDF ตัวแรกในเมล คืนค่า (attachment_id, filename)"""
    def walk(parts):
        for p in parts:
            filename = p.get("filename", "")
            if filename.lower().endswith(".pdf") and p.get("body", {}).get("attachmentId"):
                return p["body"]["attachmentId"], filename
            if "parts" in p:
                found = walk(p["parts"])
                if found:
                    return found
        return None

    payload = message.get("payload", {})
    parts = payload.get("parts", [])
    return walk(parts)


def download_attachment(msg_id, attachment_id, token):
    result = gmail_get(f"messages/{msg_id}/attachments/{attachment_id}", token)
    data = result["data"]
    # Gmail ใช้ base64url
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def decrypt_pdf(encrypted_bytes, password, out_path):
    """ถอดรหัส PDF ด้วย pikepdf แล้วเซฟไฟล์ที่ถอดแล้วชั่วคราว"""
    tmp_in = out_path + ".enc"
    with open(tmp_in, "wb") as f:
        f.write(encrypted_bytes)
    try:
        with pikepdf.open(tmp_in, password=password) as pdf:
            pdf.save(out_path)
        return True
    except pikepdf.PasswordError:
        return False
    finally:
        if os.path.exists(tmp_in):
            os.remove(tmp_in)


def extract_text(pdf_path):
    text = ""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
    return text


def parse_dime_rows(text):
    """เทียบเท่า parseAllDimeRows() ฝั่ง JS — คืน list ของ dict ธุรกรรม"""
    t = re.sub(r"\s+", " ", text)

    invoice_match = re.search(r"DIMEOS\d+", t)
    invoice_no = invoice_match.group() if invoice_match else None

    fx_match = re.search(r"THB/USD\s*=\s*([\d.]+)", t)
    fx_rate = float(fx_match.group(1)) if fx_match else None

    row_re = re.compile(
        r"(\d{6,})\s+(\d{2}/\d{2}/\d{4})\s+(BUY|SEL)\s+([A-Z]{1,6})\s+"
        r"([\d.]+)\s+([\d.]+)\s+USD\s+([\d,]+\.\d+)\s+([\d,]+\.\d+)\s+([\d,]+\.\d+)\s+([\d,]+\.\d+)\s+"
        r"\[([A-Z]+)\]\s+([\d,]+\.\d+)\s+([\d,]+\.\d+)\s+([\d,]+\.\d+)\s+([\d,]+\.\d+)"
    )

    rows = []
    for m in row_re.finditer(t):
        order_id, eff_date, tx_type, ticker, shares, unit_price, \
            gross_usd, fee_usd, wht_usd, total_usd, \
            exch, gross_thb, fee_thb, wht_thb, total_thb = m.groups()

        rows.append({
            "invoice_no": invoice_no,
            "order_id": order_id,
            "tx_type": "SELL" if tx_type == "SEL" else "BUY",
            "ticker": ticker,
            "shares": float(shares),
            "unit_price": float(unit_price),
            "gross_usd": float(gross_usd.replace(",", "")),
            "gross_thb": float(gross_thb.replace(",", "")),
            "fx_rate": fx_rate,
            "effective_date": eff_date,
        })
    return rows


def supabase_insert_pending(rows, email_message_id):
    """Insert เข้า Supabase pending_imports ผ่าน REST API (ข้าม row ที่ order_id ซ้ำและยัง pending)"""
    if not rows:
        return 0

    url = f"{SUPABASE_URL}/rest/v1/pending_imports"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=ignore-duplicates,return=minimal",
    }

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    payload = []
    for r in rows:
        payload.append({
            "id": "pi_" + r["order_id"] + "_" + now[:10].replace("-", ""),
            "invoice_no": r["invoice_no"],
            "order_id": r["order_id"],
            "ticker": r["ticker"],
            "tx_type": r["tx_type"],
            "shares": r["shares"],
            "unit_price": r["unit_price"],
            "gross_usd": r["gross_usd"],
            "gross_thb": r["gross_thb"],
            "fx_rate": r["fx_rate"],
            "effective_date": r["effective_date"],
            "source": "gmail-auto",
            "email_message_id": email_message_id,
            "detected_at": now,
            "status": "pending",
        })

    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            print(f"  Supabase insert OK: {resp.status}")
            return len(payload)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="ignore")
        print(f"  Supabase insert error {e.code}: {body}")
        return 0


def main():
    print("=== Dime Email Scanner ===")
    token = get_access_token()
    print("Gmail token OK")

    messages = search_dime_messages(token)
    print(f"พบเมลที่ตรงเงื่อนไข: {len(messages)} ฉบับ")

    if not messages:
        print("ไม่มีเมลใหม่ จบการทำงาน")
        return

    total_inserted = 0
    for msg_ref in messages:
        msg_id = msg_ref["id"]
        detail = get_message_detail(msg_id, token)
        attachment = find_pdf_attachment(detail)
        if not attachment:
            print(f"  [{msg_id}] ไม่พบ PDF แนบ ข้าม")
            continue

        attachment_id, filename = attachment
        print(f"  [{msg_id}] พบไฟล์แนบ: {filename}")

        pdf_bytes = download_attachment(msg_id, attachment_id, token)

        decrypted_path = f"/tmp/{msg_id}_decrypted.pdf"
        ok = decrypt_pdf(pdf_bytes, DIME_PDF_PASSWORD, decrypted_path)
        if not ok:
            print(f"  [{msg_id}] รหัสผ่านไม่ถูกต้อง ข้ามไฟล์นี้")
            continue

        try:
            text = extract_text(decrypted_path)
            rows = parse_dime_rows(text)
            print(f"  [{msg_id}] parse ได้ {len(rows)} รายการ")
            inserted = supabase_insert_pending(rows, msg_id)
            total_inserted += inserted
        finally:
            # ลบไฟล์ที่ถอดรหัสแล้วทิ้งทันที ไม่เก็บไว้
            if os.path.exists(decrypted_path):
                os.remove(decrypted_path)

    print(f"=== เสร็จสิ้น: insert ทั้งหมด {total_inserted} รายการ ===")


if __name__ == "__main__":
    import urllib.parse
    import urllib.error
    main()
