import os
import json
import re
from urllib.parse import urlparse
from dotenv import load_dotenv
from google import genai
from schema_ref import get_or_create_schema_reference

load_dotenv()

with open("prompt.txt", "r", encoding="utf-8") as f:
    PROMPT_TEMPLATE = f.read()

client = genai.Client(
    api_key=os.getenv("GEMINI_API_KEY")
)
MODEL_NAME = "gemini-2.5-flash"

SYSTEM_PROMPT = (
    "You are a Schema.org structured data expert and a JSON API. "
    "Return only a raw JSON array of Notion blocks. "
    "No markdown fences, no surrounding text."
)

ai_usage = {"input_tokens": 0, "output_tokens": 0}

# --- Pricing (gemini-1.5-flash) ---
INPUT_COST_PER_M  = 0.075
OUTPUT_COST_PER_M = 0.30

# --- Credits tracking (identical to claude_agent.py) ---
SCRAPER_CREDITS = {
    "cloudscraper":         0,
    "scrapingbee_standard": 5,
    "scrapingbee_premium":  75,
    "playwright":           0,
}
SCRAPER_LABELS = {
    "cloudscraper":         "cloudscraper (0 credits)",
    "scrapingbee_standard": "ScrapingBee standard (5 credits)",
    "scrapingbee_premium":  "ScrapingBee premium (75 credits)",
    "playwright":           "Playwright (0 credits)",
}

KNOWN_SCHEMA_TYPES = {
    "Organization", "LocalBusiness", "Hotel", "WebSite", "WebPage", "AboutPage",
    "ContactPage", "FAQPage", "BreadcrumbList", "ImageObject", "VideoObject",
    "Person", "Product", "Service", "Restaurant", "Event", "Article", "BlogPosting",
    "HowTo", "Recipe", "Review", "AggregateRating", "PostalAddress",
    "GeoCoordinates", "LodgingBusiness", "FoodEstablishment", "Offer"
}
NESTED_SCHEMA_TYPES = {
    "Country", "OfferCatalog", "Offer", "AggregateOffer", "Question", "Answer",
    "PostalAddress", "GeoCoordinates", "ContactPoint", "OpeningHoursSpecification",
    "MonetaryAmount", "PropertyValue", "ListItem", "ItemList",
    "EntryPoint", "SearchAction", "ReadAction", "ImageObject"
}

RICH_TEXT_BLOCKS = {
    "heading_1", "heading_2", "heading_3", "heading_4", "paragraph",
    "bulleted_list_item", "numbered_list_item", "quote", "toggle", "to_do", "callout"
}


def _sanitize_blocks(blocks):
    """Sanitize Gemini-generated blocks before sending to Notion."""
    # 1. Filter blocks missing their content object
    blocks = [
        b for b in blocks
        if isinstance(b, dict) and b.get("type") and b.get(b["type"]) is not None
    ]

    # 2. Sanitize code block rich_text — remove invalid items, strip extra keys
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "code":
            code_inner = b.get("code", {})
            valid_rt = []
            for rt in code_inner.get("rich_text", []):
                if not isinstance(rt, dict):
                    continue
                if rt.get("object") == "block":
                    continue  # Gemini mistakenly placed a block object here
                if isinstance(rt.get("text"), dict):
                    rt["text"] = {k: v for k, v in rt["text"].items() if k in ("content", "link")}
                valid_rt.append(rt)
            code_inner["rich_text"] = valid_rt

    # 3. Sanitize rich_text in all block types — fix missing/extra keys in text objects
    for b in blocks:
        if not isinstance(b, dict):
            continue
        block_type = b.get("type")
        if block_type and isinstance(b.get(block_type), dict):
            inner = b[block_type]
            for key in ["rich_text", "text"]:
                if isinstance(inner.get(key), list):
                    for rt in inner[key]:
                        if not isinstance(rt, dict):
                            continue
                        if not isinstance(rt.get("text"), dict):
                            rt["text"] = {"content": ""}
                        else:
                            if "content" not in rt["text"] or rt["text"]["content"] is None:
                                rt["text"]["content"] = ""
                            rt["text"] = {k: v for k, v in rt["text"].items() if k in ("content", "link")}

    # 4. Ensure rich_text exists in blocks that require it
    for b in blocks:
        if not isinstance(b, dict):
            continue
        block_type = b.get("type")
        if block_type in RICH_TEXT_BLOCKS and isinstance(b.get(block_type), dict):
            inner = b[block_type]
            if "rich_text" not in inner or inner["rich_text"] is None:
                inner["rich_text"] = [{"type": "text", "text": {"content": ""}}]

    return blocks


