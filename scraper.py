import os
import re
import requests
import cloudscraper
import trafilatura
import extruct
from bs4 import BeautifulSoup
from w3lib.html import get_base_url
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

load_dotenv()

SCRAPINGBEE_API_KEY = os.getenv("SCRAPINGBEE_API_KEY")


def _is_blocked(html):
    """Returns True if the HTML looks like a Cloudflare block page,
    or if the page is a JS-rendered SPA shell with too little visible text."""
    if not html or len(html) < 500:
        return True
    lower = html.lower()
    # Hard block: explicit Cloudflare challenge phrases
    if (
        "enable javascript and cookies" in lower or
        "cf-browser-verification" in lower or
        ("just a moment" in lower and "cloudflare" in lower)
    ):
        return True
    # Soft block: HTML arrived but visible text is too sparse (SPA shell)
    soup = BeautifulSoup(html, "lxml")
    visible_text = soup.get_text(" ", strip=True)
    if len(visible_text.split()) < 100:
        return True
    return False


def fetch_html_scrapingbee(url, premium=False):
    """Fetch via ScrapingBee with JS rendering.
    premium=False → 5 credits per request (standard proxy)
    premium=True  → 75 credits per request (residential proxy, bypasses CF Pro)
    """
    params = {
        "api_key": SCRAPINGBEE_API_KEY,
        "url": url,
        "render_js": "true",
        "block_ads": "true",
    }
    if premium:
        params["premium_proxy"] = "true"
    response = requests.get(
        "https://app.scrapingbee.com/api/v1/",
        params=params,
        timeout=60
    )
    response.raise_for_status()
    return response.text


def fetch_html(url):
    """Fetch HTML with 4-level fallback:
    1. cloudscraper          — free, handles basic Cloudflare (0 credits)
    2. ScrapingBee standard  — JS rendering, shared proxy (5 credits)
    3. ScrapingBee premium   — JS rendering, residential proxy, CF Pro/Enterprise (75 credits)
    4. Playwright            — local headless browser, last resort
    """
    # --- Step 1: cloudscraper ---
    try:
        scraper = cloudscraper.create_scraper()
        html = scraper.get(url, timeout=20).text
        if not _is_blocked(html):
            print(f"    [scraper] cloudscraper OK: {url}")
            return html
        print(f"    [scraper] cloudscraper blocked, trying ScrapingBee standard...")
    except Exception as e:
        print(f"    [scraper] cloudscraper error: {e}")

    if SCRAPINGBEE_API_KEY:
        # --- Step 2: ScrapingBee standard (5 credits) ---
        try:
            html = fetch_html_scrapingbee(url, premium=False)
            if not _is_blocked(html):
                print(f"    [scraper] ScrapingBee standard OK: {url}")
                return html
            print(f"    [scraper] ScrapingBee standard blocked, trying ScrapingBee premium...")
        except Exception as e:
            print(f"    [scraper] ScrapingBee standard error: {e}")

        # --- Step 3: ScrapingBee premium (75 credits) ---
        try:
            html = fetch_html_scrapingbee(url, premium=True)
            if not _is_blocked(html):
                print(f"    [scraper] ScrapingBee premium OK: {url}")
                return html
            print(f"    [scraper] ScrapingBee premium also blocked, trying Playwright...")
        except Exception as e:
            print(f"    [scraper] ScrapingBee premium error: {e}")
    else:
        print(f"    [scraper] No SCRAPINGBEE_API_KEY set, skipping ScrapingBee...")

    # --- Step 4: Playwright ---
    print(f"    [scraper] Falling back to Playwright: {url}")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        page = browser.new_page()
        page.goto(url, wait_until="networkidle", timeout=30000)
        html = page.content()
        browser.close()
        return html


def extract_structured_data(html, url):
    base_url = get_base_url(html, url)
    data = extruct.extract(
        html,
        base_url=base_url,
        syntaxes=["json-ld", "microdata", "rdfa"]
    )
    return data


def extract_main_text(html):
    text = trafilatura.extract(
        html,
        include_links=False,
        include_images=False,
        no_fallback=False
    )
    if not text:
        return {"text": None, "word_count": 0}
    paragraphs = [p.strip() for p in text.split("\n") if len(p.strip()) > 40]
    return {
        "text": text[:8000],
        "paragraphs": paragraphs[:10],
        "word_count": len(text.split())
    }
def analyze_content(html):
    soup = BeautifulSoup(html, "lxml")

    # H1
    h1_tag = soup.find("h1")
    h1 = h1_tag.get_text(strip=True) if h1_tag else None

    # H2s
    h2s = [tag.get_text(strip=True) for tag in soup.find_all("h2")]

    # Images
    images = soup.find_all("img")
    missing_alt = sum(1 for img in images if not img.get("alt", "").strip())

    # Video
    has_video = bool(
        soup.find("video") or
        soup.find("iframe", src=re.compile(r"youtube|vimeo", re.I))
    )

    # Forms
    forms = soup.find_all("form")
    form_types = []
    for form in forms:
        text = form.get_text(" ", strip=True).lower()
        if any(w in text for w in ["search", "חיפוש"]):
            form_types.append("search")
        elif any(w in text for w in ["contact", "צור קשר", "שלח"]):
            form_types.append("contact")
        elif any(w in text for w in ["book", "reserve", "הזמן"]):
            form_types.append("booking")
        else:
            form_types.append("other")

    # FAQ patterns (headings or text ending with ?)
    all_text = soup.get_text(" ", strip=True)
    question_count = len(re.findall(r"[^.!]{10,}\?", all_text))
    faq_patterns = question_count >= 3

    # Contact info in text
    phone_match = re.search(r"(\+?\d[\d\-\s]{7,}\d)", all_text)
    email_match = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", all_text)
    address_keywords = ["רחוב", "street", "avenue", "ave", "rd", "boulevard"]
    has_address = any(kw in all_text.lower() for kw in address_keywords)

    # Page type suggestion based on URL and H1
    return {
        "h1": h1,
        "h2s": h2s[:10],
        "images": {
            "total": len(images),
            "missing_alt": missing_alt
        },
        "video": has_video,
        "forms": form_types if form_types else False,
        "faq_patterns": faq_patterns,
        "question_count": question_count,
        "contact_info": {
            "phone": phone_match.group(0).strip() if phone_match else None,
            "email": email_match.group(0) if email_match else None,
            "address_detected": has_address
        }
    }


def scan_page(url):
    html = fetch_html(url)
    return {
        "url": url,
        "structured_data": extract_structured_data(html, url),
        "content_analysis": analyze_content(html),
        "page_text": extract_main_text(html)
    }