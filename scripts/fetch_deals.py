#!/usr/bin/env python3
"""
deal_scraper.py — StackDeals Amazon deal aggregator (v2)

Changelog vs v1:
  1. PRIORITY FIX — real Amazon resolution: RSS snippet -> source page fetch ->
     redirect-wrapper decoding. Deals that never resolve to an Amazon product
     URL are dropped (config: DROP_UNRESOLVED) instead of silently linking the
     source listing page.
  2. Strict Amazon hostname validation (rejects amazon.com.evil.com etc.).
  3. Context-aware price extraction ("was/reg/~~" vs "now/only"), with a
     sane discount clamp. No more lowest-price-is-deal assumptions.
  4. Stricter promo-code extraction (explicit introducer required, stopword
     and prose-word rejection).
  5. Image validation: tracking pixels / beacons / 1x1 GIFs rejected.
  6. Dedup: ASIN-first keys + normalized-title fuzzy merge (numbers must
     match, so 6-pack != 12-pack).
  7. Public/VIP lists are mutually exclusive per deal ID (migration-safe).
  8. Ranking: quality_score first, recency as tiebreaker (manual overrides
     stay pinned).
  9. Re-scraped deals keep their original created_at/first_seen_at — no
     fake freshness; they expire 48h after first sighting.
 10. seed_likes removed. `likes` only carries engagement actually published
     in the source feed (comments/votes/likes), else 0. likes_source says
     where it came from — never display 0 as organic proof.
 11. Affiliate attribution: STUB ONLY (Skimlinks application pending). Plain
     links are generated with a one-time log notice; validation/fallback
     alerting intentionally not implemented yet.
"""

import os
import re
import json
import time
import gzip
import zlib
import hashlib
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

RSS_SOURCES = [
    {"name": "Slickdeals Frontpage",     "url": "https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&searchin=first&rss=1", "priority": 10},
    {"name": "Slickdeals Popular Deals", "url": "https://slickdeals.net/newsearch.php?mode=popdeals&searcharea=deals&searchin=first&rss=1", "priority": 9},
    {"name": "DealNews Most Popular",    "url": "https://www.dealnews.com/f14/Most-Popular/?rss=1",  "priority": 9},
    {"name": "DealNews Editors Choice",  "url": "https://www.dealnews.com/f1912/Editors-Choice/?rss=1", "priority": 9},
    {"name": "Hip2Save Deals",           "url": "https://hip2save.com/feed/",                         "priority": 9},
    {"name": "9to5Toys Tech Deals",      "url": "https://9to5toys.com/feed/",                         "priority": 8},
    {"name": "Ben's Bargains Headlines", "url": "https://bensbargains.com/rss/",                      "priority": 8},
    {"name": "CamelCamelCamel Top Drops","url": "https://camelcamelcamel.com/top_drops/feed",         "priority": 9},
]

SKIMLINKS_SITE_ID = os.getenv("SKIMLINKS_SITE_ID", "").strip()
AMAZON_TAG = os.getenv("AMAZON_TAG", "").strip()
DEALS_DOMAIN = os.getenv("DEALS_DOMAIN", "https://deals.getstackdeals.xyz").rstrip("/")
PUBLIC_CAP = 55
VIP_CAP = 18
RETENTION_SECONDS = 48 * 3600

# --- link resolution ---
FETCH_SOURCE_PAGES = True          # fetch the deal page when the RSS snippet has no Amazon link
MAX_PAGE_FETCHES_PER_SOURCE = 12   # runtime bound per feed
FETCH_TIMEOUT = 15
MAX_FETCH_BYTES = 2_000_000        # 2 MB page cap
PAGE_FETCH_DELAY = 0.5
DROP_UNRESOLVED = True             # skip deals we cannot tie to an Amazon product page
REDIRECT_PARAM_NAMES = {"url", "u2", "u", "dest", "target", "r", "out",
                        "go", "redirect", "link", "href", "to"}
REDIRECT_DECODE_DEPTH = 3

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; StackDealsBot/2.0; +https://deals.getstackdeals.xyz)",
    "Accept-Encoding": "gzip, deflate",
}

# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------

