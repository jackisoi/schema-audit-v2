import os
import json
import anthropic
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

SYSTEM_PROMPT = "You are a Schema.org structured data expert and a JSON API. Return only a raw JSON array of Notion blocks. No markdown fences, no surrounding text."


def analyze_with_scan(scan_result, level, page_type, project):
    url = scan_result["url"]
    sd = scan_result["structured_data"]
    ca = scan_result["content_analysis"]
    pt = scan_result.get("page_text", {})

    json_ld = sd.get("json-ld", [])
    existing_schemas = json.dumps(json_ld, ensure_ascii=False) if json_ld else "None found"

    content_summary = f"""H1: {ca.get('h1') or 'Not found'}
H2s: {', '.join(ca.get('h2s', [])[:5]) or 'None'}
Images: {ca.get('images', {}).get('total', 0)} total, {ca.get('images', {}).get('missing_alt', 0)} missing alt
Video: {'Yes' if ca.get('video') else 'No'}
Forms: {ca.get('forms') or 'None'}
FAQ patterns: {'Yes' if ca.get('faq_patterns') else 'No'}
Phone: {ca.get('contact_info', {}).get('phone') or 'Not found'}
Email: {ca.get('contact_info', {}).get('email') or 'Not found'}"""

    page_text = pt.get("text") or ""

    path_parts = [p for p in urlparse(url).path.split('/') if p]
    is_subpage = len(path_parts) >= 2
    subpage_note = "YES - reference parent @id, do not redefine parent entity" if is_subpage else "NO - this is a top-level page"

    def build_prompt(text_limit):
        instructions = PROMPT_TEMPLATE.replace("__SUBPAGE_NOTE__", subpage_note)
        return f"""URL: {url}
Project: {project}
Page type: {page_type}
Level: {level}

CONTENT ANALYSIS:
{content_summary}

PAGE TEXT (first {text_limit} chars):
{page_text[:text_limit]}

EXISTING SCHEMAS:
{existing_schemas[:3000]}

STEP 1 - Analyze existing schemas:
Analyze the schemas above carefully for:
- Errors and wrong types
- Missing required properties
- Duplicate schemas
- Wrong schema type for this page type
If no existing schemas were provided, state "No existing schemas were provided."

STEP 2 - Cross-page awareness:
- Level 1 (home page): implement WebSite, Organization, WebPage
- Any other level: do NOT implement Organization or WebSite
- Sub-page status for this URL: {subpage_note}
- If sub-page: reference the parent entity using its @id - do NOT redefine the parent entity schema

STEP 3 - Produce the report as a JSON array of Notion blocks covering:
1. Executive Summary (heading_2 + paragraph) - schema health score (Poor/Fair/Good/Excellent), what was found, what is missing, urgent issues with warning emoji
2. Schemas to Implement (heading_2 + paragraph + code block per schema)
3. Fixes to Existing Schemas (heading_2 + paragraph + code block)
4. Additional Notes (heading_2 + paragraph) - CMS notes, Yoast conflicts

Rules:
- Return a raw JSON array only, no surrounding text
- All report text in English
- Schema JSON-LD field values must match the actual page language
- Before each code block, state explicitly: NEW ADDITION or REPLACEMENT FOR EXISTING [type] SCHEMA
- Never invent or estimate property values - use: // REQUIRED: fill in actual value
- Never write field values not explicitly present on the page
- Never add starRating or aggregateRating unless confirmed on the page
- Never include sameAs: [] - if social URLs unknown, omit sameAs entirely
- Never recommend SearchAction unless a search input is confirmed on the page
- Do NOT add FAQPage unless real Q&A content exists on the page
- Do NOT redefine Yoast-managed schemas (WebPage, BreadcrumbList, WebSite) - only flag issues
- Do NOT add Organization or WebSite on non-homepage pages
- For FAQPage with more than 3 questions: write a summary only, no full schema
- Each code block must not exceed 1900 characters - split if needed"""

    def call_claude(prompt_text):
        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=8000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt_text}]
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]
            raw = raw.rsplit("```", 1)[0].strip()
        return raw

    # First attempt — full page text
    raw = call_claude(build_prompt(2000))
    try:
        blocks = json.loads(raw)
        rec_types, rec_ids = extract_recommended_schemas(blocks)
        return {"blocks": blocks, "used_retry": False, "recommended_schemas": rec_types, "recommended_ids": rec_ids}
    except json.JSONDecodeError as e:
        print(f"JSON parse error: {e}")
        print(f"Raw response (last 300 chars): ...{raw[-300:]}")
        print("Retrying with simpler prompt...")

    # Retry — shorter page text
    raw = call_claude(build_prompt(500))
    try:
        blocks = json.loads(raw)
        rec_types, rec_ids = extract_recommended_schemas(blocks)
        return {"blocks": blocks, "used_retry": True, "recommended_schemas": rec_types, "recommended_ids": rec_ids}
    except json.JSONDecodeError as e:
        print(f"Retry also failed: {e}")
        raise
