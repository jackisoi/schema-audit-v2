import os

AI_PROVIDER = os.getenv("AI_PROVIDER", "anthropic").lower()

if AI_PROVIDER == "gemini":
    from gemini_agent import (
        analyze_with_scan,
        generate_executive_summary,
        generate_qa_report as _gemini_qa,
        extract_recommended_schemas,
        extract_schemas_from_json_ld,
        ai_usage as claude_usage,
        SCRAPER_CREDITS,
    )
    def generate_qa_report(*args, claude_tokens=None, ai_tokens=None, **kwargs):
        return _gemini_qa(*args, ai_tokens=claude_tokens or ai_tokens, **kwargs)
    print("[ai_router] Using provider: Gemini")

elif AI_PROVIDER == "openai":
    from openai_agent import (
        analyze_with_scan,
        generate_executive_summary,
        generate_qa_report,
        extract_recommended_schemas,
        extract_schemas_from_json_ld,
        claude_usage,
        SCRAPER_CREDITS,
    )
    print("[ai_router] Using provider: OpenAI")

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


def generate_text(prompt):
    """Generate plain text using the active AI provider."""
    if AI_PROVIDER == "gemini":
        from gemini_agent import generate_text as _generate_text
    elif AI_PROVIDER == "openai":
        from openai_agent import generate_text as _generate_text
    else:
        from claude_agent import generate_text as _generate_text
    return _generate_text(prompt)