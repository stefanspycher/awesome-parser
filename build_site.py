"""
Build a searchable static HTML catalog from repos.db.

Reads repos with http_status=200, extracts a description slug from each README,
and writes a single self-contained index.html with instant JS filtering.

Usage:
    uv run build_site.py [--db repos.db] [--out index.html]
"""

import argparse
import html
import json
import re
import sqlite3
from pathlib import Path

# ── description extraction ────────────────────────────────────────────────────

_BADGE_RE   = re.compile(r'!\[.*?\]\(.*?\)')          # ![alt](url)
_HTML_TAG   = re.compile(r'<[^>]+>')
_LINK_RE    = re.compile(r'\[([^\]]+)\]\([^)]+\)')    # [text](url) → text
_MULTI_SP   = re.compile(r'  +')


def _clean(line: str) -> str:
    """Strip markdown/HTML noise, return plain text."""
    line = _BADGE_RE.sub('', line)
    line = _HTML_TAG.sub('', line)
    line = _LINK_RE.sub(r'\1', line)
    line = line.replace('*', '').replace('`', '').replace('_', '')
    line = _MULTI_SP.sub(' ', line).strip()
    return line


def extract_description(readme: str | None, fallback: str) -> str:
    if not readme:
        return fallback

    lines = readme.splitlines()

    # Pass 1: prefer > blockquote (common in awesome-lists)
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('>'):
            candidate = _clean(stripped.lstrip('>').strip())
            if len(candidate) >= 20:
                return candidate[:160]

    # Pass 2: first plain-text line ≥30 chars that isn't a heading/badge/HTML
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(('#', '<', '!', '|', '-', '*', '[')):
            continue
        if stripped.startswith('=') or stripped.startswith('~'):
            continue
        candidate = _clean(stripped)
        if len(candidate) >= 30:
            return candidate[:160]

    return fallback


# ── HTML template ─────────────────────────────────────────────────────────────

HTML_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Awesome Repos</title>
<style>
  :root {{
    --bg:      #0f1117;
    --surface: #1a1d27;
    --border:  #2a2d3a;
    --accent:  #7c6af7;
    --text:    #e2e4ef;
    --muted:   #6b7280;
    --link:    #a59df8;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font: 15px/1.6 'Inter', system-ui, sans-serif;
    min-height: 100vh;
  }}
  header {{
    position: sticky; top: 0; z-index: 10;
    background: var(--bg);
    border-bottom: 1px solid var(--border);
    padding: 14px 20px;
    display: flex; align-items: center; gap: 16px;
  }}
  header h1 {{ font-size: 1rem; font-weight: 600; white-space: nowrap; }}
  #search {{
    flex: 1;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    font-size: 15px;
    padding: 8px 14px;
    outline: none;
    transition: border-color .15s;
  }}
  #search:focus {{ border-color: var(--accent); }}
  #search::placeholder {{ color: var(--muted); }}
  #count {{
    color: var(--muted);
    font-size: 13px;
    white-space: nowrap;
  }}
  main {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
    gap: 12px;
    padding: 20px;
  }}
  .card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px 16px;
    display: flex; flex-direction: column; gap: 6px;
    transition: border-color .15s;
  }}
  .card:hover {{ border-color: var(--accent); }}
  .card-title {{
    font-size: 14px; font-weight: 600;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }}
  .card-title a {{
    color: var(--link);
    text-decoration: none;
  }}
  .card-title a:hover {{ text-decoration: underline; }}
  .card-owner {{ font-size: 12px; color: var(--muted); }}
  .card-desc {{
    font-size: 13px;
    color: #9ca3af;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }}
  .card-meta {{
    display: flex; align-items: center; gap: 8px;
    margin-top: 2px;
  }}
  .card-freshness {{
    font-size: 11px;
    color: var(--muted);
    display: flex; align-items: center; gap: 3px;
  }}
  .card-freshness svg {{ opacity: .65; }}
  .hidden {{ display: none; }}
  #empty {{
    grid-column: 1/-1;
    text-align: center;
    padding: 60px 20px;
    color: var(--muted);
    font-size: 15px;
  }}