def fetch_url(url, timeout=FETCH_TIMEOUT, max_bytes=MAX_FETCH_BYTES):
    """Fetch a page with size cap and transparent gzip/deflate decoding."""
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raw = raw[:max_bytes]
        enc = (resp.headers.get("Content-Encoding") or "").lower()
    if enc == "gzip":
        raw = gzip.decompress(raw)
    elif enc == "deflate":
        raw = zlib.decompress(raw)
    return raw.decode("utf-8", errors="replace")


class PageFetcher:
    """Per-source budgeted fetcher so one slow feed can't blow the runtime."""

    def __init__(self):
        self.count = 0

    def __call__(self, url):
        if self.count >= MAX_PAGE_FETCHES_PER_SOURCE:
            return ""
        try:
            html = fetch_url(url)
            self.count += 1
            time.sleep(PAGE_FETCH_DELAY)
            return html
        except Exception as exc:
            print(f"[WARN] page fetch failed for {url[:80]}: {exc}")
            return ""


# --------------------------------------------------------------------------
# Amazon URL resolution (issue #1, #2, #3)
# --------------------------------------------------------------------------

ASIN_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?![A-Z0-9])", re.I)
AMAZON_HOST = "amazon.com"
HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
BARE_URL_RE = re.compile(r'https?://[^\s"\'<>)]+', re.I)

# links living near these words are "related products", not the deal itself
BAD_NEIGHBORHOOD = re.compile(
    r'related|sponsored|advertisement|also\s+(bought|viewed)|customers\s+also'
    r'|carousel|similar|recommended', re.I)


def valid_amazon_host(url):
    """Strict check: amazon.com or a *.amazon.com subdomain only."""
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        return False
    host = host.lower().rstrip(".")
    return host == AMAZON_HOST or host.endswith("." + AMAZON_HOST)


def canonical_amazon_url(url):
    """Return canonical https://www.amazon.com/dp/ASIN or None."""
    if not url or not valid_amazon_host(url):
        return None
    try:
        path = urllib.parse.urlparse(url).path
    except ValueError:
        return None
    m = ASIN_RE.search(path)
    if not m:
        return None
    asin = m.group(1).upper()
    if len(set(asin)) < 3:            # reject degenerate strings like 0000000000
        return None
    return f"https://www.amazon.com/dp/{asin}"


def extract_asin(url):
    canon = canonical_amazon_url(url) or ""
    m = ASIN_RE.search(urllib.parse.urlparse(canon).path) if canon else None
    return m.group(1).upper() if m else ""


def decode_wrapped_url(url, depth=0):
    """Unwrap tracker/redirect URLs that embed the destination in a query param,
    e.g. go.skimresources.com/?id=..&url=<encoded amazon url>."""
    if not url:
        return ""
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    if depth >= REDIRECT_DECODE_DEPTH:
        return url
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return url
    q = urllib.parse.parse_qs(parsed.query)
    for name in REDIRECT_PARAM_NAMES:
        for val in q.get(name, []):
            val = urllib.parse.unquote(urllib.parse.unquote(val)).strip()
            if val.startswith(("http://", "https://")):
                return decode_wrapped_url(val, depth + 1)
    return url


def _anchor_text(html, href_end):
    end = html.find("</a>", href_end)
    if end == -1 or end - href_end > 600:
        return ""
    return re.sub(r"<[^>]+>", " ", html[href_end:end])


def amazon_candidates(html, title=""):
    """Canonical Amazon product URLs found in HTML, best-first:
    1. anchor-text token overlap with the deal title,
    2. document position (the deal link usually precedes 'related' blocks).
    Links near sponsored/related blocks are skipped entirely."""
    if not html:
        return []
    title_tokens = set(norm_title(title).split())
    scored = []

    def _local_context(pos):
        """Context since the previous </a> (or 300 chars) — enough to see a
        'sponsored' wrapper or a 'Customers also viewed:' label without letting a
        distant label poison the actual deal link."""
        prev_close = html.rfind("</a>", 0, pos)
        start = max(prev_close + 4, pos - 300) if prev_close != -1 else max(0, pos - 300)
        return html[start:pos]

    def _add(pos, canon, anchor_text):
        anchor = set(norm_title(anchor_text).split())
        overlap = len(title_tokens & anchor)
        pos_bonus = 1.0 - (pos / max(len(html), 1))
        scored.append((overlap, pos_bonus, pos, canon))

    for m in HREF_RE.finditer(html):
        canon = canonical_amazon_url(decode_wrapped_url(html_unescape(m.group(1))))
        if not canon:
            continue
        if BAD_NEIGHBORHOOD.search(_local_context(m.start())):
            continue
        _add(m.start(), canon, _anchor_text(html, m.end()))
    # bare URLs in text (some feeds print links without anchor tags)
    for m in BARE_URL_RE.finditer(html):
        canon = canonical_amazon_url(decode_wrapped_url(html_unescape(m.group(0))))
        if canon and all(c[3] != canon for c in scored):
            if BAD_NEIGHBORHOOD.search(_local_context(m.start())):
                continue
            _add(m.start(), canon, "")
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    seen, out = set(), []
    for _, _, _, canon in scored:
        if canon not in seen:
            seen.add(canon)
            out.append(canon)
    return out


