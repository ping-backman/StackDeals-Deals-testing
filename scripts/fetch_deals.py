import os
import re
import json
import time
import hashlib
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

# 8 Verified Active US Deal XML RSS Feeds (Post-Nov 13, 2026 Reddit RSS shutdown)
RSS_SOURCES = [
    {
        "name": "Slickdeals Frontpage",
        "url": "https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&searchin=first&rss=1",
        "priority": 10
    },
    {
        "name": "Slickdeals Popular Deals",
        "url": "https://slickdeals.net/newsearch.php?mode=popdeals&searcharea=deals&searchin=first&rss=1",
        "priority": 9
    },
    {
        "name": "DealNews Most Popular",
        "url": "https://www.dealnews.com/f14/Most-Popular/?rss=1",
        "priority": 9
    },
    {
        "name": "DealNews Editors Choice",
        "url": "https://www.dealnews.com/f1912/Editors-Choice/?rss=1",
        "priority": 9
    },
    {
        "name": "Hip2Save Deals",
        "url": "https://hip2save.com/feed/",
        "priority": 9
    },
    {
        "name": "9to5Toys Tech Deals",
        "url": "https://9to5toys.com/feed/",
        "priority": 8
    },
    {
        "name": "Ben's Bargains Headlines",
        "url": "https://bensbargains.com/rss/",
        "priority": 8
    },
    {
        "name": "CamelCamelCamel Top Drops",
        "url": "https://camelcamelcamel.com/top_drops/feed",
        "priority": 9
    }
]

SKIMLINKS_SITE_ID = os.getenv("SKIMLINKS_SITE_ID", "").strip()
AMAZON_TAG = os.getenv("AMAZON_TAG", "").strip()
DEALS_DOMAIN = os.getenv("DEALS_DOMAIN", "https://deals.getstackdeals.xyz").rstrip("/")
PUBLIC_CAP = 55
VIP_CAP = 18
RETENTION_SECONDS = 48 * 3600

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; StackDealsBot/2.0; +https://deals.getstackdeals.xyz)"
}

def wrap_monetized_url(raw_url: str, deal_id: str) -> str:
    if not raw_url:
        return ""
    parsed = urllib.parse.urlparse(raw_url)
    clean_url = raw_url

    if "amazon.com" in parsed.netloc.lower():
        asin_match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", parsed.path, re.I)
        if asin_match:
            asin = asin_match.group(1).upper()
            clean_url = f"https://www.amazon.com/dp/{asin}"
            if AMAZON_TAG:
                clean_url = f"{clean_url}?tag={AMAZON_TAG}"

    if SKIMLINKS_SITE_ID:
        encoded_dest = urllib.parse.quote(clean_url, safe="")
        encoded_sref = urllib.parse.quote(f"{DEALS_DOMAIN}/?deal={deal_id}", safe="")
        return (
            f"https://go.skimresources.com/?id={SKIMLINKS_SITE_ID}"
            f"&url={encoded_dest}&xcust={deal_id}&sref={encoded_sref}"
        )
    return clean_url

def extract_direct_amazon_url(html_text: str, fallback_url: str) -> str:
    matches = re.findall(r'https?://(?:www\.)?amazon\.com/[^\s"\'<>]+', html_text or "", re.I)
    for url in matches:
        if re.search(r"/(?:dp|gp/product)/[A-Z0-9]{10}", url, re.I):
            return url
    return fallback_url

def extract_promo_code(text: str) -> str:
    patterns = [
        r'(?:promo\s*code|coupon\s*code|use\s*code|with\s*code|code)[:\s]+["\']?([A-Z0-9]{4,16})["\']?'
    ]
    stopwords = {"AMAZON", "PRIME", "FREE", "SHIPPING", "COUPON", "OFFER", "DEALS", "SAVE", "CART", "PAGE"}
    for pat in patterns:
        m = re.search(pat, text or "", re.I)
        if m:
            code = m.group(1).upper()
            if code not in stopwords and not code.isdigit():
                return code
    return ""

