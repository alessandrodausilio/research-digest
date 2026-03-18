---
name: research-digest
description: >
  Sistema automatizzato di digest giornaliero di articoli scientifici da PubMed e bioRxiv.
  Cerca ogni giorno alle 19:00 CET tramite GitHub Actions, invia una email con link a una
  pagina di rating su GitHub Pages, l'utente valuta gli articoli 1–5, quelli con rating ≥ 3
  entrano nel digest settimanale pubblicato ogni lunedì su GitHub Pages.
  
  Usa questa skill SEMPRE quando l'utente:
  - chiede il digest / la rassegna del giorno o della settimana
  - vuole aggiungere o rimuovere keywords o riviste
  - vuole cambiare impostazioni (soglia rating, numero candidati, giorni di ricerca)
  - chiede di avviare una ricerca manuale su PubMed o bioRxiv
  - vuole vedere le proprie preferenze apprese
  - menziona "digest", "letteratura", "nuove pubblicazioni", "PubMed", "bioRxiv"
  - vuole modificare config.json o la configurazione del sistema
  
  MCP richiesti: PubMed, bioRxiv, Gmail (solo per test manuali in chat)
  Per l'automazione: GitHub Actions + GitHub Pages (vedi README e SKILL.md)
---

# Research Digest – Skill

## Configurazione utente

### Parole chiave di ricerca
```
action perception, auditory-motor, premotor cortex, efference copy, mirror neurons,
motor control, motor cortex, motor learning, motor theory, music and brain,
sensorimotor communication, speech perception, TMS and motor cortex, TMS and music,
transcranial magnetic stimulation
```

### Riviste prioritarie (peso alto nel ranking)
```
Nature Communications, Nature Neuroscience, Neuron, Science Advances, Brain,
Current Biology, PLOS Biology, PNAS, Cerebral Cortex, eLife,
Imaging Neuroscience, iScience, Journal of Neurophysiology, Journal of Physiology,
Journal of Neuroscience, Trends in Cognitive Sciences, Trends in Neurosciences,
Nature Reviews Neuroscience
```

### Email di destinazione
`alessandro.dausilio@gmail.com`

---

## Architettura del sistema automatizzato

Il sistema gira su **GitHub Actions** (gratuito, nessun server necessario):

```
Ogni giorno 19:00 CET
  → GitHub Actions esegue daily_search.py
  → Cerca su PubMed + bioRxiv
  → Genera pagina HTML di rating su GitHub Pages
  → Invia email con link alla pagina
  → Utente valuta articoli 1–5 e salva
  
Ogni lunedì 09:00 CET
  → GitHub Actions esegue weekly_digest.py
  → Raccoglie tutti gli articoli con rating ≥ 3 della settimana
  → Pubblica digest Markdown su GitHub Pages
```

Per la configurazione completa del sistema automatizzato, vedi `README.md` nel repository.

---

## USO IN CHAT (test manuale / aggiornamento config)

### Aggiungere una keyword

Quando l'utente dice "aggiungi keyword: X":
1. Aggiorna la lista `keywords` nel file `config/config.json` del repo
2. Conferma all'utente la modifica

In chat (senza accesso al repo), aggiorna mentalmente la configurazione e comunica all'utente di aggiungere la keyword in `config/config.json`.

### Rimuovere una keyword

Come sopra, ma rimuovendo dalla lista.

### Aggiungere una rivista

Aggiorna `priority_journals` in `config/config.json`.

### Ricerca manuale in chat

Se l'utente chiede una ricerca manuale (es. "fammi il digest di oggi"):

#### FASE 1 – Ricerca
- Chiedi: quanti candidati? (default 20) e periodo (default ultimi 2 giorni)
- Cerca su PubMed con la query costruita dalle keywords
- Cerca su bioRxiv nella categoria neuroscience
- Deduplicazione per DOI

#### FASE 2 – Scoring (0–100)
| Componente | Peso | Calcolo |
|---|---|---|
| Keyword nel titolo | 30 | ogni keyword: +4pt (max 20) |
| Keyword nell'abstract | 30 | ogni keyword: +1pt (max 10) |
| Rivista prioritaria | 25 | sì/no: +25 |
| Preferenze apprese | 30 | pesi da preferences.json |
| Recency | 15 | bioRxiv=15, PubMed=10 |

#### FASE 3 – Presentazione articolo per articolo (in inglese)
Presenta un articolo alla volta nel formato:
```
[N] 📄 TITLE
    👥 Author1, Author2 et al. (Year)
    📰 Journal [⭐ priority journal]
    🏷️  Keywords: kw1, kw2
    ⭐ Score: XX/100
    
    Abstract (in English, max 300 words)
    
    🔗 https://doi.org/...
    
Valutazione: 1 (poco interessante) → 5 (molto interessante)
```

Attendi la valutazione prima di passare al prossimo articolo.
Includi nel digest gli articoli con rating ≥ 3.

#### FASE 4 – Invio digest
- Componi email HTML con gli articoli approvati (abstract in inglese)
- Usa `gmail_create_draft` per creare la bozza in Gmail
- Informa che la bozza è pronta e può essere inviata manualmente

---

## Sistema di apprendimento (preferences.json)

Il sistema aggiorna automaticamente i pesi dopo ogni sessione di rating.

### Struttura preferences.json
```json
{
  "journal_weights": { "Nature Neuroscience": 1.4 },
  "keyword_weights": { "mirror neurons": 1.3 },
  "author_weights": { "Rizzolatti G": 1.2 },
  "feedback_history": [
    { "doi": "...", "rating": 5, "date": "2026-03-18",
      "journal": "Neuron", "keywords": ["motor cortex"] }
  ]
}
```

### Aggiornamento pesi per rating 1–5
| Rating | journal_weight | keyword_weight |
|---|---|---|
| 5 | ×1.20 | ×1.20 + salva autore |
| 4 | ×1.10 | ×1.10 |
| 3 | ×1.05 | ×1.05 |
| 2 | ×0.95 | ×0.95 |
| 1 | ×0.85 | ×0.85 |

Pesi clampati tra 0.3 e 2.5.

---

## Comandi speciali in chat

| Comando | Azione |
|---|---|
| "aggiungi keyword: X" | Aggiunge X alla lista keywords |
| "rimuovi keyword: X" | Rimuove X dalla lista keywords |
| "aggiungi rivista: X" | Aggiunge X a priority_journals |
| "mostra le mie preferenze" | Mostra il contenuto di preferences.json |
| "resetta le preferenze" | Azzera preferences.json (chiede conferma) |
| "cambia soglia a N" | Aggiorna rating_threshold in config.json |
| "cambia candidati a N" | Aggiorna daily_candidates |
| "fammi il digest di oggi" | Ricerca manuale in chat |

---

## Note per il setup automatizzato

Per avviare il sistema automatizzato, l'utente deve:
1. Creare un repo GitHub privato e caricare i file
2. Abilitare GitHub Pages (Settings → Pages → GitHub Actions)
3. Impostare 4 secrets: `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `GH_PAT`
4. Editare `config/config.json` con username e repo name corretti

Vedi `README.md` per le istruzioni complete passo-passo.
