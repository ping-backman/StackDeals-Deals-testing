import os
import re
import json
import time
import hashlib
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

# 8 Verified Active US Deal XML RSS Feeds
RSS_SOURCES = [
    {"name": "Slickdeals Frontpage", "url": "https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&searchin=first&rss=1", "priority": 10},
    {"name": "Slickdeals Popular Deals", "url": "https://slickdeals.net/newsearch.php?mode=popdeals&searcharea=deals&searchin=first&rss=1", "priority": 9},
    {"name": "DealNews Most Popular", "url": "https://www.dealnews.com/f14/Most-Popular/?rss=1", "priority": 9},
    {"name": "DealNews Editors Choice", "url": "https://www.dealnews.com/f1912/Editors-Choice/?rss=1", "priority": 9},
    {"name": "Hip2Save Deals", "url": "https://hip2save.com/feed/", "priority": 9},
    {"name": "9to5Toys Tech Deals", "url": "https://9to5toys.com/feed/", "priority": 8},
    {"name": "Ben's Bargains Headlines", "url": "https://bensbargains.com/rss/", "priority": 8},
    {"name": "CamelCamelCamel Top Drops", "url": "https://camelcamelcamel.com/top_drops/feed", "priority": 9}
]

SKIMLINKS_SITE_ID = os.getenv("SKIMLINKS_SITE_ID", "").strip()
AMAZON_TAG = os.getenv("AMAZON_TAG", "").strip()
DEALS_DOMAIN = os.getenv("DEALS_DOMAIN", "https://deals.getstackdeals.xyz").rstrip("/")

# Configurable caps: Set GitHub Secret PUBLIC_CAP=9999 to remove limits when quality is verified
PUBLIC_CAP = int(os.getenv("PUBLIC_CAP", "60")) if os.getenv("PUBLIC_CAP", "60").lower() != "unlimited" else 9999
VIP_CAP = int(os.getenv("VIP_CAP", "18")) if os.getenv("VIP_CAP", "18").lower() != "unlimited" else 9999
RETENTION_SECONDS = 48 * 3600

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Connection": "keep-alive"
}

def wrap_monetized_url(raw_url: str, deal_id: str) -> str:
    if not raw_url:
        return ""
    parsed = urllib.parse.urlparse(raw_url)
    clean_url = raw_url

    if re.search(r'(?i)^(?:https?://)?(?:www\.)?amazon\.com$', f"https://{parsed.netloc}"):
        asin_match = re.search(r"/(?:dp|gp/product|exec/obidos/ASIN)/([A-Z0-9]{10})", parsed.path, re.I)
        if asin_match:
            asin = asin_match.group(1).upper()
            clean_url = f"https://www.amazon.com/dp/{asin}"
            if AMAZON_TAG:
                clean_url = f"{clean_url}?tag={AMAZON_TAG}"

    if SKIMLINKS_SITE_ID:
        encoded_dest = urllib.parse.quote(clean_url, safe="")
        encoded_sref = urllib.parse.quote(f"{DEALS_DOMAIN}/?deal={deal_id}", safe="")
        return f"https://go.skimresources.com/?id={SKIMLINKS_SITE_ID}&url={encoded_dest}&xcust={deal_id}&sref={encoded_sref}"
    return clean_url

def extract_direct_amazon_url(html_text: str) -> str:
    matches = re.findall(r'https?://(?:www\.)?amazon\.com/(?:dp|gp/product|exec/obidos/ASIN)/[A-Z0-9]{10}[^\s"\'<>]*', html_text or "", re.I)
    if matches:
        return re.sub(r'[.,;:)]+$', '', matches[0])
    return ""

def resolve_amazon_from_source(source_url: str) -> tuple[str, str]:
    """Fetches the source deal page, follows redirects (like Slickdeals click.html), and extracts Amazon URL."""
    extraction_status = "failed"
    final_url = source_url
    try:
        req = urllib.request.Request(source_url, headers=HEADERS)
        # urllib.request.urlopen automatically follows HTTP 301/302 redirects
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode('utf-8', errors='ignore')
            final_url = resp.geturl() # Get the final URL after any redirects
            
        amazon_url = extract_direct_amazon_url(html)
        if amazon_url:
            extraction_status = "success_redirect_followed" if final_url != source_url else "success_direct_found"
            return amazon_url, extraction_status
            
        # If no Amazon link, but it redirected, note it
        if final_url != source_url:
            extraction_status = "redirect_followed_no_amazon"
            
    except Exception as e:
        extraction_status = f"fetch_error: {str(e)[:50]}"
    return "", extraction_status

