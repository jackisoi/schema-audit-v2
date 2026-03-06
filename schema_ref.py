import os
from notion_client import Client as NotionClient
from dotenv import load_dotenv

load_dotenv()

SCHEMA_REFERENCE_DB_ID = "ae96685f31d6490382142f2fb200a11d"

_notion = None

def _get_notion():
    global _notion
    if _notion is None:
        _notion = NotionClient(auth=os.getenv("NOTION_API_KEY"))
    return _notion


def lookup_schema_reference(schema_type):
    """Query the Schema Reference Notion database for a given schema type.
    Returns a dict with reference data, or None if not found.
    """
    try:
        all_results = _get_notion().search(
            query=schema_type,
            filter={"property": "object", "value": "page"}
        ).get("results", [])
        results = [
            r for r in all_results
            if r.get("parent", {}).get("database_id", "").replace("-", "") == SCHEMA_REFERENCE_DB_ID.replace("-", "")
        ]
        if not results:
            return None
        props = results[0]["properties"]
        def get_text(prop):
            return "".join(r["text"]["content"] for r in prop.get("rich_text", []))
        return {
            "google_rich_result": props["Google Rich Result"]["checkbox"],
            "required_properties": get_text(props["Required Properties"]),
            "recommended_properties": get_text(props["Recommended Properties"]),
            "google_docs_url": props["Google Docs URL"].get("url") or "",
            "schema_org_url": props["schema.org URL"].get("url") or "",
            "source": (props["Source"]["select"] or {}).get("name", ""),
        }
    except Exception as e:
        print(f"    [schema_ref] lookup error for {schema_type}: {e}")
        return None


def generate_schema_reference_data(schema_type):
    """Generate reference data for a schema type using the active AI provider."""
    try:
        from ai_router import generate_text
        prompt = f"""You are a Schema.org and Google Structured Data expert.
For the schema type "{schema_type}", return a JSON object with these exact keys:
{{
  "google_rich_result": true or false,
  "required_properties": "comma-separated list of required properties per Google (empty string if none)",
  "recommended_properties": "comma-separated list of recommended properties per Google (empty string if none)",
  "google_docs_url": "https://developers.google.com/search/docs/appearance/structured-data/... (empty string if no Google docs page exists)",
  "schema_org_url": "https://schema.org/{schema_type}",
  "source": "Gemini Knowledge"
}}
Return only the JSON object, no other text."""

        raw = generate_text(prompt).strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]
            raw = raw.rsplit("```", 1)[0].strip()
        import json
        try:
            return json.loads(raw)
        except Exception:
            try:
                from json_repair import repair_json
                return json.loads(repair_json(raw))
            except Exception:
                return None
    except Exception as e:
        print(f"    [schema_ref] generate error for {schema_type}: {e}")
        return None


def save_schema_reference(schema_type, data):
    """Save a new schema reference entry to the Notion database."""
    try:
        properties = {
            "Schema Type": {"title": [{"text": {"content": schema_type}}]},
            "Google Rich Result": {"checkbox": bool(data.get("google_rich_result", False))},
            "Required Properties": {"rich_text": [{"text": {"content": data.get("required_properties", "")[:2000]}}]},
            "Recommended Properties": {"rich_text": [{"text": {"content": data.get("recommended_properties", "")[:2000]}}]},
            "Source": {"select": {"name": data.get("source", "Claude Knowledge")}},
        }
        if data.get("google_docs_url"):
            properties["Google Docs URL"] = {"url": data["google_docs_url"]}
        if data.get("schema_org_url"):
            properties["schema.org URL"] = {"url": data["schema_org_url"]}
        _get_notion().pages.create(
            parent={"database_id": SCHEMA_REFERENCE_DB_ID},
            properties=properties
        )
        print(f"    [schema_ref] saved: {schema_type}")
    except Exception as e:
        print(f"    [schema_ref] save error for {schema_type}: {e}")


def get_or_create_schema_reference(schema_type):
    """Look up schema reference in Notion; generate via Claude and save if not found.
    Returns reference dict or None on failure.
    """
    entry = lookup_schema_reference(schema_type)
    if entry:
        print(f"    [schema_ref] found in DB: {schema_type}")
        return entry
    print(f"    [schema_ref] not found, generating: {schema_type}")
    data = generate_schema_reference_data(schema_type)
    if data:
        save_schema_reference(schema_type, data)
        return data
    return None