def html_unescape(s):
    return (s.replace("&amp;", "&").replace("&quot;", '"')
             .replace("&#34;", '"').replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">"))


def resolve_amazon_url(feed_link, desc_html, title, fetcher):
    """Snippet first, then the source deal page, then give up."""
    cands = amazon_candidates(desc_html, title)
    if cands:
        return cands[0]
    if FETCH_SOURCE_PAGES and fetcher is not None:
        page = fetcher(feed_link)
        cands = amazon_candidates(page, title)
        if cands:
            return cands[0]
    return ""


# --------------------------------------------------------------------------
# Extraction: prices (#4), promo codes (#5), images (#6)
# --------------------------------------------------------------------------

PRICE_RE = re.compile(r'\$\s*([0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)')
RETAIL_HINT = re.compile(
    r'reg\.?\b|regular|was\b|wrote|originally|list price|list\b|msrp|compare at'
    r'|before|normally|previously|~~|retail|sold for', re.I)
DEAL_HINT = re.compile(r'\b(now|only|deal|after code|as low as|final|clip|today|price drop)\b', re.I)
STRIKE_TAG_RE = re.compile(r'<(s|del|strike)[^>]*>(.*?)</\1>', re.I | re.S)


def extract_prices(title, desc_html):
    """Context-aware: 'reg/was/~~' prices are retail, 'now/only' prices are deal.
    Falls back to lowest-price-with-plausible-retail only when no hints exist."""
    html = desc_html or ""

    def _strike_repl(m):
        return " was " + re.sub(r"<[^>]+>", " ", m.group(2)) + " "

    html = STRIKE_TAG_RE.sub(_strike_repl, html)
    text = re.sub(r"<[^>]+>", " ", html)
    combined = re.sub(r"\s+", " ", f"{title or ''} {text}")

    retail_c, deal_c, all_c = [], [], []
    for m in PRICE_RE.finditer(combined):
        try:
            val = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if not (0.5 <= val <= 5000.0):
            continue
        all_c.append(val)
        ctx = combined[max(0, m.start() - 45):m.start()].lower()
        if RETAIL_HINT.search(ctx):
            retail_c.append(val)
        elif DEAL_HINT.search(ctx):
            deal_c.append(val)

    if not all_c:
        return None, None

    deal = min(deal_c) if deal_c else min(all_c)
    retail = None
    retail_pool = [r for r in (retail_c or all_c) if r > deal * 1.05]
    if retail_pool:
        retail = max(retail_pool)
    if retail and not (1.05 < retail / deal <= 20.0):
        retail = None
    return deal, retail


def compute_discount(deal, retail):
    if deal and retail and retail > deal:
        return max(1, min(round((retail - deal) / retail * 100), 95))
    return 0


PROMO_PATTERNS = [
    re.compile(r'(?:promo(?:tion)?\s*code|coupon\s*code|discount\s*code|use\s*code'
               r'|with\s*code|enter\s*code|apply\s*code|code\s*[:#])\s*'
               r'[\'"“”]?\s*([A-Za-z0-9][A-Za-z0-9\-_]{3,15})', re.I),
]
CODE_STOPWORDS = {
    "CODE", "PROMO", "COUPON", "OFFER", "DEALS", "DEAL", "SAVE", "SAVING", "EXTRA",
    "AMAZON", "PRIME", "FREE", "SHIPPING", "FREESHIPPING", "CLICK", "HERE", "NOW",
    "ONLY", "APPLY", "CLIP", "CART", "PAGE", "SALE", "HOT", "NEW", "OFF", "DISCOUNT",
    "USE", "ENTER", "WITH", "CODE:", "THE", "THIS", "THAT", "YOUR", "PRICE", "DROP",
}


