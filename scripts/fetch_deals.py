import os
import re
import json
import time
import hashlib
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

# RICH RSS SOURCES: These feeds contain images and direct links natively in the XML.
# No HTML scraping required. 100% reliable.
RSS_SOURCES = [
    {"name": "Woot (Amazon Deals)", "url": "https://www.woot.com/rss", "priority": 10}, # Woot is owned by Amazon
    {"name": "9to5Toys Tech", "url": "https://9to5toys.com/feed/", "priority": 9},
    {"name": "Hip2Save", "url": "https://hip2save.com/feed/", "priority": 9},
    {"name": "TechBargains", "url": "https://www.techbargains.com/rss", "priority": 9},
    {"name": "CamelCamelCamel Drops", "url": "https://camelcamelcamel.com/top_drops/feed", "priority": 8}
]

SKIMLINKS_SITE_ID = os.getenv("SKIMLINKS_SITE_ID", "").strip()
AMAZON_TAG = os.getenv("AMAZON_TAG", "").strip()
DEALS_DOMAIN = os.getenv("DEALS_DOMAIN", "https://deals.getstackdeals.xyz").rstrip("/")

# Set to 'unlimited' in GitHub secrets when ready
PUBLIC_CAP = int(os.getenv("PUBLIC_CAP", "60")) if os.getenv("PUBLIC_CAP", "60").lower() != "unlimited" else 9999
VIP_CAP = int(os.getenv("VIP_CAP", "18")) if os.getenv("VIP_CAP", "18").lower() != "unlimited" else 9999
RETENTION_SECONDS = 48 * 3600

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

def wrap_monetized_url(raw_url: str, deal_id: str) -> str:
    if not raw_url: return ""
    parsed = urllib.parse.urlparse(raw_url)
    clean_url = raw_url

    # Strict Amazon validation
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

def extract_image_from_rss(item_elem) -> str:
    """Strictly extracts images from RSS XML tags. No HTML scraping."""
    blocked = ["pixel", "tracking", "facebook", "twitter", "google-analytics", "doubleclick", "icon", "logo"]
    
    # 1. Check media namespace (Woot, 9to5Toys use this)
    for child in item_elem:
        tag_lower = child.tag.lower()
        if "content" in tag_lower or "thumbnail" in tag_lower:
            url = child.attrib.get("url", "")
            if url.startswith("http") and not url.endswith(".gif") and not any(b in url.lower() for b in blocked):
                return url
                
    # 2. Check enclosure tags (Standard RSS images)
    for child in item_elem:
        if child.tag.lower().endswith("enclosure"):
            url = child.attrib.get("url", "")
            if url.startswith("http") and not url.endswith(".gif") and not any(b in url.lower() for b in blocked):
                return url
    return ""

def extract_prices(title: str, desc: str) -> tuple[float, float]:
    combined = f"{title} {re.sub(r'<[^>]+>', ' ', desc or '')}"
    
    # Look for explicit "Now/Was" or "$X / $Y" patterns
    now_match = re.search(r'(?:now|price|for|only)\s+\$([0-9]{1,4}(?:\.[0-9]{2})?)', combined, re.I)
    was_match = re.search(r'(?:was|list|regular|retail|originally)[^\$]*\$([0-9]{1,4}(?:\.[0-9]{2})?)', combined, re.I)
    
    deal_price = float(now_match.group(1)) if now_match else None
    retail_price = float(was_match.group(1)) if was_match else None
    
    # Fallback: find all prices, lowest is deal, highest is retail
    if not deal_price or not retail_price:
        raw_prices = re.findall(r'\$([0-9]{1,4}(?:\.[0-9]{2})?)', combined)
        nums = sorted(set(float(p) for p in raw_prices if 0.5 <= float(p) <= 2500.0))
        if nums:
            if not deal_price: deal_price = nums[0]
            if not retail_price and len(nums) > 1 and nums[-1] > deal_price * 1.15:
                retail_price = nums[-1]
                
    return deal_price, retail_price

def extract_promo_code(text: str) -> str:
    patterns = [r'(?:promo\s*code|coupon\s*code|use\s*code|with\s*code|code)[:\s]+["\']?([A-Z0-9]{4,16})["\']?']
    stopwords = {"AMAZON", "PRIME", "FREE", "SHIPPING", "COUPON", "OFFER", "DEALS", "SAVE", "CART", "PAGE", "CLICK", "HERE"}
    for pat in patterns:
        m = re.search(pat, text or "", re.I)
        if m:
            code = m.group(1).upper()
            if code not in stopwords and (re.search(r'[A-Z]', code) and re.search(r'[0-9]', code) or len(code) >= 6):
                return code
    return ""