</style>
</head>
<body>
<header>
  <h1>&#127381; Awesome Repos</h1>
  <input id="search" type="search" placeholder="Search {total} repos…" autofocus autocomplete="off">
  <span id="count">{total} repos</span>
</header>
<main id="grid">
  <p id="empty" class="hidden">No results.</p>
</main>
<script>
const DATA = {data_json};

const grid   = document.getElementById('grid');
const empty  = document.getElementById('empty');
const count  = document.getElementById('count');
const search = document.getElementById('search');
const total  = DATA.length;

function timeAgo(iso) {{
  if (!iso) return '';
  const secs = Math.floor((Date.now() - new Date(iso)) / 1000);
  if (secs < 60)              return 'just now';
  if (secs < 3600)            return Math.floor(secs / 60)        + 'm ago';
  if (secs < 86400)           return Math.floor(secs / 3600)      + 'h ago';
  if (secs < 86400 * 30)      return Math.floor(secs / 86400)     + 'd ago';
  if (secs < 86400 * 365)     return Math.floor(secs / (86400*30))+ ' months ago';
  return Math.floor(secs / (86400 * 365)) + ' years ago';
}}

const CLOCK_ICON =
  `<svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5">` +
  `<circle cx="8" cy="8" r="7"/><path d="M8 4v4l2.5 2.5"/>` +
  `</svg>`;

// Build cards once
const cards = DATA.map((r, i) => {{
  const card = document.createElement('div');
  card.className = 'card';
  const freshness = r.pushed_at
    ? `<div class="card-freshness">${{CLOCK_ICON}} ${{timeAgo(r.pushed_at)}}</div>`
    : '';
  card.innerHTML =
    `<div class="card-title"><a href="${{r.url}}" target="_blank" rel="noopener">${{esc(r.name)}}</a></div>` +
    `<div class="card-owner">${{esc(r.owner)}}</div>` +
    (r.desc ? `<div class="card-desc">${{esc(r.desc)}}</div>` : '') +
    (freshness ? `<div class="card-meta">${{freshness}}</div>` : '');
  card._needle = (r.name + ' ' + r.owner + ' ' + r.desc).toLowerCase();
  grid.appendChild(card);
  return card;
}});

function esc(s) {{
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}}

search.addEventListener('input', () => {{
  const q = search.value.trim().toLowerCase();
  const terms = q ? q.split(/\\s+/) : [];
  let shown = 0;
  cards.forEach(card => {{
    const match = terms.length === 0 || terms.every(t => card._needle.includes(t));
    card.classList.toggle('hidden', !match);
    if (match) shown++;
  }});
  count.textContent = q ? `${{shown}} / ${{total}} repos` : `${{total}} repos`;
  empty.classList.toggle('hidden', shown > 0);
}});
</script>
</body>
</html>
"""

# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Build searchable HTML catalog from repos.db")
    parser.add_argument("--db",  default="repos.db",   help="SQLite DB (default: repos.db)")
    parser.add_argument("--out", default="index.html", help="Output file (default: index.html)")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"Error: '{db_path}' not found. Run crawl.py first.")

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT url, owner, name, readme_content, pushed_at FROM repos WHERE http_status = 200 ORDER BY owner, name"
    ).fetchall()
    conn.close()

    print(f"Building catalog from {len(rows)} repos…")

    records = []
    for url, owner, name, readme, pushed_at in rows:
        desc = extract_description(readme, "")
        rec: dict = {"url": url, "owner": owner, "name": name, "desc": desc}
        if pushed_at:
            rec["pushed_at"] = pushed_at
        records.append(rec)

    data_json = json.dumps(records, ensure_ascii=False, separators=(',', ':'))

    out = HTML_TEMPLATE.format(
        total=len(records),
        data_json=data_json,
    )

    Path(args.out).write_text(out, encoding="utf-8")
    size_kb = Path(args.out).stat().st_size // 1024
    print(f"Done → {args.out}  ({size_kb} KB,  {len(records)} repos)")


if __name__ == "__main__":
    main()
