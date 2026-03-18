#!/usr/bin/env python3
"""
daily_search.py
Searches PubMed and bioRxiv, scores articles, generates a GitHub Pages rating page,
commits it to the repo, and sends an email with the rating link.

Required environment variables (set as GitHub Actions secrets):
  GMAIL_APP_PASSWORD   - Gmail App Password (not your regular password)
  GITHUB_TOKEN         - Automatically provided by GitHub Actions (repo write access)
  GITHUB_REPOSITORY    - Automatically provided by GitHub Actions (owner/repo)
  GH_PAT               - Personal Access Token with 'contents: write' scope
                         (needed to write ratings from the browser-side rating page)
"""

import os
import json
import base64
import smtplib
import datetime
import requests
import re
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from xml.etree import ElementTree as ET

# ── Config ──────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
CONFIG = json.loads((ROOT / "config" / "config.json").read_text())

KEYWORDS         = CONFIG["keywords"]
PRIORITY_JOURNALS = [j.lower() for j in CONFIG["priority_journals"]]
EMAIL_TO         = CONFIG["email"]
GITHUB_USERNAME  = CONFIG["github"]["username"]
GITHUB_REPO      = CONFIG["github"]["repo"]
PAGES_URL        = CONFIG["github"]["pages_url"]
SEARCH_DAYS      = CONFIG["settings"]["search_days"]
N_CANDIDATES     = CONFIG["settings"]["daily_candidates"]
RATING_THRESHOLD = CONFIG["settings"]["rating_threshold"]

TODAY = datetime.date.today().isoformat()          # e.g. "2026-03-18"
DATE_FROM = (datetime.date.today() - datetime.timedelta(days=SEARCH_DAYS)).isoformat()

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GH_PAT       = os.environ.get("GH_PAT", "")        # for browser-side writes
GMAIL_USER   = os.environ.get("GMAIL_USER", EMAIL_TO)
GMAIL_PASS   = os.environ["GMAIL_APP_PASSWORD"]


# ── PubMed search ────────────────────────────────────────────────────────────
def build_pubmed_query():
    terms = " OR ".join(f'"{kw}"[tiab]' for kw in KEYWORDS)
    return f"({terms})"


