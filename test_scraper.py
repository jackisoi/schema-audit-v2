from scraper import scan_page
import json

result = scan_page("https://www.fattalcolors.co.il/")
print(json.dumps(result, indent=2, ensure_ascii=False))