def extract_prices(title: str, desc: str):
    combined = f"{title} {re.sub(r'<[^>]+>', ' ', desc or '')}"
    raw_prices = re.findall(r'\$([0-9]{1,4}(?:\.[0-9]{2})?)', combined)
    nums = sorted(set(float(p) for p in raw_prices if 0.5 <= float(p) <= 2500.0))
    if not nums:
        return None, None
    deal_price = nums[0]
    retail_price = nums[-1] if len(nums) > 1 and nums[-1] > deal_price * 1.1 else None
    return deal_price, retail_price

def extract_image(item_elem, desc_html: str) -> str:
    for child in item_elem:
        tag_lower = child.tag.lower()
        if tag_lower.endswith("thumbnail") or tag_lower.endswith("content") or tag_lower.endswith("enclosure"):
            url = child.attrib.get("url", "")
            if url.startswith("http") and not url.endswith(".gif"):
                return url
    img_match = re.search(r'<img[^>]+src=["\'](https?://[^"\']+)["\']', desc_html or "", re.I)
    if img_match and not img_match.group(1).endswith(".gif"):
        return img_match.group(1)
    return ""

def categorize_deal(title: str) -> str:
    t = title.lower()
    if any(k in t for k in ["ssd", "laptop", "headphone", "earbud", "usb", "charger", "monitor", "tv", "anker", "apple", "galaxy", "tablet", "speaker", "camera"]):
        return "Tech & Electronics"
    if any(k in t for k in ["kitchen", "pan", "air fryer", "coffee", "vacuum", "tumbler", "towel", "bed", "sheet", "pillow", "blender", "organizer"]):
        return "Home & Kitchen"
    if any(k in t for k in ["drill", "tool", "dewalt", "milwaukee", "garage", "led", "light", "auto", "car", "battery"]):
        return "Tools & Auto"
    if any(k in t for k in ["shirt", "shoes", "jacket", "watch", "bag", "backpack", "nike", "adidas", "hoodie", "slipper"]):
        return "Apparel & Gear"
    if any(k in t for k in ["snack", "pack", "detergent", "soap", "shampoo", "vitamin", "gummies", "coffee", "tea", "paper"]):
        return "Everyday Essentials"
    return "Top Finds"

def score_deal(discount_pct: int, promo_code: str, source_priority: int, is_amazon: bool) -> int:
    score = source_priority * 5
    if is_amazon:
        score += 15
    if discount_pct >= 50:
        score += 35
    elif discount_pct >= 35:
        score += 20
    elif discount_pct >= 20:
        score += 10
    if promo_code:
        score += 20
    return score