def extract_recommended_schemas(blocks):
    """Extract @type and @id values from Claude's recommended schema code blocks.
    Used to feed the QA report with what was RECOMMENDED, not what exists on the live site.
    """
    import re
    types_found = []
    ids_found = []
    for block in blocks:
        if block.get("type") == "code":
            rich_text = block.get("code", {}).get("rich_text", [])
            content = "".join(
                rt.get("text", {}).get("content", "")
                for rt in rich_text
                if isinstance(rt, dict)
            )
            for match in re.findall(r'"@type"\s*:\s*"([^"]+)"', content):
                if match not in types_found:
                    types_found.append(match)
            for match in re.findall(r'"@id"\s*:\s*"([^"]+)"', content):
                if match not in ids_found:
                    ids_found.append(match)
    return types_found, ids_found


def generate_executive_summary(page_summaries, project):
    """
    page_summaries: list of dicts with url, level, page_type, used_retry, schemas_found, content_analysis
    Returns: JSON array of Notion blocks (Hebrew executive summary)
    """
    pages_text = ""
    retry_pages = [p["url"] for p in page_summaries if p.get("used_retry")]

    for p in page_summaries:
        ca = p.get("content_analysis", {})
        schemas = ", ".join(p.get("schemas_found", [])) or "None"
        retry_note = " ⚠️ [נותח חלקית - retry]" if p.get("used_retry") else ""
        pages_text += f"""
דף: {p['url']}{retry_note}
  סוג: {p['page_type']} | רמה: {p['level']}
  H1: {ca.get('h1') or 'לא נמצא'}
  סכמות קיימות: {schemas}
  תמונות: {ca.get('images', {}).get('total', 0)} סה"כ, {ca.get('images', {}).get('missing_alt', 0)} ללא alt
  FAQ: {'כן' if ca.get('faq_patterns') else 'לא'}
"""

    retry_warning = ""
    if retry_pages:
        retry_warning = "\n⚠️ הערה: הדפים הבאים נותחו עם נתונים חלקיים (retry mode):\n" + "\n".join(retry_pages)

    prompt = f"""Project: {project}
Total pages analyzed: {len(page_summaries)}

PAGES SUMMARY:
{pages_text}
{retry_warning}

Write a comprehensive executive summary in Hebrew as a JSON array of Notion blocks.

Structure:
1. heading_2: "סיכום מנהלים — {project}"
2. paragraph: ציון בריאות כולל (Poor / Fair / Good / Excellent) + הסבר קצר
3. heading_2: "ממצאים לפי דף"
4. bulleted_list_item per page: שם הדף + מה נמצא + מה חסר (שורה אחת קצרה)
5. heading_2: "⚠️ בעיות דחופות"
6. bulleted_list_item: בעיות קריטיות מכלל הדפים
7. heading_2: "סדר עדיפויות מומלץ"
8. numbered_list_item: משימות לפי סדר חשיבות
9. heading_2: "הערות כלליות"
10. paragraph: המלצות כלליות (CMS, Yoast, נגישות)
{f'11. callout (orange_background, icon ⚠️): "דפים שנותחו חלקית (retry): {chr(44).join(retry_pages)}"' if retry_pages else ""}

Rules:
- All text in Hebrew
- Return raw JSON array of Notion blocks only, no surrounding text
- Be specific and actionable
- Do not invent data not present in the summary above"""

    message = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=6000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
        raw = raw.rsplit("```", 1)[0].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Executive summary JSON parse error: {e}")
        print(f"Raw (last 300): ...{raw[-300:]}")
        raise
