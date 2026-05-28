#!/usr/bin/env python3
"""
new_episode_scrape.py — Tout-en-un
Scrape les nouvelles sorties sur anime-sama.to (#containerAjoutsAnimes)
et met à jour Firestore (animes + saison_chunks) pour OtakuFlix.

Usage:
  python new_episode_scrape.py --dry-run
  python new_episode_scrape.py

GitHub Actions: voir .github/workflows/new_episode_scrape.yml
Secret requis: FIREBASE_SERVICE_ACCOUNT_JSON
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from playwright.async_api import async_playwright

# ─── Config ───────────────────────────────────────────────────
BASE_URL = "https://anime-sama.to"
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "cfc454f98433e15eaa3b67f178fd8774")
TMDB_BASE = "https://api.themoviedb.org/3"
JIKAN_BASE = "https://api.jikan.moe/v4"
KITSU_BASE = "https://kitsu.io/api/edge"
EPISODE_DELAY = float(os.environ.get("EPISODE_DELAY", "0.25"))
JIKAN_DELAY = 0.35
MAX_RETRIES = 3
MAX_EPISODES_PER_CHUNK = 50
STATE_FILE = Path(__file__).resolve().parent / "sync_state.json"
RELEASE_URL_RE = re.compile(r"/catalogue/([^/]+)/([^/]+)/([^/]+)/?$", re.I)

_start = time.time()


def log(msg: str):
    e = int(time.time() - _start)
    print(f"[{e//60:02d}m{e%60:02d}s] {msg}", flush=True)


# ─── Firestore helpers ────────────────────────────────────────
def _strip_diacritics(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def slugify_compact_id(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", _strip_diacritics(str(s)).lower())


def parse_episode_num(episode_title: str, fallback_num: int) -> int:
    m = re.search(r"(\d+)", episode_title or "")
    if not m:
        return fallback_num
    try:
        return int(m.group(1))
    except ValueError:
        return fallback_num


def segment_to_titre_vignette(segment: str) -> str:
    seg = (segment or "").lower().strip()
    if seg.startswith("saison"):
        num = re.search(r"(\d+)", seg)
        return f"Saison {num.group(1) if num else '1'}"
    if seg.startswith("partie"):
        num = re.search(r"(\d+)", seg)
        return f"Partie {num.group(1) if num else '1'}"
    if "film" in seg:
        num = re.search(r"(\d+)", seg)
        return "Film" + (f" {num.group(1)}" if num else "")
    return segment.replace("-", " ").title() or "Saison 1"


def build_saison_id(anime_id: str, titre_vignette: str, langue: str) -> str:
    return f"{anime_id}_{slugify_compact_id(titre_vignette)}_{langue.lower()}"


def scraped_episodes_to_firestore(episodes_in: list, start_num: int = 1) -> list:
    out = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for i, ep in enumerate(episodes_in):
        raw = ep.get("episode", "")
        out.append({
            "num": parse_episode_num(raw, start_num + i),
            "titre": raw,
            "lecteurs": ep.get("lecteurs", []),
            "addedAt": ep.get("addedAt") or now_iso,
        })
    return out


def anime_doc_from_scraped(anime_data: dict, anime_id: str) -> dict:
    saisons_out = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for saison in anime_data.get("saisons", []):
        eps = saison.get("episodes", []) or []
        tv = saison.get("titreVignette", "")
        lang = str(saison.get("langue", "") or "").lower()
        sid = build_saison_id(anime_id, tv, lang)
        saisons_out.append({
            "id": sid,
            "titreVignette": tv,
            "titreComplet": saison.get("titreComplet", tv),
            "langue": lang,
            "parts": math.ceil(len(eps) / MAX_EPISODES_PER_CHUNK) if eps else 0,
            "updatedAt": saison.get("updatedAt") or now_iso,
        })
    return {
        "nom": anime_data.get("nom"),
        "type": anime_data.get("type"),
        "genres": anime_data.get("genres", []),
        "langues": anime_data.get("langues", []),
        "image": anime_data.get("image"),
        "noms_alt": anime_data.get("noms_alt", []),
        "synopsis": anime_data.get("synopsis"),
        "bande_annonce": anime_data.get("bande_annonce"),
        "ids": anime_data.get("ids", {}),
        "saisons": saisons_out,
        "createdAt": anime_data.get("createdAt") or now_iso,
        "updatedAt": now_iso,
    }


def chunks_from_saison(anime_id: str, saison_id: str, episodes_in: list, anime_nom: str = None, anime_image: str = None, anime_ids: dict = None, langue: str = None) -> list:
    if not episodes_in:
        return []
    eps = scraped_episodes_to_firestore(episodes_in)
    writes = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for part_index in range(math.ceil(len(eps) / MAX_EPISODES_PER_CHUNK)):
        part = part_index + 1
        start = part_index * MAX_EPISODES_PER_CHUNK + 1
        end = min((part_index + 1) * MAX_EPISODES_PER_CHUNK, len(eps))
        slice_eps = eps[start - 1 : end]
        writes.append((f"{saison_id}_part{part}", {
            "saisonId": saison_id, "animeId": anime_id, "part": part,
            "start": start, "end": end, "episodes": slice_eps, "count": len(slice_eps),
            "updatedAt": now_iso,
            "animeNom": anime_nom,
            "animeImage": anime_image,
            "animeIds": anime_ids,
            "langue": langue,
        }))
    return writes


def merge_episodes_into_chunks(existing_chunks: list, new_episodes: list, anime_nom: str = None, anime_image: str = None, anime_ids: dict = None, langue: str = None):
    if not new_episodes or not existing_chunks:
        return [], False
    chunks = sorted(existing_chunks, key=lambda c: c.get("part", 0))
    all_eps = []
    for ch in chunks:
        all_eps.extend(ch.get("episodes", []))
    existing_nums = {int(e["num"]) for e in all_eps}
    
    added_any = False
    for ep in new_episodes:
        if int(ep["num"]) not in existing_nums:
            all_eps.append(ep)
            added_any = True
            
    if not added_any:
        return [], False
        
    all_eps.sort(key=lambda e: int(e["num"]))
    sid, aid = chunks[0].get("saisonId", ""), chunks[0].get("animeId", "")
    
    nom = anime_nom or chunks[0].get("animeNom")
    img = anime_image or chunks[0].get("animeImage")
    ids = anime_ids or chunks[0].get("animeIds")
    lang = langue or chunks[0].get("langue")
    
    writes = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for part_index in range(math.ceil(len(all_eps) / MAX_EPISODES_PER_CHUNK)):
        part = part_index + 1
        start = part_index * MAX_EPISODES_PER_CHUNK + 1
        end = min((part_index + 1) * MAX_EPISODES_PER_CHUNK, len(all_eps))
        writes.append((f"{sid}_part{part}", {
            "saisonId": sid, "animeId": aid, "part": part,
            "start": start, "end": end,
            "episodes": all_eps[start - 1 : end], "count": end - start + 1,
            "updatedAt": now_iso,
            "animeNom": nom,
            "animeImage": img,
            "animeIds": ids,
            "langue": lang,
        }))
    return writes, True


def _load_firebase_service_account() -> dict:
    """Charge le JSON service account depuis l'env ou un fichier."""
    raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip().lstrip("\ufeff")
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"FIREBASE_SERVICE_ACCOUNT_JSON invalide: {exc}") from exc

    for key in ("GOOGLE_APPLICATION_CREDENTIALS", "FIREBASE_SERVICE_ACCOUNT"):
        path = os.environ.get(key, "").strip()
        if not path or not os.path.isfile(path):
            continue
        text = Path(path).read_text(encoding="utf-8-sig").strip()
        if not text:
            continue
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Fichier Firebase invalide ({path}): {exc}") from exc

    raise RuntimeError(
        "Credentials Firebase manquants. "
        "Définir FIREBASE_SERVICE_ACCOUNT_JSON (JSON complet) ou FIREBASE_SERVICE_ACCOUNT (chemin fichier)."
    )


