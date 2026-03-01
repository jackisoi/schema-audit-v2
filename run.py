import os
import sys
from dotenv import load_dotenv
from scraper import scan_page
from notion_writer import write_scan_to_notion

load_dotenv()

PARENT_PAGE_ID = os.getenv("NOTION_PARENT_PAGE_ID")

if len(sys.argv) < 2:
    print("Usage: python run.py <url>")
    sys.exit(1)

url = sys.argv[1]
print(f"Scanning: {url}")

scan = scan_page(url)
notion_url = write_scan_to_notion(PARENT_PAGE_ID, scan)

print(f"Done! Page created in Notion: {notion_url}")