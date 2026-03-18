#!/usr/bin/env python3
"""
daily_search.py
Searches PubMed and bioRxiv, scores articles, generates a GitHub Pages rating page,
commits it to the repo, and sends an email with the rating link.

Required GitHub Actions secrets:
  GMAIL_APP_PASSWORD   - Gmail App Password
  GMAIL_USER           - your Gmail address
  GH_PAT               - Personal Access Token with contents:write on this repo
"""

import os
import json
import base64
import smtplib
import datetime
import requests
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from xml.etree import ElementTree as ET

# ── Config ───────────────────────────────────────────────────────────────────
ROOT   = Path(__file__).parent.parent
CONFIG = json.loads((ROOT / "config" / "config.json").read_text())

KEYWORDS          = CONFIG["keywords"]
PRIORITY_JOURNALS = [j.lower() for j in CONFIG["priority_journals"]]
EMAIL_TO          = CONFIG["email"]
GITHUB_USERNAME   = CONFIG["github"]["username"]
GITHUB_REPO       = CONFIG["github"]["repo"]
PAGES_URL         = CONFIG["github"]["pages_url"]
SEARCH_DAYS       = CONFIG["settings"]["search_days"]
N_CANDIDATES      = CONFIG["settings"]["daily_candidates"]
RATING_THRESHOLD  = CONFIG["settings"]["rating_threshold"]

TODAY     = datetime.date.today().isoformat()
DATE_FROM = (datetime.date.today() - datetime.timedelta(days=SEARCH_DAYS)).isoformat()

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GH_PAT       = os.environ.get("GH_PAT", "")
GMAIL_USER   = os.environ.get("GMAIL_USER", EMAIL_TO)
GMAIL_PASS   = os.environ["GMAIL_APP_PASSWORD"]


# ── PubMed ───────────────────────────────────────────────────────────────────
def build_pubmed_query():
    terms = " OR ".join('"' + kw + '"[tiab]' for kw in KEYWORDS)
    return "(" + terms + ")"


def search_pubmed(max_results=60):
    query     = build_pubmed_query()
    date_str  = DATE_FROM.replace("-", "/")
    today_str = TODAY.replace("-", "/")
    params    = {
        "db": "pubmed", "term": query, "retmax": max_results,
        "mindate": date_str, "maxdate": today_str,
        "datetype": "edat", "retmode": "json",
    }
    r = requests.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                     params=params, timeout=30)
    r.raise_for_status()
    pmids = r.json()["esearchresult"].get("idlist", [])
    print("[PubMed] Found", len(pmids), "articles")
    return pmids


def fetch_pubmed_details(pmids):
    if not pmids:
        return []
    r = requests.get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
        params={"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"},
        timeout=60,
    )
    r.raise_for_status()
    root     = ET.fromstring(r.text)
    articles = []
    for article in root.findall(".//PubmedArticle"):
        try:
            title    = article.findtext(".//ArticleTitle", "").strip()
            abstract = " ".join(t.text or "" for t in article.findall(".//AbstractText")).strip()
            journal  = article.findtext(".//Journal/Title", "")
            year     = article.findtext(".//PubDate/Year", "")
            authors  = []
            for a in article.findall(".//Author")[:3]:
                ln = a.findtext("LastName", "")
                fn = a.findtext("ForeName", "")
                if ln:
                    authors.append(ln + (" " + fn[0] + "." if fn else ""))
            doi = ""
            for eid in article.findall(".//ELocationID"):
                if eid.get("EIdType") == "doi":
                    doi = eid.text or ""
                    break
            if not doi:
                continue
            articles.append({
                "title": title, "abstract": abstract, "journal": journal,
                "year": year, "authors": authors, "doi": doi,
                "source": "PubMed", "url": "https://doi.org/" + doi,
            })
        except Exception as e:
            print("[PubMed] parse error:", e)
    return articles


