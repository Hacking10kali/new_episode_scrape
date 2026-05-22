# Mise en ligne sur GitHub — `scrape_pages-main`

Guide pas à pas pour publier ce projet et activer le cron Firebase.

## État attendu du projet

Fichiers importants :

| Fichier | Rôle |
|---------|------|
| `scraper.py` | Scrape catalogue paginé → JSON `AnimeData/` |
| `sync_new_releases.py` | Nouvelles sorties (accueil) → Firestore |
| `firebase_sync.py` | Format Firestore (`animes` + `saison_chunks`) |
| `.github/workflows/scrape.yml` | Cron / manuel catalogue |
| `.github/workflows/sync-new-releases.yml` | Cron toutes les 6 h → Firebase |
| `requirements.txt` | Dépendances Python |
| `.gitignore` | Exclut les clés Firebase |

**Ne jamais committer** : `*firebase-adminsdk*.json`, `.env`, tokens.

---

## Étape 1 — Vérifier Git (PowerShell)

```powershell
cd C:\Users\user\Desktop\sleeping_dogs\scrape_pages-main

# Git installé ?
git --version

# Dépôt déjà initialisé ?
Test-Path .git

git status -sb
git remote -v
```

- Si `Test-Path .git` → **False** : passez à l’étape 2.
- Si un `origin` existe déjà : passez à l’étape 4 (commit + push).

---

## Étape 2 — Premier commit (si pas encore de `.git`)

```powershell
cd C:\Users\user\Desktop\sleeping_dogs\scrape_pages-main

git init
git branch -M main

git add .gitignore
git add scraper.py sync_new_releases.py firebase_sync.py
git add requirements.txt README.md GITHUB_SETUP.md
git add .github/

# Données déjà scrapées (optionnel mais recommandé si vous les utilisez)
git add AnimeData/

# Vérifier qu’aucun secret n’est stagé
git diff --cached --name-only
# → ne doit PAS lister de fichier *firebase-adminsdk*

git commit -m "Add catalogue scraper and Firebase sync for new releases"
```

---

## Étape 3 — Créer le dépôt GitHub

### Option A — Site web

1. [https://github.com/new](https://github.com/new)
2. Nom : **`anime-sama-scraper`** (ou autre)
3. **Private** recommandé
4. Ne pas ajouter README / .gitignore (déjà en local)
5. **Create repository**

### Option B — GitHub CLI (`gh`)

```powershell
gh auth login
gh auth status
gh api user -q .login
```

Si connecté :

```powershell
cd C:\Users\user\Desktop\sleeping_dogs\scrape_pages-main

# Crée le repo privé et pousse (remplace si le nom existe déjà)
gh repo create anime-sama-scraper --private --source=. --remote=origin --push
```

URL du repo : `https://github.com/VOTRE_USER/anime-sama-scraper`

---

## Étape 4 — Lier le remote et pousser (sans `gh repo create`)

Remplacez `VOTRE_USER` par votre identifiant GitHub :

```powershell
cd C:\Users\user\Desktop\sleeping_dogs\scrape_pages-main

git remote add origin https://github.com/VOTRE_USER/anime-sama-scraper.git
# Si origin existe déjà : git remote set-url origin https://github.com/VOTRE_USER/anime-sama-scraper.git

git push -u origin main
```

Authentification : **Personal Access Token** (Settings → Developer settings → Tokens) ou `gh auth login`.

---

## Étape 5 — Secret Firebase (obligatoire pour le cron)

1. Repo GitHub → **Settings** → **Secrets and variables** → **Actions**
2. **New repository secret**
3. **Name** : `FIREBASE_SERVICE_ACCOUNT_JSON`
4. **Value** : contenu **complet** du fichier local (exemple) :

   `C:\Users\user\Desktop\sleeping_dogs\page_refactore_and_deploy_firebase\otakuflixversion2-firebase-adminsdk-fbsvc-c8e82ade03.json`

5. **Add secret**

Sans ce secret, le workflow **Sync nouvelles sorties → Firebase** échoue à l’étape « Write Firebase credentials ».

---

## Étape 6 — Tester les Actions

1. GitHub → **Actions**
2. **Sync nouvelles sorties → Firebase** → **Run workflow**
3. Vérifier les logs (Playwright + écriture Firestore)

Pour le scrape catalogue : **Anime Sama Scraper** → **Run workflow** (paramètre `page_offset`).

---

## Mises à jour ultérieures

```powershell
cd C:\Users\user\Desktop\sleeping_dogs\scrape_pages-main
git add -A
git status
git commit -m "Description de vos changements"
git push
```

---

## Dépannage

| Problème | Solution |
|----------|----------|
| `git push` rejeté (non-fast-forward) | `git pull --rebase origin main` puis `git push` |
| Repo trop gros (AnimeData) | Git LFS ou repo séparé pour les JSON |
| Workflow : secret manquant | Ajouter `FIREBASE_SERVICE_ACCOUNT_JSON` |
| `gh` introuvable | Installer [GitHub CLI](https://cli.github.com/) |

---

## Récap sécurité

- Clés Firebase : **uniquement** dans GitHub Secrets, jamais dans le code.
- `.gitignore` bloque `*firebase-adminsdk*.json`.
- Vérifier avant chaque push : `git diff --cached --name-only`.
