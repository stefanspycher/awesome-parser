# Awesome Parser

A tool that crawls GitHub [awesome-list](https://github.com/sindresorhus/awesome) repositories, extracts every linked project, enriches each one with freshness data from the GitHub API, and builds a **self-contained, searchable static HTML catalog** — deployed via GitHub Pages.

🔗 **Live site:** [stefanspycher.github.io/awesome-parser](https://stefanspycher.github.io/awesome-parser/)

---

## How it works

```
repos.txt  →  crawl.py  →  repos.db  →  build_site.py  →  index.html
```

| Step | Command | What it does |
|---|---|---|
| 1. Crawl READMEs | `uv run crawl.py` | Fetches READMEs from seed lists, extracts all linked GitHub repos |
| 2. Enrich metadata | `uv run crawl.py --meta` | Adds `pushed_at` / stars for each repo via the GitHub REST API |
| 3. Build site | `uv run build_site.py` | Generates a single `index.html` with instant JS search |
| 4. Deploy | `git push` | GitHub Pages picks up the updated `index.html` automatically |

### Setup

```bash
# Install dependencies
uv sync

# (Optional but recommended) add your GitHub token to avoid rate limits
echo "GITHUB_TOKEN=ghp_yourtoken" > .env

# Full run
uv run crawl.py
uv run crawl.py --meta
uv run build_site.py
```

A GitHub token raises the API rate limit from 60 → 5 000 requests/hour. Without one the `--meta` enrichment phase will be very slow for large catalogs.

---

## Source lists

The catalog is built from the following awesome lists. All credit goes to their respective authors and communities — this tool is just a reader.

| Repository | Author | Description |
|---|---|---|
| [agarrharr/awesome-cli-apps](https://github.com/agarrharr/awesome-cli-apps) | [@agarrharr](https://github.com/agarrharr) | A curated list of command line apps |
| [sindresorhus/awesome](https://github.com/sindresorhus/awesome) | [@sindresorhus](https://github.com/sindresorhus) | The meta-list of awesome lists |
| [Slackadays/Clipboard](https://github.com/Slackadays/Clipboard) | [@Slackadays](https://github.com/Slackadays) | Cut, copy, and paste anything, anywhere, all from the terminal |
| [junegunn/fzf](https://github.com/junegunn/fzf) | [@junegunn](https://github.com/junegunn) | A command-line fuzzy finder |
| [kohler/gifsicle](https://github.com/kohler/gifsicle) | [@kohler](https://github.com/kohler) | Create, manipulate, and optimize GIF images |
| [unixorn/awesome-zsh-plugins](https://github.com/unixorn/awesome-zsh-plugins) | [@unixorn](https://github.com/unixorn) | A collection of Zsh plugins, themes, and helpers |
| [k4m4/terminals-are-sexy](https://github.com/k4m4/terminals-are-sexy) | [@k4m4](https://github.com/k4m4) | A curated list for CLI lovers |
| [jondot/awesome-devenv](https://github.com/jondot/awesome-devenv) | [@jondot](https://github.com/jondot) | A curated list of development environment tools and resources |

> All listed repositories and their contents belong to their respective owners and are subject to their own licenses.

---

## Adding more lists

Add one GitHub URL per line to `repos.txt`, then re-run the full pipeline:

```bash
uv run crawl.py
uv run crawl.py --meta
uv run build_site.py
git add index.html && git commit -m "Refresh catalog" && git push
```