# ── bioRxiv ──────────────────────────────────────────────────────────────────
def search_biorxiv(max_results=60):
    url = "https://api.biorxiv.org/details/biorxiv/" + DATE_FROM + "/" + TODAY + "/0/json"
    r   = requests.get(url, timeout=30)
    r.raise_for_status()
    collection = r.json().get("collection", [])
    kw_lower   = [k.lower() for k in KEYWORDS]
    articles   = []
    for item in collection:
        title    = item.get("title", "")
        abstract = item.get("abstract", "")
        text     = (title + " " + abstract).lower()
        matched  = [kw for kw in kw_lower if kw in text]
        if not matched:
            continue
        doi = item.get("doi", "")
        articles.append({
            "title": title, "abstract": abstract,
            "journal": "bioRxiv", "year": item.get("date", "")[:4],
            "authors": [a.strip() for a in item.get("authors", "").split(";")][:3],
            "doi": doi, "source": "bioRxiv preprint",
            "url": "https://doi.org/" + doi,
            "matched_keywords": matched,
        })
        if len(articles) >= max_results:
            break
    print("[bioRxiv] Found", len(articles), "matching articles")
    return articles


# ── Scoring ──────────────────────────────────────────────────────────────────
def load_preferences():
    p = ROOT / "data" / "preferences.json"
    if p.exists():
        return json.loads(p.read_text())
    return {"journal_weights": {}, "keyword_weights": {}, "author_weights": {}}


def score_article(article, prefs):
    title_l   = article["title"].lower()
    abstr_l   = article["abstract"].lower()
    journal_l = article["journal"].lower()
    kw_lower  = [k.lower() for k in KEYWORDS]

    kw_score = 0
    matched  = []
    for kw in kw_lower:
        if kw in title_l:
            kw_score += 4
            matched.append(kw)
        elif kw in abstr_l:
            kw_score += 1
            if kw not in matched:
                matched.append(kw)
    kw_score = min(kw_score, 30)

    journal_score = 0
    for pj in PRIORITY_JOURNALS:
        if pj in journal_l or journal_l in pj:
            journal_score = 25
            break

    pref_j     = prefs["journal_weights"].get(article["journal"], 1.0)
    pref_k     = max((prefs["keyword_weights"].get(kw, 1.0) for kw in matched), default=1.0)
    pref_score = min((kw_score / 30) * 30 * pref_j * pref_k, 30)
    recency    = 15 if article["source"] == "bioRxiv preprint" else 10

    article["score"]            = round(min(kw_score + journal_score + pref_score + recency, 100))
    article["matched_keywords"] = matched[:5]
    return article


def rank_articles(articles):
    prefs  = load_preferences()
    scored = [score_article(a, prefs) for a in articles]
    seen   = set()
    unique = []
    for a in scored:
        if a["doi"] and a["doi"] not in seen:
            seen.add(a["doi"])
            unique.append(a)
    return sorted(unique, key=lambda x: x["score"], reverse=True)


