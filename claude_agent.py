import os
import json
import re
import anthropic
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()

with open("prompt.txt", "r", encoding="utf-8") as f:
    PROMPT_TEMPLATE = f.read()

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

SYSTEM_PROMPT = "You are a Schema.org structured data expert and a JSON API. Return only a raw JSON array of Notion blocks. No markdown fences, no surrounding text."


def extract_schemas_from_json_ld(json_ld):
    """Extract schema @types and @ids from json-ld blocks."""
    types = set()
    ids = []
    for block in json_ld:
        items = block.get("@graph", [block])
        for item in items:
            t = item.get("@type")
            if isinstance(t, list):
                types.update(t)
            elif t:
                types.add(t)
            i = item.get("@id")
            if i:
                ids.append(i)
    return sorted(types), ids


KNOWN_SCHEMA_TYPES = {
    "Organization", "LocalBusiness", "Hotel", "WebSite", "WebPage", "AboutPage",
    "ContactPage", "FAQPage", "BreadcrumbList", "ImageObject", "VideoObject",
    "Person", "Product", "Service", "Restaurant", "Event", "Article", "BlogPosting",
    "HowTo", "Recipe", "Review", "AggregateRating", "PostalAddress",
    "GeoCoordinates", "LodgingBusiness", "FoodEstablishment", "Offer"
}
# Sub-types that appear nested inside schemas — not top-level page schemas
NESTED_SCHEMA_TYPES = {
    "Country", "OfferCatalog", "Offer", "Question", "Answer",
    "PostalAddress", "GeoCoordinates", "ContactPoint", "OpeningHoursSpecification",
    "MonetaryAmount", "PropertyValue", "ListItem", "ItemList",
    "EntryPoint", "SearchAction", "ReadAction", "ImageObject"
}
def extract_recommended_schemas(blocks):
    """Extract @type and @id values from Claude's recommended schema blocks.
    Scans ALL block types (not just code) for explicit @type/@id patterns.
    Falls back to known Schema.org type name matching in text when no explicit
    patterns found (e.g. when all schemas are managed by Yoast/RankMath).
    Returns (types_found, ids_found) — used in QA report and parent context.
    """
    types_found = []
    ids_found = []
    for block in blocks:
        block_type = block.get("type")
        inner = block.get(block_type, {})
        rich_text = inner.get("rich_text", [])
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
    # Fallback: no explicit @type patterns found (all-managed page like Yoast/RankMath)
    # Scan all text content for known Schema.org type names
    if not types_found:
        for block in blocks:
            block_type = block.get("type")
            inner = block.get(block_type, {})
            rich_text = inner.get("rich_text", [])
            content = "".join(
                rt.get("text", {}).get("content", "")
                for rt in rich_text
                if isinstance(rt, dict)
            )
            for schema_type in KNOWN_SCHEMA_TYPES:
                if schema_type in content and schema_type not in types_found:
                    types_found.append(schema_type)
    # Filter out nested/sub-types — keep only top-level page schema types
    types_found = [t for t in types_found if t not in NESTED_SCHEMA_TYPES]
    print(f"    [extract_recommended_schemas] types: {types_found} | ids count: {len(ids_found)}")
    return types_found, ids_found


def extract_faq_summary(json_ld):
    """Extract FAQ questions/answers as compact readable list for analysis."""
    faqs = []
    for block in json_ld:
        items = block.get("@graph", [block])
        for item in items:
            if item.get("@type") == "FAQPage":
                for i, q in enumerate(item.get("mainEntity", []), 1):
                    question = q.get("name", "")
                    answer_obj = q.get("acceptedAnswer", {})
                    answer = answer_obj.get("text", "") if isinstance(answer_obj, dict) else ""
                    faqs.append(f"Q{i}: {question[:120]}\nA{i}: {answer[:120]}")
    return faqs


def summarize_existing_schemas(json_ld):
    """Return existing schemas with FAQPage mainEntity replaced by a count summary."""
    summarized = []
    for block in json_ld:
        if "@graph" in block:
            graph = []
            for item in block["@graph"]:
                if item.get("@type") == "FAQPage":
                    questions = item.get("mainEntity", [])
                    item = dict(item)
                    item["mainEntity"] = f"// {len(questions)} questions — see FAQ CONTENT section below"
                graph.append(item)
            block = dict(block)
            block["@graph"] = graph
        elif block.get("@type") == "FAQPage":
            questions = block.get("mainEntity", [])
            block = dict(block)
            block["mainEntity"] = f"// {len(questions)} questions — see FAQ CONTENT section below"
        summarized.append(block)
    return summarized


def safe_parse(raw_text):
    """Try json.loads first, then json_repair as fallback."""
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        try:
            from json_repair import repair_json
            repaired = repair_json(raw_text)
            return json.loads(repaired)
        except Exception:
            return None