def search_pubmed(max_results=60):
    query = build_pubmed_query()
    date_str = DATE_FROM.replace("-", "/")
    today_str = TODAY.replace("-", "/")
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    params = {
        "db": "pubmed", "term": query, "retmax": max_results,
        "mindate": date_str, "maxdate": today_str,
        "datetype": "edat", "retmode": "json",
        "usehistory": "y"
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    pmids = data["esearchresult"].get("idlist", [])
    print(f"[PubMed] Found {len(pmids)} articles")
    return pmids


def fetch_pubmed_details(pmids):
    if not pmids:
        return []
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
    params = {"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"}
    r = requests.get(url, params=params, timeout=60)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    articles = []
    for article in root.findall(".//PubmedArticle"):
        try:
            title = article.findtext(".//ArticleTitle", "").strip()
            abstract = " ".join(
                t.text or "" for t in article.findall(".//AbstractText")
            ).strip()
            journal = article.findtext(".//Journal/Title", "")
            year = article.findtext(".//PubDate/Year", "")
            month = article.findtext(".//PubDate/Month", "")
            # Authors
            authors = []
            for author in article.findall(".//Author")[:3]:
                ln = author.findtext("LastName", "")
                fn = author.findtext("ForeName", "")
                if ln:
                    authors.append(f"{ln} {fn[0]}." if fn else ln)
            # DOI
            doi = ""
            for eid in article.findall(".//ELocationID"):
                if eid.get("EIdType") == "doi":
                    doi = eid.text or ""
                    break
            if not doi:
                continue  # skip articles without DOI
            articles.append({
                "title": title, "abstract": abstract, "journal": journal,
                "year": year, "month": month, "authors": authors,
                "doi": doi, "source": "PubMed",
                "url": f"https://doi.org/{doi}"
            })
        except Exception as e:
            print(f"[PubMed] Error parsing article: {e}")
    return articles


# ── bioRxiv search ───────────────────────────────────────────────────────────
def search_biorxiv(max_results=60):
    url = f"https://api.biorxiv.org/details/biorxiv/{DATE_FROM}/{TODAY}/0/json"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()
    collection = data.get("collection", [])
    articles = []
    # Filter by keyword match in title or abstract
    kw_lower = [k.lower() for k in KEYWORDS]
    for item in collection[:max_results * 3]:  # overfetch then filter
        title = item.get("title", "")
        abstract = item.get("abstract", "")
        text = (title + " " + abstract).lower()
        matched = [kw for kw in kw_lower if kw in text]
        if not matched:
            continue
        authors_raw = item.get("authors", "")
        author_list = [a.strip() for a in authors_raw.split(";")][:3]
        articles.append({
            "title": title, "abstract": abstract,
            "journal": "bioRxiv", "year": item.get("date", "")[:4],
            "month": item.get("date", "")[5:7],
            "authors": author_list,
            "doi": item.get("doi", ""), "source": "bioRxiv preprint",
            "url": f"https://doi.org/{item.get('doi', '')}",
            "matched_keywords": matched
        })
        if len(articles) >= max_results:
            break
    print(f"[bioRxiv] Found {len(articles)} matching articles")
    return articles


# ── Scoring ──────────────────────────────────────────────────────────────────
def load_preferences():
    pref_path = ROOT / "data" / "preferences.json"
    if pref_path.exists():
        return json.loads(pref_path.read_text())
    return {"journal_weights": {}, "keyword_weights": {}, "author_weights": {}}


def score_article(article, prefs):
    title_lower    = article["title"].lower()
    abstract_lower = article["abstract"].lower()
    journal_lower  = article["journal"].lower()
    kw_lower       = [k.lower() for k in KEYWORDS]

    # 1. Keyword relevance (30 pts)
    kw_score = 0
    matched_kws = []
    for kw in kw_lower:
        if kw in title_lower:
            kw_score += 4
            matched_kws.append(kw)
        elif kw in abstract_lower:
            kw_score += 1
            if kw not in matched_kws:
                matched_kws.append(kw)
    kw_score = min(kw_score, 30)

    # 2. Priority journal (25 pts)
    journal_score = 0
    for pj in PRIORITY_JOURNALS:
        if pj in journal_lower or journal_lower in pj:
            journal_score = 25
            break

    # 3. Learned preferences (30 pts)
    pref_mult_j = prefs["journal_weights"].get(article["journal"], 1.0)
    pref_mult_k = max(
        (prefs["keyword_weights"].get(kw, 1.0) for kw in matched_kws),
        default=1.0
    )
    pref_score = min((kw_score / 30) * 30 * pref_mult_j * pref_mult_k, 30)

    # 4. Recency (15 pts) — bioRxiv today = 15, PubMed older = less
    recency_score = 15 if article["source"] == "bioRxiv preprint" else 10

    total = kw_score + journal_score + pref_score + recency_score
    article["score"]    = round(min(total, 100))
    article["matched_keywords"] = matched_kws[:5]
    return article


def rank_articles(articles):
    prefs = load_preferences()
    scored = [score_article(a, prefs) for a in articles]
    # Deduplicate by DOI
    seen_dois = set()
    unique = []
    for a in scored:
        if a["doi"] and a["doi"] not in seen_dois:
            seen_dois.add(a["doi"])
            unique.append(a)
    return sorted(unique, key=lambda x: x["score"], reverse=True)


# ── Generate HTML rating page ─────────────────────────────────────────────────
def generate_rating_html(articles):
    articles_json = json.dumps(articles, ensure_ascii=False)
    repo_full = f"{GITHUB_USERNAME}/{GITHUB_REPO}"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Research Digest – Rate Articles – {TODAY}</title>
<style>
  :root {{ --bg:#fff; --fg:#1a1a1a; --muted:#666; --border:#e0e0e0;
           --accent:#2c5f8a; --star:#f0a500; --hover:#f5f5f5; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#1a1a1a; --fg:#e8e8e8; --muted:#999; --border:#333;
             --accent:#5a9fd4; --star:#f0c040; --hover:#2a2a2a; }} }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: system-ui, -apple-system, sans-serif; background: var(--bg);
          color: var(--fg); max-width: 800px; margin: 0 auto; padding: 24px 16px; }}
  h1 {{ font-size: 22px; font-weight: 600; margin-bottom: 4px; color: var(--accent); }}
  .subtitle {{ font-size: 14px; color: var(--muted); margin-bottom: 24px; }}
  .article {{ border: 1px solid var(--border); border-radius: 10px;
              padding: 18px; margin-bottom: 16px; background: var(--bg); }}
  .article-num {{ font-size: 12px; color: var(--muted); margin-bottom: 4px; }}
  .article-title {{ font-size: 16px; font-weight: 600; margin-bottom: 6px; line-height: 1.4; }}
  .article-title a {{ color: var(--accent); text-decoration: none; }}
  .article-title a:hover {{ text-decoration: underline; }}
  .article-meta {{ font-size: 13px; color: var(--muted); margin-bottom: 10px; }}
  .article-abstract {{ font-size: 14px; line-height: 1.6; margin-bottom: 14px; }}
  .keywords {{ font-size: 12px; color: var(--muted); margin-bottom: 12px; }}
  .keywords span {{ background: var(--hover); border: 1px solid var(--border);
                    border-radius: 4px; padding: 2px 7px; margin-right: 6px;
                    display: inline-block; margin-bottom: 4px; }}
  .rating-row {{ display: flex; align-items: center; gap: 8px; }}
  .rating-label {{ font-size: 13px; color: var(--muted); min-width: 80px; }}
  .stars {{ display: flex; gap: 4px; }}
  .star {{ font-size: 24px; cursor: pointer; color: var(--border);
           transition: color 0.1s; user-select: none; line-height: 1; }}
  .star.active, .star:hover {{ color: var(--star); }}
  .star-group:hover .star {{ color: var(--star); }}
  .star-group .star:hover ~ .star {{ color: var(--border); }}
  .source-badge {{ display: inline-block; font-size: 11px; padding: 2px 8px;
                   border-radius: 4px; margin-left: 8px;
                   background: #fff3cd; color: #856404; border: 1px solid #ffc107; }}
  .source-badge.pubmed {{ background: #d1e7ff; color: #084298; border-color: #9ec5fe; }}
  .submit-section {{ margin-top: 32px; text-align: center; }}
  #submit-btn {{ background: var(--accent); color: #fff; border: none;
                 padding: 14px 40px; border-radius: 8px; font-size: 16px;
                 cursor: pointer; font-weight: 600; transition: opacity 0.2s; }}
  #submit-btn:hover {{ opacity: 0.85; }}
  #submit-btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
  #status {{ margin-top: 16px; font-size: 14px; color: var(--muted); min-height: 20px; }}
  .progress {{ font-size: 13px; color: var(--accent); font-weight: 600; }}
  .rated {{ background: #f0f7ed; border-color: #4caf50; }}
  @media (prefers-color-scheme: dark) {{
    .rated {{ background: #1a2d1a; border-color: #4caf50; }}
    .source-badge {{ background: #3a2f00; color: #f0c040; border-color: #8a6d00; }}
    .source-badge.pubmed {{ background: #001a3a; color: #7eb8ff; border-color: #2a5a8a; }}
  }}
</style>
</head>
<body>
<h1>📚 Research Digest – Rate Articles</h1>
<div class="subtitle">{TODAY} &nbsp;|&nbsp; <span id="progress-text">0 of {len(articles)} rated</span></div>

<div id="articles-container"></div>

<div class="submit-section">
  <button id="submit-btn" onclick="submitRatings()">Submit ratings & save</button>
  <div id="status"></div>
</div>

<script>
const ARTICLES = {articles_json};
const TODAY = "{TODAY}";
const REPO = "{repo_full}";
const GH_PAT = "{GH_PAT}";
const ratings = {{}};

function renderArticles() {{
  const container = document.getElementById('articles-container');
  container.innerHTML = ARTICLES.map((a, i) => `
    <div class="article" id="art-${{i}}">
      <div class="article-num">Article ${{i+1}} of ${{len(articles)}} · Score: ${{a.score}}/100</div>
      <div class="article-title">
        <a href="${{a.url}}" target="_blank">${{escHtml(a.title)}}</a>
        <span class="source-badge ${{a.source === 'PubMed' ? 'pubmed' : ''}}">${{a.source}}</span>
      </div>
      <div class="article-meta">
        ${{a.authors.slice(0,3).join(', ')}}${{a.authors.length > 3 ? ' et al.' : ''}}
        &nbsp;|&nbsp; <em>${{escHtml(a.journal)}}</em> ${{a.year}}
      </div>
      ${{a.matched_keywords.length ? `<div class="keywords">${{a.matched_keywords.map(k => `<span>${{k}}</span>`).join('')}}</div>` : ''}}
      <div class="article-abstract">${{escHtml(a.abstract.slice(0, 400))}}${{a.abstract.length > 400 ? '…' : ''}}</div>
      <div class="rating-row">
        <span class="rating-label">Rating:</span>
        <div class="stars star-group" id="stars-${{i}}">
          ${{[1,2,3,4,5].map(n => `<span class="star" data-i="${{i}}" data-v="${{n}}" onclick="rate(${{i}},${{n}})">★</span>`).join('')}}
        </div>
        <span id="label-${{i}}" style="font-size:13px;color:var(--muted);margin-left:8px;"></span>
      </div>
    </div>
  `).join('');
}}

function escHtml(s) {{
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}

const LABELS = ['','Not relevant','Slightly interesting','Interesting','Very interesting','Must read'];

function rate(i, v) {{
  ratings[i] = v;
  const stars = document.querySelectorAll(`#stars-${{i}} .star`);
  stars.forEach((s, idx) => s.classList.toggle('active', idx < v));
  document.getElementById(`label-${{i}}`).textContent = LABELS[v];
  document.getElementById(`art-${{i}}`).classList.toggle('rated', v >= {RATING_THRESHOLD});
  updateProgress();
}}

function updateProgress() {{
  const n = Object.keys(ratings).length;
  document.getElementById('progress-text').textContent = `${{n}} of ${{ARTICLES.length}} rated`;
}}

async function submitRatings() {{
  const btn = document.getElementById('submit-btn');
  const status = document.getElementById('status');
  btn.disabled = true;
  status.textContent = 'Saving ratings...';
  
  const payload = ARTICLES.map((a, i) => ({{
    doi: a.doi, title: a.title, journal: a.journal, year: a.year,
    authors: a.authors, url: a.url, source: a.source,
    score: a.score, rating: ratings[i] || null,
    in_digest: (ratings[i] || 0) >= {RATING_THRESHOLD}
  }}));
  
  const content = btoa(unescape(encodeURIComponent(JSON.stringify(payload, null, 2))));
  const filePath = `data/ratings/${{TODAY}}.json`;
  
  try {{
    // Check if file exists (to get sha for update)
    let sha = null;
    const checkResp = await fetch(
      `https://api.github.com/repos/${{REPO}}/contents/${{filePath}}`,
      {{ headers: {{ 'Authorization': `token ${{GH_PAT}}`, 'Accept': 'application/vnd.github.v3+json' }} }}
    );
    if (checkResp.ok) {{
      const existing = await checkResp.json();
      sha = existing.sha;
    }}
    
    const body = {{ message: `ratings: ${{TODAY}}`, content, branch: 'main' }};
    if (sha) body.sha = sha;
    
    const resp = await fetch(
      `https://api.github.com/repos/${{REPO}}/contents/${{filePath}}`,
      {{
        method: 'PUT',
        headers: {{
          'Authorization': `token ${{GH_PAT}}`,
          'Accept': 'application/vnd.github.v3+json',
          'Content-Type': 'application/json'
        }},
        body: JSON.stringify(body)
      }}
    );
    
    if (resp.ok) {{
      status.innerHTML = "✅ Ratings saved! Articles rated ≥{RATING_THRESHOLD} will appear in Monday's digest.";    }} else {{
      const err = await resp.json();
      status.textContent = `Error: ${{err.message}}`;
      btn.disabled = false;
    }}
  }} catch(e) {{
    status.textContent = `Error: ${{e.message}}`;
    btn.disabled = false;
  }}
}}

renderArticles();
</script>
</body>
</html>"""
    return html


# ── Commit file to GitHub via API ─────────────────────────────────────────────
def github_commit(file_path, content_str, commit_message):
    """Commit a file to the repo using the GitHub Contents API."""
    repo_full = f"{GITHUB_USERNAME}/{GITHUB_REPO}"
    api_url = f"https://api.github.com/repos/{repo_full}/contents/{file_path}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    content_b64 = base64.b64encode(content_str.encode("utf-8")).decode("ascii")
    # Check if file exists to get sha
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


# ── Send email ────────────────────────────────────────────────────────────────
def send_email(rating_url, n_articles, top_titles):
    titles_html = "".join(f"<li>{t}</li>" for t in top_titles[:5])
    html_body = f"""
    <div style="font-family:system-ui,sans-serif;max-width:600px;margin:0 auto;color:#333">
      <h2 style="color:#2c5f8a;border-bottom:2px solid #2c5f8a;padding-bottom:8px">
        📚 Research Digest – {TODAY}
      </h2>
      <p style="color:#666;font-size:14px">{n_articles} new articles found today on PubMed and bioRxiv.</p>
      <p style="font-size:15px">Top candidates today:</p>
      <ul style="font-size:14px;line-height:1.8">{titles_html}</ul>
      <div style="margin:28px 0;text-align:center">
        <a href="{rating_url}"
           style="background:#2c5f8a;color:#fff;padding:14px 32px;border-radius:8px;
                  text-decoration:none;font-size:16px;font-weight:600;display:inline-block">
          ⭐ Rate today's articles
        </a>
      </div>
      <p style="font-size:12px;color:#aaa;text-align:center">
        Articles rated ≥{RATING_THRESHOLD} will be included in Monday's GitHub digest.
      </p>
    </div>"""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"📚 Research Digest – {TODAY} ({n_articles} articles)"
    msg["From"]    = GMAIL_USER
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_USER, GMAIL_PASS)
        smtp.sendmail(GMAIL_USER, EMAIL_TO, msg.as_string())
    print(f"[Email] Sent to {EMAIL_TO}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"=== Daily Search – {TODAY} ===")

    # 1. Fetch articles
    pmids    = search_pubmed(max_results=60)
    time.sleep(0.4)  # NCBI rate limit
    pm_arts  = fetch_pubmed_details(pmids)
    bx_arts  = search_biorxiv(max_results=60)

    all_articles = pm_arts + bx_arts
    print(f"[Total] {len(all_articles)} articles before ranking")

    # 2. Rank
    ranked = rank_articles(all_articles)[:N_CANDIDATES]
    print(f"[Ranked] Top {len(ranked)} candidates selected")

    if not ranked:
        print("[Warning] No articles found today, skipping email.")
        return

    # 3. Generate rating page
    rating_html = generate_rating_html(ranked)
    rating_file = f"docs/rate/{TODAY}.html"
    github_commit(rating_file, rating_html, f"rating page: {TODAY}")

    # 4. Also save article list to data/
    articles_file = f"data/articles/{TODAY}.json"
    github_commit(articles_file, json.dumps(ranked, indent=2, ensure_ascii=False),
                  f"articles: {TODAY}")

    # 5. Send email
    rating_url  = f"{PAGES_URL}/rate/{TODAY}.html"
    top_titles  = [a["title"][:80] + ("…" if len(a["title"]) > 80 else "")
                   for a in ranked[:5]]
    send_email(rating_url, len(ranked), top_titles)

    print("=== Done ===")


if __name__ == "__main__":
    main()
