import os
from schema_ref import get_or_create_schema_reference

PAGE_TYPE_SCHEMA_MAP = {
    "Home Page":           ["WebSite", "Organization"],
    "Product Page":        ["Product"],
    "Contact Page":        ["ContactPage"],
    "About Page":          ["AboutPage"],
    "Article / Blog Post": ["Article"],
    "Category Page":       ["CollectionPage"],
    "FAQ Page":            ["FAQPage"],
    "Service Page":        ["Service"],
    "Services Page":       ["ItemList", "Service"],
    "Hotel Page":          ["LodgingBusiness"],
    "Restaurant Page":     ["Restaurant"],
    "Recipe Page":         ["Recipe"],
    "Event Page":          ["Event"],
    "Job Posting Page":    ["JobPosting"],
    "Local Business Page": ["LocalBusiness"],
    "Doctor Page":         ["Physician"],
    "Expert Page":         ["Person"],
    "Team Page":           ["Person"],
    "Portfolio Page":      ["CreativeWork"],
}

SITE_TYPE_ORG_MAP = {
    "E-commerce Store (online only)":       "Organization",
    "E-commerce Store + Physical Location": "LocalBusiness",
    "Restaurant":                           "Restaurant",
    "Restaurant Chain":                     "Restaurant",
    "Hotel":                                "LodgingBusiness",
    "Hotel Chain / Hotel Group":            "LodgingBusiness",
    "Medical — Clinic":                     "MedicalClinic",
    "Medical — Hospital":                   "Hospital",
    "Medical — Treatment Room / Practice":  "MedicalBusiness",
    "Law Firm":                             "LegalService",
    "Real Estate Agency":                   "RealEstateAgent",
    "SaaS / Tech Startup":                  "SoftwareApplication",
}


def get_recommended_schemas(page_type, site_type):
    schemas = list(PAGE_TYPE_SCHEMA_MAP.get(page_type, []))
    if page_type == "Home Page" and "Organization" in schemas:
        org_subtype = SITE_TYPE_ORG_MAP.get(site_type, "Organization")
        schemas = [org_subtype if s == "Organization" else s for s in schemas]
    return schemas


def get_schema_fields(schema_type):
    ref = get_or_create_schema_reference(schema_type)
    if not ref:
        return {"required": [], "recommended": [], "google_rich_result": False}

    def parse_fields(raw):
        if not raw:
            return []
        return [f.strip() for f in raw.split(",") if f.strip()]

    return {
        "required":           parse_fields(ref.get("required_properties", "")),
        "recommended":        parse_fields(ref.get("recommended_properties", "")),
        "google_rich_result": ref.get("google_rich_result", False),
    }


def get_all_fields_for_page(page_type, site_type, existing_valid=None):
    existing_valid = existing_valid or []
    schemas = get_recommended_schemas(page_type, site_type)
    result = []
    for schema_type in schemas:
        if schema_type in existing_valid:
            print(f"  [schema_mapper] skipping {schema_type} — already valid")
            continue
        fields = get_schema_fields(schema_type)
        result.append({
            "schema_type":        schema_type,
            "required":           fields["required"],
            "recommended":        fields["recommended"],
            "google_rich_result": fields["google_rich_result"],
        })
        print(f"  [schema_mapper] {schema_type} → required: {fields['required']}")
    return result


if __name__ == "__main__":
    import sys, json
    result = get_all_fields_for_page("Home Page", "E-commerce Store (online only)")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit("STEP 1 OK — בדוק פלט")