def analyze_with_scan(scan_result, level, page_type, project, parent_context=None):
    url = scan_result["url"]
    sd = scan_result["structured_data"]
    ca = scan_result["content_analysis"]
    pt = scan_result.get("page_text", {})

    json_ld = sd.get("json-ld", [])

    summarized = summarize_existing_schemas(json_ld)
    existing_schemas = json.dumps(summarized, ensure_ascii=False) if summarized else "None found"

    # Microdata
    microdata = sd.get("microdata", [])
    microdata_section = ""
    if microdata:
        microdata_section = f"\nMICRODATA SCHEMAS:\n{json.dumps(microdata[:5], ensure_ascii=False)[:2000]}"
    # RDFa
    rdfa = sd.get("rdfa", [])
    rdfa_section = ""
    if rdfa:
        rdfa_section = f"\nRDFA SCHEMAS:\n{json.dumps(rdfa[:5], ensure_ascii=False)[:2000]}"
    faq_items = extract_faq_summary(json_ld)
    faq_section = ""
    if faq_items:
        faq_section = f"\nFAQ CONTENT ({len(faq_items)} items — check for swapped Q/A):\n"
        faq_section += "\n".join(faq_items)

    content_summary = f"""H1: {ca.get('h1') or 'Not found'}
H2s: {', '.join(ca.get('h2s', [])[:5]) or 'None'}
Video: {'Yes' if ca.get('video') else 'No'}
Forms: {ca.get('forms') or 'None'}
FAQ patterns: {'Yes' if ca.get('faq_patterns') else 'No'}
Phone: {ca.get('contact_info', {}).get('phone') or 'Not found'}
Email: {ca.get('contact_info', {}).get('email') or 'Not found'}"""

    page_text = pt.get("text") or ""

    path_parts = [p for p in urlparse(url).path.split('/') if p]
    is_subpage = len(path_parts) >= 2
    subpage_note = "YES - reference parent @id, do not redefine parent entity" if is_subpage else "NO - this is a top-level page"

    parent_section = ""
    if parent_context:
        parent_section = "\nRECOMMENDED SCHEMAS FROM PARENT PAGES:\n"
        parent_section += "CRITICAL: Use these @ids exactly. Do not redefine these entities.\n"
        for lvl in sorted(parent_context.keys(), key=lambda x: str(x)):
            data = parent_context[lvl]
            types = ", ".join(data.get("recommended_schemas", [])) or "None"
            ids = ", ".join(data.get("recommended_ids", [])) or "None"
            parent_section += f"\nLevel {lvl} ({data['url']}):\n  @types: {types}\n  @ids: {ids}\n"

    def build_prompt(text_limit):
        instructions = PROMPT_TEMPLATE.replace("__SUBPAGE_NOTE__", subpage_note)
        return f"""URL: {url}
Project: {project}
Page type: {page_type}
Level: {level}
{parent_section}
CONTENT ANALYSIS:
{content_summary}

PAGE TEXT (first {text_limit} chars):
{page_text[:text_limit]}

EXISTING SCHEMAS (JSON-LD):
{existing_schemas[:6000]}
{microdata_section}
{rdfa_section}
{faq_section}

{instructions}"""

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

    # First attempt
    raw = call_claude(build_prompt(2000))
    blocks = safe_parse(raw)
    if blocks is not None:
        rec_types, rec_ids = extract_recommended_schemas(blocks)
        return {
            "blocks": blocks,
            "used_retry": False,
            "recommended_schemas": rec_types,
            "recommended_ids": rec_ids
        }
    print(f"JSON parse error (last 300 chars): ...{raw[-300:]}")
    print("Retrying with simpler prompt...")

    # Retry
    raw = call_claude(build_prompt(500))
    blocks = safe_parse(raw)
    if blocks is not None:
        rec_types, rec_ids = extract_recommended_schemas(blocks)
        return {
            "blocks": blocks,
            "used_retry": True,
            "recommended_schemas": rec_types,
            "recommended_ids": rec_ids
        }
    raise ValueError(f"Both attempts failed. Last 300 chars: ...{raw[-300:]}")