def extract_promo_code(text, title=""):
    title_words = set(norm_title(title).split())
    for pat in PROMO_PATTERNS:
        m = pat.search(text or "")
        if not m:
            continue
        code = m.group(1).strip()
        up = code.upper()
        if up in CODE_STOPWORDS:
            continue
        if up.isdigit() or re.fullmatch(r"(19|20)\d\d", up):
            continue
        letters = sum(ch.isalpha() for ch in up)
        if letters == 0:
            continue
        # reject plain dictionary words lifted from the title (e.g. "SHIPPING")
        if up.lower() in title_words and letters >= 4:
            continue
        # reject lowercase prose (real codes are uppercase/mixed with digits)
        if code.islower() and letters >= 4 and not any(ch.isdigit() for ch in code):
            continue
        return up
    return ""


BAD_IMAGE_PARTS = ("pixel", "track", "beacon", "spacer", "doubleclick", "analytics",
                   "googletag", "1x1", "adsystem", "adserver", "impression", "counter")
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".avif")
CDN_HINT = re.compile(r'media-amazon|images-amazon|ssl-images-amazon|cloudfront|'
                      r'wp\.com|wp-content|cdn|static|imgs|assets', re.I)


def valid_image_url(u):
    if not u:
        return ""
    u = html_unescape(u.strip())
    if u.startswith("//"):
        u = "https:" + u
    if not u.startswith(("http://", "https://")):
        return ""
    low = u.lower()
    if any(b in low for b in BAD_IMAGE_PARTS) or low.endswith(".gif"):
        return ""
    try:
        p = urllib.parse.urlparse(u)
    except ValueError:
        return ""
    path = p.path.lower()
    fname = path.rsplit("/", 1)[-1]
    if path.endswith(IMG_EXT):
        return u
    if "." not in fname and CDN_HINT.search(p.hostname or ""):
        return u
    return ""


IMG_TAG_RE = re.compile(r'<img\b[^>]*>', re.I)


def extract_image(item_elem, desc_html):
    # 1) feed-native media: enclosure / media:content / media:thumbnail
    for child in item_elem:
        tag = child.tag.lower().split("}")[-1]
        if tag in ("enclosure", "content", "thumbnail"):
            url = child.attrib.get("url", "")
            if tag == "enclosure" and not child.attrib.get("type", "").startswith("image"):
                continue
            if tag == "content" and child.attrib.get("type", "") not in ("", "image/jpeg", "image/png", "image/webp"):
                continue
            ok = valid_image_url(url)
            if ok:
                return ok
    # 2) first usable <img> in the description
    for tag in IMG_TAG_RE.findall(desc_html or ""):
        wm = re.search(r'width=["\']?(\d+)', tag, re.I)
        hm = re.search(r'height=["\']?(\d+)', tag, re.I)
        if (wm and wm.group(1) == "1") or (hm and hm.group(1) == "1"):
            continue
        src = re.search(r'src=["\']([^"\']+)["\']', tag, re.I)
        if src:
            ok = valid_image_url(src.group(1))
            if ok:
                return ok
    return ""


def extract_engagement(item_elem, text):
    """Real social proof only. Returns (likes:int, source:str|None)."""
    for tag in ("comments", "{http://purl.org/rss/1.0/modules/slash/}comments"):
        v = item_elem.findtext(tag) if tag.startswith("{") else None
        if v is None and item_elem is not None:
            for ch in item_elem:
                if ch.tag.lower().split("}")[-1] == "comments" and (ch.text or "").strip().isdigit():
                    v = ch.text.strip()
                    break
        if v and str(v).strip().isdigit():
            return int(str(v).strip()), "comments"
    m = re.search(r'([\d.]+)\s*(k)?\s*(?:upvotes?|votes|likes?|thumbs|clicks)', text or "", re.I)
    if m:
        val = float(m.group(1)) * (1000 if m.group(2) else 1)
        return int(val), "feed_text"
    return 0, None


# --------------------------------------------------------------------------
# Dedup: ASIN-first keys + fuzzy normalized-title merge (#7)
# --------------------------------------------------------------------------

