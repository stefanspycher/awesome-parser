"""
Crawl GitHub awesome-list READMEs and extract repo links.

Phase 1: Fetch READMEs from seed repos (repos.txt), extract github.com/user/repo links.
Phase 2: Fetch READMEs from all discovered repos concurrently, store in repos.db.
Phase 3 (--meta): Enrich repos with pushed_at / stars via GitHub REST API.

Usage:
    uv run crawl.py [--seeds repos.txt] [--concurrency 50] [--token GITHUB_TOKEN] [--meta]

Next step:
    uv run build_site.py
"""

import argparse
import asyncio
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

# Load .env if present (simple key=value parser, no extra dependency needed)
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# ── constants ─────────────────────────────────────────────────────────────────

GITHUB_REPO_RE = re.compile(
    r'https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)'
)

RAW_README_URLS = [
    "https://raw.githubusercontent.com/{owner}/{repo}/HEAD/README.md",
    "https://raw.githubusercontent.com/{owner}/{repo}/HEAD/readme.md",
    "https://raw.githubusercontent.com/{owner}/{repo}/HEAD/Readme.md",
    "https://raw.githubusercontent.com/{owner}/{repo}/main/README.md",
    "https://raw.githubusercontent.com/{owner}/{repo}/main/readme.md",
    "https://raw.githubusercontent.com/{owner}/{repo}/master/README.md",
    "https://raw.githubusercontent.com/{owner}/{repo}/master/readme.md",
]

# Noise: orgs/paths that aren't real repos
SKIP_OWNERS = {"orgs", "topics", "trending", "explore", "marketplace", "features"}
SKIP_REPOS  = {""}

DB_PATH = Path("repos.db")

# ── database ──────────────────────────────────────────────────────────────────

