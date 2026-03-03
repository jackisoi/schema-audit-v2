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
    """Find existing project page or create a new one under PARENT_PAGE_ID.
    Page title includes the current date: 'Project Name — YYYY-MM-DD HH:MM'
    """
    from datetime import datetime
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
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
    # Not found — create it
    response = notion.pages.create(
        parent={"page_id": PARENT_PAGE_ID},
        properties={"title": [{"text": {"content": full_title}}]}
    )
    return response["id"]


# Keys allowed inside each block type's inner content dict
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
    """Split a long code string into Notion code blocks, cutting only at newline boundaries."""
    blocks = []
    lines = content.splitlines(keepends=True)
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) > 1900:
            if chunk:
                blocks.append({
                    "object": "block",
                    "type": "code",
                    "code": {
                        "language": language,
                        "rich_text": [{"type": "text", "text": {"content": chunk}}]
                    }
                })
            chunk = line
        else:
            chunk += line
    if chunk:
        blocks.append({
            "object": "block",
            "type": "code",
            "code": {
                "language": language,
                "rich_text": [{"type": "text", "text": {"content": chunk}}]
            }
        })
    return blocks


def sanitize_blocks(blocks):
    """Remove invalid fields from Claude-generated Notion blocks.
    - Strips unknown keys from inner block content dicts
    - Auto-splits code blocks exceeding 1900 chars at newline boundaries
    """
    clean = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if not block_type:
            continue
        inner = block.get(block_type, {})
        if not isinstance(inner, dict):
            continue

        # Strip invalid keys from inner content
        valid_keys = _VALID_INNER_KEYS.get(block_type, {"rich_text"})
        inner = {k: v for k, v in inner.items() if k in valid_keys}

        # Auto-split code blocks that exceed 1900 chars
        if block_type == "code":
            rich_text = inner.get("rich_text", [])
            content = "".join(
                rt.get("text", {}).get("content", "")
                for rt in rich_text
                if isinstance(rt, dict)
            )
            language = inner.get("language", "plain text")
            if len(content) > 1900:
                clean.extend(_split_code_content(content, language))
            else:
                clean.append({"object": "block", "type": block_type, block_type: inner})
            continue

        # Clean rich_text annotations
        rich_text = inner.get("rich_text", [])
        clean_rt = []
        for rt in rich_text:
            if isinstance(rt, dict):
                text_obj = rt.get("text", {})
                # Move misplaced annotations from text{} up to rt level
                if "annotations" in text_obj:
                    rt["annotations"] = text_obj.pop("annotations")
                # Strip invalid keys from text{} — only 'content' and 'link' are allowed
                rt["text"] = {k: v for k, v in text_obj.items() if k in {"content", "link"}}
                clean_rt.append(rt)
        if rich_text:
            inner["rich_text"] = clean_rt
        clean.append({"object": "block", "type": block_type, block_type: inner})
    return clean


def write_report_to_notion(project_page_id, url, blocks, used_retry=False):
    """Create a schema report sub-page under the project page."""
    title = f"Schema Report — {url}"
    all_blocks = []
    if used_retry:
        all_blocks.append({
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {"content": "⚠️ דף זה נותח עם נתונים חלקיים (retry mode) — ייתכן שחלק מתוכן הדף לא נלקח בחשבון. מומלץ לבדוק ידנית."}}],
                "icon": {"emoji": "⚠️"},
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
    """Create the QA report page under the project page."""
    title = f"דוח QA — {project}"
    response = notion.pages.create(
        parent={"page_id": project_page_id},
        properties={"title": [{"text": {"content": title}}]},
        children=sanitize_blocks(blocks)[:100]
    )
    return response["url"]


def write_executive_summary(project_page_id, project, blocks):
    """Create the executive summary page under the project page."""
    title = f"סיכום מנהלים — {project}"
    response = notion.pages.create(
        parent={"page_id": project_page_id},
        properties={"title": [{"text": {"content": title}}]},
        children=sanitize_blocks(blocks)[:100]
    )
    return response["url"]


def build_scan_blocks(scan_result):
    blocks = []
    ca = scan_result["content_analysis"]
    sd = scan_result["structured_data"]
    pt = scan_result.get("page_text", {})

    blocks.append(h2("Content Analysis"))
    blocks.append(para(f"H1: {ca.get('h1') or 'NOT FOUND'}"))
    h2s = ca.get("h2s", [])
    blocks.append(para(f"H2s ({len(h2s)}): " + " | ".join(h2s) if h2s else "H2s: none"))
    img = ca.get("images", {})
    blocks.append(para(f"Images: {img.get('total', 0)} total, {img.get('missing_alt', 0)} missing alt"))
    blocks.append(para(f"Video: {'Yes' if ca.get('video') else 'No'}"))
    blocks.append(para(f"Forms: {ca.get('forms') or 'None'}"))
    blocks.append(para(f"FAQ patterns: {'Yes (' + str(ca.get('question_count', 0)) + ' questions)' if ca.get('faq_patterns') else 'No'}"))
    contact = ca.get("contact_info", {})
    blocks.append(para(f"Phone: {contact.get('phone') or 'Not found'}"))
    blocks.append(para(f"Email: {contact.get('email') or 'Not found'}"))
    blocks.append(para(f"Address detected: {'Yes' if contact.get('address_detected') else 'No'}"))

    blocks.append(h2("Page Text"))
    paragraphs = pt.get("paragraphs", [])
    if paragraphs:
        for p in paragraphs:
            blocks.append(para(p))
    else:
        blocks.append(para("No text extracted."))

    blocks.append(h2("Structured Data"))
    json_ld = sd.get("json-ld", [])
    if json_ld:
        for i, item in enumerate(json_ld):
            blocks.append(para(f"JSON-LD block {i + 1}:"))
            content = json.dumps(item, indent=2, ensure_ascii=False)
            blocks.extend(_split_code_content(content, "json"))
    else:
        blocks.append(para("No JSON-LD found."))
    return blocks


def write_scan_to_notion(parent_id, scan_result):
    url = scan_result["url"]
    title = f"Scan — {url}"
    blocks = build_scan_blocks(scan_result)
    response = notion.pages.create(
        parent={"page_id": parent_id},
        properties={"title": [{"text": {"content": title}}]},
        children=blocks[:100]
    )
    return response["url"]