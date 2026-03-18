# Research Digest

Automated daily neuroscience literature digest with personal rating system and weekly GitHub Pages publication.

## How it works

1. **Every day at 19:00 CET** – GitHub Actions searches PubMed and bioRxiv for new articles matching your keywords, generates a personal rating page on GitHub Pages, and sends you an email with the link.
2. **You rate articles** – Click the link, rate each article 1–5 stars, submit. Articles rated ≥ 3 are saved.
3. **Every Monday at 09:00 CET** – GitHub Actions compiles all articles rated ≥ 3 from the past week into a digest and publishes it to your GitHub Pages site.

## Setup (one-time, ~15 minutes)

### 1. Create a new GitHub repository

Create a new **private** repo (e.g. `research-digest`) and push this code to it:

```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR_USERNAME/research-digest.git
git push -u origin main
```

### 2. Enable GitHub Pages

Go to your repo → **Settings** → **Pages** → Source: **GitHub Actions**.

### 3. Configure `config/config.json`

Edit the file and replace:
- `REPLACE_WITH_YOUR_GITHUB_USERNAME` → your GitHub username
- `REPLACE_WITH_YOUR_REPO_NAME` → your repo name (e.g. `research-digest`)

You can add/remove keywords and journals at any time by editing this file.

### 4. Create a Gmail App Password

1. Go to your Google Account → **Security** → **2-Step Verification** (must be enabled)
2. At the bottom: **App passwords**
3. Create one named "Research Digest"
4. Copy the 16-character password

### 5. Create a GitHub Personal Access Token (PAT)

1. GitHub → **Settings** → **Developer settings** → **Personal access tokens** → **Fine-grained tokens**
2. Name: `research-digest-writer`
3. Repository access: select only your `research-digest` repo
4. Permissions: **Contents** → **Read and write**
5. Copy the token

### 6. Add GitHub Actions secrets

Go to your repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**:

| Secret name | Value |
|---|---|
| `GMAIL_USER` | your Gmail address (e.g. `you@gmail.com`) |
| `GMAIL_APP_PASSWORD` | the 16-char App Password from step 4 |
| `GH_PAT` | the Personal Access Token from step 5 |

### 7. Test it

Go to **Actions** tab → **Daily Research Search** → **Run workflow** to test manually.

---

## Customization

### Add/remove keywords

Edit `config/config.json`, add to the `"keywords"` array, commit and push:

```json
"keywords": [
  "mirror neurons",
  "YOUR NEW KEYWORD HERE",
  ...
]
```

Or, from Claude.ai: just say *"aggiungi keyword: X"* and the skill will update the config for you.

### Add/remove priority journals

Same as above, edit the `"priority_journals"` array.

### Change number of daily candidates

Edit `"daily_candidates"` in `config/config.json` (default: 20).

### Change rating threshold

Edit `"rating_threshold"` (default: 3). Articles rated ≥ this value enter the weekly digest.

---

## File structure

```
research-digest/
├── .github/workflows/
│   ├── daily_search.yml     # runs daily at 19:00 CET
│   └── weekly_digest.yml    # runs Monday at 09:00 CET
├── config/
│   └── config.json          # ← edit this to customize
├── data/
│   ├── articles/            # daily article lists (auto-generated)
│   ├── ratings/             # your daily ratings (auto-generated)
│   └── preferences.json     # learned preferences (auto-generated)
├── docs/                    # GitHub Pages output
│   ├── index.html           # digest archive
│   ├── rate/                # daily rating pages
│   └── digest/              # weekly digest Markdown files
├── scripts/
│   ├── daily_search.py      # search + generate rating page + send email
│   └── weekly_digest.py     # compile weekly digest
└── README.md
```

---

## Troubleshooting

**Email not arriving?**
- Check that Gmail 2-Step Verification is enabled
- Make sure you used an App Password, not your regular password
- Check GitHub Actions logs for errors

**Rating page not loading?**
- Check that GitHub Pages is enabled (Settings → Pages)
- Wait ~2 minutes after the workflow runs for Pages to rebuild

**No articles found?**
- Check Actions logs for PubMed/bioRxiv API errors
- Try increasing `search_days` in config.json to 3–7