def init_firestore():
    import firebase_admin
    from firebase_admin import credentials, firestore

    if firebase_admin._apps:
        return firestore.client()

    cred = credentials.Certificate(_load_firebase_service_account())
    firebase_admin.initialize_app(cred)
    return firestore.client()


class FirestoreSync:
    def __init__(self, db):
        self.db = db

    def get_anime(self, anime_id: str):
        doc = self.db.collection("animes").document(anime_id).get()
        return doc.to_dict() if doc.exists else None

    def get_saison_chunks(self, saison_id: str, parts: int) -> list:
        chunks = []
        for part in range(1, max(parts, 1) + 1):
            doc = self.db.collection("saison_chunks").document(f"{saison_id}_part{part}").get()
            if doc.exists:
                chunks.append(doc.to_dict())
        return chunks

    def upsert(self, collection: str, writes: list, merge: bool = True):
        if not writes:
            return
        col = self.db.collection(collection)
        batch, n = self.db.batch(), 0
        for doc_id, data in writes:
            batch.set(col.document(doc_id), data, merge=merge)
            n += 1
            if n >= 400:
                batch.commit()
                batch, n = self.db.batch(), 0
        if n:
            batch.commit()

    def write_full_anime(self, anime_id: str, anime_data: dict):
        chunk_writes = []
        for saison in anime_data.get("saisons", []):
            eps = saison.get("episodes", []) or []
            if not eps:
                continue
            tv = saison.get("titreVignette", "")
            lang = str(saison.get("langue", "") or "").lower()
            sid = build_saison_id(anime_id, tv, lang)
            chunk_writes.extend(chunks_from_saison(
                anime_id, sid, eps,
                anime_nom=anime_data.get("nom"),
                anime_image=anime_data.get("image"),
                anime_ids=anime_data.get("ids"),
                langue=lang
            ))
        self.upsert("animes", [(anime_id, anime_doc_from_scraped(anime_data, anime_id))], merge=False)
        self.upsert("saison_chunks", chunk_writes, merge=False)

    def append_episodes(self, anime_id, saison_id, titre_v, titre_c, langue, scraped_eps) -> int:
        anime_doc = self.get_anime(anime_id)
        if not anime_doc:
            return 0
        new_eps = scraped_episodes_to_firestore(scraped_eps)
        now_iso = datetime.now(timezone.utc).isoformat()
        
        anime_doc["updatedAt"] = now_iso
        anime_nom = anime_doc.get("nom")
        anime_image = anime_doc.get("image")
        anime_ids = anime_doc.get("ids")
        
        meta = next((s for s in anime_doc.get("saisons", []) if s.get("id") == saison_id), None)
        if meta:
            chunks = self.get_saison_chunks(saison_id, int(meta.get("parts", 0)))
            if not chunks:
                cw = chunks_from_saison(
                    anime_id, saison_id, scraped_eps,
                    anime_nom=anime_nom, anime_image=anime_image,
                    anime_ids=anime_ids, langue=langue
                )
                for s in anime_doc["saisons"]:
                    if s["id"] == saison_id:
                        s["parts"] = len(cw)
                        s["updatedAt"] = now_iso
                self.upsert("animes", [(anime_id, anime_doc)], merge=True)
                self.upsert("saison_chunks", cw, merge=False)
                return len(new_eps)
            cw, changed = merge_episodes_into_chunks(
                chunks, new_eps,
                anime_nom=anime_nom, anime_image=anime_image,
                anime_ids=anime_ids, langue=langue
            )
            if not changed:
                return 0
            np = max(c[1]["part"] for c in cw)
            for s in anime_doc["saisons"]:
                if s["id"] == saison_id:
                    s["parts"] = np
                    s["updatedAt"] = now_iso
            self.upsert("animes", [(anime_id, anime_doc)], merge=True)
            before = sum(len(c.get("episodes", [])) for c in chunks)
            self.upsert("saison_chunks", cw, merge=True)
            after = sum(len(c[1].get("episodes", [])) for c in cw)
            return max(0, after - before)
        
        cw = chunks_from_saison(
            anime_id, saison_id, scraped_eps,
            anime_nom=anime_nom, anime_image=anime_image,
            anime_ids=anime_ids, langue=langue
        )
        anime_doc.setdefault("saisons", []).append({
            "id": saison_id, "titreVignette": titre_v, "titreComplet": titre_c,
            "langue": langue.lower(), "parts": len(cw), "updatedAt": now_iso
        })
        self.upsert("animes", [(anime_id, anime_doc)], merge=True)
        self.upsert("saison_chunks", cw, merge=False)
        return len(new_eps)


