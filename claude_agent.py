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

def extract_recommended_schemas(blocks):
    """Extract @type and @id values from Claude's recommended schema code blocks.
    Returns (types_found, ids_found) — used in QA report and parent context.
    """
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

EXISTING SCHEMAS:
{existing_schemas[:6000]}
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
        retry_note = " \u26a0\ufe0f [\u05e0\u05d5\u05ea\u05d7 \u05d7\u05dc\u05e7\u05d9\u05ea - retry]" if p.get("used_retry") else ""
        pages_text += f"""
\u05d3\u05e3: {p['url']}{retry_note}
  \u05e1\u05d5\u05d2: {p['page_type']} | \u05e8\u05de\u05d4: {p['level']}
  H1: {ca.get('h1') or '\u05dc\u05d0 \u05e0\u05de\u05e6\u05d0'}
  \u05e1\u05db\u05de\u05d5\u05ea \u05e7\u05d9\u05d9\u05de\u05d5\u05ea: {schemas}
  FAQ: {'\u05db\u05df' if ca.get('faq_patterns') else '\u05dc\u05d0'}
"""

    retry_warning = ""
    if retry_pages:
        retry_warning = "\n\u26a0\ufe0f \u05d4\u05e2\u05e8\u05d4: \u05d4\u05d3\u05e4\u05d9\u05dd \u05d4\u05d1\u05d0\u05d9\u05dd \u05e0\u05d5\u05ea\u05d7\u05d5 \u05e2\u05dd \u05e0\u05ea\u05d5\u05e0\u05d9\u05dd \u05d7\u05dc\u05e7\u05d9\u05d9\u05dd (retry mode):\n" + "\n".join(retry_pages)

    prompt = f"""Project: {project}
Total pages analyzed: {len(page_summaries)}

PAGES SUMMARY:
{pages_text}
{retry_warning}

Write a comprehensive executive summary in Hebrew as a JSON array of Notion blocks.

Structure:
1. heading_2: "\u05e1\u05d9\u05db\u05d5\u05dd \u05de\u05e0\u05d4\u05dc\u05d9\u05dd \u2014 {project}"
2. paragraph: \u05e6\u05d9\u05d5\u05df \u05d1\u05e8\u05d9\u05d0\u05d5\u05ea \u05db\u05d5\u05dc\u05dc (Poor / Fair / Good / Excellent) + \u05d4\u05e1\u05d1\u05e8 \u05e7\u05e6\u05e8
3. heading_2: "\u05de\u05de\u05e6\u05d0\u05d9\u05dd \u05dc\u05e4\u05d9 \u05d3\u05e3"
4. bulleted_list_item per page: \u05e9\u05dd \u05d4\u05d3\u05e3 + \u05de\u05d4 \u05e0\u05de\u05e6\u05d0 + \u05de\u05d4 \u05d7\u05e1\u05e8
5. heading_2: "\u26a0\ufe0f \u05d1\u05e2\u05d9\u05d5\u05ea \u05d3\u05d7\u05d5\u05e4\u05d5\u05ea"
6. bulleted_list_item: \u05d1\u05e2\u05d9\u05d5\u05ea \u05e7\u05e8\u05d9\u05d8\u05d9\u05d5\u05ea (\u05dc\u05dc\u05d0 \u05e0\u05d2\u05d9\u05e9\u05d5\u05ea)
7. heading_2: "\u05e1\u05d3\u05e8 \u05e2\u05d3\u05d9\u05e4\u05d5\u05d9\u05d5\u05ea \u05de\u05d5\u05de\u05dc\u05e5"
8. numbered_list_item: \u05de\u05e9\u05d9\u05de\u05d5\u05ea \u05dc\u05e4\u05d9 \u05e1\u05d3\u05e8 \u05d7\u05e9\u05d9\u05d1\u05d5\u05ea
9. heading_2: "\u05d4\u05e2\u05e8\u05d5\u05ea \u05db\u05dc\u05dc\u05d9\u05d5\u05ea"
10. paragraph: \u05d4\u05de\u05dc\u05e6\u05d5\u05ea \u05db\u05dc\u05dc\u05d9\u05d5\u05ea (CMS, Yoast)

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
        rec_types = ", ".join(p.get("recommended_schemas", [])) or "\u05dc\u05d0 \u05d7\u05d5\u05dc\u05e6\u05d5"
        rec_ids = "\n    ".join(p.get("recommended_ids", [])) or "\u05dc\u05d0 \u05d7\u05d5\u05dc\u05e6\u05d5"
        retry_note = " \u26a0\ufe0f [\u05e0\u05d5\u05ea\u05d7 \u05d7\u05dc\u05e7\u05d9\u05ea]" if p.get("used_retry") else ""
        pages_data += f"""
