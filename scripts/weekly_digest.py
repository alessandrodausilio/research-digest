#!/usr/bin/env python3
"""
weekly_digest.py
Reads all rating JSON files from the past week, collects articles rated >= threshold,
generates a Markdown digest, and commits it to GitHub Pages (docs/).

Run every Monday at 09:00 CET via GitHub Actions.
"""

import os
import json
import base64
import datetime
import requests
from pathlib import Path

ROOT   = Path(__file__).parent.parent
CONFIG = json.loads((ROOT / "config" / "config.json").read_text())

GITHUB_USERNAME  = CONFIG["github"]["username"]
GITHUB_REPO      = CONFIG["github"]["repo"]
RATING_THRESHOLD = CONFIG["settings"]["rating_threshold"]
GITHUB_TOKEN     = os.environ["GITHUB_TOKEN"]

TODAY     = datetime.date.today()
WEEK_AGO  = TODAY - datetime.timedelta(days=7)
WEEK_LABEL = f"{WEEK_AGO.strftime('%b %d')}–{TODAY.strftime('%b %d, %Y')}"


def github_get(file_path):
    repo_full = f"{GITHUB_USERNAME}/{GITHUB_REPO}"
    url = f"https://api.github.com/repos/{repo_full}/contents/{file_path}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    r = requests.get(url, headers=headers)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list):
        return data  # directory listing
    content = base64.b64decode(data["content"]).decode("utf-8")
    return content


def github_commit(file_path, content_str, commit_message):
    repo_full = f"{GITHUB_USERNAME}/{GITHUB_REPO}"
    api_url = f"https://api.github.com/repos/{repo_full}/contents/{file_path}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    content_b64 = base64.b64encode(content_str.encode("utf-8")).decode("ascii")
    sha = None
    r = requests.get(api_url, headers=headers)
    if r.status_code == 200:
        sha = r.json()["sha"]
    body = {"message": commit_message, "content": content_b64, "branch": "main"}
    if sha:
        body["sha"] = sha
    r = requests.put(api_url, headers=headers, json=body)
    r.raise_for_status()
    print(f"[GitHub] Committed: {file_path}")


def collect_week_ratings():
    """Collect all rated articles from the past 7 days."""
    selected = []
    for i in range(7):
        day = WEEK_AGO + datetime.timedelta(days=i)
        file_path = f"data/ratings/{day.isoformat()}.json"
        content = github_get(file_path)
        if not content:
            print(f"[Skip] No ratings for {day.isoformat()}")
            continue
        articles = json.loads(content)
        day_selected = [a for a in articles
                        if a.get("rating") and a["rating"] >= RATING_THRESHOLD]
        print(f"[{day.isoformat()}] {len(day_selected)}/{len(articles)} selected")
        selected.extend(day_selected)
    return selected


def format_source_badge(source):
    if "bioRxiv" in source:
        return " 🔬 *preprint*"
    return ""


def generate_digest_markdown(articles):
    if not articles:
        return f"""# Weekly Research Digest – {WEEK_LABEL}

*No articles met the rating threshold (≥{RATING_THRESHOLD}/5) this week.*

---
*Generated automatically by Research Digest · [View on GitHub]({
    "https://github.com/" + GITHUB_USERNAME + "/" + GITHUB_REPO})*
"""

    # Sort by rating desc, then score desc
    articles_sorted = sorted(articles, key=lambda x: (x.get("rating", 0), x.get("score", 0)), reverse=True)

    lines = [
        f"# Weekly Research Digest",
        f"## {WEEK_LABEL}",
        f"",
        f"**{len(articles_sorted)} articles** selected this week (rating ≥ {RATING_THRESHOLD}/5)",
        f"",
        f"---",
        f""
    ]

    for i, a in enumerate(articles_sorted, 1):
        rating_stars = "★" * a.get("rating", 0) + "☆" * (5 - a.get("rating", 0))
        authors_str = ", ".join(a.get("authors", [])[:3])
        if len(a.get("authors", [])) > 3:
            authors_str += " et al."
        badge = format_source_badge(a.get("source", ""))
        abstract = a.get("abstract", "")
        if len(abstract) > 400:
            abstract = abstract[:400] + "…"

        lines += [
            f"### {i}. [{a['title']}]({a['url']}){badge}",
            f"",
            f"**{authors_str}** · *{a['journal']}* {a.get('year', '')} · Rating: {rating_stars}",
            f"",
            f"> {abstract}",
            f"",
            f"🔗 [Read full article]({a['url']})",
            f"",
            f"---",
            f""
        ]

    lines += [
        f"*Generated automatically by Research Digest · "
        f"[View on GitHub](https://github.com/{GITHUB_USERNAME}/{GITHUB_REPO})*"
    ]

    return "\n".join(lines)


def generate_index_html(digests):
    """Generate/update the docs/index.html listing all weekly digests."""
    items = "".join(
        f'<li><a href="digest/{d}">{d.replace(".md", "").replace("-", " ")}</a></li>'
        for d in sorted(digests, reverse=True)[:20]
    )
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Research Digest – {GITHUB_USERNAME}</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 700px; margin: 48px auto; padding: 0 16px; }}
  h1 {{ font-size: 28px; color: #2c5f8a; }}
  ul {{ line-height: 2; }}
  a {{ color: #2c5f8a; }}
</style>
</head>
<body>
<h1>📚 Research Digest</h1>
<p>Weekly neuroscience literature digest – curated by Alessandro d'Ausilio.</p>
<h2>Weekly editions</h2>
<ul>{items}</ul>
</body>
</html>"""
    return html


def main():
    print(f"=== Weekly Digest – {WEEK_LABEL} ===")

    # 1. Collect rated articles
    articles = collect_week_ratings()
    print(f"[Total] {len(articles)} articles selected for digest")

    # 2. Generate markdown
    digest_md = generate_digest_markdown(articles)
    digest_filename = f"digest-{TODAY.strftime('%Y-%m-%d')}.md"
    digest_path = f"docs/digest/{digest_filename}"

    # 3. Commit digest
    github_commit(digest_path, digest_md, f"digest: week of {WEEK_LABEL}")

    # 4. Update index
    # List existing digests
    existing = github_get("docs/digest") or []
    digest_files = [f["name"] for f in existing if isinstance(existing, list)
                    if f["name"].endswith(".md")] if isinstance(existing, list) else []
    if digest_filename not in digest_files:
        digest_files.append(digest_filename)
    index_html = generate_index_html(digest_files)
    github_commit("docs/index.html", index_html, f"index: add {digest_filename}")

    print(f"=== Done – digest published at docs/digest/{digest_filename} ===")


if __name__ == "__main__":
    main()
