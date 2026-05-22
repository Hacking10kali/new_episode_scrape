import asyncio
import aiohttp
import json
import re
import os
import time
import subprocess
from datetime import datetime
from playwright.async_api import async_playwright

# ══════════════════════════════════════════════════════════════
#  CONFIG — modifier uniquement ces variables
# ══════════════════════════════════════════════════════════════
BASE_URL      = "https://anime-sama.to"
CATALOGUE_URL = "https://anime-sama.to/catalogue/?page={page}"
ANIME_ONLY    = True
TMDB_API_KEY  = "cfc454f98433e15eaa3b67f178fd8774"
TMDB_BASE     = "https://api.themoviedb.org/3"
JIKAN_BASE    = "https://api.jikan.moe/v4"
KITSU_BASE    = "https://kitsu.io/api/edge"
OUTPUT_DIR    = "AnimeData"

# Injectées par GitHub Actions via env
PAGE_NUM = int(os.environ.get("PAGE_NUM", "1"))

# Délai minimum entre chaque requête Playwright (ms → s)
# 200ms = 0.2s : rapide mais poli
EPISODE_DELAY = float(os.environ.get("EPISODE_DELAY", "0.2"))
JIKAN_DELAY   = 0.35
MAX_RETRIES   = 3

_start = time.time()
def log(msg):
    e = int(time.time() - _start)
    print(f"[{e//60:02d}m{e%60:02d}s] {msg}", flush=True)

# ══════════════════════════════════════════════════════════════
#  SAUVEGARDE PROGRESSIVE — 1 fichier JSON par animé
# ══════════════════════════════════════════════════════════════
def git_push(path, message):
    """Push immédiat d'un fichier vers GitHub."""
    try:
        subprocess.run(["git", "add", path], check=True, capture_output=True)
        # Vérifier s'il y a quelque chose à commiter
        result = subprocess.run(
            ["git", "diff", "--staged", "--quiet"],
            capture_output=True
        )
        if result.returncode != 0:
            # Il y a des changements à commiter
            subprocess.run(["git", "commit", "-m", message],
                           check=True, capture_output=True)
            # Push avec retry
            for attempt in range(3):
                try:
                    subprocess.run(
                        ["git", "pull", "--rebase", "origin", "main"],
                        check=True, capture_output=True
                    )
                    subprocess.run(
                        ["git", "push", "origin", "main"],
                        check=True, capture_output=True
                    )
                    break
                except subprocess.CalledProcessError:
                    if attempt == 2:
                        log(f"  push failed after 3 attempts: {path}")
                    time.sleep(2 * (attempt + 1))
    except subprocess.CalledProcessError as e:
        log(f"  git error: {e.stderr.decode()[:100] if e.stderr else str(e)}")


