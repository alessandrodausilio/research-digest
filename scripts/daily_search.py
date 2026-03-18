#!/usr/bin/env python3
import os, json, base64, smtplib, datetime, requests, time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from xml.etree import ElementTree as ET

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


def build_pubmed_query():
    terms = " OR ".join('"' + kw + '"[tiab]' for kw in KEYWORDS)
    return "(" + terms + ")"

def search_pubmed(max_results=60):
    params = {
        "db": "pubmed", "term": build_pubmed_query(), "retmax": max_results,
        "mindate": DATE_FROM.replace("-", "/"), "maxdate": TODAY.replace("-", "/"),
        "datetype": "edat", "retmode": "json",
    }
    r = requests.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi", params=params, timeout=30)
    r.raise_for_status()
    pmids = r.json()["esearchresult"].get("idlist", [])
    print("[PubMed] Found", len(pmids))
    return pmids

def fetch_pubmed_details(pmids):
    if not pmids:
        return []
    r = requests.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                     params={"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"}, timeout=60)
    r.raise_for_status()
    articles = []
    for article in ET.fromstring(r.text).findall(".//PubmedArticle"):
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
            articles.append({"title": title, "abstract": abstract, "journal": journal,
                              "year": year, "authors": authors, "doi": doi,
                              "source": "PubMed", "url": "https://doi.org/" + doi,
                              "matched_keywords": []})
        except Exception as e:
            print("[PubMed] parse error:", e)
    return articles

def search_biorxiv(max_results=60):
    r = requests.get("https://api.biorxiv.org/details/biorxiv/" + DATE_FROM + "/" + TODAY + "/0/json", timeout=30)
    r.raise_for_status()
    kw_lower = [k.lower() for k in KEYWORDS]
    articles = []
    for item in r.json().get("collection", []):
        title    = item.get("title", "")
        abstract = item.get("abstract", "")
        matched  = [kw for kw in kw_lower if kw in (title + " " + abstract).lower()]
        if not matched:
            continue
        doi = item.get("doi", "")
        articles.append({"title": title, "abstract": abstract,
                         "journal": "bioRxiv", "year": item.get("date", "")[:4],
                         "authors": [a.strip() for a in item.get("authors", "").split(";")][:3],
                         "doi": doi, "source": "bioRxiv preprint",
                         "url": "https://doi.org/" + doi, "matched_keywords": matched})
        if len(articles) >= max_results:
            break
    print("[bioRxiv] Found", len(articles))
    return articles

def load_preferences():
    p = ROOT / "data" / "preferences.json"
    return json.loads(p.read_text()) if p.exists() else {"journal_weights": {}, "keyword_weights": {}}

def score_article(article, prefs):
    title_l   = article["title"].lower()
    abstr_l   = article["abstract"].lower()
    journal_l = article["journal"].lower()
    kw_lower  = [k.lower() for k in KEYWORDS]
    kw_score  = 0
    matched   = []
    for kw in kw_lower:
        if kw in title_l:
            kw_score += 4
            matched.append(kw)
        elif kw in abstr_l:
            kw_score += 1
            if kw not in matched:
                matched.append(kw)
    kw_score = min(kw_score, 30)
    journal_score = 25 if any(pj in journal_l or journal_l in pj for pj in PRIORITY_JOURNALS) else 0
    pref_j = prefs["journal_weights"].get(article["journal"], 1.0)
    pref_k = max((prefs["keyword_weights"].get(kw, 1.0) for kw in matched), default=1.0)
    pref_score = min((kw_score / 30) * 30 * pref_j * pref_k, 30)
    recency = 15 if article["source"] == "bioRxiv preprint" else 10
    article["score"] = round(min(kw_score + journal_score + pref_score + recency, 100))
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

