import os
import traceback
import json
import requests
from flask import Flask, request, jsonify
from notion_client import Client
from scraper import scan_page
from claude_agent import (
    analyze_with_scan,
    generate_executive_summary,
    generate_qa_report,
    extract_recommended_schemas,
    extract_schemas_from_json_ld,
    claude_usage,
    SCRAPER_CREDITS,
)
from notion_writer import (
    get_or_create_project_page,
    write_report_to_notion,
    write_executive_summary,
    write_qa_report
)
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)
notion = Client(auth=os.getenv("NOTION_API_KEY"))
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "jacki-schema-audit-2026")

def send_ntfy(message):
    try:
        ntfy_url = "https://" + "ntfy.sh/" + NTFY_TOPIC
        requests.post(ntfy_url, data=message.encode("utf-8"), timeout=5)
    except Exception:
        pass

def is_page_blocked(scan):
    """Returns True if the page is blocked by Cloudflare or a JS gate."""
    pt = scan.get("page_text", {})
    text = pt.get("text") or ""
    return (
        "Enable JavaScript" in text or
        "cf-browser-verification" in text
    )

@app.route("/webhook", methods=["POST"])
def webhook():
    if request.args.get("key") != os.getenv("WEBHOOK_SECRET"):
        return jsonify({"status": "unauthorized"}), 401

    try:
        raw = request.form.get("rawRequest", "{}")
        try:
            data = json.loads(raw)
        except Exception:
            data = dict(request.form)

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

        # Reset Claude usage counter for this run
        claude_usage["input_tokens"]  = 0
        claude_usage["output_tokens"] = 0

        results = []
        page_summaries = []
        urls_sorted = sorted(urls, key=lambda x: str(x["Level"]))
        parent_context = {}

        for item in urls_sorted:
            url = item["URL"]
            level = item["Level"]
            page_type = item["Page type"]

            print(f"  Scanning level {level}: {url}")
            scan = scan_page(url)

            if str(level) == "1" and is_page_blocked(scan):
                print(f"  ⚠️ Homepage blocked — aborting run")
                blocked_blocks = [{
                    "object": "block",
                    "type": "callout",
                    "callout": {
                        "rich_text": [{"type": "text", "text": {"content": f"⚠️ דף הבית ({url}) חסום על ידי Cloudflare או הגנת JavaScript. לא ניתן לנתח את האתר. יש לפתור את חסימת הסריקה לפני הפקת דוחות."}}],
                        "icon": {"emoji": "🚫"},
                        "color": "red_background"
                    }
                }]
                notion.blocks.children.append(project_page_id, children=blocked_blocks)
                return jsonify({"status": "blocked", "project": project, "url": url})

            if scan.get("scrape_failed"):
                print(f"  ⚠️ Scrape failed for {url} — skipping analysis")
                page_summaries.append({
                    "url": url,
                    "level": level,
                    "page_type": page_type,
                    "scrape_failed": True,
                    "used_retry": False,
                    "recommended_schemas": [],
                    "recommended_ids": [],
                    "schemas_found": [],
                    "schema_ids": [],
                    "content_analysis": {},
                    "scraper_used": "failed",
                })
                results.append({"url": url, "notion": None})
                continue

            print(f"  Analyzing: {url}")
            result = analyze_with_scan(
                scan, level, page_type, project,
                parent_context=parent_context
            )
            blocks = result["blocks"]
            used_retry = result["used_retry"]

            # Store recommended schemas AND ids for child levels
            parent_context[level] = {
                "url": url,
                "recommended_schemas": result["recommended_schemas"],
                "recommended_ids": result["recommended_ids"]
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
                "recommended_schemas": result.get("recommended_schemas", []),
                "recommended_ids": result.get("recommended_ids", []),
                "schemas_found": schemas_found,
                "schema_ids": schema_ids,
                "content_analysis": scan["content_analysis"],
                "scraper_used": scan.get("scraper_used", "unknown"),
            })


        # Build credits summary — MUST be before the try block below
        credits_summary = [
            {
                "url": p["url"],
                "method": p.get("scraper_used", "unknown"),
                "credits": SCRAPER_CREDITS.get(p.get("scraper_used", ""), 0),
            }
            for p in page_summaries
        ]
        total_scraping_credits = sum(c["credits"] for c in credits_summary)
        claude_tokens_snapshot = dict(claude_usage)  # snapshot before QA call
        print("  Generating executive summary...")
        try:
            summary_blocks = generate_executive_summary(page_summaries, project)
            summary_url = write_executive_summary(project_page_id, project, summary_blocks)
            print(f"  Executive summary: {summary_url}")
        except Exception as e:
            print(f"  Executive summary failed: {e}")
            summary_url = None

        print("  Generating QA report...")
        try:
            qa_blocks = generate_qa_report(
                page_summaries, project,
                credits_summary=credits_summary,
                total_scraping_credits=total_scraping_credits,
                claude_tokens=claude_tokens_snapshot,
            )
            qa_url = write_qa_report(project_page_id, project, qa_blocks)
            print(f"  QA report: {qa_url}")
        except Exception as e:
            print(f"  QA report failed: {e}")
            qa_url = None

        if summary_url and qa_url:
            status_emoji = "✅"
            status_text = "ריצה הסתיימה בהצלחה"
        else:
            failed_parts = []
            if not summary_url:
                failed_parts.append("exec summary")
            if not qa_url:
                failed_parts.append("QA report")
            status_emoji = "⚠️"
            status_text = f"ריצה הסתיימה עם שגיאות: {', '.join(failed_parts)}"

        send_ntfy(f"{status_emoji} {project} — {status_text} ({len(results)} דפים)")
        return jsonify({
            "status": "ok",
            "project": project,
            "results": results,
            "summary": summary_url,
            "qa": qa_url
        })

    except Exception as e:
        traceback.print_exc()
        _project = locals().get("project", "Unknown")
        send_ntfy(f"❌ {_project} — ריצה נכשלה: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)