def _call_gemini(prompt_text):
    full_prompt = f"{SYSTEM_PROMPT}\n\n{prompt_text}"
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=full_prompt,
        config={
            "max_output_tokens": 8000,
        }
    )
    if hasattr(response, "usage_metadata"):
        ai_usage["input_tokens"]  += response.usage_metadata.prompt_token_count or 0
        ai_usage["output_tokens"] += response.usage_metadata.candidates_token_count or 0
    raw = response.text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
        raw = raw.rsplit("```", 1)[0].strip()
    return raw


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


def extract_recommended_schemas(blocks):
    types_found = []
    ids_found = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
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
                if schema_type not in types_found:
                    if f'"@type": "{schema_type}"' in content or f'"@type":"{schema_type}"' in content:
                        types_found.append(schema_type)
    types_found = [t for t in types_found if t not in NESTED_SCHEMA_TYPES]
    print(f"    [extract_recommended_schemas] types: {types_found} | ids count: {len(ids_found)}")
    return types_found, ids_found


def extract_faq_summary(json_ld):
    faqs = []
    for block in json_ld:
        items = block.get("@graph", [block])
        for item in items:
            if item.get("@type") == "FAQPage":
                for i, q in enumerate(item.get("mainEntity", []), 1):
                    question = q.get("name", "")
                    answer_obj = q.get("acceptedAnswer", {})
                    answer = answer_obj.get("text", "") if isinstance(answer_obj, dict) else ""
                    faqs.append(f"Q{i}: {question[:2000]}\nA{i}: {answer[:2000]}")
    return faqs


def summarize_existing_schemas(json_ld):
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


