import os

AI_PROVIDER = os.getenv("AI_PROVIDER", "anthropic").lower()

if AI_PROVIDER == "gemini":
    from gemini_agent import (
        analyze_with_scan,
        generate_executive_summary,
        generate_qa_report,
        extract_recommended_schemas,
        extract_schemas_from_json_ld,
        ai_usage as claude_usage,
        SCRAPER_CREDITS,
    )
    print("[ai_router] Using provider: Gemini")
else:
    from claude_agent import (
        analyze_with_scan,
        generate_executive_summary,
        generate_qa_report,
        extract_recommended_schemas,
        extract_schemas_from_json_ld,
        claude_usage,
        SCRAPER_CREDITS,
    )
    print("[ai_router] Using provider: Anthropic (default)")