TITLE_SOURCE_SUFFIX = re.compile(
    r'\s*[–—|-]\s*(amazon\.com|slickdeals|dealnews|hip2save|9to5toys|bensbargains'
    r'|camelcamelcamel|deals?)\.?\s*$', re.I)


def norm_title(t):
    t = TITLE_SOURCE_SUFFIX.sub("", (t or "").lower())
    t = re.sub(r'\$\s?\d+(?:\.\d+)?', " ", t)          # prices change; they shouldn't fork IDs
    t = re.sub(r'[^a-z0-9\s]', " ", t)
    return re.sub(r'\s+', " ", t).strip()


def title_numbers(t):
    return set(re.findall(r'\d+', norm_title(t)))


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def titles_near_dup(a, b):
    ta, tb = set(norm_title(a).split()), set(norm_title(b).split())
    if not ta or not tb:
        return False
    if title_numbers(a) != title_numbers(b):   # 6-pack != 12-pack
        return False
    return jaccard(ta, tb) >= 0.8


def deal_key(asin, title):
    if asin:
        return "A:" + asin
    return "T:" + hashlib.md5(norm_title(title).encode()).hexdigest()[:12]


# --------------------------------------------------------------------------
# Categorization / scoring / badges
# --------------------------------------------------------------------------

def categorize_deal(title):
    t = (title or "").lower()
    if any(k in t for k in ["ssd", "laptop", "headphone", "earbud", "usb", "charger",
                            "monitor", "tv", "anker", "apple", "galaxy", "tablet",
                            "speaker", "camera", "iphone", "ipad", "keyboard", "mouse"]):
        return "Tech & Electronics"
    if any(k in t for k in ["kitchen", "pan", "air fryer", "coffee", "vacuum", "tumbler",
                            "towel", "bed", "sheet", "pillow", "blender", "organizer"]):
        return "Home & Kitchen"
    if any(k in t for k in ["drill", "tool", "dewalt", "milwaukee", "garage", "led",
                            "light", "auto", "car", "battery"]):
        return "Tools & Auto"
    if any(k in t for k in ["shirt", "shoes", "jacket", "watch", "bag", "backpack",
                            "nike", "adidas", "hoodie", "slipper", "dress", "socks"]):
        return "Apparel & Gear"
    if any(k in t for k in ["snack", "pack", "detergent", "soap", "shampoo", "vitamin",
                            "gummies", "tea", "paper", "cleaner", "wipes"]):
        return "Everyday Essentials"
    return "Top Finds"


def score_deal(discount_pct, promo_code, source_priority, likes):
    score = source_priority * 5
    if discount_pct >= 50:
        score += 35
    elif discount_pct >= 35:
        score += 20
    elif discount_pct >= 20:
        score += 10
    if promo_code:
        score += 20
    score += min(int(likes), 200) // 10          # real engagement nudges rank, never fabricates it
    return score


def make_badge(discount_pct, promo_code):
    if promo_code and discount_pct >= 15:
        return f"{discount_pct}% OFF + CODE"
    if discount_pct >= 15:
        return f"{discount_pct}% OFF"
    if promo_code:
        return "PROMO CODE"
    return "HOT FIND"


# --------------------------------------------------------------------------
# Monetization — STUB (Skimlinks application pending; see header note #11)
# --------------------------------------------------------------------------

_AFFILIATE_WARNED = False


def wrap_monetized_url(raw_url, deal_id):
    """Wrap a clean Amazon URL for monetization.

    STUB: no Skimlinks/Amazon-Associates attribution validation yet. When
    SKIMLINKS_SITE_ID is unset this returns the plain link and logs once.
    TODO(post-approval): verify the redirect chain returns an Amazon
    destination with the tag intact; alert + fall back to plain link on
    failure. Do not remove this note until implemented.
    """
    global _AFFILIATE_WARNED
    if not raw_url:
        return ""
    clean_url = raw_url
    if valid_amazon_host(raw_url):
        asin = extract_asin(raw_url)
        if asin:
            clean_url = f"https://www.amazon.com/dp/{asin}"
            if AMAZON_TAG:
                clean_url = f"{clean_url}?tag={AMAZON_TAG}"

    if SKIMLINKS_SITE_ID:
        encoded_dest = urllib.parse.quote(clean_url, safe="")
        encoded_sref = urllib.parse.quote(f"{DEALS_DOMAIN}/?deal={deal_id}", safe="")
        return (f"https://go.skimresources.com/?id={SKIMLINKS_SITE_ID}"
                f"&url={encoded_dest}&xcust={deal_id}&sref={encoded_sref}")
    if not _AFFILIATE_WARNED:
        print("[STUB] SKIMLINKS_SITE_ID not set — emitting plain links "
              "(affiliate attribution validation pending; see header note #11)")
        _AFFILIATE_WARNED = True
    return clean_url