# ── HTML rating page ──────────────────────────────────────────────────────────
def generate_rating_html(articles):
    """
    Build the rating page using plain string concatenation only.
    Python variables are injected once as JSON — the JS body is never
    inside a Python f-string, so curly braces are never at risk.
    """
    repo_full   = GITHUB_USERNAME + "/" + GITHUB_REPO
    cfg_json    = json.dumps({
        "TODAY":     TODAY,
        "REPO":      repo_full,
        "GH_PAT":    GH_PAT,
        "THRESHOLD": RATING_THRESHOLD,
        "TOTAL":     len(articles),
    })
    articles_json = json.dumps(articles, ensure_ascii=False).replace("</", "<\\/")

    css = (
        "* { box-sizing: border-box; margin: 0; padding: 0; }\n"
        ":root {\n"
        "  --bg:#fff; --fg:#1a1a1a; --muted:#666; --border:#e0e0e0;\n"
        "  --accent:#2c5f8a; --son:#f0a500; --soff:#ccc; --hover:#f5f5f5;\n"
        "}\n"
        "@media (prefers-color-scheme: dark) {\n"
        "  :root { --bg:#1a1a1a; --fg:#e8e8e8; --muted:#999; --border:#333;\n"
        "          --accent:#5a9fd4; --son:#f0c040; --soff:#444; --hover:#2a2a2a; }\n"
        "}\n"
        "body { font-family:system-ui,sans-serif; background:var(--bg); color:var(--fg);\n"
        "       max-width:800px; margin:0 auto; padding:24px 16px; }\n"
        "h1   { font-size:22px; font-weight:600; color:var(--accent); margin-bottom:4px; }\n"
        ".sub { font-size:14px; color:var(--muted); margin-bottom:24px; }\n"
        ".card { border:1px solid var(--border); border-radius:10px;\n"
        "        padding:18px; margin-bottom:16px; }\n"
        ".card.ok { border-color:#4caf50; background:#f0f7ed; }\n"
        "@media (prefers-color-scheme:dark) { .card.ok { background:#1a2d1a; } }\n"
        ".num  { font-size:12px; color:var(--muted); margin-bottom:4px; }\n"
        ".ttl  { font-size:16px; font-weight:600; line-height:1.4; margin-bottom:6px; }\n"
        ".ttl a { color:var(--accent); text-decoration:none; }\n"
        ".ttl a:hover { text-decoration:underline; }\n"
        ".meta { font-size:13px; color:var(--muted); margin-bottom:10px; }\n"
        ".kws  { font-size:12px; color:var(--muted); margin-bottom:12px; }\n"
        ".kw   { background:var(--hover); border:1px solid var(--border);\n"
        "        border-radius:4px; padding:2px 7px; margin-right:5px;\n"
        "        display:inline-block; margin-bottom:4px; }\n"
        ".abst { font-size:14px; line-height:1.6; margin-bottom:14px; }\n"
        ".bdg  { display:inline-block; font-size:11px; padding:2px 7px;\n"
        "        border-radius:4px; margin-left:8px;\n"
        "        background:#fff3cd; color:#856404; border:1px solid #ffc107; }\n"
        ".bdg.pm { background:#d1e7ff; color:#084298; border-color:#9ec5fe; }\n"
        ".stars { display:flex; gap:6px; align-items:center; }\n"
        ".star  { font-size:28px; cursor:pointer; line-height:1;\n"
        "         color:var(--soff); transition:color 0.1s; user-select:none; }\n"
        ".star.on { color:var(--son); }\n"
        ".rlbl { font-size:13px; color:var(--muted); margin-left:8px; }\n"
        ".foot { margin-top:32px; text-align:center; }\n"
        "#btn  { background:var(--accent); color:#fff; border:none;\n"
        "        padding:14px 40px; border-radius:8px;\n"
        "        font-size:16px; font-weight:600; cursor:pointer; }\n"
        "#btn:disabled { opacity:0.5; cursor:not-allowed; }\n"
        "#status { margin-top:14px; font-size:14px; color:var(--muted); min-height:20px; }\n"
    )

    # JavaScript — written as a plain Python string, never an f-string.
    # Python values arrive via the CFG and ARTICLES variables injected below.
    js = (
        "var CFG = " + cfg_json + ";\n"
        "var ARTICLES = " + articles_json + ";\n"
        "var ratings = {};\n"
        "var LABELS = ['','Not relevant','Slightly interesting','Interesting','Very interesting','Must read'];\n"
        "\n"
        "function esc(s) {\n"
        "  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;')\n"
        "    .replace(/>/g,'&gt;').replace(/\"/g,'&quot;');\n"
        "}\n"
        "\n"
        "function render() {\n"
        "  var out = '';\n"
        "  for (var i = 0; i < ARTICLES.length; i++) {\n"
        "    var a = ARTICLES[i];\n"
        "    var auth = a.authors.slice(0,3).join(', ') + (a.authors.length > 3 ? ' et al.' : '');\n"
        "    var badge = a.source === 'PubMed'\n"
        "      ? '<span class=\"bdg pm\">PubMed</span>'\n"
        "      : '<span class=\"bdg\">bioRxiv preprint</span>';\n"
        "    var kwHtml = '';\n"
        "    if (a.matched_keywords && a.matched_keywords.length) {\n"
        "      kwHtml = '<div class=\"kws\">';\n"
        "      for (var k = 0; k < a.matched_keywords.length; k++)\n"
        "        kwHtml += '<span class=\"kw\">' + esc(a.matched_keywords[k]) + '</span>';\n"
        "      kwHtml += '</div>';\n"
        "    }\n"
        "    var abstr = a.abstract.length > 400 ? a.abstract.slice(0,400) + '...' : a.abstract;\n"
        "    var starsHtml = '<div class=\"stars\" id=\"stars-' + i + '\">';\n"
        "    for (var s = 1; s <= 5; s++)\n"
        "      starsHtml += '<span class=\"star\" onclick=\"rate(' + i + ',' + s + ')\">&#9733;</span>';\n"
        "    starsHtml += '<span class=\"rlbl\" id=\"lbl-' + i + '\"></span></div>';\n"
        "    out += '<div class=\"card\" id=\"card-' + i + '\">'\n"
        "         + '<div class=\"num\">Article ' + (i+1) + ' of ' + ARTICLES.length\n"
        "         + ' &middot; Score: ' + a.score + '/100</div>'\n"
        "         + '<div class=\"ttl\"><a href=\"' + esc(a.url) + '\" target=\"_blank\">'\n"
        "         + esc(a.title) + '</a>' + badge + '</div>'\n"
        "         + '<div class=\"meta\">' + esc(auth)\n"
        "         + ' &middot; <em>' + esc(a.journal) + '</em> ' + esc(a.year) + '</div>'\n"
        "         + kwHtml\n"
        "         + '<div class=\"abst\">' + esc(abstr) + '</div>'\n"
        "         + starsHtml + '</div>';\n"
        "  }\n"
        "  document.getElementById('articles').innerHTML = out;\n"
        "}\n"
        "\n"
        "function rate(i, v) {\n"
        "  ratings[i] = v;\n"
        "  var stars = document.querySelectorAll('#stars-' + i + ' .star');\n"
        "  for (var s = 0; s < stars.length; s++)\n"
        "    stars[s].classList.toggle('on', s < v);\n"
        "  document.getElementById('lbl-' + i).textContent = LABELS[v];\n"
        "  document.getElementById('card-' + i).classList.toggle('ok', v >= CFG.THRESHOLD);\n"
        "  var n = Object.keys(ratings).length;\n"
        "  document.getElementById('prog').textContent = n + ' of ' + ARTICLES.length + ' rated';\n"
        "}\n"
        "\n"
        "function submitRatings() {\n"
        "  var btn = document.getElementById('btn');\n"
        "  var st  = document.getElementById('status');\n"
        "  btn.disabled = true;\n"
        "  st.textContent = 'Saving...';\n"
        "  var payload = [];\n"
        "  for (var i = 0; i < ARTICLES.length; i++) {\n"
        "    var a = ARTICLES[i];\n"
        "    payload.push({ doi:a.doi, title:a.title, journal:a.journal,\n"
        "      year:a.year, authors:a.authors, url:a.url, source:a.source,\n"
        "      score:a.score, rating:ratings[i]||null,\n"
        "      in_digest:(ratings[i]||0) >= CFG.THRESHOLD });\n"
        "  }\n"
        "  var content  = btoa(unescape(encodeURIComponent(JSON.stringify(payload,null,2))));\n"
        "  var filePath = 'data/ratings/' + CFG.TODAY + '.json';\n"
        "  var apiUrl   = 'https://api.github.com/repos/' + CFG.REPO + '/contents/' + filePath;\n"
        "  var hdrs     = { 'Authorization':'token ' + CFG.GH_PAT,\n"
        "                   'Accept':'application/vnd.github.v3+json',\n"
        "                   'Content-Type':'application/json' };\n"
        "  fetch(apiUrl, { headers: hdrs })\n"
        "    .then(function(r) { return r.ok ? r.json() : null; })\n"
        "    .then(function(ex) {\n"
        "      var body = { message:'ratings: ' + CFG.TODAY, content:content, branch:'main' };\n"
        "      if (ex && ex.sha) body.sha = ex.sha;\n"
        "      return fetch(apiUrl, { method:'PUT', headers:hdrs, body:JSON.stringify(body) });\n"
        "    })\n"
        "    .then(function(r) {\n"
        "      if (r.ok) {\n"
        "        st.textContent = 'Saved! Articles rated >= ' + CFG.THRESHOLD + ' go into the Monday digest.';\n"
        "        btn.textContent = 'Saved';\n"
        "      } else { return r.json().then(function(e) { throw new Error(e.message); }); }\n"
        "    })\n"
        "    .catch(function(e) { st.textContent = 'Error: ' + e.message; btn.disabled = false; });\n"
        "}\n"
        "\n"
        "render();\n"
    )

    html = (
        "<!DOCTYPE html>\n"
        "<html lang='en'>\n"
        "<head>\n"
        "<meta charset='UTF-8'>\n"
        "<meta name='viewport' content='width=device-width, initial-scale=1.0'>\n"
        "<title>Research Digest - " + TODAY + "</title>\n"
        "<style>" + css + "</style>\n"
        "</head>\n"
        "<body>\n"
        "<h1>&#128218; Research Digest &#8211; Rate Articles</h1>\n"
        "<div class='sub'>" + TODAY + " &nbsp;|&nbsp; <span id='prog'>0 rated</span></div>\n"
        "<div id='articles'></div>\n"
        "<div class='foot'>\n"
        "  <button id='btn' onclick='submitRatings()'>Submit ratings &amp; save</button>\n"
        "  <div id='status'></div>\n"
        "</div>\n"
        "<script>\n" + js + "\n</script>\n"
        "</body>\n"
        "</html>\n"
    )
    return html