def generate_rating_html(date, json_url, threshold, repo, pat):
    # HTML is built with plain string concatenation.
    # Article data is NOT embedded here — the page fetches it from json_url at runtime.
    # Only simple scalar values (date, repo, pat, threshold) are injected as JS strings.
    return (
        "<!DOCTYPE html>"
        "<html lang='en'>"
        "<head>"
        "<meta charset='UTF-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Research Digest " + date + "</title>"
        "<style>"
        "*{box-sizing:border-box;margin:0;padding:0}"
        ":root{--bg:#fff;--fg:#1a1a1a;--mu:#666;--bo:#e0e0e0;--ac:#2c5f8a;--son:#f0a500;--sof:#ccc}"
        "@media(prefers-color-scheme:dark){:root{--bg:#1a1a1a;--fg:#e8e8e8;--mu:#999;--bo:#333;--ac:#5a9fd4;--son:#f0c040;--sof:#444}}"
        "body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--fg);max-width:800px;margin:0 auto;padding:24px 16px}"
        "h1{font-size:22px;font-weight:600;color:var(--ac);margin-bottom:4px}"
        ".sub{font-size:14px;color:var(--mu);margin-bottom:24px}"
        ".card{border:1px solid var(--bo);border-radius:10px;padding:18px;margin-bottom:16px}"
        ".card.ok{border-color:#4caf50;background:#f0f7ed}"
        "@media(prefers-color-scheme:dark){.card.ok{background:#1a2d1a}}"
        ".num{font-size:12px;color:var(--mu);margin-bottom:4px}"
        ".ttl{font-size:16px;font-weight:600;line-height:1.4;margin-bottom:6px}"
        ".ttl a{color:var(--ac);text-decoration:none}"
        ".ttl a:hover{text-decoration:underline}"
        ".meta{font-size:13px;color:var(--mu);margin-bottom:10px}"
        ".kws{font-size:12px;color:var(--mu);margin-bottom:12px}"
        ".kw{background:#f5f5f5;border:1px solid var(--bo);border-radius:4px;padding:2px 7px;margin-right:5px;display:inline-block;margin-bottom:4px}"
        ".abs{font-size:14px;line-height:1.6;margin-bottom:14px}"
        ".bdg{font-size:11px;padding:2px 7px;border-radius:4px;margin-left:8px;background:#fff3cd;color:#856404;border:1px solid #ffc107}"
        ".bdg.pm{background:#d1e7ff;color:#084298;border-color:#9ec5fe}"
        ".stars{display:flex;gap:6px;align-items:center;margin-top:8px}"
        ".star{font-size:28px;cursor:pointer;color:var(--sof);user-select:none}"
        ".star.on{color:var(--son)}"
        ".rlbl{font-size:13px;color:var(--mu);margin-left:8px}"
        ".foot{margin-top:32px;text-align:center}"
        "#btn{background:var(--ac);color:#fff;border:none;padding:14px 40px;border-radius:8px;font-size:16px;font-weight:600;cursor:pointer;display:none}"
        "#btn:disabled{opacity:0.5;cursor:not-allowed}"
        "#st{margin-top:14px;font-size:14px;color:var(--mu);min-height:20px}"
        "#msg{text-align:center;padding:40px;color:var(--mu)}"
        "</style>"
        "</head>"
        "<body>"
        "<h1>Research Digest - Rate Articles</h1>"
        "<div class='sub'>" + date + " | <span id='prog'>Loading...</span></div>"
        "<div id='msg'>Loading articles...</div>"
        "<div id='arts'></div>"
        "<div class='foot'>"
        "<button id='btn' onclick='sub()'>Submit ratings and save</button>"
        "<div id='st'></div>"
        "</div>"
        "<script>"
        "var D='" + date + "',R='" + repo + "',P='" + pat + "',T=" + str(threshold) + ";"
        "var A=[],ratings={};"
        "var L=['','Not relevant','Slightly interesting','Interesting','Very interesting','Must read'];"
        "function e(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\"/g,'&quot;');}"
        "function render(){"
        "var o='';"
        "for(var i=0;i<A.length;i++){"
        "var a=A[i];"
        "var au=a.authors.slice(0,3).join(', ')+(a.authors.length>3?' et al.':'');"
        "var bd=a.source==='PubMed'?'<span class=\"bdg pm\">PubMed</span>':'<span class=\"bdg\">bioRxiv</span>';"
        "var kh='';"
        "if(a.matched_keywords&&a.matched_keywords.length){kh='<div class=\"kws\">';for(var k=0;k<a.matched_keywords.length;k++)kh+='<span class=\"kw\">'+e(a.matched_keywords[k])+'</span>';kh+='</div>';}"
        "var ab=a.abstract.length>400?a.abstract.slice(0,400)+'...':a.abstract;"
        "var sh='<div class=\"stars\" id=\"ss'+i+'\">';"
        "for(var s=1;s<=5;s++)sh+='<span class=\"star\" onclick=\"rate('+i+','+s+')\">&#9733;</span>';"
        "sh+='<span class=\"rlbl\" id=\"sl'+i+'\"></span></div>';"
        "o+='<div class=\"card\" id=\"c'+i+'\">';"
        "o+='<div class=\"num\">Article '+(i+1)+' of '+A.length+' - Score: '+a.score+'/100</div>';"
        "o+='<div class=\"ttl\"><a href=\"'+e(a.url)+'\" target=\"_blank\">'+e(a.title)+'</a>'+bd+'</div>';"
        "o+='<div class=\"meta\">'+e(au)+' - '+e(a.journal)+' '+e(a.year)+'</div>';"
        "o+=kh+'<div class=\"abs\">'+e(ab)+'</div>'+sh+'</div>';"
        "}"
        "document.getElementById('msg').style.display='none';"
        "document.getElementById('arts').innerHTML=o;"
        "document.getElementById('btn').style.display='inline-block';"
        "document.getElementById('prog').textContent='0 of '+A.length+' rated';"
        "}"
        "function rate(i,v){"
        "ratings[i]=v;"
        "var ss=document.querySelectorAll('#ss'+i+' .star');"
        "for(var s=0;s<ss.length;s++)ss[s].classList.toggle('on',s<v);"
        "document.getElementById('sl'+i).textContent=L[v];"
        "document.getElementById('c'+i).classList.toggle('ok',v>=T);"
        "document.getElementById('prog').textContent=Object.keys(ratings).length+' of '+A.length+' rated';"
        "}"
        "function sub(){"
        "var btn=document.getElementById('btn'),st=document.getElementById('st');"
        "btn.disabled=true;st.textContent='Saving...';"
        "var p=[];"
        "for(var i=0;i<A.length;i++){"
        "var a=A[i];"
        "p.push({doi:a.doi,title:a.title,journal:a.journal,year:a.year,authors:a.authors,"
        "url:a.url,source:a.source,score:a.score,rating:ratings[i]||null,"
        "in_digest:(ratings[i]||0)>=T});"
        "}"
        "var c=btoa(unescape(encodeURIComponent(JSON.stringify(p,null,2))));"
        "var fp='data/ratings/'+D+'.json';"
        "var u='https://api.github.com/repos/'+R+'/contents/'+fp;"
        "var h={'Authorization':'token '+P,'Accept':'application/vnd.github.v3+json','Content-Type':'application/json'};"
        "fetch(u,{headers:h})"
        ".then(function(r){return r.ok?r.json():null;})"
        ".then(function(x){"
        "var b={message:'ratings: '+D,content:c,branch:'main'};"
        "if(x&&x.sha)b.sha=x.sha;"
        "return fetch(u,{method:'PUT',headers:h,body:JSON.stringify(b)});"
        "})"
        ".then(function(r){"
        "if(r.ok){st.textContent='Saved! Articles rated '+T+'+ go into Monday digest.';btn.textContent='Saved';}"
        "else return r.json().then(function(e){throw new Error(e.message);});"
        "})"
        ".catch(function(e){st.textContent='Error: '+e.message;btn.disabled=false;});"
        "}"
        "fetch('" + json_url + "')"
        ".then(function(r){return r.json();})"
        ".then(function(d){A=d;render();})"
        ".catch(function(e){document.getElementById('msg').textContent='Error loading: '+e.message;});"
        "</script>"
        "</body>"
        "</html>"
    )