# ─── Scraping (Playwright) ────────────────────────────────────
def new_ctx(browser):
    return browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
        locale="fr-FR",
    )


def clean_title(title):
    t = re.sub(r"\s*(saison|season|partie|part|film)\s*\d*", "", title, flags=re.I)
    return re.sub(r"\s*\d+$", "", t).strip()


def slug_from_url(url):
    m = re.search(r"/catalogue/([^/]+)/?$", url or "")
    return m.group(1) if m else None


def build_saison_url(anime_lien, titre, langue):
    slug = slug_from_url(anime_lien)
    if not slug:
        return None
    t = titre.lower().strip()
    if "film" in t:
        num = re.search(r"\d+", t)
        segment = "film" + (num.group() if num else "")
    else:
        s_num = re.search(r"saison\s*(\d+)", t)
        p_num = re.search(r"partie\s*(\d+)", t)
        if s_num:
            segment = "saison" + s_num.group(1) + (("-partie" + p_num.group(1)) if p_num else "")
        elif p_num:
            segment = "partie" + p_num.group(1)
        else:
            num = re.search(r"\d+", t)
            segment = "saison" + (num.group() if num else "1")
    return f"{BASE_URL}/catalogue/{slug}/{segment}/{langue.lower()}/"


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
                f"() => document.querySelector('{selector}')?.options.length || 0"
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
        "() => { const f=document.querySelector('#playerDF'); if(!f) return null;"
        "let s=f.getAttribute('src')||f.getAttribute('data-src');"
        "if(s&&s.length>10&&!s.includes('about:blank'))return s;"
        "for(const el of f.querySelectorAll('iframe,[src],[data-src]')){"
        "const v=el.getAttribute('src')||el.getAttribute('data-src')||'';"
        "if(v.length>10&&!v.includes('about:blank'))return v;} return null; }"
    )