def generate_qa_report(page_summaries, project):
    """
    Cross-page QA: checks Claude's RECOMMENDED schemas and @ids (not the live site).
    Returns: JSON array of Notion blocks (Hebrew QA report)
    """
    pages_data = ""
    for p in page_summaries:
        rec_types = ", ".join(p.get("recommended_schemas", [])) or "לא חולצו"
        rec_ids = "\n    ".join(p.get("recommended_ids", [])) or "לא חולצו"
        retry_note = " ⚠️ [נותח חלקית]" if p.get("used_retry") else ""
        pages_data += f"""
דף: {p['url']}{retry_note}
  סוג: {p['page_type']} | רמה: {p['level']}
  @types שהומלצו בדוח:
    {rec_types}
  @ids שהומלצו בדוח:
    {rec_ids}
"""
    prompt = f"""Project: {project}
Total pages: {len(page_summaries)}
CRITICAL CONTEXT: The data below contains the schemas and @ids that were RECOMMENDED
by the analysis reports — NOT what currently exists on the live site.
Your job is to cross-check these RECOMMENDATIONS for consistency across pages.
PAGES DATA (recommended schemas per page):
{pages_data}
You are a Schema.org QA auditor. Perform a cross-page quality check on the RECOMMENDED schemas.
Return a JSON array of Hebrew Notion blocks.
Structure:
1. heading_2: "דוח QA — {project}"
2. paragraph: תקציר ממצאי ה-QA (כמה בעיות עקביות נמצאו, מה המצב הכולל בין הדפים)
3. heading_2: "בדיקת שרשור @id"
4. bulleted_list_item per issue:
   - האם @id של Organization מוגדר בדף הבית ומוזכר בצורה זהה בדפי הילד?
   - האם @id של Hotel עקבי? בדוק slash/no-slash (למשל /nucha#hotel לעומת /nucha/#hotel)
   - האם יש @id שמוגדר בדף אחד אבל מוזכר בצורה שונה בדף אחר?
   - ציין @id בעייתיים בצורה מדויקת עם הערך המלא. אם אין בעיות: כתוב "לא נמצאו אי-עקביות — השרשור תקין"
5. heading_2: "סכמות לפי דף — בדיקת כיסוי"
6. bulleted_list_item per page: שם הדף + הסכמות שהומלצו + האם הכיסוי הגיוני לסוג הדף?
7. heading_2: "⚠️ דפים שנותחו חלקית"
8. bulleted_list_item: דפים עם retry + המלצה לבדיקה ידנית. אם אין: paragraph: "כל הדפים נותחו בהצלחה"
9. heading_2: "✅ צ'קליסט לפני פרסום"
10. to_do blocks: פעולות ספציפיות לפני עליה לאוויר — לפי הממצאים למעלה
Rules:
- All text in Hebrew
- Return raw JSON array of Notion blocks only
- to_do blocks: type is "to_do", checked is false
- Be precise: mention specific @id values, schema types, page URLs
- Base everything ONLY on the recommended data above — do not invent
- The QA checks consistency between pages, not whether schemas are already implemented"""
    message = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=6000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
        raw = raw.rsplit("```", 1)[0].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"QA report JSON parse error: {e}")
        print(f"Raw (last 300): ...{raw[-300:]}")
        raise