def parse_rss_source(source: dict, now_ts: int) -> list:
    results = []
    try:
        req = urllib.request.Request(source["url"], headers=HEADERS)
        with urllib.request.urlopen(req, timeout=20) as resp:
            xml_data = resp.read()
        root = ET.fromstring(xml_data)
        for item in root.findall(".//item"):
            title = re.sub(r'\s+', ' ', (item.findtext("title") or "")).strip()
            link = (item.findtext("link") or "").strip()
            desc = (item.findtext("description") or "").strip()
            if not title or not link:
                continue

            combined = f"{title} {desc}"
            is_amazon = ("amazon" in combined.lower()) or ("camelcamelcamel" in source["url"])
            if not is_amazon:
                continue

            direct_url = extract_direct_amazon_url(desc, link)
            deal_id = "sd-" + hashlib.md5(title.lower().encode()).hexdigest()[:8]
            deal_price, retail_price = extract_prices(title, desc)
            promo_code = extract_promo_code(combined)
            image_url = extract_image(item, desc)

            discount_pct = 0
            if deal_price and retail_price and retail_price > deal_price:
                discount_pct = round(((retail_price - deal_price) / retail_price) * 100)

            badge = f"{discount_pct}% OFF" if discount_pct >= 15 else ("PROMO CODE" if promo_code else "HOT FIND")
            if promo_code and discount_pct >= 15:
                badge = f"{discount_pct}% OFF + CODE"

            quality_score = score_deal(discount_pct, promo_code, source["priority"], is_amazon)
            is_vip_exclusive = (discount_pct >= 48 or (bool(promo_code) and discount_pct >= 35))

            share_landing_url = f"{DEALS_DOMAIN}/?deal={deal_id}"
            fb_caption = (
                f"🔥 {badge}: {title}\n"
                + (f"💰 Now ${deal_price:.2f}" + (f" (Reg. ${retail_price:.2f})" if retail_price else "") + "\n" if deal_price else "")
                + (f"🎟️ Promo Code: {promo_code}\n" if promo_code else "")
                + "👇 Grab the direct link in the first comment!"
            )

            results.append({
                "id": deal_id,
                "manual_override": False,
                "is_vip": is_vip_exclusive,
                "source": source["name"],
                "category": categorize_deal(title),
                "badge": badge,
                "discount_pct": discount_pct,
                "quality_score": quality_score,
                "title": title[:145],
                "retail_price": retail_price,
                "deal_price": deal_price,
                "promo_code": promo_code,
                "affiliate_url": wrap_monetized_url(direct_url, deal_id),
                "raw_url": direct_url,
                "share_url": share_landing_url,
                "fb_caption": fb_caption,
                "fb_comment_link": f"👉 Get this deal here: {share_landing_url}",
                "image_url": image_url,
                "seed_likes": max(12, (quality_score // 2)),
                "created_at": now_ts
            })
    except Exception as exc:
        print(f"[WARN] {source['name']} skipped: {exc}")
    return results

def load_existing_json(path: str) -> list:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("deals", [])
    except Exception:
        return []

def main():
    now_ts = int(time.time())
    public_path = "deals/data/public-deals.json"
    vip_path = "deals/data/vip-deals.json"

    existing_public = [d for d in load_existing_json(public_path) if d.get("manual_override")]
    existing_vip = [d for d in load_existing_json(vip_path) if d.get("manual_override")]

    recent_public = [
        d for d in load_existing_json(public_path)
        if not d.get("manual_override") and (now_ts - d.get("created_at", 0)) < RETENTION_SECONDS
    ]
    recent_vip = [
        d for d in load_existing_json(vip_path)
        if not d.get("manual_override") and (now_ts - d.get("created_at", 0)) < RETENTION_SECONDS
    ]

    scraped = []
    for src in RSS_SOURCES:
        items = parse_rss_source(src, now_ts)
        print(f"[INFO] {src['name']}: {len(items)} Amazon deals parsed")
        scraped.extend(items)

    vip_map = {d["id"]: d for d in recent_vip}
    pub_map = {d["id"]: d for d in recent_public}

    for item in scraped:
        item["affiliate_url"] = wrap_monetized_url(item["raw_url"], item["id"])
        if item["is_vip"]:
            vip_map[item["id"]] = item
        else:
            pub_map[item["id"]] = item

    for m in existing_vip:
        m["affiliate_url"] = wrap_monetized_url(m.get("raw_url", m.get("affiliate_url", "")), m["id"])
        vip_map[m["id"]] = m

    for m in existing_public:
        m["affiliate_url"] = wrap_monetized_url(m.get("raw_url", m.get("affiliate_url", "")), m["id"])
        pub_map[m["id"]] = m

    vip_sorted = sorted(
        vip_map.values(),
        key=lambda x: (x.get("manual_override", False), x.get("created_at", 0), x.get("quality_score", 0)),
        reverse=True
    )[:VIP_CAP]

    pub_sorted = sorted(
        pub_map.values(),
        key=lambda x: (x.get("manual_override", False), x.get("created_at", 0), x.get("quality_score", 0)),
        reverse=True
    )[:PUBLIC_CAP]

    updated_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    os.makedirs("deals/data", exist_ok=True)

    with open(public_path, "w", encoding="utf-8") as f:
        json.dump({
            "updated_at": updated_iso,
            "total": len(pub_sorted),
            "instructions": "Edit any deal manually in GitHub. Set 'manual_override': true so the 6-hour bot preserves your edits.",
            "deals": pub_sorted
        }, f, indent=2)

    with open(vip_path, "w", encoding="utf-8") as f:
        json.dump({
            "updated_at": updated_iso,
            "total": len(vip_sorted),
            "instructions": "VIP-exclusive high-discount & promo-code deals unlocked at 5 qualified referrals + 1 completed task.",
            "deals": vip_sorted
        }, f, indent=2)

    print(f"[DONE] Saved {len(pub_sorted)} public deals and {len(vip_sorted)} VIP deals.")

if __name__ == "__main__":
    main()
