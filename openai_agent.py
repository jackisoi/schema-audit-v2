import os
import json
from openai import OpenAI
from dotenv import load_dotenv
from schema_ref import get_or_create_schema_reference

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
MODEL_NAME = "gpt-4o"

# Phase 2 analysis: returns a single JSON object
SYSTEM_PROMPT_V2 = "You are a Schema.org structured data expert and a JSON API. Return only a raw JSON object matching the specified schema. No markdown fences, no surrounding text."

# Executive summary + QA report: return a JSON array of Notion blocks
SYSTEM_PROMPT = "You are a Schema.org structured data expert and a JSON API. Return only a raw JSON array of Notion blocks. No markdown fences, no surrounding text."

claude_usage = {"input_tokens": 0, "output_tokens": 0}
INPUT_COST_PER_M  = 0.15
OUTPUT_COST_PER_M = 0.60

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


# ─── Internal callers ────────────────────────────────────────────────────────

def _call_openai(prompt_text):
    """Phase 1-style caller: returns JSON array of Notion blocks."""
    response = client.chat.completions.create(
        model=MODEL_NAME,
        max_tokens=8000,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": prompt_text},
        ]
    )
    claude_usage["input_tokens"]  += response.usage.prompt_tokens
    claude_usage["output_tokens"] += response.usage.completion_tokens
    raw = response.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
        raw = raw.rsplit("```", 1)[0].strip()
    print(f"    [openai] raw_len={len(raw)} | starts={raw[:50]!r}")
    return raw


def _call_openai_v2(prompt_text):
    """Phase 2 caller: returns a single JSON object."""
    response = client.chat.completions.create(
        model=MODEL_NAME,
        max_tokens=8000,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT_V2},
            {"role": "user",   "content": prompt_text},
        ]
    )
    claude_usage["input_tokens"]  += response.usage.prompt_tokens
    claude_usage["output_tokens"] += response.usage.completion_tokens
    raw = response.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
        raw = raw.rsplit("```", 1)[0].strip()
    print(f"    [openai_v2] raw_len={len(raw)} | starts={raw[:50]!r}")
    return raw


# ─── Shared helpers ───────────────────────────────────────────────────────────

def safe_parse(raw_text):
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        try:
            from json_repair import repair_json
            return json.loads(repair_json(raw_text))
        except Exception:
            return None


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


