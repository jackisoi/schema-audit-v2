import os

AI_PROVIDER = os.getenv("AI_PROVIDER", "openai").lower()

if AI_PROVIDER == "gemini":
    from gemini_agent import (
        analyze_page_v2,
        generate_executive_summary,
        generate_qa_report,
        generate_text,
        ai_usage as claude_usage,
        SCRAPER_CREDITS,
    )
    print("[ai_router] Using provider: Gemini")

elif AI_PROVIDER == "openai":
    from openai_agent import (
        analyze_page_v2,
        generate_executive_summary,
        generate_qa_report,
        generate_text,
        claude_usage,
        SCRAPER_CREDITS,
    )
    print("[ai_router] Using provider: OpenAI")

else:
    # fallback: Anthropic Claude
    from claude_agent import (
        analyze_page_v2,
        generate_executive_summary,
        generate_qa_report,
        generate_text,
        claude_usage,
        SCRAPER_CREDITS,
    )
    print("[ai_router] Using provider: Anthropic (default)")