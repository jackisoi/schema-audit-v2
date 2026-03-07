import os
import json
from dotenv import load_dotenv
from notion_client import Client

load_dotenv()
notion = Client(auth=os.getenv("NOTION_API_KEY"))
PARENT_PAGE_ID = os.getenv("NOTION_PARENT_PAGE_ID")


def h2(text):
    return {"object": "block", "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": text}}]}}


def para(text):
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": str(text)}}]}}


def code(text, language="json"):
    content = text if len(text) <= 1900 else text[:1900]
    return {"object": "block", "type": "code",
            "code": {"language": language,
                     "rich_text": [{"type": "text", "text": {"content": content}}]}}


def get_or_create_project_page(project_name):
    from datetime import datetime
    date_str   = datetime.now().strftime("%Y-%m-%d %H:%M")
    full_title = f"{project_name} — {date_str}"
    results = notion.search(
        query=full_title,
        filter={"property": "object", "value": "page"}
    ).get("results", [])
    for page in results:
        title = page.get("properties", {}).get("title", {}).get("title", [])
        if title and title[0]["text"]["content"] == full_title:
            parent = page.get("parent", {})
            if parent.get("page_id") == PARENT_PAGE_ID:
                return page["id"]
    response = notion.pages.create(
        parent={"page_id": PARENT_PAGE_ID},
        properties={"title": [{"text": {"content": full_title}}]}
    )
    return response["id"]


_VALID_INNER_KEYS = {
    "paragraph":            {"rich_text", "color"},
    "heading_1":            {"rich_text", "color", "is_toggleable"},
    "heading_2":            {"rich_text", "color", "is_toggleable"},
    "heading_3":            {"rich_text", "color", "is_toggleable"},
    "bulleted_list_item":   {"rich_text", "color", "children"},
    "numbered_list_item":   {"rich_text", "color", "children"},
    "to_do":                {"rich_text", "checked", "color", "children"},
    "toggle":               {"rich_text", "color", "children"},
    "quote":                {"rich_text", "color", "children"},
    "callout":              {"rich_text", "icon", "color", "children"},
    "code":                 {"rich_text", "language", "caption"},
    "divider":              set(),
}


def _split_code_content(content, language):
    blocks = []
    lines  = content.splitlines(keepends=True)
    chunk  = ""
    for line in lines:
        if len(chunk) + len(line) > 1900:
            if chunk:
                blocks.append({
                    "object": "block", "type": "code",
                    "code": {"language": language,
                             "rich_text": [{"type": "text", "text": {"content": chunk}}]}
                })
            chunk = line
        else:
            chunk += line
    if chunk:
        blocks.append({
            "object": "block", "type": "code",
            "code": {"language": language,
                     "rich_text": [{"type": "text", "text": {"content": chunk}}]}
        })
    return blocks


def sanitize_blocks(blocks):
    clean = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if not block_type:
            continue
        inner = block.get(block_type, {})
        if not isinstance(inner, dict):
            inner = {}
        if not inner and "text" in block:
            text_val = block["text"]
            if isinstance(text_val, str):
                inner = {"rich_text": [{"type": "text", "text": {"content": text_val}}]}
            elif isinstance(text_val, list):
                inner = {"rich_text": text_val}
        if "text" in inner and "rich_text" not in inner:
            text_val = inner.pop("text")
            if isinstance(text_val, str):
                inner["rich_text"] = [{"type": "text", "text": {"content": text_val}}]
            elif isinstance(text_val, list):
                inner["rich_text"] = text_val
        valid_keys = _VALID_INNER_KEYS.get(block_type, {"rich_text"})
        inner = {k: v for k, v in inner.items() if k in valid_keys}
        if block_type == "code":
            rich_text = inner.get("rich_text", [])
            content   = "".join(
                rt.get("text", {}).get("content", "")
                for rt in rich_text if isinstance(rt, dict)
            )
            language = inner.get("language", "plain text")
            if len(content) > 1900:
                clean.extend(_split_code_content(content, language))
            else:
                clean.append({"object": "block", "type": block_type, block_type: inner})
            continue
        rich_text = inner.get("rich_text", [])
        clean_rt  = []
        for rt in rich_text:
            if isinstance(rt, dict):
                text_obj = rt.get("text", {})
                if "annotations" in text_obj:
                    rt["annotations"] = text_obj.pop("annotations")
                rt["text"] = {k: v for k, v in text_obj.items() if k in {"content", "link"}}
                clean_rt.append(rt)
        if rich_text:
            inner["rich_text"] = clean_rt
        clean.append({"object": "block", "type": block_type, block_type: inner})
    return clean


