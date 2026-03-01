import traceback
import json
from flask import Flask, request, jsonify
from scraper import scan_page
from claude_agent import (
    analyze_with_scan,
    generate_executive_summary,
    generate_qa_report,
    extract_schemas_from_json_ld,
    extract_recommended_schemas
)
from notion_writer import (
    get_or_create_project_page,
    write_report_to_notion,
    write_executive_summary,
    write_qa_report
)

app = Flask(__name__)


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        # JotForm sends multipart/form-data with a 'rawRequest' field containing JSON
        raw = request.form.get("rawRequest", "{}")
        try:
            data = json.loads(raw)
        except Exception:
            data = dict(request.form)

        # JotForm field names: q5_project and q6_typeA
        project = data.get("q5_project") or data.get("Project", "Unknown Project")
        urls_raw = data.get("q6_typeA") or data.get("URLs for analysis", "[]")

        if isinstance(urls_raw, str):
            try:
                urls = json.loads(urls_raw)
            except Exception:
                urls = []
        else:
            urls = urls_raw

        print(f"Project: {project} | URLs: {len(urls)}")
        print(f"DEBUG urls list: {json.dumps(urls, ensure_ascii=False)}")

        project_page_id = get_or_create_project_page(project)
        results = []
        page_summaries = []

        # Sort by level before processing so Level 1 always runs first
        urls_sorted = sorted(urls, key=lambda x: str(x["Level"]))
        parent_context = {}

        for item in urls_sorted:
            url = item["URL"]
            level = item["Level"]
            page_type = item["Page type"]

            print(f"  Scanning level {level}: {url}")
            scan = scan_page(url)

            print(f"  Analyzing: {url}")
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

            notion_url = write_report_to_notion(
                project_page_id, url, blocks, used_retry=used_retry
            )
            print(f"  Done: {notion_url}")
            results.append({"url": url, "notion": notion_url})

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

        # Generate executive summary after all pages
        print("  Generating executive summary...")
        try:
            summary_blocks = generate_executive_summary(page_summaries, project)
            summary_url = write_executive_summary(project_page_id, project, summary_blocks)
            print(f"  Executive summary: {summary_url}")
        except Exception as e:
            print(f"  Executive summary failed: {e}")
            summary_url = None

        # Generate QA report
        print("  Generating QA report...")
        try:
            qa_blocks = generate_qa_report(page_summaries, project)
            qa_url = write_qa_report(project_page_id, project, qa_blocks)
            print(f"  QA report: {qa_url}")
        except Exception as e:
            print(f"  QA report failed: {e}")
            qa_url = None

        return jsonify({
            "status": "ok",
            "project": project,
            "results": results,
            "summary": summary_url,
            "qa": qa_url
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    app.run(port=5000, debug=True)