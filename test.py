import os
from dotenv import load_dotenv
import anthropic
from notion_client import Client

load_dotenv()

# Test Claude
claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
message = claude.messages.create(
    model="claude-sonnet-4-5",
    max_tokens=100,
    messages=[{"role": "user", "content": "Say: Claude is connected!"}]
)
print("Claude:", message.content[0].text)

# Test Notion
notion = Client(auth=os.getenv("NOTION_API_KEY"))
page = notion.pages.retrieve(os.getenv("NOTION_PARENT_PAGE_ID"))
print("Notion page title:", page["properties"]["title"]["title"][0]["plain_text"])