# ─── Phase 2: write_report_to_notion ─────────────────────────────────────────

def write_report_to_notion(project_page_id, url, analysis, page_type,
                           parent_schemas=None, used_retry=False):
    context = {
        "page_url":       url,
        "page_type":      page_type,
        "parent_schemas": parent_schemas or {},
    }
    blocks = build_report_blocks(analysis, context)
    title  = f"Schema Report — {url}"
    all_blocks = []
    if used_retry:
        all_blocks.append({
            "object": "block", "type": "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {
                    "content": "\u26a0\ufe0f \u05d3\u05e3 \u05d6\u05d4 \u05e0\u05d5\u05ea\u05d7 \u05e2\u05dd \u05e0\u05ea\u05d5\u05e0\u05d9\u05dd \u05d7\u05dc\u05e7\u05d9\u05d9\u05dd (retry mode) \u2014 \u05d9\u05d9\u05ea\u05db\u05df \u05e9\u05d7\u05dc\u05e7 \u05de\u05ea\u05d5\u05db\u05df \u05d4\u05d3\u05e3 \u05dc\u05d0 \u05e0\u05dc\u05e7\u05d7 \u05d1\u05d7\u05e9\u05d1\u05d5\u05df. \u05de\u05d5\u05de\u05dc\u05e5 \u05dc\u05d1\u05d3\u05d5\u05e7 \u05d9\u05d3\u05e0\u05d9\u05ea."
                }}],
                "icon":  {"emoji": "\u26a0\ufe0f"},
                "color": "orange_background"
            }
        })
    all_blocks.extend(sanitize_blocks(blocks))
    response = notion.pages.create(
        parent={"page_id": project_page_id},
        properties={"title": [{"text": {"content": title}}]},
        children=all_blocks[:100]
    )
    return response["url"]


def write_qa_report(project_page_id, project, blocks):
    title    = f"\u05d3\u05d5\u05d7 QA \u2014 {project}"
    response = notion.pages.create(
        parent={"page_id": project_page_id},
        properties={"title": [{"text": {"content": title}}]},
        children=sanitize_blocks(blocks)[:100]
    )
    return response["url"]


def write_executive_summary(project_page_id, project, blocks):
    print(f"[exec_debug] block[0]: {blocks[0] if blocks else 'EMPTY'}", flush=True)
    title    = f"סיכום מנהלים — {project}"
    response = notion.pages.create(
        parent={"page_id": project_page_id},
        properties={"title": [{"text": {"content": title}}]},
        children=sanitize_blocks(blocks)[:100]
    )
    return response["url"]


def build_scan_blocks(scan_result):
    blocks  = []
    ca      = scan_result["content_analysis"]
    sd      = scan_result["structured_data"]
    pt      = scan_result.get("page_text", {})
    blocks.append(h2("Content Analysis"))
    blocks.append(para(f"H1: {ca.get('h1') or 'NOT FOUND'}"))
    h2s = ca.get("h2s", [])
    blocks.append(para(f"H2s ({len(h2s)}): " + " | ".join(h2s) if h2s else "H2s: none"))
    img = ca.get("images", {})
    blocks.append(para(f"Images: {img.get('total', 0)} total, {img.get('missing_alt', 0)} missing alt"))
    blocks.append(para(f"Video: {'Yes' if ca.get('video') else 'No'}"))
    q_str = f"Yes ({ca.get('question_count', 0)} questions)" if ca.get('faq_patterns') else "No"
    blocks.append(para(f"FAQ patterns: {q_str}"))
    contact = ca.get("contact_info", {})
    blocks.append(para(f"Phone: {contact.get('phone') or 'Not found'}"))
    blocks.append(para(f"Email: {contact.get('email') or 'Not found'}"))
    blocks.append(para(f"Address detected: {'Yes' if contact.get('address_detected') else 'No'}"))
    blocks.append(h2("Page Text"))
    paragraphs = pt.get("paragraphs", [])
    for p in (paragraphs or ["No text extracted."]):
        blocks.append(para(p))
    blocks.append(h2("Structured Data"))
    json_ld = sd.get("json-ld", [])
    if json_ld:
        for i, item in enumerate(json_ld):
            blocks.append(para(f"JSON-LD block {i + 1}:"))
            blocks.extend(_split_code_content(json.dumps(item, indent=2, ensure_ascii=False), "json"))
    else:
        blocks.append(para("No JSON-LD found."))
    return blocks