def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS repos (
            url             TEXT PRIMARY KEY,
            owner           TEXT NOT NULL,
            name            TEXT NOT NULL,
            discovered_from TEXT,
            readme_content  TEXT,
            http_status     INTEGER,
            fetched_at      TEXT,
            pushed_at       TEXT,
            stars           INTEGER
        )
    """)
    conn.commit()
    # Migrate existing DBs that predate pushed_at / stars columns
    for col, definition in [("pushed_at", "TEXT"), ("stars", "INTEGER")]:
        try:
            conn.execute(f"ALTER TABLE repos ADD COLUMN {col} {definition}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    return conn


def upsert_repo(conn: sqlite3.Connection, url: str, owner: str, name: str,
                discovered_from: str | None, readme: str | None, status: int) -> None:
    conn.execute("""
        INSERT INTO repos (url, owner, name, discovered_from, readme_content, http_status, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            readme_content = excluded.readme_content,
            http_status    = excluded.http_status,
            fetched_at     = excluded.fetched_at
    """, (url, owner, name, discovered_from,
          readme, status, datetime.now(timezone.utc).isoformat()))
    conn.commit()


def already_fetched(conn: sqlite3.Connection, url: str) -> bool:
    row = conn.execute(
        "SELECT http_status FROM repos WHERE url = ? AND http_status IS NOT NULL", (url,)
    ).fetchone()
    return row is not None

# ── fetching ──────────────────────────────────────────────────────────────────

def parse_github_url(url: str) -> tuple[str, str] | None:
    """Return (owner, repo) from a github.com URL, or None if not a valid repo URL."""
    m = GITHUB_REPO_RE.match(url.rstrip("/"))
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    # Strip .git suffix
    repo = repo.removesuffix(".git")
    if owner.lower() in SKIP_OWNERS or repo in SKIP_REPOS:
        return None
    # Reject paths deeper than /owner/repo
    path = url.split("github.com/", 1)[1].rstrip("/")
    if path.count("/") > 1:
        return None
    return owner, repo


def extract_github_repos(text: str) -> set[tuple[str, str]]:
    """Extract all (owner, repo) pairs mentioned in a text blob."""
    found: set[tuple[str, str]] = set()
    for match in GITHUB_REPO_RE.finditer(text):
        owner, repo = match.group(1), match.group(2)
        repo = repo.removesuffix(".git")
        if owner.lower() in SKIP_OWNERS or not repo:
            continue
        # Only accept URLs that end right after /owner/repo
        end = match.end()
        rest = text[end:end+1]
        if rest and rest not in (" ", "\n", "\r", ")", "]", '"', "'", "#", ",", "."):
            continue
        found.add((owner, repo))
    return found


async def fetch_readme(client: httpx.AsyncClient, owner: str, repo: str) -> tuple[str | None, int]:
    """Try multiple branch names, return (content, http_status)."""
    for template in RAW_README_URLS:
        url = template.format(owner=owner, repo=repo)
        try:
            r = await client.get(url, follow_redirects=True)
            if r.status_code == 200:
                return r.text, 200
        except httpx.RequestError:
            pass
    return None, 404

# ── GitHub metadata ───────────────────────────────────────────────────────────

async def fetch_repo_meta(client: httpx.AsyncClient, owner: str, repo: str) -> dict | None:
    """Fetch pushed_at and stargazers_count from the GitHub REST API."""
    url = f"https://api.github.com/repos/{owner}/{repo}"
    try:
        r = await client.get(url)
        if r.status_code == 200:
            data = r.json()
            return {
                "pushed_at": data.get("pushed_at"),
                "stars":     data.get("stargazers_count"),
            }
    except httpx.RequestError:
        pass
    return None


# ── phases ────────────────────────────────────────────────────────────────────

async def phase1_seed_crawl(
    seeds: list[tuple[str, str]],
    client: httpx.AsyncClient,
    conn: sqlite3.Connection,
) -> set[tuple[str, str]]:
    """Fetch seed READMEs, return all discovered (owner, repo) pairs."""
    discovered: set[tuple[str, str]] = set()

    for owner, repo in seeds:
        canonical_url = f"https://github.com/{owner}/{repo}"
        print(f"  seed  {canonical_url}")
        content, status = await fetch_readme(client, owner, repo)
        upsert_repo(conn, canonical_url, owner, repo, None, content, status)

        if content:
            found = extract_github_repos(content)
            # Don't re-add the seed itself
            found.discard((owner, repo))
            discovered.update(found)
            print(f"         → {len(found)} links found")
        else:
            print(f"         → README not found (HTTP {status})")

    return discovered


async def phase2_bulk_crawl(
    targets: set[tuple[str, str]],
    client: httpx.AsyncClient,
    conn: sqlite3.Connection,
    concurrency: int,
    seeds: set[tuple[str, str]],
) -> None:
    """Fetch READMEs for all discovered repos concurrently."""
    # Skip seeds (already fetched) and already-fetched repos
    to_fetch = [
        (owner, repo) for owner, repo in targets
        if (owner, repo) not in seeds
        and not already_fetched(conn, f"https://github.com/{owner}/{repo}")
    ]

    total = len(to_fetch)
    done = 0
    sem = asyncio.Semaphore(concurrency)

    async def fetch_one(owner: str, repo: str) -> None:
        nonlocal done
        async with sem:
            url = f"https://github.com/{owner}/{repo}"
            content, status = await fetch_readme(client, owner, repo)
            upsert_repo(conn, url, owner, repo,
                        None,  # discovered_from not tracked at bulk level
                        content, status)
            done += 1
            ok = "✓" if status == 200 else "✗"
            print(f"  [{done:4}/{total}] {ok} {url}", flush=True)

    await asyncio.gather(*[fetch_one(o, r) for o, r in to_fetch])


async def phase3_enrich_meta(
    conn: sqlite3.Connection,
    client: httpx.AsyncClient,
    concurrency: int,
) -> None:
    """Enrich repos with pushed_at / stars from the GitHub API (skips already-enriched rows)."""
    rows = conn.execute(
        "SELECT url, owner, name FROM repos WHERE http_status = 200 AND pushed_at IS NULL"
    ).fetchall()

    if not rows:
        print("  All repos already enriched — nothing to do.")
        return

    total = len(rows)
    done = 0
    sem = asyncio.Semaphore(concurrency)

    async def enrich_one(url: str, owner: str, repo: str) -> None:
        nonlocal done
        async with sem:
            meta = await fetch_repo_meta(client, owner, repo)
            if meta:
                conn.execute(
                    "UPDATE repos SET pushed_at = ?, stars = ? WHERE url = ?",
                    (meta["pushed_at"], meta["stars"], url),
                )
                conn.commit()
            done += 1
            ok = "✓" if meta else "✗"
            print(f"  [{done:4}/{total}] {ok} {url}", flush=True)

    await asyncio.gather(*[enrich_one(url, o, r) for url, o, r in rows])


# ── main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(description="Crawl GitHub awesome-list READMEs")
    parser.add_argument("--seeds", default="repos.txt",
                        help="Text file with one GitHub URL per line (default: repos.txt)")
    parser.add_argument("--concurrency", type=int, default=50,
                        help="Max concurrent requests in phase 2 (default: 50)")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"),
                        help="GitHub personal access token (raises rate limit to 5000/hr); "
                             "falls back to GITHUB_TOKEN env var / .env file")
    parser.add_argument("--meta", action="store_true",
                        help="Run phase 3: enrich repos with pushed_at/stars via GitHub API")
    args = parser.parse_args()

    seeds_path = Path(args.seeds)
    if not seeds_path.exists():
        print(f"Error: seed file '{seeds_path}' not found.", file=sys.stderr)
        sys.exit(1)

    raw_lines = [l.strip() for l in seeds_path.read_text().splitlines() if l.strip() and not l.startswith("#")]
    seeds: list[tuple[str, str]] = []
    for line in raw_lines:
        parsed = parse_github_url(line if line.startswith("http") else f"https://github.com/{line}")
        if parsed:
            seeds.append(parsed)
        else:
            print(f"Warning: could not parse '{line}', skipping.", file=sys.stderr)

    if not seeds:
        print("No valid seed repos found.", file=sys.stderr)
        sys.exit(1)

    headers = {"User-Agent": "awesome-parser/1.0"}
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"

    conn = init_db()

    limits = httpx.Limits(max_connections=args.concurrency + 10, max_keepalive_connections=args.concurrency)
    async with httpx.AsyncClient(headers=headers, timeout=20.0, limits=limits) as client:
        if args.meta:
            enrichable = conn.execute(
                "SELECT COUNT(*) FROM repos WHERE http_status = 200 AND pushed_at IS NULL"
            ).fetchone()[0]
            print(f"\n── Phase 3: enriching {enrichable} repo(s) with GitHub metadata ──")
            await phase3_enrich_meta(conn, client, args.concurrency)
        else:
            print(f"\n── Phase 1: crawling {len(seeds)} seed repo(s) ──")
            discovered = await phase1_seed_crawl(seeds, client, conn)
            print(f"\n── Phase 2: fetching {len(discovered)} discovered repo(s) (concurrency={args.concurrency}) ──")
            await phase2_bulk_crawl(discovered, client, conn, args.concurrency, set(seeds))

    total = conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0]
    ok    = conn.execute("SELECT COUNT(*) FROM repos WHERE http_status = 200").fetchone()[0]
    enriched = conn.execute("SELECT COUNT(*) FROM repos WHERE pushed_at IS NOT NULL").fetchone()[0]
    conn.close()

    if args.meta:
        print(f"\nDone. {enriched}/{ok} repos enriched with metadata → {DB_PATH}")
    else:
        print(f"\nDone. {ok}/{total} READMEs fetched → {DB_PATH}")
    print("Next step: uv run build_site.py")


if __name__ == "__main__":
    asyncio.run(main())