# ── GitHub commit ─────────────────────────────────────────────────────────────
def github_commit(file_path, content_str, commit_message):
    repo_full   = GITHUB_USERNAME + "/" + GITHUB_REPO
    api_url     = "https://api.github.com/repos/" + repo_full + "/contents/" + file_path
    headers     = {
        "Authorization": "token " + GITHUB_TOKEN,
        "Accept": "application/vnd.github.v3+json",
    }
    content_b64 = base64.b64encode(content_str.encode("utf-8")).decode("ascii")
    sha = None
    r   = requests.get(api_url, headers=headers)
    if r.status_code == 200:
        sha = r.json()["sha"]
    body = {"message": commit_message, "content": content_b64, "branch": "main"}
    if sha:
        body["sha"] = sha
    r = requests.put(api_url, headers=headers, json=body)
    r.raise_for_status()
    print("[GitHub] Committed:", file_path)


# ── Email ─────────────────────────────────────────────────────────────────────
def send_email(rating_url, n_articles, top_titles):
    items = "".join("<li>" + t + "</li>" for t in top_titles)
    body  = (
        "<div style='font-family:system-ui,sans-serif;max-width:600px;margin:0 auto;color:#333'>"
        "<h2 style='color:#2c5f8a;border-bottom:2px solid #2c5f8a;padding-bottom:8px'>"
        "&#128218; Research Digest &#8211; " + TODAY + "</h2>"
        "<p>" + str(n_articles) + " new articles found today.</p>"
        "<ul style='font-size:14px;line-height:1.8'>" + items + "</ul>"
        "<div style='margin:28px 0;text-align:center'>"
        "<a href='" + rating_url + "' style='background:#2c5f8a;color:#fff;"
        "padding:14px 32px;border-radius:8px;text-decoration:none;"
        "font-size:16px;font-weight:600;display:inline-block'>"
        "&#11088; Rate today's articles</a></div>"
        "<p style='font-size:12px;color:#aaa;text-align:center'>"
        "Articles rated >= " + str(RATING_THRESHOLD) + " appear in Monday digest.</p>"
        "</div>"
    )
    msg            = MIMEMultipart("alternative")
    msg["Subject"] = "Research Digest " + TODAY + " (" + str(n_articles) + " articles)"
    msg["From"]    = GMAIL_USER
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(body, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_USER, GMAIL_PASS)
        smtp.sendmail(GMAIL_USER, EMAIL_TO, msg.as_string())
    print("[Email] Sent to", EMAIL_TO)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=== Daily Search ===", TODAY)

    pmids   = search_pubmed(max_results=60)
    time.sleep(0.4)
    pm_arts = fetch_pubmed_details(pmids)
    bx_arts = search_biorxiv(max_results=60)

    all_arts = pm_arts + bx_arts
    print("[Total] before ranking:", len(all_arts))

    ranked = rank_articles(all_arts)[:N_CANDIDATES]
    print("[Ranked] top candidates:", len(ranked))

    if not ranked:
        print("[Warning] No articles found.")
        return

    github_commit(
        "data/articles/" + TODAY + ".json",
        json.dumps(ranked, indent=2, ensure_ascii=False),
        "articles: " + TODAY,
    )

    github_commit(
        "docs/rate/" + TODAY + ".html",
        generate_rating_html(ranked),
        "rating page: " + TODAY,
    )

    rating_url = PAGES_URL + "/rate/" + TODAY + ".html"
    top_titles = [a["title"][:80] + ("..." if len(a["title"]) > 80 else "") for a in ranked[:5]]
    send_email(rating_url, len(ranked), top_titles)

    print("=== Done ===")


if __name__ == "__main__":
    main()