def extract_promo_code(text: str) -> str:
    patterns = [
        r'(?:promo\s*code|coupon\s*code|use\s*code|with\s*code|code)[:\s]+["\']?([A-Z0-9]{4,16})["\']?',
        r'["\']([A-Z0-9]{4,16})["\']\s+(?:at\s+checkout|to\s+save|for\s+discount)',
    ]
    stopwords = {"AMAZON", "PRIME", "FREE", "SHIPPING", "COUPON", "OFFER", "DEALS", "SAVE", "CART", "PAGE", "CLICK", "HERE", "HTTP", "WWW"}
    for pat in patterns:
        m = re.search(pat, text or "", re.I)
        if m:
            code = m.group(1).upper()
            if code not in stopwords and (re.search(r'[A-Z]', code) and re.search(r'[0-9]', code) or len(code) >= 6):
                return code
    return ""

def extract_prices(title: str, desc: str) -> tuple[float, float, str]:
    combined = f"{title} {re.sub(r'<[^>]+>', ' ', desc or '')}"
    raw_price_text = ""
    
    # Look for explicit patterns first
    now_match = re.search(r'(?:now|price|for|only)\s+\$([0-9]{1,4}(?:\.[0-9]{2})?)', combined, re.I)
    was_match = re.search(r'(?:was|list|regular|retail|originally)[^\$]*\$([0-9]{1,4}(?:\.[0-9]{2})?)', combined, re.I)
    
    deal_price = None
    retail_price = None
    
    if now_match:
        deal_price = float(now_match.group(1))
        raw_price_text += f"Now:{deal_price} "
    if was_match:
        retail_price = float(was_match.group(1))
        raw_price_text += f"Was:{retail_price}"
        
    # Fallback heuristic
    if not deal_price or not retail_price:
        raw_prices = re.findall(r'\$([0-9]{1,4}(?:\.[0-9]{2})?)', combined)
        nums = sorted(set(float(p) for p in raw_prices if 0.5 <= float(p) <= 2500.0))
        if nums:
            if not deal_price:
                deal_price = nums[0]
            if not retail_price and len(nums) > 1 and nums[-1] > deal_price * 1.15:
                retail_price = nums[-1]
            raw_price_text += f"Heuristic:{nums}"
            
    return deal_price, retail_price, raw_price_text.strip()

def extract_image(item_elem, desc_html: str) -> str:
    blocked_domains = ["pixel", "tracking", "facebook", "twitter", "google-analytics", "doubleclick", "slickdeals.net/cdn"]
    
    # 1. Check media namespace tags (common in Slickdeals/WordPress)
    for child in item_elem:
        tag_lower = child.tag.lower()
        if "content" in tag_lower or "thumbnail" in tag_lower:
            url = child.attrib.get("url", "")
            if url.startswith("http") and not url.endswith(".gif") and not any(b in url.lower() for b in blocked_domains):
                return url

    # 2. Check description HTML
    img_match = re.search(r'<img[^>]+src=["\'](https?://[^"\']+)["\']', desc_html or "", re.I)
    if img_match:
        url = img_match.group(1)
        if not url.endswith(".gif") and not any(b in url.lower() for b in blocked_domains):
            return url
            
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
    if is_amazon: score += 15
    if discount_pct >= 50: score += 35
    elif discount_pct >= 35: score += 20
    elif discount_pct >= 20: score += 10
    if promo_code: score += 20
    return score

def generate_deal_id(title: str, source_url: str) -> str:
    normalized_title = re.sub(r'[^a-z0-9\s]', '', title.lower())[:60].strip()
    domain = urllib.parse.urlparse(source_url).netloc
    return "sd-" + hashlib.md5(f"{normalized_title}|{domain}".encode()).hexdigest()[:8]

