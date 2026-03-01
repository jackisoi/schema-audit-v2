# Local test — simulates a JotForm submission without a webhook
from scraper import scan_page
from claude_agent import analyze_with_scan, generate_executive_summary, generate_qa_report, extract_schemas_from_json_ld
from notion_writer import get_or_create_project_page, write_report_to_notion, write_executive_summary, write_qa_report

# --- Simulated JotForm input ---
project = "Fattal Colors"
urls = [
    {"URL": "https://www.fattalcolors.co.il/", "Level": "1", "Page type": "Home page"},
    {"URL": "https://www.fattalcolors.co.il/nucha", "Level": "2", "Page type": "Hotel Page"},
    {"URL": "https://www.fattalcolors.co.il/nucha/zahara-restaurant", "Level": "21", "Page type": "Restaurant Page"},
    {"URL": "https://www.fattalcolors.co.il/nucha/special-offers", "Level": "22", "Page type": "Special Offers Page"},
    {"URL": "https://www.fattalcolors.co.il/nucha/rooms", "Level": "23", "Page type": "Rooms Page"}
]

# --- Run ---
project_page_id = get_or_create_project_page(project)
page_summaries = []

# Sort by level so Level 1 always runs first (parent context flows down)
urls_sorted = sorted(urls, key=lambda x: str(x["Level"]))
parent_context = {}  # {level: {"url": ..., "recommended_schemas": [...]}}

for item in urls_sorted:
    url = item["URL"]
    level = item["Level"]
    page_type = item["Page type"]

    print(f"Scanning: {url}")
    scan = scan_page(url)

    print(f"Analyzing: {url}")
    result = analyze_with_scan(
        scan, level, page_type, project,
        parent_context=parent_context
    )
    blocks = result["blocks"]
    used_retry = result["used_retry"]

    # Store recommended schemas for child levels
    parent_context[level] = {
        "url": url,
        "recommended_schemas": result["recommended_schemas"]
    }

    notion_url = write_report_to_notion(project_page_id, url, blocks, used_retry=used_retry)
    print(f"Report created: {notion_url}")

    sd = scan["structured_data"]
    json_ld = sd.get("json-ld", [])
    schemas_found, schema_ids = extract_schemas_from_json_ld(json_ld)
    page_summaries.append({
        "url": url,
        "level": level,
        "page_type": page_type,
        "used_retry": used_retry,
        "schemas_found": schemas_found,
        "schema_ids": schema_ids,
        "content_analysis": scan["content_analysis"]
    })

print("Generating executive summary...")
summary_blocks = generate_executive_summary(page_summaries, project)
summary_url = write_executive_summary(project_page_id, project, summary_blocks)
print(f"Executive summary: {summary_url}")
print("Generating QA report...")
qa_blocks = generate_qa_report(page_summaries, project)
qa_url = write_qa_report(project_page_id, project, qa_blocks)
print(f"QA report: {qa_url}")