async def wait_player(page, old_src="", timeout=6000):
    for attempt in range(MAX_RETRIES):
        try:
            await page.wait_for_function(
                "(old)=>{const f=document.querySelector('#playerDF');if(!f)return false;"
                "const srcs=[f.getAttribute('src')||'',f.getAttribute('data-src')||'',"
                "...[...f.querySelectorAll('iframe,[src],[data-src]')].map(e=>e.getAttribute('src')||e.getAttribute('data-src')||'')]"
                ".filter(s=>s.length>10&&!s.includes('about:blank'));return srcs.length>0&&srcs[0]!==old;}",
                arg=old_src, timeout=timeout,
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
        if any(x in title for x in ["error", "403", "429", "blocked", "captcha"]):
            return True
        return not await page.evaluate(
            "() => !!document.querySelector('#selectEpisodes,#playerDF,h1')"
        )
    except Exception:
        return True


async def get_jikan_id(session, title, is_film=False):
    query = clean_title(title)
    if not query:
        return None
    await asyncio.sleep(JIKAN_DELAY)
    media = "movie" if is_film else "tv"
    try:
        async with session.get(f"{JIKAN_BASE}/anime?q={query}&type={media}&limit=1",
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = (await r.json()).get("data", []) if r.status == 200 else []
        if not data:
            async with session.get(f"{JIKAN_BASE}/anime?q={query}&limit=1",
                                   timeout=aiohttp.ClientTimeout(total=10)) as r2:
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
            f"{TMDB_BASE}/search/{media}?api_key={TMDB_API_KEY}&query={query}&language=fr-FR&page=1",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            results = (await r.json()).get("results", []) if r.status == 200 else []
            return results[0].get("id") if results else None
    except Exception:
        return None


async def get_kitsu_id(session, title, is_film=False):
    query = clean_title(title)
    if not query:
        return None
    hdrs = {"Accept": "application/vnd.api+json"}
    subtype = "movie" if is_film else "TV"
    try:
        async with session.get(
            f"{KITSU_BASE}/anime?filter[text]={query}&filter[subtype]={subtype}&page[limit]=1",
            headers=hdrs, timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            data = (await r.json()).get("data", []) if r.status == 200 else []
        if not data:
            async with session.get(f"{KITSU_BASE}/anime?filter[text]={query}&page[limit]=1",
                                   headers=hdrs, timeout=aiohttp.ClientTimeout(total=10)) as r2:
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
        async with session.head(url, timeout=aiohttp.ClientTimeout(total=6), allow_redirects=True) as r:
            return r.status == 200
    except Exception:
        return False


async def collect_lecteurs(page):
    lecteurs = []
    for lect in await get_options(page, "#selectLecteurs"):
        try:
            await page.select_option("#selectLecteurs", value=lect["value"])
            await page.wait_for_timeout(300)
        except Exception:
            continue
        src = None
        for wait_ms in (500, 800, 1200, 2000, 3000):
            await page.wait_for_timeout(wait_ms)
            src = await read_player(page)
            if src:
                break
        if src:
            lecteurs.append({"lecteur": lect["label"], "url": src})
    return lecteurs


async def scrape_saison_episodes(browser, saison_url):
    slug = "/".join(saison_url.rstrip("/").split("/")[-2:])
    for attempt in range(MAX_RETRIES):
        ctx = await new_ctx(browser)
        page = await ctx.new_page()
        episodes, success = [], False
        try:
            await goto_page(page, saison_url)
            if await is_blocked(page):
                await asyncio.sleep(10 * (attempt + 1))
            elif await wait_select(page, "#selectEpisodes", timeout=25000):
                await page.wait_for_timeout(500)
                eps_opts = await get_options(page, "#selectEpisodes")
                if eps_opts:
                    log(f"    {len(eps_opts)} ep [{slug}]")
                    for ep in eps_opts:
                        try:
                            await page.select_option("#selectEpisodes", value=ep["value"])
                            await page.wait_for_timeout(400)
                            if await wait_select(page, "#selectLecteurs", timeout=10000):
                                lecteurs = await collect_lecteurs(page)
                            else:
                                src = await wait_player(page)
                                lecteurs = [{"lecteur": "default", "url": src}] if src else []
                            episodes.append({"episode": ep["label"], "lecteurs": lecteurs})
                        except Exception:
                            episodes.append({"episode": ep["label"], "lecteurs": []})
                        await asyncio.sleep(EPISODE_DELAY)
                    success = True
        except Exception as e:
            log(f"    error {slug}: {e}")
        finally:
            await ctx.close()
        if success:
            break
        await asyncio.sleep(5)
    ok = sum(1 for e in episodes if e.get("lecteurs"))
    log(f"    {ok}/{len(episodes)} OK [{slug}]")
    return episodes


async def scrape_detail(browser, url):
    ctx = await new_ctx(browser)
    page = await ctx.new_page()
    result = {}
    try:
        await goto_page(page, url)
        await page.wait_for_timeout(600)
        result = await page.evaluate(
            "() => {"
            "const img=document.querySelector('#coverOeuvre');"
            "const image=img?.getAttribute('src')||img?.getAttribute('data-src')||null;"
            "const alt=document.querySelector('#titreAlter');"
            "const nomsAlt=alt?alt.innerText.trim().split(',').map(s=>s.trim()).filter(Boolean):[];"
            "const syn=document.querySelector('p.text-sm.text-gray-300.leading-relaxed');"
            "const synopsis=syn?.innerText.trim()||null;"
            "const ifr=document.querySelector('#bandeannonce');"
            "const bandeAnnonce=ifr?(ifr.getAttribute('src')||ifr.getAttribute('data-src')):null;"
            "const cont=document.querySelector('.flex.flex-wrap.overflow-y-hidden.justify-start.bg-slate-900.bg-opacity-70.rounded.mt-2.h-auto');"
            "const saisons=[];"
            "if(cont){cont.querySelectorAll('a').forEach(a=>{"
            "let lbl=a.querySelector('.text-white.font-bold.text-center.absolute.w-28')"
            "||a.querySelector('[class*=\"font-bold\"][class*=\"text-center\"]');"
            "const tv=lbl?.innerText.trim()||a.innerText.trim();"
            "const tc=a.getAttribute('title')||a.getAttribute('aria-label')||tv;"
            "if(tv)saisons.push({titreVignette:tv,titreComplet:tc,isFilm:tv.toLowerCase().includes('film')});"
            "});}"
            "return{image,nomsAlt,synopsis,bandeAnnonce,saisons};}"
        )
    except Exception as e:
        log(f"detail error: {e}")
    finally:
        await ctx.close()
    return result


async def process_saison(browser, session, anime_nom, anime_lien, saison, langues_anime):
    titre = saison["titreVignette"]
    is_film = saison.get("isFilm", False)
    saison["ids"] = await fetch_ids(session, anime_nom + " " + titre, is_film=is_film)
    url_vf = build_saison_url(anime_lien, titre, "vf")
    url_vostfr = build_saison_url(anime_lien, titre, "vostfr")
    prefer_vf = "VF" in [l.upper() for l in langues_anime]
    url_cible, langue_eff = None, None
    if prefer_vf and url_vf and await check_url(session, url_vf):
        url_cible, langue_eff = url_vf, "vf"
    elif url_vostfr and await check_url(session, url_vostfr):
        url_cible, langue_eff = url_vostfr, "vostfr"
    else:
        url_cible = url_vf if prefer_vf else url_vostfr
        langue_eff = "vf" if prefer_vf else "vostfr"
    saison["langue"] = langue_eff
    saison["episodes"] = await scrape_saison_episodes(browser, url_cible) if url_cible else []
    return saison


# ─── Accueil + sync ───────────────────────────────────────────
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"processed": {}, "last_run": None}


def save_state(state):
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_release_url(url: str):
    m = RELEASE_URL_RE.search(urlparse(url).path)
    if not m:
        return None
    slug, segment, langue = m.group(1), m.group(2), m.group(3).lower()
    tv = segment_to_titre_vignette(segment)
    return {
        "url": url, "anime_id": slug, "segment": segment, "langue": langue,
        "titre_vignette": tv, "titre_complet": tv,
        "catalogue_url": f"{BASE_URL}/catalogue/{slug}/",
    }


async def scrape_home_releases(browser):
    ctx = await new_ctx(browser)
    page = await ctx.new_page()
    releases = []
    try:
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_selector("#containerAjoutsAnimes", timeout=20000)
        await page.wait_for_timeout(1500)
        raw = await page.evaluate(
            """() => {
            const root = document.querySelector('#containerAjoutsAnimes');
            if (!root) return [];
            const out = [], seen = new Set();
            root.querySelectorAll('.anime-card-premium').forEach(card => {
              card.querySelectorAll('a[href*="/catalogue/"]').forEach(a => {
                const href = a.getAttribute('href');
                if (!href || seen.has(href)) return;
                const full = href.startsWith('http') ? href : location.origin + href;
                if (full.match(/\\/catalogue\\/[^/]+\\/[^/]+\\/[^/]+\\/?$/)) {
                  seen.add(href);
                  out.push({ href: full, name: (card.querySelector('h2,h3,.card-title')?.innerText||'').trim() });
                }
              });
            });
            return out;
          }"""
        )
        for item in raw:
            p = parse_release_url(item["href"])
            if p:
                p["card_name"] = item.get("name", "")
                releases.append(p)
    except Exception as e:
        log(f"home error: {e}")
    finally:
        await ctx.close()
    unique = {f"{r['anime_id']}|{r['segment']}|{r['langue']}": r for r in releases}
    log(f"  {len(unique)} sortie(s) sur l'accueil")
    return list(unique.values())


async def scrape_full_anime(browser, session, catalogue_url, card_name="", prefer_langue="vostfr"):
    ctx = await new_ctx(browser)
    page = await ctx.new_page()
    nom = card_name
    try:
        await goto_page(page, catalogue_url)
        if not nom:
            nom = await page.evaluate("() => document.querySelector('h1')?.innerText?.trim() || ''")
    except Exception:
        pass
    finally:
        await ctx.close()
    anime = {
        "nom": nom or catalogue_url.rstrip("/").split("/")[-1].replace("-", " ").title(),
        "type": "Anime", "genres": [], "langues": [],
        "lien": catalogue_url, "image": None, "noms_alt": [], "synopsis": None,
        "bande_annonce": None, "ids": {"jikan_id": None, "tmdb_id": None, "kitsu_id": None},
        "saisons": [],
    }
    detail = await scrape_detail(browser, catalogue_url)
    anime.update({
        "image": detail.get("image"), "noms_alt": detail.get("nomsAlt", []),
        "synopsis": detail.get("synopsis"), "bande_annonce": detail.get("bandeAnnonce"),
    })
    for s in detail.get("saisons", []):
        s.update({"ids": {"jikan_id": None, "tmdb_id": None, "kitsu_id": None},
                  "langue": None, "episodes": []})
    anime["saisons"] = detail.get("saisons", [])
    anime["ids"] = await fetch_ids(session, anime["nom"])
    langues = ["VF", "VOSTFR"] if prefer_langue == "vf" else ["VOSTFR", "VF"]
    anime["langues"] = langues
    for s in anime["saisons"]:
        await process_saison(browser, session, anime["nom"], anime["lien"], s, langues)
    return anime


async def process_release(browser, session, fs, release, state, force):
    key = f"{release['anime_id']}|{release['segment']}|{release['langue']}"
    sid = build_saison_id(release["anime_id"], release["titre_vignette"], release["langue"])
    
    skip_main = not force and state.get("processed", {}).get(key) == release["url"]
    anime_doc = fs.get_anime(release["anime_id"])
    
    if anime_doc:
        if not skip_main:
            log(f"  UPDATE {release['anime_id']} / {sid}")
            eps = await scrape_saison_episodes(browser, release["url"])
            added = fs.append_episodes(
                release["anime_id"], sid, release["titre_vignette"],
                release["titre_complet"], release["langue"], eps,
            ) if eps else 0
            state.setdefault("processed", {})[key] = release["url"]
        else:
            log(f"  SKIP main release: {key} (Checking other seasons...)")
            
        # Heal any empty seasons
        for saison in anime_doc.get("saisons", []):
            if not skip_main and saison.get("id") == sid:
                continue
            
            s_id = saison.get("id")
            parts = int(saison.get("parts", 0))
            is_empty = False
            if parts == 0:
                is_empty = True
            else:
                chunks = fs.get_saison_chunks(s_id, 1)
                if not chunks:
                    is_empty = True
                    
            if is_empty:
                log(f"    Season {s_id} is empty in Firestore. Re-scraping all episodes...")
                catalogue_url = f"{BASE_URL}/catalogue/{release['anime_id']}/"
                s_url = build_saison_url(catalogue_url, saison.get("titreVignette"), saison.get("langue"))
                if s_url:
                    s_eps = await scrape_saison_episodes(browser, s_url)
                    if s_eps:
                        added_s = fs.append_episodes(
                            release["anime_id"], s_id, saison.get("titreVignette"),
                            saison.get("titreComplet"), saison.get("langue"), s_eps,
                        )
                        log(f"    Scraped and added {added_s} episodes for empty season {s_id}")
        
        return {"status": "skipped" if skip_main else "updated"}
        
    log(f"  CREATE {release['anime_id']}")
    data = await scrape_full_anime(
        browser, session, release["catalogue_url"],
        release.get("card_name", ""), release["langue"],
    )
    if release.get("card_name"):
        data["nom"] = release["card_name"]
    fs.write_full_anime(release["anime_id"], data)
    state.setdefault("processed", {})[key] = release["url"]
    return {"status": "created"}


async def run(dry_run: bool, force: bool):
    log("=== new_episode_scrape ===")
    state = load_state()
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        connector = aiohttp.TCPConnector(limit=10)
        async with aiohttp.ClientSession(connector=connector) as session:
            releases = await scrape_home_releases(browser)
            if dry_run:
                for r in releases:
                    log(f"  {r['anime_id']} | {r['titre_vignette']} ({r['langue']})")
                await browser.close()
                return
            fs = FirestoreSync(init_firestore())
            for i, r in enumerate(releases, 1):
                log(f"[{i}/{len(releases)}] {r['anime_id']}")
                try:
                    await process_release(browser, session, fs, r, state, force)
                except Exception as e:
                    log(f"  ERROR: {e}")
        await browser.close()
    save_state(state)
    log("=== FIN ===")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if not args.dry_run:
        has_json = bool(os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip())
        has_file = any(
            os.path.isfile(os.environ.get(k, ""))
            for k in ("FIREBASE_SERVICE_ACCOUNT", "GOOGLE_APPLICATION_CREDENTIALS")
        )
        if not has_json and not has_file:
            print("ERROR: secret Firebase manquant (FIREBASE_SERVICE_ACCOUNT_JSON)", file=sys.stderr)
            return 2
    asyncio.run(run(args.dry_run, args.force))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