def analyze_with_scan(scan_result, level, page_type, project, parent_context=None):
    url = scan_result["url"]
    sd = scan_result["structured_data"]
    ca = scan_result["content_analysis"]
    pt = scan_result.get("page_text", {})
    json_ld = sd.get("json-ld", [])
    summarized = summarize_existing_schemas(json_ld)
    existing_schemas = json.dumps(summarized, ensure_ascii=False) if summarized else "None found"
    microdata = sd.get("microdata", [])
    microdata_section = ""
    if microdata:
        microdata_section = f"\nMICRODATA SCHEMAS:\n{json.dumps(microdata[:5], ensure_ascii=False)[:2000]}"
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
H2s: {', '.join(ca.get('h2s', [])[:10]) or 'None'}
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

    # First attempt
    raw = _call_gemini(build_prompt(5000))
    blocks = safe_parse(raw)
    if blocks is not None:
        if blocks and isinstance(blocks[0], list):
            blocks = [item for sublist in blocks for item in (sublist if isinstance(sublist, list) else [sublist])]
        blocks = _sanitize_blocks(blocks)
        rec_types, rec_ids = extract_recommended_schemas(blocks)
        if not rec_types and not rec_ids:
            rec_types, rec_ids = extract_schemas_from_json_ld(json_ld)
            print(f"    [analyze_with_scan] fallback to existing JSON-LD: types={rec_types} | ids={len(rec_ids)}")
        return {
            "blocks": blocks,
            "used_retry": False,
            "recommended_schemas": rec_types,
            "recommended_ids": rec_ids
        }

    print(f"JSON parse error (last 300 chars): ...{raw[-300:]}")
    print("Retrying with simpler prompt...")

    # Retry
    raw = _call_gemini(build_prompt(1000))
    blocks = safe_parse(raw)
    if blocks is not None:
        if blocks and isinstance(blocks[0], list):
            blocks = [item for sublist in blocks for item in (sublist if isinstance(sublist, list) else [sublist])]
        blocks = _sanitize_blocks(blocks)
        rec_types, rec_ids = extract_recommended_schemas(blocks)
        if not rec_types and not rec_ids:
            rec_types, rec_ids = extract_schemas_from_json_ld(json_ld)
            print(f"    [analyze_with_scan] fallback to existing JSON-LD: types={rec_types} | ids={len(rec_ids)}")
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
        pages_text += f"""דף: {p['url']}{retry_note}
  סוג: {p['page_type']} | רמה: {p['level']}
  H1: {h1_val}
  סכמות קיימות: {schemas}
  FAQ: {faq_val}"""
    failed_pages = [p["url"] for p in page_summaries if p.get("scrape_failed")]
    retry_warning = ""
    if retry_pages:
        retry_callout_line = ", ".join(retry_pages)
        retry_warning = f"\n⚠️ הערה: הדפים הבאים נותחו עם נתונים חלקיים (retry mode):\n{retry_callout_line}"
    if failed_pages:
        failed_line = ", ".join(failed_pages)
        retry_warning += f"\n⚠️ הערה: הדפים הבאים לא נותחו כלל בשל בעיות גישה (timeout):\n{failed_line}"
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
4. bulleted_list_item per page: שם הדף + סכמות שנמצאו בלבד. אין לציין מה חסר בסעיף זה.
5. heading_2: "סדר עדיפויות מומלץ"
6. numbered_list_item: משימות לפי סדר חשיבות. סמן פריטים קריטיים עם ⚠️ בתחילת השורה. כל פריט: פעולה + הסבר של שורה אחת בלבד. אין לחזור על מידע שכבר מופיע בסעיפים אחרים.
7. heading_2: "הערות כלליות" — הכלל רק אם יש מידע חדש שלא הוזכר בסדר העדיפויות. מקסימום 3 נקודות. כלול רק: המלצות על כלי ניהול סכמות (Yoast, Schema Pro וכו') + אימות ב-Rich Results Test. אם אין מה להוסיף: השמט סעיף זה לחלוטין.
Rules:
- All text in Hebrew
- Return raw JSON array of Notion blocks only
- Be specific and actionable
- Do NOT mention accessibility, alt text, WCAG, or screen readers
- Do NOT include Minor Observations — only Critical Issues
- Do not invent data not present in the summary above
- Do NOT write QA audit notes, internal verification statements, or negative-result checks
- Formatting lists: if a section contains only one item, write it as a plain paragraph with no numbering. If a section contains two or more items, you MUST use numbered_list_item blocks (1. 2. 3. etc.). This applies to all sections including "סדר עדיפויות מומלץ" and "הערות כלליות". Using paragraph blocks instead of numbered_list_item when there are 2+ items is a formatting error."""
    raw = _call_gemini(prompt)
    result = safe_parse(raw)
    if result is not None:
        return result
    raise ValueError(f"Executive summary parse failed. Last 300: ...{raw[-300:]}")


def build_credits_blocks(credits_summary, total_scraping_credits, ai_tokens):
    blocks = [{
        "object": "block", "type": "heading_2",
        "heading_2": {"rich_text": [{"type": "text", "text": {"content": "Run Credits Summary"}}]}
    }]
    # --- ScrapingBee ---
    blocks.append({
        "object": "block", "type": "heading_3",
        "heading_3": {"rich_text": [{"type": "text", "text": {"content": "ScrapingBee"}}]}
    })
    for item in credits_summary:
        label = SCRAPER_LABELS.get(item["method"], item["method"])
        blocks.append({
            "object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [{"type": "text", "text": {
                "content": f"{item['url']} — {label}"
            }}]}
        })
    blocks.append({
        "object": "block", "type": "paragraph",
        "paragraph": {"rich_text": [{"type": "text", "text": {
            "content": f"Total ScrapingBee credits used: {total_scraping_credits}"
        }}]}
    })
    # --- Gemini API ---
    input_t     = ai_tokens.get("input_tokens", 0)
    output_t    = ai_tokens.get("output_tokens", 0)
    input_cost  = round(input_t  / 1_000_000 * INPUT_COST_PER_M, 4)
    output_cost = round(output_t / 1_000_000 * OUTPUT_COST_PER_M, 4)
    total_cost  = round(input_cost + output_cost, 4)
    blocks.append({
        "object": "block", "type": "heading_3",
        "heading_3": {"rich_text": [{"type": "text", "text": {"content": f"Gemini API ({MODEL_NAME})"}}]}
    })
    blocks.append({
        "object": "block", "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": [{"type": "text", "text": {
            "content": f"Input tokens:  {input_t:,}  (${input_cost})"
        }}]}
    })
    blocks.append({
        "object": "block", "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": [{"type": "text", "text": {
            "content": f"Output tokens: {output_t:,}  (${output_cost})"
        }}]}
    })
    blocks.append({
        "object": "block", "type": "paragraph",
        "paragraph": {"rich_text": [{"type": "text", "text": {
            "content": f"Total Gemini cost this run: ${total_cost}"
        }}]}
    })
    return blocks


def generate_qa_report(page_summaries, project, credits_summary=None, total_scraping_credits=0, ai_tokens=None):
    pages_data = ""
    for p in page_summaries:
        rec_types = ", ".join(p.get("recommended_schemas", [])) or "none extracted"
        rec_ids = "\n    ".join(p.get("recommended_ids", [])) or "none extracted"
        retry_note = " ⚠️ [partial analysis]" if p.get("used_retry") else ""
        pages_data += f"""Page: {p['url']}{retry_note}
  Type: {p['page_type']} | Level: {p['level']}
  Recommended @types:    {rec_types}
  Recommended @ids:    {rec_ids}"""
    all_schema_types = set()
    for p in page_summaries:
        all_schema_types.update(p.get("recommended_schemas", []))
    schema_ref_context = ""
    for schema_type in sorted(all_schema_types):
        ref = get_or_create_schema_reference(schema_type)
        if ref:
            rich = "✅ Google Rich Result" if ref["google_rich_result"] else "❌ No rich result"
            req = ref["required_properties"] or "not specified"
            schema_ref_context += f"\n{schema_type}: {rich} | Required: {req}"
    if schema_ref_context:
        schema_ref_context = "\nSCHEMA REFERENCE (from Google documentation):" + schema_ref_context + "\n"
    prompt = f"""Project: {project}
Total pages: {len(page_summaries)}
CONTEXT:
Each page has already received a dedicated Schema Report with specific fix instructions.
The tool already enforced @id consistency during analysis via parent context propagation.
Your ONLY job: check whether the recommended @ids and @types listed below contradict each other.
If the same entity appears with a different @id or @type on two different pages — flag it.
If the recommendations are internally consistent — nothing to report.
DO NOT introduce any new issues, observations, or advice of any kind.
{schema_ref_context}
PAGES DATA (recommended schemas and @ids per page, after implementation):
{pages_data}
You are a Schema.org QA auditor. Return a JSON array of English Notion blocks.
Structure — include ONLY these sections:
1. heading_2: "@id Consistency Issues"
   - List ONLY actual problems found
   - If no issues found: one bulleted_list_item: "No @id inconsistencies found"
2. heading_2: "Schema Type Conflicts"
   - Flag ONLY cases where the same real-world entity is recommended as different @types on different pages
   - If no conflicts: omit this section entirely
3. heading_2: "Uncertain — Needs Review"
   - ONLY include if there are things that could not be determined
   - If nothing uncertain: omit this section entirely
4. heading_2: "Partially Analyzed Pages"
   - ONLY include if any page used retry mode
   - Otherwise: omit this section entirely
5. heading_2: "Report Quality Issues"
   - Flag only if the same issue appears in more than one section of the same page report
   - If no such issues: omit this section entirely
ABSOLUTE RULES:
- DO NOT write an opening summary or paragraph
- DO NOT write positive confirmations
- DO NOT include pre-launch checklists, implementation advice, or testing instructions
- DO NOT repeat any recommendation from individual page reports
- ONLY flag genuine cross-page inconsistencies and uncertainties
- Return raw JSON array of Notion blocks only, all text in English"""
    raw = _call_gemini(prompt)
    result = safe_parse(raw)
    if result is not None:
        if credits_summary is not None:
            result += build_credits_blocks(credits_summary, total_scraping_credits, ai_tokens or {})
        return result
    raise ValueError(f"QA report parse failed. Last 300: ...{raw[-300:]}")