def save_anime(anime, page_num):
    """Sauvegarde + push immédiat dès qu'un animé est scraped."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    page_dir = os.path.join(OUTPUT_DIR, f"page_{page_num}")
    os.makedirs(page_dir, exist_ok=True)
    safe = re.sub(r'[^\w\-]', '_', anime["nom"])[:80]
    path = os.path.join(page_dir, f"{safe}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(anime, f, ensure_ascii=False, indent=2)
    log(f"  saved {path}")
    # Push immédiat vers GitHub
    msg = f"anime: {anime['nom'][:50]} [p{page_num}]"
    git_push(path, msg)

def save_page_summary(animes, page_num, elapsed):
    """Sauvegarde le fichier page_N.json global une fois tout terminé."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, f"page_{page_num}.json")
    data = {
        "page":       page_num,
        "scraped_at": datetime.now().strftime("%Y%m%d_%H%M%S"),
        "duration_s": elapsed,
        "total":      len(animes),
        "animes":     animes,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log(f"saved {path} ({len(animes)} animes, {elapsed//60}m{elapsed%60:02d}s)")

def has_empty_lecteurs(anime_data):
    """Retourne True si au moins un épisode a des lecteurs vides."""
    for saison in anime_data.get("saisons", []):
        for ep in saison.get("episodes", []):
            if not ep.get("lecteurs"):
                return True
    return False

def already_done(nom, page_num):
    """
    Vérifie si un animé a déjà été scraped ET que tous les lecteurs sont remplis.
    Si le fichier existe mais a des lecteurs vides → retourne False pour re-scraper.
    """
    safe = re.sub(r'[^\w\-]', '_', nom)[:80]
    path = os.path.join(OUTPUT_DIR, f"page_{page_num}", f"{safe}.json")
    if not os.path.exists(path):
        return False
    # Charger et vérifier les lecteurs
    data = load_done_anime(nom, page_num)
    if data is None:
        return False
    if has_empty_lecteurs(data):
        log(f"  re-scrape {nom} (lecteurs vides détectés)")
        return False
    return True

def load_done_anime(nom, page_num):
    """Charge un animé déjà scraped depuis le disque."""
    safe = re.sub(r'[^\w\-]', '_', nom)[:80]
    path = os.path.join(OUTPUT_DIR, f"page_{page_num}", f"{safe}.json")
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None

# ══════════════════════════════════════════════════════════════
#  UTILS
# ══════════════════════════════════════════════════════════════
def build_url(href):
    if not href:
        return None
    if href.startswith("http"):
        return href
    return BASE_URL + ("" if href.startswith("/") else "/") + href

def parse_info_rows(rows):
    info = {"genres": [], "type": None, "langues": []}
    for row in rows:
        label = row.get("label", "").lower()
        value = row.get("value", "").strip()
        if "genre" in label:
            info["genres"]  = [g.strip() for g in value.split(",") if g.strip()]
        elif "type" in label:
            info["type"]    = value
        elif "lang" in label:
            info["langues"] = [l.strip() for l in value.split(",") if l.strip()]
    return info

def clean_title(title):
    t = re.sub(r'\s*(saison|season|partie|part|film)\s*\d*', '', title, flags=re.IGNORECASE)
    return re.sub(r'\s*\d+$', '', t).strip()

def slug_from_url(url):
    m = re.search(r'/catalogue/([^/]+)/?$', url or "")
    return m.group(1) if m else None

def build_saison_url(anime_lien, titre, langue):
    slug = slug_from_url(anime_lien)
    if not slug:
        return None
    t = titre.lower().strip()
    if "film" in t:
        num = re.search(r'\d+', t)
        segment = "film" + (num.group() if num else "")
    else:
        s_num = re.search(r'saison\s*(\d+)', t)
        p_num = re.search(r'partie\s*(\d+)', t)
        if s_num:
            segment = "saison" + s_num.group(1)
            if p_num:
                segment += "-partie" + p_num.group(1)
        elif p_num:
            segment = "partie" + p_num.group(1)
        else:
            num = re.search(r'\d+', t)
            segment = "saison" + num.group() if num else "saison1"
    return BASE_URL + "/catalogue/" + slug + "/" + segment + "/" + langue.lower() + "/"

def new_ctx(browser):
    return browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        locale="fr-FR",
    )