def parse_rss_source(source: dict, now_ts: int, existing_deals_map: dict) -> list:
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
            
            direct_url = extract_direct_amazon_url(desc)
            extraction_status = "success_direct_in_rss"
            
            if not direct_url and is_amazon:
                direct_url, extraction_status = resolve_amazon_from_source(link)
            
            final_url = direct_url if direct_url else link
            deal_id = generate_deal_id(title, link)
            
            existing_deal = existing_deals_map.get(deal_id)
            created_at = existing_deal["created_at"] if existing_deal else now_ts

            deal_price, retail_price, raw_price_text = extract_prices(title, desc)
            promo_code = extract_promo_code(combined)
            image_url = extract_image(item, desc)

            discount_pct = 0
            if deal_price and retail_price and retail_price > deal_price:
                discount_pct = round(((retail_price - deal_price) / retail_price) * 100)

            badge = f"{discount_pct}% OFF" if discount_pct >= 15 else ("PROMO CODE" if promo_code else "HOT FIND")
            if promo_code and discount_pct >= 15:
                badge = f"{discount_pct}% OFF + CODE"

            quality_score = score_deal(discount_pct, promo_code, source["priority"], bool(direct_url and "amazon.com" in direct_url))
            is_vip_exclusive = (discount_pct >= 48 or (bool(promo_code) and discount_pct >= 35))

            share_landing_url = f"{DEALS_DOMAIN}/?deal={deal_id}"
            seed_likes = existing_deal.get("seed_likes", 0) if existing_deal else max(15, (quality_score // 2) + int(time.time() % 50))

            results.append({
                "id": deal_id,
                "manual_override": existing_deal.get("manual_override", False) if existing_deal else False,
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
                "affiliate_url": "",
                "raw_url": final_url,
                "share_url": share_landing_url,
                "image_url": image_url,
                "seed_likes": seed_likes,
                "created_at": created_at,
                # ADMIN DEBUG FIELDS: Hidden from frontend, vital for your manual review
                "admin_debug": {
                    "source_url": link,
                    "extraction_status": extraction_status,
                    "raw_price_text": raw_price_text,
                    "has_direct_amazon_link": bool(direct_url),
                    "resolved_url": direct_url if direct_url else "None"
                }
            })
    except Exception as exc:
        print(f"[WARN] {source['name']} skipped: {exc}")
    return results

def load_existing_json(path: str) -> list:
    if not os.path.exists(path): return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("deals", [])
    except Exception:
        return []

def main():
    now_ts = int(time.time())
    public_path = "deals/data/public-deals.json"
    vip_path = "deals/data/vip-deals.json"

    existing_public = load_existing_json(public_path)
    existing_vip = load_existing_json(vip_path)
    all_existing_map = {d["id"]: d for d in existing_public + existing_vip}

    recent_public = [d for d in existing_public if not d.get("manual_override") and (now_ts - d.get("created_at", 0)) < RETENTION_SECONDS]
    recent_vip = [d for d in existing_vip if not d.get("manual_override") and (now_ts - d.get("created_at", 0)) < RETENTION_SECONDS]

    scraped = []
    for src in RSS_SOURCES:
        items = parse_rss_source(src, now_ts, all_existing_map)
        print(f"[INFO] {src['name']}: {len(items)} deals parsed")
        scraped.extend(items)

    vip_map = {d["id"]: d for d in recent_vip}
    pub_map = {d["id"]: d for d in recent_public}

    for item in scraped:
        if not item.get("manual_override") and (now_ts - item.get("created_at", 0)) >= RETENTION_SECONDS:
            continue
            
        item["affiliate_url"] = wrap_monetized_url(item["raw_url"], item["id"])
        if item["is_vip"]:
            vip_map[item["id"]] = item
            if item["id"] in pub_map: del pub_map[item["id"]]
        else:
            if item["id"] not in vip_map:
                pub_map[item["id"]] = item

    for m in existing_vip + existing_public:
        if m.get("manual_override"):
            m["affiliate_url"] = wrap_monetized_url(m.get("raw_url", m.get("affiliate_url", "")), m["id"])
            if m["is_vip"] or m["id"] in vip_map:
                vip_map[m["id"]] = m
                if m["id"] in pub_map: del pub_map[m["id"]]
            else:
                pub_map[m["id"]] = m

    vip_sorted = sorted(vip_map.values(), key=lambda x: (x.get("manual_override", False), x.get("quality_score", 0), x.get("created_at", 0)), reverse=True)[:VIP_CAP]
    pub_sorted = sorted(pub_map.values(), key=lambda x: (x.get("manual_override", False), x.get("quality_score", 0), x.get("created_at", 0)), reverse=True)[:PUBLIC_CAP]

    updated_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    os.makedirs("deals/data", exist_ok=True)

    with open(public_path, "w", encoding="utf-8") as f:
        json.dump({"updated_at": updated_iso, "total": len(pub_sorted), "deals": pub_sorted}, f, indent=2)
    with open(vip_path, "w", encoding="utf-8") as f:
        json.dump({"updated_at": updated_iso, "total": len(vip_sorted), "deals": vip_sorted}, f, indent=2)

    print(f"[DONE] Saved {len(pub_sorted)} public and {len(vip_sorted)} VIP deals.")

if __name__ == "__main__":
    main()
