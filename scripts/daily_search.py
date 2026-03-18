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
    # Article data is NOT embedded in the HTML.
    # The page fetches it from json_url at runtime.
    # Only simple scalars are injected as JS variables.
    page  = "<!DOCTYPE html>"
    page += "<html lang='en'>"
    page += "<head>"
    page += "<meta charset='UTF-8'>"
    page += "<meta name='viewport' content='width=device-width,initial-scale=1'>"
    page += "<title>Research Digest " + date + "</title>"
    page += "<style>"
    page += "*{box-sizing:border-box;margin:0;padding:0}"
    page += ":root{--bg:#fff;--fg:#1a1a1a;--mu:#666;--bo:#e0e0e0;--ac:#2c5f8a;--son:#f0a500;--sof:#ccc}"
    page += "@media(prefers-color-scheme:dark){:root{--bg:#1a1a1a;--fg:#e8e8e8;--mu:#999;--bo:#333;--ac:#5a9fd4;--son:#f0c040;--sof:#444}}"
    page += "body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--fg);max-width:800px;margin:0 auto;padding:24px 16px}"
    page += "h1{font-size:22px;font-weight:600;color:var(--ac);margin-bottom:4px}"
    page += ".sub{font-size:14px;color:var(--mu);margin-bottom:24px}"
    page += ".card{border:1px solid var(--bo);border-radius:10px;padding:18px;margin-bottom:16px}"
    page += ".card.ok{border-color:#4caf50;background:#f0f7ed}"
    page += ".num{font-size:12px;color:var(--mu);margin-bottom:4px}"
    page += ".ttl{font-size:16px;font-weight:600;line-height:1.4;margin-bottom:6px}"
    page += ".ttl a{color:var(--ac);text-decoration:none}"
    page += ".ttl a:hover{text-decoration:underline}"
    page += ".meta{font-size:13px;color:var(--mu);margin-bottom:10px}"
    page += ".kws{font-size:12px;color:var(--mu);margin-bottom:12px}"
    page += ".kw{background:#f5f5f5;border:1px solid var(--bo);border-radius:4px;padding:2px 7px;margin-right:5px;display:inline-block;margin-bottom:4px}"
    page += ".abs{font-size:14px;line-height:1.6;margin-bottom:14px}"
    page += ".bdg{font-size:11px;padding:2px 7px;border-radius:4px;margin-left:8px;background:#fff3cd;color:#856404;border:1px solid #ffc107}"
    page += ".bdg.pm{background:#d1e7ff;color:#084298;border-color:#9ec5fe}"
    page += ".stars{display:flex;gap:6px;align-items:center;margin-top:8px}"
    page += ".star{font-size:28px;cursor:pointer;color:var(--sof);user-select:none}"
    page += ".star.on{color:var(--son)}"
    page += ".rlbl{font-size:13px;color:var(--mu);margin-left:8px}"
    page += ".foot{margin-top:32px;text-align:center}"
    page += "#btn{background:var(--ac);color:#fff;border:none;padding:14px 40px;border-radius:8px;font-size:16px;font-weight:600;cursor:pointer;display:none}"
    page += "#btn:disabled{opacity:0.5;cursor:not-allowed}"
    page += "#st{margin-top:14px;font-size:14px;color:var(--mu);min-height:20px}"
    page += "#msg{text-align:center;padding:40px;color:var(--mu)}"
    page += "</style>"
    page += "</head>"
    page += "<body>"
    page += "<h1>Research Digest - Rate Articles</h1>"
    page += "<div class='sub'>" + date + " | <span id='prog'>Loading...</span></div>"
    page += "<div id='msg'>Loading articles...</div>"
    page += "<div id='arts'></div>"
    page += "<div class='foot'>"
    page += "<button id='btn' onclick='submitRatings()'>Submit ratings and save</button>"
    page += "<div id='st'></div>"
    page += "</div>"
    page += "<script>"
    page += "var DATE='" + date + "';"
    page += "var REPO='" + repo + "';"
    page += "var PAT='" + pat + "';"
    page += "var THR=" + str(threshold) + ";"
    page += "var ARTS=[];"
    page += "var ratings={};"
    page += "var LABELS=['','Not relevant','Slightly interesting','Interesting','Very interesting','Must read'];"
    page += "function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\"/g,'&quot;');}"
    page += "function render(){"
    page += "var o='';"
    page += "for(var i=0;i<ARTS.length;i++){"
    page += "var a=ARTS[i];"
    page += "var au=a.authors.slice(0,3).join(', ')+(a.authors.length>3?' et al.':'');"
    page += "var bd=a.source==='PubMed'?'<span class=\"bdg pm\">PubMed</span>':'<span class=\"bdg\">bioRxiv</span>';"
    page += "var kh='';"
    page += "if(a.matched_keywords&&a.matched_keywords.length){kh='<div class=\"kws\">';for(var k=0;k<a.matched_keywords.length;k++)kh+='<span class=\"kw\">'+esc(a.matched_keywords[k])+'</span>';kh+='</div>';}"
    page += "var ab=a.abstract.length>400?a.abstract.slice(0,400)+'...':a.abstract;"
    page += "var sh='<div class=\"stars\" id=\"ss'+i+'\">';"
    page += "for(var s=1;s<=5;s++)sh+='<span class=\"star\" onclick=\"rate('+i+','+s+')\">&#9733;</span>';"
    page += "sh+='<span class=\"rlbl\" id=\"sl'+i+'\"></span></div>';"
    page += "o+='<div class=\"card\" id=\"c'+i+'\">';"
    page += "o+='<div class=\"num\">Article '+(i+1)+' of '+ARTS.length+' - Score: '+a.score+'/100</div>';"
    page += "o+='<div class=\"ttl\"><a href=\"'+esc(a.url)+'\" target=\"_blank\">'+esc(a.title)+'</a>'+bd+'</div>';"
    page += "o+='<div class=\"meta\">'+esc(au)+' - '+esc(a.journal)+' '+esc(a.year)+'</div>';"
    page += "o+=kh+'<div class=\"abs\">'+esc(ab)+'</div>'+sh+'</div>';"
    page += "}"
    page += "document.getElementById('msg').style.display='none';"
    page += "document.getElementById('arts').innerHTML=o;"
    page += "document.getElementById('btn').style.display='inline-block';"
    page += "document.getElementById('prog').textContent='0 of '+ARTS.length+' rated';"
    page += "}"
    page += "function rate(i,v){"
    page += "ratings[i]=v;"
    page += "var ss=document.querySelectorAll('#ss'+i+' .star');"
    page += "for(var s=0;s<ss.length;s++)ss[s].classList.toggle('on',s<v);"
    page += "document.getElementById('sl'+i).textContent=LABELS[v];"
    page += "document.getElementById('c'+i).classList.toggle('ok',v>=THR);"
    page += "document.getElementById('prog').textContent=Object.keys(ratings).length+' of '+ARTS.length+' rated';"
    page += "}"
    page += "function submitRatings(){"
    page += "var btn=document.getElementById('btn'),st=document.getElementById('st');"
    page += "btn.disabled=true;st.textContent='Saving...';"
    page += "var p=[];"
    page += "for(var i=0;i<ARTS.length;i++){"
    page += "var a=ARTS[i];"
    page += "p.push({doi:a.doi,title:a.title,journal:a.journal,year:a.year,authors:a.authors,url:a.url,source:a.source,score:a.score,rating:ratings[i]||null,in_digest:(ratings[i]||0)>=THR});"
    page += "}"
    page += "var c=btoa(unescape(encodeURIComponent(JSON.stringify(p,null,2))));"
    page += "var fp='data/ratings/'+DATE+'.json';"
    page += "var u='https://api.github.com/repos/'+REPO+'/contents/'+fp;"
    page += "var h={'Authorization':'token '+PAT,'Accept':'application/vnd.github.v3+json','Content-Type':'application/json'};"
    page += "fetch(u,{headers:h})"
    page += ".then(function(r){return r.ok?r.json():null;})"
    page += ".then(function(x){"
    page += "var b={message:'ratings: '+DATE,content:c,branch:'main'};"
    page += "if(x&&x.sha)b.sha=x.sha;"
    page += "return fetch(u,{method:'PUT',headers:h,body:JSON.stringify(b)});"
    page += "})"
    page += ".then(function(r){"
    page += "if(r.ok){st.textContent='Saved!';btn.textContent='Saved';}"
    page += "else return r.json().then(function(e){throw new Error(e.message);});"
    page += "})"
    page += ".catch(function(e){st.textContent='Error: '+e.message;btn.disabled=false;});"
    page += "}"
    page += "fetch('" + json_url + "')"
    page += ".then(function(r){return r.json();})"
    page += ".then(function(d){ARTS=d;render();})"
    page += ".catch(function(e){document.getElementById('msg').textContent='Error loading: '+e.message;});"
    page += "</script>"
    page += "</body>"
    page += "</html>"
    return page

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
    repo_full     = GITHUB_USERNAME + "/" + GITHUB_REPO
    articles_json = json.dumps(ranked, indent=2, ensure_ascii=False)
    github_commit("docs/rate/" + TODAY + ".json", articles_json, "articles json: " + TODAY)
    github_commit("data/articles/" + TODAY + ".json", articles_json, "articles: " + TODAY)
    json_url    = PAGES_URL + "/rate/" + TODAY + ".json"
    github_commit("docs/rate/" + TODAY + ".html", generate_rating_html(TODAY, json_url, RATING_THRESHOLD, repo_full, GH_PAT), "rating page: " + TODAY)
    rating_url  = PAGES_URL + "/rate/" + TODAY + ".html"
    top_titles  = [a["title"][:80] + ("..." if len(a["title"]) > 80 else "") for a in ranked[:5]]
    send_email(rating_url, len(ranked), top_titles)
    print("=== Done ===")

if __name__ == "__main__":
    main()
