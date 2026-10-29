# Optional upstream asset refresh tool; fonts are already bundled in this snapshot.
#!/usr/bin/env python3
"""Fetch the exact upstream hass-gicisky font files, not substitutes.

Run from the repository root: python3 tools/fetch_upstream_fonts.py
Review upstream licensing/attribution before redistributing downloaded assets.
This fetches every upstream font into the integration folder, including fonts with
unclear redistribution terms; never commit those, and note a HACS update replaces
this folder (keep extras in /config/www/fonts/ instead).
"""
from pathlib import Path
import json
import urllib.request

SOURCE = 'https://api.github.com/repos/eigger/hass-gicisky/contents/custom_components/gicisky/fonts?ref=master'
DEST = Path(__file__).resolve().parents[1] / 'custom_components' / 'gicisky_102' / 'fonts'
REQUIRED = {'NotoSansKR-Regular.ttf', 'NotoSansKR-Medium.ttf', 'NotoSansKR-Bold.ttf', 'materialdesignicons-webfont.ttf', 'materialdesignicons-webfont_meta.json'}

def get(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'gicisky-mqtt-bridge-font-fetcher', 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(request, timeout=40) as response:
        return response.read()

def main():
    data = json.loads(get(SOURCE))
    if not isinstance(data, list):
        raise RuntimeError('Expected a GitHub directory listing')
    DEST.mkdir(parents=True, exist_ok=True)
    downloaded = set()
    for item in data:
        if item.get('type') != 'file':
            continue
        name = item['name']
        if not name.lower().endswith(('.ttf', '.otf', '.json', '.txt', '.md', '.license')):
            continue
        url = item.get('download_url')
        if not url:
            raise RuntimeError(f'Missing download URL for {name}')
        (DEST / name).write_bytes(get(url))
        downloaded.add(name)
        print('Fetched:', name)
    missing = REQUIRED - downloaded
    if missing:
        raise RuntimeError(f'Missing required files upstream: {sorted(missing)}')
    print(f'Fetched {len(downloaded)} files. Check each asset license before publication.')

if __name__ == '__main__':
    main()