def generate_text(prompt):
    """Generate plain text using OpenAI."""
    response = client.chat.completions.create(
        model=MODEL_NAME,
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.choices[0].message.content


# ─── Phase 2: analyze_page_v2 ─────────────────────────────────────────────────

def analyze_page_v2(scan_result, level, page_type, site_type, parent_context=None):
    """
    Phase 2 analysis. Returns:
      {
        "analysis":   dict   (parsed JSON from AI),
        "used_retry": bool
      }

    scan_result    : output of scraper.py
    level          : page level (1, 2, 2.5, 3 ...)
    page_type      : e.g. "Home Page", "Product Page"
    site_type      : e.g. "Hotel", "E-commerce", "Medical Clinic"
    parent_context : {level: {url, recommended_schemas}} or None
    """
    with open("prompt.txt", "r", encoding="utf-8") as f:
        prompt_template = f.read()

    url     = scan_result["url"]
    sd      = scan_result["structured_data"]
    ca      = scan_result["content_analysis"]
    pt      = scan_result.get("page_text", {})
    json_ld = sd.get("json-ld", [])

    existing_schemas_str = json.dumps(summarize_existing_schemas(json_ld), ensure_ascii=False) if json_ld else "None found"
    faq_items   = extract_faq_summary(json_ld)
    faq_section = (f"\nFAQ CONTENT ({len(faq_items)} items):\n" + "\n".join(faq_items)) if faq_items else ""

    parent_schemas_str = "None"
    if parent_context:
        lines = []
        for lvl in sorted(parent_context.keys(), key=str):
            d = parent_context[lvl]
            rec = d.get("recommended_schemas", [])
            types_str = ", ".join(
                r["type"] if isinstance(r, dict) else r for r in rec
            ) or "None"
            lines.append(f"Level {lvl} ({d['url']}):\n  @types: {types_str}")
        parent_schemas_str = "\n".join(lines)

    content_summary = f"""H1: {ca.get('h1') or 'Not found'}
H2s: {', '.join(ca.get('h2s', [])[:10]) or 'None'}
Video: {'Yes' if ca.get('video') else 'No'}
FAQ patterns: {'Yes' if ca.get('faq_patterns') else 'No'}
Phone: {ca.get('contact_info', {}).get('phone') or 'Not found'}
Email: {ca.get('contact_info', {}).get('email') or 'Not found'}"""

    def build_prompt(text_limit):
        page_content = (
            content_summary
            + f"\n\nPAGE TEXT (first {text_limit} chars):\n"
            + (pt.get("text") or "")[:text_limit]
            + faq_section
        )
        return prompt_template.format(
            page_url         = url,
            page_type        = page_type,
            site_type        = site_type,
            page_content     = page_content,
            existing_schemas = existing_schemas_str[:6000],
            parent_schemas   = parent_schemas_str,
        )

    raw      = _call_openai_v2(build_prompt(5000))
    analysis = safe_parse(raw)
    if analysis is not None:
        return {"analysis": analysis, "used_retry": False}

    print("Phase 2 parse error. Retrying with less content...")
    raw      = _call_openai_v2(build_prompt(1000))
    analysis = safe_parse(raw)
    if analysis is not None:
        return {"analysis": analysis, "used_retry": True}

    raise ValueError(f"Phase 2: both attempts failed. Last 300: ...{raw[-300:]}")


# ─── Executive Summary ────────────────────────────────────────────────────────

def generate_executive_summary(page_summaries, project):
    pages_text   = ""
    retry_pages  = [p["url"] for p in page_summaries if p.get("used_retry")]
    failed_pages = [p["url"] for p in page_summaries if p.get("scrape_failed")]

    for p in page_summaries:
        analysis    = p.get("analysis", {})
        existing    = analysis.get("existing_schemas", {})
        valid       = existing.get("valid", [])
        issues      = existing.get("issues", [])
        recommended = analysis.get("recommended_schemas", [])
        errors      = [i for i in issues if i.get("severity") == "error"]
        warnings    = [i for i in issues if i.get("severity") == "warning"]
        retry_note  = " ⚠️ [נותח חלקית - retry]" if p.get("used_retry") else ""
        scrape_note = " ⚠️ [לא נסרק]" if p.get("scrape_failed") else ""

        pages_text += f"""
דף: {p['url']}{retry_note}{scrape_note}
  סוג: {p['page_type']} | רמה: {p['level']}
  סכמות תקינות: {', '.join(valid) or 'אין'}
  שגיאות: {len(errors)} | אזהרות: {len(warnings)}
  מומלץ להוסיף: {', '.join(r['type'] for r in recommended) or 'אין'}
"""

    retry_warning = ""
    if retry_pages:
        retry_warning += f"\n⚠️ הערה: הדפים הבאים נותחו עם נתונים חלקיים (retry mode):\n{', '.join(retry_pages)}"
    if failed_pages:
        retry_warning += f"\n⚠️ הערה: הדפים הבאים לא נותחו כלל בשל בעיות גישה:\n{', '.join(failed_pages)}"

    prompt = f"""Project: {project}
Total pages analyzed: {len(page_summaries)}

PAGES SUMMARY:
{pages_text}
{retry_warning}

Write a comprehensive executive summary in Hebrew as a JSON array of Notion blocks.

Structure:
1. heading_2: "סיכום מנהלים — {project}"
2. paragraph: ציון בריאות כולל (Poor / Fair / Good / Excellent) + הסבר קצר המבוסס על מספר השגיאות והאזהרות
3. heading_2: "ממצאים לפי דף"
4. bulleted_list_item per page: שם הדף + סכמות תקינות שנמצאו + מספר שגיאות/אזהרות. אין לציין מה חסר בסעיף זה.
5. heading_2: "סדר עדיפויות מומלץ"
6. numbered_list_item: משימות לפי סדר חשיבות. סמן פריטים קריטיים עם ⚠️ בתחילת השורה. כל פריט: פעולה + הסבר של שורה אחת בלבד. אין לחזור על מידע שכבר מופיע בסעיפים אחרים.
7. heading_2: "הערות כלליות" — הכלל רק אם יש מידע חדש שלא הוזכר בסדר העדיפויות. מקסימום 3 נקודות.

Rules:
- All text in Hebrew
- Return raw JSON array of Notion blocks only
- Be specific and actionable
- Do NOT mention accessibility, alt text, WCAG, or screen readers
- Do NOT include Minor Observations — only Critical Issues
- Do not invent data not present in the summary above
- Formatting lists: if a section contains only one item, write it as a plain paragraph. If two or more items, use numbered_list_item blocks."""

    raw    = _call_openai(prompt)
    result = safe_parse(raw)
    if result is not None:
        return result
    raise ValueError(f"Executive summary parse failed. Last 300: ...{raw[-300:]}")


# ─── QA Report ────────────────────────────────────────────────────────────────

def generate_qa_report(page_summaries, project, credits_summary=None, total_scraping_credits=0, claude_tokens=None):
    pages_data = ""
    for p in page_summaries:
        analysis    = p.get("analysis", {})
        existing    = analysis.get("existing_schemas", {})
        valid       = ', '.join(existing.get("valid", [])) or "none"
        issues      = existing.get("issues", [])
        recommended = [r["type"] for r in analysis.get("recommended_schemas", [])]
        retry_note  = " ⚠️ [partial analysis]" if p.get("used_retry") else ""
        scrape_note = " ⚠️ [scrape failed]" if p.get("scrape_failed") else ""

        issue_lines = "\n    ".join(
            f"[{i['severity']}] {i['schema']}: {i['problem']}"
            for i in issues
        ) or "none"

        pages_data += f"""
Page: {p['url']}{retry_note}{scrape_note}
  Type: {p['page_type']} | Level: {p['level']}
  Existing (valid): {valid}
  Issues: {issue_lines}
  Recommended to add: {', '.join(recommended) or 'none'}
"""

    # Schema reference context for cross-page QA
    all_schema_types = set()
    for p in page_summaries:
        analysis = p.get("analysis", {})
        for r in analysis.get("recommended_schemas", []):
            all_schema_types.add(r["type"])
        all_schema_types.update(analysis.get("existing_schemas", {}).get("valid", []))

    schema_ref_context = ""
    for schema_type in sorted(all_schema_types):
        ref = get_or_create_schema_reference(schema_type)
        if ref:
            rich = "✅ Google Rich Result" if ref["google_rich_result"] else "❌ No rich result"
            req  = ref["required_properties"] or "not specified"
            schema_ref_context += f"\n{schema_type}: {rich} | Required: {req}"
    if schema_ref_context:
        schema_ref_context = "\nSCHEMA REFERENCE (from Google documentation):" + schema_ref_context + "\n"

    prompt = f"""Project: {project}
Total pages: {len(page_summaries)}

CONTEXT:
Each page has already received a dedicated Schema Report.
@ids are generated deterministically as: page_url + "#" + schema_type_lowercase
Your ONLY job: check whether the schemas listed below CONTRADICT each other across pages.
Flag ONLY:
- Organization or WebSite recommended on a non-Home Page
- Same schema type flagged with conflicting severity on different pages
- Errors that indicate a structural inconsistency across the site
DO NOT introduce any new issues, observations, or advice of any kind.
{schema_ref_context}
PAGES DATA:
{pages_data}

You are a Schema.org QA auditor. Return a JSON array of English Notion blocks.

Structure — include ONLY these sections:
1. heading_2: "Cross-Page Consistency Issues"
   - If no issues: one bulleted_list_item: "No cross-page inconsistencies found"
2. heading_2: "Schema Type Conflicts" — omit if no conflicts
3. heading_2: "Partially Analyzed Pages" — omit if no retry/failed pages
4. heading_2: "Report Quality Issues" — omit if no issues

ABSOLUTE RULES:
- DO NOT write positive confirmations
- DO NOT include implementation advice or testing instructions
- Return raw JSON array of Notion blocks only, all text in English"""

    raw    = _call_openai(prompt)
    result = safe_parse(raw)
    if result is not None:
        if credits_summary is not None:
            result += build_credits_blocks(credits_summary, total_scraping_credits, claude_tokens)
        return result
    raise ValueError(f"QA report parse failed. Last 300: ...{raw[-300:]}")


# ─── Credits Blocks ───────────────────────────────────────────────────────────

def build_credits_blocks(credits_summary, total_scraping_credits, claude_tokens):
    blocks = [{"object": "block", "type": "heading_2",
               "heading_2": {"rich_text": [{"type": "text", "text": {"content": "Run Credits Summary"}}]}}]
    blocks.append({"object": "block", "type": "heading_3",
                   "heading_3": {"rich_text": [{"type": "text", "text": {"content": "ScrapingBee"}}]}})
    for item in credits_summary:
        label = SCRAPER_LABELS.get(item["method"], item["method"])
        blocks.append({"object": "block", "type": "bulleted_list_item",
                       "bulleted_list_item": {"rich_text": [{"type": "text", "text": {
                           "content": f"{item['url']} — {label}"}}]}})
    blocks.append({"object": "block", "type": "paragraph",
                   "paragraph": {"rich_text": [{"type": "text", "text": {
                       "content": f"Total ScrapingBee credits used: {total_scraping_credits}"}}]}})

    input_t     = claude_tokens.get("input_tokens", 0)
    output_t    = claude_tokens.get("output_tokens", 0)
    input_cost  = round(input_t  / 1_000_000 * INPUT_COST_PER_M,  4)
    output_cost = round(output_t / 1_000_000 * OUTPUT_COST_PER_M, 4)
    total_cost  = round(input_cost + output_cost, 4)

    blocks.append({"object": "block", "type": "heading_3",
                   "heading_3": {"rich_text": [{"type": "text", "text": {"content": f"OpenAI API ({MODEL_NAME})"}}]}})
    blocks.append({"object": "block", "type": "bulleted_list_item",
                   "bulleted_list_item": {"rich_text": [{"type": "text", "text": {
                       "content": f"Input tokens:  {input_t:,}  (${input_cost})"}}]}})
    blocks.append({"object": "block", "type": "bulleted_list_item",
                   "bulleted_list_item": {"rich_text": [{"type": "text", "text": {
                       "content": f"Output tokens: {output_t:,}  (${output_cost})"}}]}})
    blocks.append({"object": "block", "type": "paragraph",
                   "paragraph": {"rich_text": [{"type": "text", "text": {
                       "content": f"Total OpenAI cost this run: ${total_cost}"}}]}})
    return blocks