\u05d3\u05e3: {p['url']}{retry_note}
  \u05e1\u05d5\u05d2: {p['page_type']} | \u05e8\u05de\u05d4: {p['level']}
  @types \u05e9\u05d4\u05d5\u05de\u05dc\u05e6\u05d5 \u05d1\u05d3\u05d5\u05d7:
    {rec_types}
  @ids \u05e9\u05d4\u05d5\u05de\u05dc\u05e6\u05d5 \u05d1\u05d3\u05d5\u05d7:
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
1. heading_2: "\u05d3\u05d5\u05d7 QA \u2014 {project}"
2. paragraph: \u05ea\u05e7\u05e6\u05d9\u05e8 \u05de\u05de\u05e6\u05d0\u05d9 \u05d4-QA
3. heading_2: "\u05d1\u05d3\u05d9\u05e7\u05ea \u05e9\u05e8\u05e9\u05d5\u05e8 @id"
4. bulleted_list_item per issue:
   - \u05d4\u05d0\u05dd @id \u05e9\u05dc Organization \u05e2\u05e7\u05d1\u05d9 \u05d1\u05d9\u05df \u05d4\u05d3\u05e4\u05d9\u05dd?
   - \u05d1\u05d3\u05d5\u05e7 slash/no-slash
   - \u05d0\u05dd \u05d0\u05d9\u05df \u05d1\u05e2\u05d9\u05d5\u05ea: \"\u05dc\u05d0 \u05e0\u05de\u05e6\u05d0\u05d5 \u05d0\u05d9-\u05e2\u05e7\u05d1\u05d9\u05d5\u05ea\"
5. heading_2: "\u05e1\u05db\u05de\u05d5\u05ea \u05dc\u05e4\u05d9 \u05d3\u05e3 \u2014 \u05d1\u05d3\u05d9\u05e7\u05ea \u05db\u05d9\u05e1\u05d5\u05d9"
6. bulleted_list_item per page: \u05e9\u05dd + \u05e1\u05db\u05de\u05d5\u05ea \u05e9\u05d4\u05d5\u05de\u05dc\u05e6\u05d5 + \u05d4\u05d0\u05dd \u05d4\u05db\u05d9\u05e1\u05d5\u05d9 \u05d4\u05d2\u05d9\u05d5\u05e0\u05d9?
7. heading_2: "\u26a0\ufe0f \u05d3\u05e4\u05d9\u05dd \u05e9\u05e0\u05d5\u05ea\u05d7\u05d5 \u05d7\u05dc\u05e7\u05d9\u05ea"
8. bulleted_list_item or paragraph \"\u05db\u05dc \u05d4\u05d3\u05e4\u05d9\u05dd \u05e0\u05d5\u05ea\u05d7\u05d5 \u05d1\u05d4\u05e6\u05dc\u05d7\u05d4\"
9. heading_2: "\u2705 \u05e6'\u05e7\u05dc\u05d9\u05e1\u05d8 \u05dc\u05e4\u05e0\u05d9 \u05e4\u05e8\u05e1\u05d5\u05dd"
10. to_do blocks: \u05e4\u05e2\u05d5\u05dc\u05d5\u05ea \u05dc\u05e4\u05e0\u05d9 \u05e2\u05dc\u05d9\u05d9\u05d4 \u05dc\u05d0\u05d5\u05d5\u05d9\u05e8
Rules:
- All text in Hebrew
- Return raw JSON array of Notion blocks only
- to_do blocks: type is \"to_do\", checked is false
- Be precise: mention specific @id values, schema types, page URLs
- Base everything ONLY on the recommended data above
- The QA checks consistency between pages, not whether schemas exist on live site"""

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
