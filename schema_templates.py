import os
import json
from notion_client import Client
from dotenv import load_dotenv

load_dotenv()
notion = Client(auth=os.getenv("NOTION_API_KEY"))
SCHEMA_FIELDS_DB_ID = os.getenv("SCHEMA_FIELDS_DB_ID")

# ─── Startup Cache ────────────────────────────────────────────────────────────

_cache = {}
# Structure: { "FAQPage": [ {id, field_name, field_type, level, requirement, notes, channels, parent_ids}, ... ] }

def load_cache():
    global _cache
    _cache = {}
    has_more, cursor = True, None

    while has_more:
        kwargs = {"database_id": SCHEMA_FIELDS_DB_ID, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        res = notion.databases.query(**kwargs)

        for page in res["results"]:
            p = page["properties"]
            schema_type = _text(p, "schema")
            if not schema_type:
                continue
            _cache.setdefault(schema_type, []).append({
                "id":         page["id"],
                "field_name": _title(p, "field_name"),
                "field_type": _select(p, "field_type"),
                "level":      _select(p, "level"),
                "requirement":_select(p, "schema_org_requirement"),
                "notes":      _text(p, "notes"),
                "channels":   _multi(p, "channels"),
                "parent_ids": _relation(p, "parent_schema"),
            })

        has_more = res.get("has_more", False)
        cursor   = res.get("next_cursor")


# ─── Public API ───────────────────────────────────────────────────────────────

def get_template(schema_type: str, ai_fields: dict, context: dict) -> str:
    """
    Build a JSON-LD string for the given schema type.

    schema_type : e.g. "FAQPage"
    ai_fields   : {field_name: value}  — value is None if not found on page
    context     : {page_url, parent_schemas: {"Organization": "https://.../#org"}}
    """
    if not _cache:
        load_cache()

    fields = _cache.get(schema_type, [])

    obj = {
        "@context": "https://schema.org",
        "@type":    schema_type,
        "@id":      f"{context['page_url']}#{schema_type.lower()}",
    }

    # Build id → field map for parent lookup
    id_map = {f["id"]: f for f in fields}

    # Only process top-level fields (no parent_ids)
    for field in fields:
        if field["parent_ids"]:
            continue  # handled inside _build_nested

        name     = field["field_name"]
        children = [f for f in fields if field["id"] in f.get("parent_ids", [])]

        if children:
            nested = _build_nested(name, children, ai_fields)
            if nested:
                obj[name] = nested
        else:
            val = _resolve(ai_fields.get(name), field)
            if val is not None:
                obj[name] = val

    # Inject parent @id references if relevant
    parent_schemas = context.get("parent_schemas", {})
    if schema_type not in ("Organization", "WebSite"):
        for key, parent_id in parent_schemas.items():
            if key == "Organization":
                obj["isPartOf"] = {"@id": parent_id}

    return json.dumps(_remove_nulls(obj), indent=2, ensure_ascii=False)


def get_channels(schema_type: str) -> str:
    """
    Return channel icons string for display in report header.
    Example: '⭐ 🔍 🤖'
    """
    if not _cache:
        load_cache()
    fields = _cache.get(schema_type, [])
    present = set()
    for f in fields:
        present.update(f.get("channels", []))
    return " ".join(c for c in ["⭐", "🔍", "🤖"] if c in present)


# ─── Nested Object Builder ────────────────────────────────────────────────────

_NESTED_TYPES = {
    "address":            "PostalAddress",
    "offers":             "Offer",
    "aggregateRating":    "AggregateRating",
    "author":             "Person",
    "publisher":          "Organization",
    "location":           "Place",
    "hiringOrganization": "Organization",
    "potentialAction":    "SearchAction",
    "starRating":         "Rating",
}

def _build_nested(parent_name: str, children: list, ai_fields: dict) -> dict | None:
    nested = {}
    nested_type = _NESTED_TYPES.get(parent_name)
    if nested_type:
        nested["@type"] = nested_type

    for field in children:
        val = _resolve(ai_fields.get(field["field_name"]), field)
        if val is not None:
            nested[field["field_name"]] = val

    # Return None if nested has only @type (nothing useful)
    return nested if len(nested) > 1 else None


# ─── Value Resolver ───────────────────────────────────────────────────────────

def _resolve(value, field: dict):
    """
    value       : value from AI (or None)
    field       : field metadata dict

    Rules:
    - Value exists → use it
    - Required + no value → placeholder string
    - Recommended/Optional + no value → omit (return None)
    """
    if value is not None:
        return value
    if field["requirement"] == "Required":
        description = field.get("notes") or field["field_name"]
        return f"// REQUIRED: {description}"
    return None  # Recommended / Optional with no value → omit

def _remove_nulls(obj):
    if isinstance(obj, dict):
        return {k: _remove_nulls(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_remove_nulls(i) for i in obj if i is not None]
    return obj

# ─── Notion Property Readers ──────────────────────────────────────────────────

def _title(props: dict, key: str) -> str:
    try:
        return props[key]["title"][0]["text"]["content"]
    except (KeyError, IndexError):
        return ""

def _text(props: dict, key: str) -> str:
    try:
        return props[key]["rich_text"][0]["text"]["content"]
    except (KeyError, IndexError):
        return ""

def _select(props: dict, key: str) -> str | None:
    try:
        return props[key]["select"]["name"]
    except (KeyError, TypeError):
        return None

def _multi(props: dict, key: str) -> list:
    try:
        return [opt["name"] for opt in props[key]["multi_select"]]
    except (KeyError, TypeError):
        return []

def _relation(props: dict, key: str) -> list:
    try:
        return [r["id"] for r in props[key]["relation"]]
    except (KeyError, TypeError):
        return []