def github_commit(file_path, content_str, commit_message):
    api_url = "https://api.github.com/repos/" + GITHUB_USERNAME + "/" + GITHUB_REPO + "/contents/" + file_path
    headers = {"Authorization": "token " + GITHUB_TOKEN, "Accept": "application/vnd.github.v3+json"}
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
    print("[GitHub] Committed:", file_path)

def send_email(rating_url, n_articles, top_titles):
    items = "".join("<li>" + t + "</li>" for t in top_titles)
    body = (
        "<div style='font-family:system-ui,sans-serif;max-width:600px;margin:0 auto'>"
        "<h2 style='color:#2c5f8a'>Research Digest - " + TODAY + "</h2>"
        "<p>" + str(n_articles) + " new articles found today.</p>"
        "<ul style='font-size:14px;line-height:1.8'>" + items + "</ul>"
        "<div style='margin:28px 0;text-align:center'>"
        "<a href='" + rating_url + "' style='background:#2c5f8a;color:#fff;padding:14px 32px;"
        "border-radius:8px;text-decoration:none;font-size:16px;font-weight:600;display:inline-block'>"
        "Rate today's articles</a></div>"
        "<p style='font-size:12px;color:#aaa;text-align:center'>"
        "Articles rated " + str(RATING_THRESHOLD) + "+ appear in Monday digest.</p>"
        "</div>"
    )
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Research Digest " + TODAY + " (" + str(n_articles) + " articles)"
    msg["From"]    = GMAIL_USER
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(body, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_USER, GMAIL_PASS)
        smtp.sendmail(GMAIL_USER, EMAIL_TO, msg.as_string())
    print("[Email] Sent to", EMAIL_TO)

def main():
    print("=== Daily Search ===", TODAY)
    pmids   = search_pubmed(max_results=60)
    time.sleep(0.4)
    pm_arts = fetch_pubmed_details(pmids)
    bx_arts = search_biorxiv(max_results=60)
    all_arts = pm_arts + bx_arts
    print("[Total]", len(all_arts))
    ranked = rank_articles(all_arts)[:N_CANDIDATES]
    print("[Ranked]", len(ranked))
    if not ranked:
        print("[Warning] No articles found.")
        return
    repo_full = GITHUB_USERNAME + "/" + GITHUB_REPO
    articles_json = json.dumps(ranked, indent=2, ensure_ascii=False)
    github_commit("docs/rate/" + TODAY + ".json", articles_json, "articles json: " + TODAY)
    github_commit("data/articles/" + TODAY + ".json", articles_json, "articles: " + TODAY)
    json_url   = PAGES_URL + "/rate/" + TODAY + ".json"
    rating_html = generate_rating_html(TODAY, json_url, RATING_THRESHOLD, repo_full, GH_PAT)
    github_commit("docs/rate/" + TODAY + ".html", rating_html, "rating page: " + TODAY)
    rating_url = PAGES_URL + "/rate/" + TODAY + ".html"
    top_titles = [a["title"][:80] + ("..." if len(a["title"]) > 80 else "") for a in ranked[:5]]
    send_email(rating_url, len(ranked), top_titles)
    print("=== Done ===")

if __name__ == "__main__":
    main()