def generate_executive_summary(page_summaries, project):
    pages_text = ""
    retry_pages = [p["url"] for p in page_summaries if p.get("used_retry")]

    for p in page_summaries:
        ca = p.get("content_analysis", {})
        schemas = ", ".join(p.get("schemas_found", [])) or "None"
        retry_note = " ⚠️ [נותח חלקית - retry]" if p.get("used_retry") else ""
        h1_val = ca.get('h1') or 'לא נמצא'
        faq_val = 'כן' if ca.get('faq_patterns') else 'לא'
        pages_text += f"""
דף: {p['url']}{retry_note}
  סוג: {p['page_type']} | רמה: {p['level']}
  H1: {h1_val}
  סכמות קיימות: {schemas}
  FAQ: {faq_val}
"""

    retry_warning = ""
    if retry_pages:
        retry_callout_line = ", ".join(retry_pages)
        retry_warning = f"\n⚠️ הערה: הדפים הבאים נותחו עם נתונים חלקיים (retry mode):\n{retry_callout_line}"

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
4. bulleted_list_item per page: שם הדף + מה נמצא + מה חסר
5. heading_2: "⚠️ בעיות דחופות"
6. bulleted_list_item: בעיות קריטיות (ללא נגישות)
7. heading_2: "סדר עדיפויות מומלץ"
8. numbered_list_item: משימות לפי סדר חשיבות
9. heading_2: "הערות כלליות"
10. paragraph: המלצות כלליות (CMS, Yoast)

Rules:
- All text in Hebrew
- Return raw JSON array of Notion blocks only
- Be specific and actionable
- Do NOT mention accessibility, alt text, WCAG, or screen readers
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
    result = safe_parse(raw)
    if result is not None:
        return result
    raise ValueError(f"Executive summary parse failed. Last 300: ...{raw[-300:]}")


def generate_qa_report(page_summaries, project):
    """
    Cross-page QA: checks Claude's RECOMMENDED schemas and @ids (not the live site).
    """
    pages_data = ""
    for p in page_summaries:
        rec_types = ", ".join(p.get("recommended_schemas", [])) or "none extracted"
        rec_ids = "\n    ".join(p.get("recommended_ids", [])) or "none extracted"
        retry_note = " ⚠️ [partial analysis]" if p.get("used_retry") else ""
        pages_data += f"""
Page: {p['url']}{retry_note}
  Type: {p['page_type']} | Level: {p['level']}
  Recommended @types:
    {rec_types}
  Recommended @ids:
    {rec_ids}
"""

    prompt = f"""Project: {project}
Total pages: {len(page_summaries)}
CONTEXT:
Each page has already received a dedicated Schema Report with specific fix instructions.
ASSUME all those recommendations will be fully implemented by the client.
Your job is ONLY to identify cross-page consistency issues that individual reports cannot catch.
DO NOT re-flag any issue that was already addressed in a per-page report.
PAGES DATA (recommended schemas and @ids per page, after implementation):
{pages_data}
You are a Schema.org QA auditor. Flag ONLY issues that:
1. Span multiple pages (e.g. same entity uses different @id on different pages)
2. Could not have been caught by a single-page review
3. Were NOT already covered by individual page recommendations
Return a JSON array of English Notion blocks.
Structure:
1. heading_2: "QA Report — {project}"
2. paragraph: brief summary of unresolved cross-page issues only
3. heading_2: "@id Consistency"
   - Check that the same entity uses the same @id across all pages
   - Check slash/no-slash consistency in @ids
   - Check that subpages correctly reference parent @ids
   - If no issues: single bulleted_list_item: "No @id inconsistencies found"
4. heading_2: "Cross-Page Issues"
   - Only issues that span multiple pages and were not addressed per-page
   - If no issues: single bulleted_list_item: "No cross-page issues found"
4b. heading_2: "Uncertain — Needs Review"
   - Issues you are unsure whether they were already covered in per-page reports
   - For each: briefly explain what the issue is AND why you are uncertain
   - If nothing uncertain: omit this section entirely
5. heading_2: "Partially Analyzed Pages"
   - Only include this section if any page used retry mode
   - Otherwise: omit this section entirely
6. heading_2: "Pre-Launch Checklist"
   - to_do blocks: ONLY validation steps not covered by individual reports
   - Focus on: cross-page validation, Google Rich Results Test after full deployment
ABSOLUTE FILTER — apply this test to EVERY issue before including it:
"Is there a recommendation in ANY individual page report that, if implemented, would resolve this issue?"
If YES → exclude it entirely. Do not mention it. Do not summarize it. Do not reference it.
The QA report exists ONLY for issues with NO corresponding recommendation in any page report.
Rules:
- All text in English
- Return raw JSON array of Notion blocks only
- to_do blocks: type is \"to_do\", checked is false
- Be precise: reference specific @id values and page URLs
- DO NOT flag missing schemas — those are handled in individual page reports
- DO NOT repeat any recommendation from individual page reports
- ONLY flag genuine cross-page inconsistencies
- When in doubt whether an issue was already covered — include it under a separate heading_2: "Uncertain — Needs Review", with a brief note explaining why it may or may not be redundant with per-page reports"""

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
    result = safe_parse(raw)
    if result is not None:
        return result
    raise ValueError(f"QA report parse failed. Last 300: ...{raw[-300:]}")