# ══════════════════════════════════════════════════════════════
#  APIS
# ══════════════════════════════════════════════════════════════
async def get_jikan_id(session, title, is_film=False):
    query = clean_title(title)
    if not query:
        return None
    await asyncio.sleep(JIKAN_DELAY)
    media = "movie" if is_film else "tv"
    try:
        async with session.get(
            JIKAN_BASE + "/anime?q=" + query + "&type=" + media + "&limit=1",
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            data = (await r.json()).get("data", []) if r.status == 200 else []
        if not data:
            async with session.get(
                JIKAN_BASE + "/anime?q=" + query + "&limit=1",
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r2:
                data = (await r2.json()).get("data", []) if r2.status == 200 else []
        return data[0].get("mal_id") if data else None
    except Exception:
        return None

async def get_tmdb_id(session, title, is_film=False):
    query = clean_title(title)
    if not query:
        return None
    media = "movie" if is_film else "tv"
    try:
        async with session.get(
            TMDB_BASE + "/search/" + media
            + "?api_key=" + TMDB_API_KEY
            + "&query=" + query + "&language=fr-FR&page=1",
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            results = (await r.json()).get("results", []) if r.status == 200 else []
            return results[0].get("id") if results else None
    except Exception:
        return None

async def get_kitsu_id(session, title, is_film=False):
    query = clean_title(title)
    if not query:
        return None
    subtype = "movie" if is_film else "TV"
    hdrs = {"Accept": "application/vnd.api+json"}
    try:
        async with session.get(
            KITSU_BASE + "/anime?filter[text]=" + query
            + "&filter[subtype]=" + subtype + "&page[limit]=1",
            headers=hdrs,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            data = (await r.json()).get("data", []) if r.status == 200 else []
        if not data:
            async with session.get(
                KITSU_BASE + "/anime?filter[text]=" + query + "&page[limit]=1",
                headers=hdrs,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r2:
                data = (await r2.json()).get("data", []) if r2.status == 200 else []
        return data[0].get("id") if data else None
    except Exception:
        return None

async def fetch_ids(session, title, is_film=False):
    j, t, k = await asyncio.gather(
        get_jikan_id(session, title, is_film),
        get_tmdb_id(session, title, is_film),
        get_kitsu_id(session, title, is_film),
    )
    return {"jikan_id": j, "tmdb_id": t, "kitsu_id": k}

async def check_url(session, url):
    try:
        async with session.head(
            url,
            timeout=aiohttp.ClientTimeout(total=6),
            allow_redirects=True
        ) as r:
            return r.status == 200
    except Exception:
        return False

# ══════════════════════════════════════════════════════════════
#  HELPERS PLAYWRIGHT
# ══════════════════════════════════════════════════════════════
async def goto_page(page, url):
    for strategy in ("networkidle", "domcontentloaded", "load"):
        try:
            await page.goto(url, wait_until=strategy, timeout=45000)
            return True
        except Exception:
            pass
    return False

async def wait_select(page, selector, timeout=20000):
    for _ in range(MAX_RETRIES):
        try:
            await page.wait_for_selector(selector, timeout=timeout)
            n = await page.evaluate(
                "() => document.querySelector('" + selector + "')?.options.length || 0"
            )
            if n > 0:
                return True
            await page.wait_for_timeout(800)
        except Exception:
            await page.wait_for_timeout(1000)
    return False

async def get_options(page, selector):
    for _ in range(MAX_RETRIES):
        opts = await page.evaluate(
            "() => { const s=document.querySelector('" + selector + "'); "
            "return s ? Array.from(s.options).map(o=>({value:o.value,label:o.text.trim()})) : []; }"
        )
        if opts:
            return opts
        await page.wait_for_timeout(800)
    return []

async def read_player(page):
    return await page.evaluate(
        "() => {"
        "  const f = document.querySelector('#playerDF');"
        "  if (!f) return null;"
        "  let s = f.getAttribute('src') || f.getAttribute('data-src');"
        "  if (s && s.length > 10 && !s.includes('about:blank')) return s;"
        "  for (const el of f.querySelectorAll('iframe,[src],[data-src]')) {"
        "    const v = el.getAttribute('src') || el.getAttribute('data-src') || '';"
        "    if (v.length > 10 && !v.includes('about:blank')) return v;"
        "  }"
        "  return null;"
        "}"
    )

async def wait_player(page, old_src="", timeout=6000):
    for attempt in range(MAX_RETRIES):
        try:
            await page.wait_for_function(
                "(old) => {"
                "  const f = document.querySelector('#playerDF');"
                "  if (!f) return false;"
                "  const srcs = ["
                "    f.getAttribute('src') || '',"
                "    f.getAttribute('data-src') || '',"
                "    ...[...f.querySelectorAll('iframe,[src],[data-src]')]"
                "      .map(e => e.getAttribute('src') || e.getAttribute('data-src') || '')"
                "  ].filter(s => s.length > 10 && !s.includes('about:blank'));"
                "  return srcs.length > 0 && srcs[0] !== old;"
                "}",
                arg=old_src,
                timeout=timeout
            )
        except Exception:
            pass
        src = await read_player(page)
        if src and src != old_src:
            return src
        await page.wait_for_timeout(700 * (attempt + 1))
    return await read_player(page)

async def is_blocked(page):
    try:
        title = (await page.title()).lower()
        url   = page.url
        if any(x in title for x in ["error", "403", "429", "blocked", "captcha"]):
            return True
        if any(x in url for x in ["login", "captcha", "blocked"]):
            return True
        has_content = await page.evaluate(
            "() => document.querySelector('#selectEpisodes') !== null"
            " || document.querySelector('#playerDF') !== null"
            " || document.querySelector('h1') !== null"
        )
        return not has_content
    except Exception:
        return False

# ══════════════════════════════════════════════════════════════
#  SCRAPING — Catalogue
# ══════════════════════════════════════════════════════════════
async def scrape_catalogue(browser, page_num):
    ctx  = await new_ctx(browser)
    page = await ctx.new_page()
    await page.route("**/*.{png,jpg,jpeg,gif,webp,woff,woff2,ttf,mp4,mp3}", lambda r: r.abort())
    raw = []
    try:
        await page.goto(
            CATALOGUE_URL.format(page=page_num),
            wait_until="domcontentloaded",
            timeout=30000
        )
        await page.wait_for_selector("div.catalog-card", timeout=15000)

        # Scroll progressif pour déclencher le lazy loading
        # On scrolle par étapes jusqu'en bas puis on remonte
        await page.evaluate("""
            async () => {
                const step   = 600;
                const delay  = 200;
                const height = document.body.scrollHeight;
                for (let y = 0; y < height; y += step) {
                    window.scrollTo(0, y);
                    await new Promise(r => setTimeout(r, delay));
                }
                window.scrollTo(0, 0);
            }
        """)
        # Attendre que les nouvelles cards soient rendues
        await page.wait_for_timeout(1000)

        raw = await page.evaluate("""
            () => {
                const cards = Array.from(document.querySelectorAll("div.shrink-0.catalog-card.card-base"));
                return cards.map(card => {
                    const name = card.querySelector("h2.card-title")?.innerText.trim() || "Inconnu";
                    let href = null;
                    card.querySelectorAll("a[href]").forEach(a => {
                        const h = a.getAttribute("href");
                        if (h && h.includes("/catalogue/") && !href) href = h;
                    });
                    if (!href) href = card.querySelector("a[href]")?.getAttribute("href") || null;
                    const infoRows = [];
                    card.querySelectorAll("div.info-row span").forEach(span => {
                        const p = span.nextElementSibling;
                        if (p && p.tagName === "P") {
                            infoRows.push({ label: span.innerText.trim(), value: p.innerText.trim() });
                        }
                    });
                    return { name, href, infoRows };
                });
            }
        """)
    except Exception as e:
        log(f"catalogue error: {e}")
    finally:
        try:
            await ctx.close()
        except Exception:
            pass

    animes = []
    for r in raw:
        info = parse_info_rows(r["infoRows"])
        if ANIME_ONLY and info["type"]:
            # Exclure uniquement "Scans" (avec S majuscule, seul, sans autre type)
            # Garder : "Anime", "Anime, Scans", "Film", "", None, tout autre type
            t = info["type"].strip()
            if t == "Scans":
                continue
        animes.append({
            "nom":           r["name"],
            "type":          info["type"],
            "genres":        info["genres"],
            "langues":       info["langues"],
            "lien":          build_url(r["href"]),
            "image":         None,
            "noms_alt":      [],
            "synopsis":      None,
            "bande_annonce": None,
            "ids":           {"jikan_id": None, "tmdb_id": None, "kitsu_id": None},
            "saisons":       [],
        })
    return animes

# ══════════════════════════════════════════════════════════════
#  SCRAPING — Detail
# ══════════════════════════════════════════════════════════════
async def scrape_detail(browser, url):
    ctx  = await new_ctx(browser)
    page = await ctx.new_page()
    await page.route("**/*.{png,jpg,jpeg,gif,webp,woff,woff2,ttf,mp4,mp3}", lambda r: r.abort())
    result = {}
    try:
        await goto_page(page, url)
        try:
            await page.wait_for_function(
                "() => !!document.querySelector('#coverOeuvre') || !!document.querySelector('h1')",
                timeout=12000
            )
        except Exception:
            pass
        await page.wait_for_timeout(600)
        result = await page.evaluate(
            "() => {"
            "  const img = document.querySelector('#coverOeuvre');"
            "  const image = img?.getAttribute('src') || img?.getAttribute('data-src') || null;"
            "  const alt = document.querySelector('#titreAlter');"
            "  const nomsAlt = alt ? alt.innerText.trim().split(',').map(s=>s.trim()).filter(Boolean) : [];"
            "  const syn = document.querySelector('p.text-sm.text-gray-300.leading-relaxed');"
            "  const synopsis = syn?.innerText.trim() || null;"
            "  const ifr = document.querySelector('#bandeannonce');"
            "  const bandeAnnonce = ifr ? (ifr.getAttribute('src') || ifr.getAttribute('data-src')) : null;"
            "  const cont = document.querySelector('.flex.flex-wrap.overflow-y-hidden.justify-start.bg-slate-900.bg-opacity-70.rounded.mt-2.h-auto');"
            "  const saisons = [];"
            "  if (cont) {"
            "    cont.querySelectorAll('a').forEach(a => {"
            "      let lbl = a.querySelector('.text-white.font-bold.text-center.absolute.w-28')"
            "             || a.querySelector('[class*=\"font-bold\"][class*=\"text-center\"]');"
            "      const tv = lbl?.innerText.trim() || a.innerText.trim();"
            "      const tc = a.getAttribute('title') || a.getAttribute('aria-label') || tv;"
            "      if (tv) saisons.push({ titreVignette: tv, titreComplet: tc, isFilm: tv.toLowerCase().includes('film') });"
            "    });"
            "  }"
            "  return { image, nomsAlt, synopsis, bandeAnnonce, saisons };"
            "}"
        )
    except Exception as e:
        log(f"detail error {url}: {e}")
    finally:
        try:
            await ctx.close()
        except Exception:
            pass
    return result

# ══════════════════════════════════════════════════════════════
#  SCRAPING — Tous les épisodes d'une saison (1 page = tous les épisodes)
# ══════════════════════════════════════════════════════════════
async def collect_lecteurs(page):
    """
    Lit le src de #playerDF pour chaque lecteur.
    Stratégie : sélectionner + attendre que src soit non-vide et non-blank.
    On ne compare pas avec l'ancien src car le JS peut recycler la même iframe.
    """
    lecteurs = []
    opts = await get_options(page, "#selectLecteurs")
    for lect in opts:
        selected = False
        try:
            await page.select_option("#selectLecteurs", value=lect["value"])
            await page.wait_for_timeout(300)
            selected = True
        except Exception:
            selected = False

        if not selected:
            continue

        # Attendre que #playerDF ait un src valide
        src = None
        for wait_ms in [500, 800, 1200, 2000, 3000]:
            await page.wait_for_timeout(wait_ms)
            src = await page.evaluate("""
                () => {
                    const f = document.querySelector("#playerDF");
                    if (!f) return null;
                    // Lire src directement sur l'attribut HTML (pas la prop JS)
                    const s = f.getAttribute("src") || f.getAttribute("data-src") || "";
                    if (s && s.length > 10 && !s.includes("about:blank")) return s;
                    // Chercher dans les enfants
                    for (const el of f.querySelectorAll("iframe,[src],[data-src]")) {
                        const v = el.getAttribute("src") || el.getAttribute("data-src") || "";
                        if (v.length > 10 && !v.includes("about:blank")) return v;
                    }
                    return null;
                }
            """)
            if src:
                break

        if src:
            lecteurs.append({"lecteur": lect["label"], "url": src})
        else:
            # Dernier recours : lire le outerHTML pour debug
            html = await page.evaluate(
                "() => document.querySelector('#playerDF')?.outerHTML?.substring(0, 200) || 'absent'"
            )
            log(f"      lecteur {lect['label']} vide — playerDF: {html}")

    return lecteurs


async def scrape_saison_episodes(browser, saison_url):
    """
    Charge la page UNE SEULE FOIS et itère tous les épisodes dessus.
    Pause EPISODE_DELAY entre chaque épisode pour éviter le rate limit.
    Si des épisodes sont vides → recharge la page une fois et retente.
    """
    slug = "/".join(saison_url.rstrip("/").split("/")[-2:])

    for attempt in range(MAX_RETRIES):
        ctx  = await new_ctx(browser)
        page = await ctx.new_page()
        episodes = []
        success  = False
        try:
            await goto_page(page, saison_url)

            blocked = await is_blocked(page)
            if blocked:
                log(f"    blocked on {slug}, pause 10s")
                await asyncio.sleep(10 * (attempt + 1))
            else:
                has_eps = await wait_select(page, "#selectEpisodes", timeout=25000)
                if has_eps:
                    await page.wait_for_timeout(500)
                    eps_opts = await get_options(page, "#selectEpisodes")

                    if eps_opts:
                        log(f"    {len(eps_opts)} ep [{slug}]")
                        for ep in eps_opts:
                            ep_ok = False
                            try:
                                await page.select_option("#selectEpisodes", value=ep["value"])
                                await page.wait_for_timeout(400)
                                ep_ok = True
                            except Exception:
                                ep_ok = False

                            if ep_ok:
                                has_lect = await wait_select(page, "#selectLecteurs", timeout=10000)
                                if not has_lect:
                                    await page.wait_for_timeout(800)
                                    src = await wait_player(page)
                                    if src:
                                        episodes.append({
                                            "episode":  ep["label"],
                                            "lecteurs": [{"lecteur": "default", "url": src}]
                                        })
                                    else:
                                        episodes.append({"episode": ep["label"], "lecteurs": []})
                                else:
                                    lecteurs = await collect_lecteurs(page)
                                    episodes.append({"episode": ep["label"], "lecteurs": lecteurs})
                            else:
                                episodes.append({"episode": ep["label"], "lecteurs": []})

                            # Pause polie entre chaque épisode
                            await asyncio.sleep(EPISODE_DELAY)

                        success = True

        except Exception as e:
            log(f"    error on {slug} attempt {attempt+1}: {e}")
        finally:
            try:
                await ctx.close()
            except Exception:
                pass

        if success:
            break
        await asyncio.sleep(5)

    if not episodes:
        log(f"    SKIP no episodes: {slug}")
        return []

    # Retry des épisodes vides — recharge la page une fois
    vides = [i for i, e in enumerate(episodes) if not e["lecteurs"]]
    if vides:
        pause = 15 if len(vides) / len(episodes) > 0.4 else 5
        log(f"    retry {len(vides)} empty ep (pause {pause}s)")
        await asyncio.sleep(pause)

        ctx2  = await new_ctx(browser)
        page2 = await ctx2.new_page()
        try:
            await goto_page(page2, saison_url)
            if not await is_blocked(page2):
                if await wait_select(page2, "#selectEpisodes", timeout=20000):
                    await page2.wait_for_timeout(500)
                    eps_opts2 = await get_options(page2, "#selectEpisodes")
                    for i in vides:
                        if i < len(eps_opts2):
                            ep     = eps_opts2[i]
                            ep_ok2 = False
                            try:
                                await page2.select_option("#selectEpisodes", value=ep["value"])
                                await page2.wait_for_timeout(400)
                                ep_ok2 = True
                            except Exception:
                                ep_ok2 = False
                            if ep_ok2:
                                has_lect2 = await wait_select(page2, "#selectLecteurs", timeout=10000)
                                if has_lect2:
                                    lecteurs2 = await collect_lecteurs(page2)
                                    if lecteurs2:
                                        episodes[i] = {"episode": ep["label"], "lecteurs": lecteurs2}
                                else:
                                    src2 = await wait_player(page2)
                                    if src2:
                                        episodes[i] = {"episode": ep["label"],
                                                       "lecteurs": [{"lecteur": "default", "url": src2}]}
                            await asyncio.sleep(EPISODE_DELAY)
        except Exception as e:
            log(f"    retry error: {e}")
        finally:
            try:
                await ctx2.close()
            except Exception:
                pass

    ok = sum(1 for e in episodes if e["lecteurs"])
    log(f"    {ok}/{len(episodes)} OK [{slug}]")
    return episodes

# ══════════════════════════════════════════════════════════════
#  SCRAPING — Saison complète (IDs + épisodes)
# ══════════════════════════════════════════════════════════════
async def process_saison(browser, session, anime_nom, anime_lien, saison, langues_anime):
    titre   = saison["titreVignette"]
    is_film = saison.get("isFilm", False)
    log(f"  saison: {titre}")

    saison["ids"] = await fetch_ids(session, anime_nom + " " + titre, is_film=is_film)

    url_vf     = build_saison_url(anime_lien, titre, "vf")
    url_vostfr = build_saison_url(anime_lien, titre, "vostfr")
    saison["lien_vf"]     = url_vf
    saison["lien_vostfr"] = url_vostfr

    prefer_vf  = "VF" in [l.upper() for l in langues_anime]
    url_cible  = None
    langue_eff = None

    if prefer_vf and url_vf:
        if await check_url(session, url_vf):
            url_cible, langue_eff = url_vf, "vf"
    if url_cible is None and url_vostfr:
        if await check_url(session, url_vostfr):
            url_cible, langue_eff = url_vostfr, "vostfr"
    if url_cible is None:
        url_cible  = url_vf     if prefer_vf else url_vostfr
        langue_eff = "vf"       if prefer_vf else "vostfr"

    saison["langue"] = langue_eff
    log(f"    langue: {langue_eff}")

    if url_cible:
        try:
            saison["episodes"] = await scrape_saison_episodes(browser, url_cible)
        except Exception as e:
            log(f"    episodes error: {e}")
            saison["episodes"] = []
    else:
        saison["episodes"] = []
    return saison

# ══════════════════════════════════════════════════════════════
#  SCRAPING — Anime complet (séquentiel, sauvegarde immédiate)
# ══════════════════════════════════════════════════════════════
async def process_anime(browser, session, anime, idx, total, page_num):
    nom = anime["nom"]

    # Reprise : déjà scraped ?
    if already_done(nom, page_num):
        log(f"[{idx}/{total}] SKIP (already done): {nom}")
        done = load_done_anime(nom, page_num)
        if done:
            return done
        return anime

    log(f"[{idx}/{total}] {nom}")

    # Détail
    if anime["lien"]:
        try:
            detail = await scrape_detail(browser, anime["lien"])
            anime["image"]         = detail.get("image")
            anime["noms_alt"]      = detail.get("nomsAlt", [])
            anime["synopsis"]      = detail.get("synopsis")
            anime["bande_annonce"] = detail.get("bandeAnnonce")
            saisons = detail.get("saisons", [])
            for s in saisons:
                s["ids"]         = {"jikan_id": None, "tmdb_id": None, "kitsu_id": None}
                s["langue"]      = None
                s["lien_vf"]     = None
                s["lien_vostfr"] = None
                s["episodes"]    = []
            anime["saisons"] = saisons
            log(f"  detail OK — {len(saisons)} saison(s)")
        except Exception as e:
            log(f"  detail error: {e}")

    # IDs
    anime["ids"] = await fetch_ids(session, nom)
    log(f"  ids: jikan={anime['ids']['jikan_id']} tmdb={anime['ids']['tmdb_id']} kitsu={anime['ids']['kitsu_id']}")

    # Saisons — séquentielles
    for s in anime["saisons"]:
        try:
            await process_saison(browser, session, nom, anime["lien"], s, anime.get("langues", []))
        except Exception as e:
            log(f"  saison error ({s.get('titreVignette','')}): {e}")
            s["episodes"] = []

    nb_eps = sum(len(s.get("episodes", [])) for s in anime["saisons"])
    log(f"  DONE {nom} — {len(anime['saisons'])}s {nb_eps}ep")

    # Sauvegarde immédiate dès que l'animé est terminé
    save_anime(anime, page_num)
    return anime

# ══════════════════════════════════════════════════════════════
#  SCRAPING — Page complète (animés séquentiels)
# ══════════════════════════════════════════════════════════════
async def process_page(browser, session, page_num):
    t0 = time.time()
    log(f"=== PAGE {page_num} START ===")

    animes = await scrape_catalogue(browser, page_num)
    log(f"  {len(animes)} animes detected")

    # Traitement séquentiel — 1 animé après l'autre
    for idx, anime in enumerate(animes, 1):
        try:
            result = await process_anime(browser, session, anime, idx, len(animes), page_num)
            animes[idx - 1] = result
        except Exception as e:
            log(f"  ERROR [{idx}] {anime.get('nom','?')}: {e}")

    elapsed = int(time.time() - t0)
    save_page_summary(animes, page_num, elapsed)
    log(f"=== PAGE {page_num} DONE in {elapsed//60}m{elapsed%60:02d}s ===")
    return animes

# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════
async def main():
    log(f"START page {PAGE_NUM} | episode_delay={EPISODE_DELAY}s")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage", "--disable-gpu"]
        )
        connector = aiohttp.TCPConnector(limit=20)
        async with aiohttp.ClientSession(connector=connector) as session:
            await process_page(browser, session, PAGE_NUM)
        await browser.close()

    elapsed = int(time.time() - _start)
    log(f"ALL DONE in {elapsed//60}m{elapsed%60:02d}s")

if __name__ == "__main__":
    asyncio.run(main())