def write_scan_to_notion(parent_id, scan_result):
    url      = scan_result["url"]
    title    = f"Scan — {url}"
    blocks   = build_scan_blocks(scan_result)
    response = notion.pages.create(
        parent={"page_id": parent_id},
        properties={"title": [{"text": {"content": title}}]},
        children=blocks[:100]
    )
    return response["url"]


# ─── Phase 2: Report Builder ──────────────────────────────────────────────────

from schema_templates import get_template, get_channels

SEVERITY_EMOJI = {"error": "🔴", "warning": "🟡", "info": "🔵"}


def build_report_blocks(analysis: dict, context: dict) -> list:
    blocks  = []
    blocks += _build_overview(analysis)
    blocks += _build_issues_section(analysis)
    blocks += _build_schemas_section(analysis, context)
    blocks += _build_action_items(analysis)
    blocks += _build_observations(analysis)
    blocks += _build_content_recommendations(analysis)
    return blocks


def _build_overview(analysis: dict) -> list:
    existing    = analysis.get("existing_schemas", {})
    recommended = analysis.get("recommended_schemas", [])
    all_issues  = existing.get("issues", [])
    errors      = [i for i in all_issues if i.get("severity") == "error"]
    warnings    = [i for i in all_issues if i.get("severity") == "warning"]
    summary = (
        f"{len(existing.get('valid', []))} valid schema(s)  ·  "
        f"{len(errors)} error(s)  ·  {len(warnings)} warning(s)  ·  "
        f"{len(recommended)} schema(s) to implement"
    )
    return [h2("📋 Overview"), para(summary)]


def _build_issues_section(analysis: dict) -> list:
    existing   = analysis.get("existing_schemas", {})
    all_issues = existing.get("issues", [])

    if not all_issues:
        return [h2("✅ Issues Found"), para("No issues detected.")]

    _order     = {"error": 0, "warning": 1, "info": 2}
    all_issues = sorted(all_issues, key=lambda x: _order.get(x.get("severity"), 3))

    blocks = [h2("⚠️ Issues Found")]
    for issue in all_issues:
        emoji  = SEVERITY_EMOJI.get(issue.get("severity"), "•")
        schema = issue.get("schema", "")
        blocks.append(para(f"{emoji}  {schema} — {issue.get('problem', '')}"))
    return blocks


def _build_schemas_section(analysis: dict, context: dict) -> list:
    recommended = analysis.get("recommended_schemas", [])
    if not recommended:
        return []
    blocks = [h2("🔧 Schemas to Implement")]
    for schema in recommended:
        schema_type = schema["type"]
        channels    = get_channels(schema_type)
        header      = f"{schema_type}   {channels}".strip()
        blocks.append({
            "object": "block", "type": "heading_3",
            "heading_3": {"rich_text": [{"type": "text", "text": {"content": header}}]}
        })
        json_ld = get_template(schema_type, schema.get("fields", {}), context)
        blocks.extend(_split_code_content(json_ld, "json"))
    return blocks


def _build_action_items(analysis: dict) -> list:
    items      = []
    existing   = analysis.get("existing_schemas", {})
    all_issues = existing.get("issues", [])
    for issue in all_issues:
        if issue.get("severity") == "error":
            items.append(f"Fix {issue.get('schema', '')}: {issue.get('problem', '')}")
    for issue in all_issues:
        if issue.get("severity") == "warning":
            items.append(f"Review {issue.get('schema', '')}: {issue.get('problem', '')}")
    for schema in analysis.get("recommended_schemas", []):
        channels = get_channels(schema["type"])
        items.append(f"Implement {schema['type']}  {channels}".strip())
    if not items:
        return []
    blocks = [h2("✅ Action Items")]
    for item in items:
        blocks.append({
            "object": "block", "type": "numbered_list_item",
            "numbered_list_item": {"rich_text": [{"type": "text", "text": {"content": item}}]}
        })
    return blocks


def _build_observations(analysis: dict) -> list:
    obs = [o for o in analysis.get("observations", []) if o.get("type") != "content_recommendation"]
    if not obs:
        return []
    blocks = [h2("💡 Observations")]
    for o in obs:
        blocks.append({
            "object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": o.get("text", "")}}]}
        })
    return blocks


def _build_content_recommendations(analysis: dict) -> list:
    recs = [o for o in analysis.get("observations", []) if o.get("type") == "content_recommendation"]
    if not recs:
        return []
    blocks = [h2("📝 Content Recommendations")]
    for i, rec in enumerate(recs, 1):
        blocks.append(para(f"Step {i}: {rec.get('text', '')}"))
    return blocks