def validate_affiliate_attribution(*args, **kwargs):
    """STUB — intentionally a no-op until the Skimlinks application is approved."""
    return {"ok": None, "note": "affiliate validation not implemented (stub)"}


# --------------------------------------------------------------------------
# RSS parsing
# --------------------------------------------------------------------------

def strip_tags(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or ""))


def parse_rss_source(source, now_ts, fetcher):
    results = []
    try:
        req = urllib.request.Request(source["url"], headers=HEADERS)
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            xml_data = resp.read(4_000_000)
        root = ET.fromstring(xml_data)
    except Exception as exc:
        print(f"[WARN] {source['name']} feed unavailable: {exc}")
        return results

    for item in root.findall(".//item"):
        title = re.sub(r"\s+", " ", (item.findtext("title") or "")).strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        if not title or not link:
            continue

        combined = strip_tags(f"{title} {desc}")
        # gate: keep only Amazon-relevant items (Camel feed is Amazon-only by design)
        if "amazon" not in combined.lower() and "camelcamelcamel" not in source["url"]:
            continue

        # --- issue #1: resolve the REAL Amazon product URL ---
        raw_url = resolve_amazon_url(link, desc, title, fetcher)
        if not raw_url:
            if DROP_UNRESOLVED:
                print(f"[SKIP] no Amazon link resolvable: {title[:70]}")
                continue
        asin = extract_asin(raw_url)

        deal_price, retail_price = extract_prices(title, desc)
        discount_pct = compute_discount(deal_price, retail_price)
        promo_code = extract_promo_code(combined, title)
        image_url = extract_image(item, desc)
        likes, likes_source = extract_engagement(item, combined)
        key = deal_key(asin, title)

        quality_score = score_deal(discount_pct, promo_code, source["priority"], likes)
        is_vip_exclusive = discount_pct >= 48 or (bool(promo_code) and discount_pct >= 35)

        share_landing_url = f"{DEALS_DOMAIN}/?deal={key}"
        badge = make_badge(discount_pct, promo_code)
        price_line = ""
        if deal_price:
            price_line = f"💰 Now ${deal_price:.2f}"
            if retail_price:
                price_line += f" (Reg. ${retail_price:.2f})"
        fb_caption = (f"🔥 {badge}: {title}\n"
                      + (price_line + "\n" if price_line else "")
                      + (f"🎟️ Promo Code: {promo_code}\n" if promo_code else "")
                      + "👇 Grab the direct link in the first comment!")

        results.append({
            "id": key,
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
            "asin": asin,
            "affiliate_url": wrap_monetized_url(raw_url, key),
            "raw_url": raw_url,
            "share_url": share_landing_url,
            "fb_caption": fb_caption,
            "fb_comment_link": f"👉 Get this deal here: {share_landing_url}",
            "image_url": image_url,
            "image_fallback": f"https://images-na.ssl-images-amazon.com/images/P/{asin}.01._SL500_.jpg" if asin else "",
            "likes": likes,
            "likes_source": likes_source,
            "created_at": now_ts,
            "first_seen_at": now_ts,
            "last_seen_at": now_ts,
        })
    return results


# --------------------------------------------------------------------------
# Merging (#8 migration, #9 staleness, manual-override preservation)
# --------------------------------------------------------------------------

