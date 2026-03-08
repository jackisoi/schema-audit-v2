import os
import traceback
import json
import requests
from flask import Flask, request, jsonify
from notion_client import Client
from scraper import scan_page
from ai_router import (
    analyze_page_v2,
    generate_executive_summary,
    generate_qa_report,
    claude_usage,
    SCRAPER_CREDITS,
)
from notion_writer import (
    get_or_create_project_page,
    write_report_to_notion,
    write_executive_summary,
    write_qa_report,
)
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)
notion     = Client(auth=os.getenv("NOTION_API_KEY"))
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "jacki-schema-audit-2026")


def send_ntfy(message):
    try:
        ntfy_url = "https://" + "ntfy.sh/" + NTFY_TOPIC
        requests.post(ntfy_url, data=message.encode("utf-8"), timeout=5)
    except Exception:
        pass


def is_page_blocked(scan):
    pt   = scan.get("page_text", {})
    text = pt.get("text") or ""
    return "Enable JavaScript" in text or "cf-browser-verification" in text


@app.route("/webhook", methods=["POST"])
def webhook():
    if request.args.get("key") != os.getenv("WEBHOOK_SECRET"):
        return jsonify({"status": "unauthorized"}), 401

    try:
        raw = request.form.get("rawRequest", "{}")
        try:
            data = json.loads(raw)
        except Exception as e:
            print(f"[DEBUG] json.loads failed: {e}")
            try:
                from json_repair import repair_json
                data = json.loads(repair_json(raw))
                print(f"[DEBUG] json_repair succeeded")
            except Exception as e2:
                print(f"[DEBUG] json_repair failed: {e2}")
                data = dict(request.form)

        project   = data.get("q5_project")  or data.get("Project",   "Unknown Project")
        site_type_raw = data.get("q11_typeA11", [])
        if isinstance(site_type_raw, list):
            site_type = ", ".join(site_type_raw) or "Unknown"
        else:
            site_type = site_type_raw or "Unknown"
        urls_raw  = data.get("q6_typeA")    or data.get("URLs for analysis", "[]")
        if isinstance(urls_raw, str):
            try:
                urls = json.loads(urls_raw)
            except Exception:
                urls = []
        else:
            urls = urls_raw

        print(f"[DEBUG RAW FULL] data keys: {list(data.keys())}")
        print(f"[DEBUG RAW FULL] raw request.form keys: {list(request.form.keys())}")
        raw_full = request.form.get("rawRequest", "")
        print(f"[DEBUG RAW FULL] rawRequest length: {len(raw_full)}")
        print(f"[DEBUG RAW FULL] rawRequest[:3000]: {raw_full[:3000]}")
        print(f"[DEBUG RAW FULL] data.get('pretty'): {repr(data.get('pretty'))}")
        print(f"Project: {project} | Site type: {site_type} | URLs: {len(urls)}")

        project_page_id = get_or_create_project_page(project)

        # Reset usage counter for this run
        claude_usage["input_tokens"]  = 0
        claude_usage["output_tokens"] = 0

        results        = []
        page_summaries = []
        urls_sorted    = sorted(urls, key=lambda x: str(x["Level"]))
        parent_context = {}   # level → {url, recommended_schemas}

        import sys
        from schema_mapper import get_all_fields_for_page

        for item in urls_sorted[:1]:  # רק דף הבית
            url       = item["URL"]
            level     = item["Level"]
            page_type = item["Page type"]
            scan = scan_page(url)

            # חלץ סכמות תקינות קיימות
            json_ld = scan["structured_data"].get("json-ld", [])
            existing_valid = []
            for block in json_ld:
                for entry in block.get("@graph", [block]):
                    t = entry.get("@type")
                    if t:
                        existing_valid.append(t) if isinstance(t, str) else existing_valid.extend(t)

            print(f"[DEBUG STEP 3] existing_valid={existing_valid}")
            fields = get_all_fields_for_page(page_type, site_type, existing_valid=existing_valid)
            print(f"[DEBUG STEP 3] fields to extract:")
            print(json.dumps(fields, ensure_ascii=False, indent=2))
            sys.exit("STEP 3 OK — בדוק פלט")

        for item in urls_sorted:
            url       = item["URL"]
            level     = item["Level"]
            page_type = item["Page type"]

            print(f"  Scanning level {level}: {url}")
            scan = scan_page(url)

            if str(level) == "1" and is_page_blocked(scan):
                print(f"  ⚠️ Homepage blocked — aborting run")
                notion.blocks.children.append(project_page_id, children=[{
                    "object": "block", "type": "callout",
                    "callout": {
                        "rich_text": [{"type": "text", "text": {
                            "content": f"⚠️ דף הבית ({url}) חסום על ידי Cloudflare או הגנת JavaScript. לא ניתן לנתח את האתר."
                        }}],
                        "icon":  {"emoji": "🚫"},
                        "color": "red_background"
                    }
                }])
                return jsonify({"status": "blocked", "project": project, "url": url})

            print(f"  Analyzing: {url}")
            result     = analyze_page_v2(scan, level, page_type, site_type,
                                         parent_context=parent_context)
            analysis   = result["analysis"]
            used_retry = result["used_retry"]

            # Update parent context for child levels
            parent_context[level] = {
                "url":                url,
                "recommended_schemas": analysis.get("recommended_schemas", []),
            }

            # Build parent_schemas for @id injection in schema templates
            parent_schemas = {}
            for lvl, ctx in parent_context.items():
                for r in ctx.get("recommended_schemas", []):
                    t = r["type"] if isinstance(r, dict) else r
                    if t in ("Organization", "WebSite"):
                        parent_schemas[t] = f"{ctx['url']}#{t.lower()}"

            notion_url = write_report_to_notion(
                project_page_id, url, analysis, page_type,
                parent_schemas=parent_schemas,
                used_retry=used_retry,
            )
            print(f"  Done: {notion_url}")
            results.append({"url": url, "notion": notion_url})

            page_summaries.append({
                "url":           url,
                "level":         level,
                "page_type":     page_type,
                "site_type":     site_type,
                "used_retry":    used_retry,
                "scrape_failed": False,
                "analysis":      analysis,
                "scraper_used":  scan.get("scraper_used", "unknown"),
            })

        print("  Generating executive summary...")
        try:
            summary_blocks = generate_executive_summary(page_summaries, project)
            summary_url    = write_executive_summary(project_page_id, project, summary_blocks)
            print(f"  Executive summary: {summary_url}")
        except Exception as e:
            print(f"  Executive summary failed: {e}")
            summary_url = None

        credits_summary = [
            {
                "url":     p["url"],
                "method":  p.get("scraper_used", "unknown"),
                "credits": SCRAPER_CREDITS.get(p.get("scraper_used", ""), 0),
            }
            for p in page_summaries
        ]
        total_scraping_credits = sum(c["credits"] for c in credits_summary)
        claude_tokens_snapshot = dict(claude_usage)

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
            status_text  = "ריצה הסתיימה בהצלחה"
        else:
            failed_parts = []
            if not summary_url: failed_parts.append("exec summary")
            if not qa_url:      failed_parts.append("QA report")
            status_emoji = "⚠️"
            status_text  = f"ריצה הסתיימה עם שגיאות: {', '.join(failed_parts)}"

        send_ntfy(f"{status_emoji} {project} — {status_text} ({len(results)} דפים)")
        return jsonify({
            "status":  "ok",
            "project": project,
            "results": results,
            "summary": summary_url,
            "qa":      qa_url,
        })

    except Exception as e:
        traceback.print_exc()
        _project = locals().get("project", "Unknown")
        send_ntfy(f"❌ {_project} — ריצה נכשלה: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)