def categorize_deal(title: str) -> str:
    t = title.lower()
    if any(k in t for k in ["ssd", "laptop", "headphone", "earbud", "usb", "charger", "monitor", "tv", "anker", "apple", "galaxy", "tablet", "speaker", "camera"]): return "Tech & Electronics"
    if any(k in t for k in ["kitchen", "pan", "air fryer", "coffee", "vacuum", "tumbler", "towel", "bed", "sheet", "pillow", "blender", "organizer"]): return "Home & Kitchen"
    if any(k in t for k in ["drill", "tool", "dewalt", "milwaukee", "garage", "led", "light", "auto", "car", "battery"]): return "Tools & Auto"
    if any(k in t for k in ["shirt", "shoes", "jacket", "watch", "bag", "backpack", "nike", "adidas", "hoodie", "slipper"]): return "Apparel & Gear"
    if any(k in t for k in ["snack", "pack", "detergent", "soap", "shampoo", "vitamin", "gummies", "coffee", "tea", "paper"]): return "Everyday Essentials"
    return "Top Finds"

def score_for_facebook(discount_pct: int, promo_code: str, title: str) -> int:
    """Scores deals based on what actually goes viral on Facebook."""
    score = 0
    if discount_pct >= 60: score += 50
    elif discount_pct >= 40: score += 30
    elif discount_pct >= 25: score += 15
    
    if promo_code: score += 20
    
    # "FB Bait" keywords that drive massive engagement
    fb_bait_words = ['glitch', 'free', 'coupon', 'error price', 'clearance', 'stock']
    if any(w in title.lower() for w in fb_bait_words): score += 30
        
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
            if not title or not link: continue

            # 1. STRICT QUALITY GATE: Must have an image in the RSS XML
            image_url = extract_image_from_rss(item)
            if not image_url:
                continue 
                
            combined = f"{title} {desc}"
            deal_id = generate_deal_id(title, link)
            existing_deal = existing_deals_map.get(deal_id)
            created_at = existing_deal["created_at"] if existing_deal else now_ts

            deal_price, retail_price = extract_prices(title, desc)
            promo_code = extract_promo_code(combined)
            
            # Must have a deal price to be useful on FB
            if not deal_price: continue

            discount_pct = 0
            if retail_price and retail_price > deal_price:
                discount_pct = round(((retail_price - deal_price) / retail_price) * 100)

            badge = f"{discount_pct}% OFF" if discount_pct >= 15 else ("PROMO CODE" if promo_code else "HOT FIND")
            if promo_code and discount_pct >= 15:
                badge = f"{discount_pct}% OFF + CODE"

            fb_score = score_for_facebook(discount_pct, promo_code, title)
            
            # VIP Logic: Extreme discounts or high-value coupons
            is_vip_exclusive = (discount_pct >= 50 or (bool(promo_code) and discount_pct >= 30))

            share_landing_url = f"{DEALS_DOMAIN}/?deal={deal_id}"
            
            # ==========================================
            # FACEBOOK READY CAPTION GENERATOR
            # ==========================================
            price_line = f"💰 Now ${deal_price:.2f}"
            if retail_price: price_line += f" (Reg. ${retail_price:.2f})"
            
            fb_caption = (
                f"🔥 {badge}: {title}\n"
                f"{price_line}\n"
                + (f"🎟️ Promo Code: {promo_code}\n" if promo_code else "")
                + f"👇 Grab it here: {share_landing_url}"
            )

            results.append({
                "id": deal_id,
                "manual_override": existing_deal.get("manual_override", False) if existing_deal else False,
                "is_vip": is_vip_exclusive,
                "source": source["name"],
                "category": categorize_deal(title),
                "badge": badge,
                "discount_pct": discount_pct,
                "fb_score": fb_score, # Used for sorting the most viral deals to the top
                "title": title[:145],
                "retail_price": retail_price,
                "deal_price": deal_price,
                "promo_code": promo_code,
                "affiliate_url": "",
                "raw_url": link,
                "share_url": share_landing_url,
                "image_url": image_url,
                "fb_caption": fb_caption, # Ready to copy-paste to Facebook
                "seed_likes": existing_deal.get("seed_likes", 0) if existing_deal else max(25, (fb_score // 2)),
                "created_at": created_at
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
        print(f"[INFO] {src['name']}: {len(items)} FB-Ready deals parsed")
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

    # Sort by FB Score (virality) first, then created_at (newest)
    vip_sorted = sorted(vip_map.values(), key=lambda x: (x.get("manual_override", False), x.get("fb_score", 0), x.get("created_at", 0)), reverse=True)[:VIP_CAP]
    pub_sorted = sorted(pub_map.values(), key=lambda x: (x.get("manual_override", False), x.get("fb_score", 0), x.get("created_at", 0)), reverse=True)[:PUBLIC_CAP]

    updated_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    os.makedirs("deals/data", exist_ok=True)

    with open(public_path, "w", encoding="utf-8") as f:
        json.dump({"updated_at": updated_iso, "total": len(pub_sorted), "deals": pub_sorted}, f, indent=2)
    with open(vip_path, "w", encoding="utf-8") as f:
        json.dump({"updated_at": updated_iso, "total": len(vip_sorted), "deals": vip_sorted}, f, indent=2)

    print(f"[DONE] Saved {len(pub_sorted)} public and {len(vip_sorted)} VIP deals.")

if __name__ == "__main__":
    main()