def merge_deal(old, new):
    """Refresh a re-scraped deal WITHOUT resetting its age or clobbering manual edits."""
    if old.get("manual_override"):
        # hand-edited deal: preserve the editor's fields entirely; only refresh timestamps/monetization
        merged = dict(old)
        merged["last_seen_at"] = new["created_at"]
        merged["quality_score"] = max(old.get("quality_score", 0), new.get("quality_score", 0))
        return merged
    merged = dict(new)
    merged["created_at"] = old.get("created_at", new["created_at"])       # no fake freshness
    merged["first_seen_at"] = old.get("first_seen_at", old.get("created_at", new["created_at"]))
    merged["last_seen_at"] = new["created_at"]
    merged["quality_score"] = max(old.get("quality_score", 0), new.get("quality_score", 0))
    merged["discount_pct"] = max(old.get("discount_pct", 0), new.get("discount_pct", 0))
    merged["likes"] = max(old.get("likes", 0), new.get("likes", 0))
    if not merged.get("likes_source"):
        merged["likes_source"] = old.get("likes_source")
    for f in ("image_url", "retail_price", "promo_code", "raw_url", "asin", "badge"):
        if not merged.get(f) and old.get(f):
            merged[f] = old[f]
    return merged


def find_near_dup(item, existing_map):
    """Fuzzy pass: a slightly reworded title of the same product merges into
    the existing deal (earliest created_at wins)."""
    for k, old in existing_map.items():
        if old.get("manual_override"):
            continue
        if titles_near_dup(item["title"], old.get("title", "")):
            return k
    return None


def insert_deal(item, target_map, other_map):
    """Insert with dedup. `other_map` is the opposite tier: a deal migrating
    public<->VIP is removed from its old tier so it can't live in both."""
    k = item["id"]
    dup = find_near_dup(item, target_map)
    if dup:
        target_map[dup] = merge_deal(target_map[dup], item)
        other_map.pop(dup, None)
        return
    if k in target_map:
        target_map[k] = merge_deal(target_map[k], item)
    else:
        target_map[k] = item
    other_map.pop(k, None)   # #8: mutual exclusivity across tiers


# --------------------------------------------------------------------------
# IO + main
# --------------------------------------------------------------------------

def load_existing_json(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("deals", [])
    except Exception as exc:
        print(f"[WARN] could not read {path}: {exc}")
        return []


def main():
    now_ts = int(time.time())
    public_path = "deals/data/public-deals.json"
    vip_path = "deals/data/vip-deals.json"

    existing_public = [d for d in load_existing_json(public_path) if d.get("manual_override")]
    existing_vip = [d for d in load_existing_json(vip_path) if d.get("manual_override")]
    recent_public = [d for d in load_existing_json(public_path)
                     if not d.get("manual_override") and (now_ts - d.get("created_at", 0)) < RETENTION_SECONDS]
    recent_vip = [d for d in load_existing_json(vip_path)
                 if not d.get("manual_override") and (now_ts - d.get("created_at", 0)) < RETENTION_SECONDS]

    scraped = []
    for src in RSS_SOURCES:
        fetcher = PageFetcher()
        items = parse_rss_source(src, now_ts, fetcher)
        print(f"[INFO] {src['name']}: {len(items)} Amazon deals parsed "
              f"({fetcher.count} page fetches used)")
        scraped.extend(items)

    vip_map = {d["id"]: d for d in recent_vip}
    pub_map = {d["id"]: d for d in recent_public}

    for item in scraped:
        item["affiliate_url"] = wrap_monetized_url(item.get("raw_url", ""), item["id"])
        insert_deal(item, vip_map if item["is_vip"] else pub_map,
                    pub_map if item["is_vip"] else vip_map)

    # manual overrides are re-added last and always survive
    for m in existing_vip:
        m["affiliate_url"] = wrap_monetized_url(m.get("raw_url") or m.get("affiliate_url", ""), m["id"])
        vip_map[m["id"]] = m
        pub_map.pop(m["id"], None)
    for m in existing_public:
        m["affiliate_url"] = wrap_monetized_url(m.get("raw_url") or m.get("affiliate_url", ""), m["id"])
        pub_map[m["id"]] = m
        vip_map.pop(m["id"], None)

    # #10: score first, recency tiebreak, manual overrides pinned
    def rank_key(x):
        return (bool(x.get("manual_override")),
                x.get("quality_score", 0),
                x.get("created_at", 0))

    vip_sorted = sorted(vip_map.values(), key=rank_key, reverse=True)[:VIP_CAP]
    pub_sorted = sorted(pub_map.values(), key=rank_key, reverse=True)[:PUBLIC_CAP]

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
