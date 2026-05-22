#!/usr/bin/env python3
"""
Synchronise les nouvelles sorties anime-sama.to (section #containerAjoutsAnimes)
vers Firestore (collections animes + saison_chunks).

Réutilise le scraping Playwright de scraper.py.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from playwright.async_api import async_playwright

import scraper as sama
from firebase_sync import (
    FirestoreSync,
    build_saison_id,
    init_firestore,
    segment_to_titre_vignette,
)

BASE_URL = sama.BASE_URL
STATE_FILE = Path(__file__).resolve().parent / "sync_state.json"
RELEASE_URL_RE = re.compile(
    r"/catalogue/([^/]+)/([^/]+)/([^/]+)/?$", re.IGNORECASE
)


def log(msg: str):
    sama.log(msg)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with STATE_FILE.open(encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"processed": {}, "last_run": None}


def save_state(state: dict):
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def parse_release_url(url: str) -> dict | None:
    path = urlparse(url).path
    m = RELEASE_URL_RE.search(path)
    if not m:
        return None
    slug, segment, langue = m.group(1), m.group(2), m.group(3).lower()
    titre_v = segment_to_titre_vignette(segment)
    return {
        "url": url,
        "anime_id": slug,
        "segment": segment,
        "langue": langue,
        "titre_vignette": titre_v,
        "titre_complet": titre_v,
        "catalogue_url": f"{BASE_URL}/catalogue/{slug}/",
    }


async def scrape_home_releases(browser) -> list[dict]:
    """Lit #containerAjoutsAnimes sur la page d'accueil."""
    ctx = await sama.new_ctx(browser)
    page = await ctx.new_page()
    releases: list[dict] = []
    try:
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_selector("#containerAjoutsAnimes", timeout=20000)
        await page.wait_for_timeout(1500)

        raw_links = await page.evaluate(
            """() => {
            const root = document.querySelector('#containerAjoutsAnimes');
            if (!root) return [];
            const cards = root.querySelectorAll(
              '.anime-card-premium.card-base.flex.shrink-0, .anime-card-premium'
            );
            const out = [];
            const seen = new Set();
            cards.forEach(card => {
              card.querySelectorAll('a[href*="/catalogue/"]').forEach(a => {
                const href = a.getAttribute('href');
                if (!href || seen.has(href)) return;
                const full = href.startsWith('http') ? href : (location.origin + href);
                if (full.match(/\\/catalogue\\/[^/]+\\/[^/]+\\/[^/]+\\/?$/)) {
                  seen.add(href);
                  const name = card.querySelector('h2, h3, .card-title')?.innerText?.trim() || '';
                  out.push({ href: full, name });
                }
              });
            });
            return out;
          }"""
        )

        for item in raw_links:
            parsed = parse_release_url(item["href"])
            if parsed:
                parsed["card_name"] = item.get("name", "")
                releases.append(parsed)
    except Exception as e:
        log(f"home scrape error: {e}")
    finally:
        try:
            await ctx.close()
        except Exception:
            pass

    # Dédupliquer par (anime_id, segment, langue)
    unique: dict[str, dict] = {}
    for r in releases:
        key = f"{r['anime_id']}|{r['segment']}|{r['langue']}"
        unique[key] = r
    result = list(unique.values())
    log(f"  {len(result)} sortie(s) détectée(s) sur l'accueil")
    return result


async def scrape_catalogue_title(browser, catalogue_url: str) -> str:
    ctx = await sama.new_ctx(browser)
    page = await ctx.new_page()
    try:
        await sama.goto_page(page, catalogue_url)
        return (
            await page.evaluate(
                "() => document.querySelector('h1')?.innerText?.trim() || ''"
            )
            or ""
        )
    except Exception:
        return ""
    finally:
        try:
            await ctx.close()
        except Exception:
            pass


async def scrape_full_anime(
    browser,
    session,
    catalogue_url: str,
    card_name: str = "",
    prefer_langue: str = "vostfr",
) -> dict:
    """Scrape un animé complet (toutes saisons + épisodes) comme scraper.py."""
    titre = card_name or await scrape_catalogue_title(browser, catalogue_url)
    anime = {
        "nom": titre or catalogue_url.rstrip("/").split("/")[-1].replace("-", " ").title(),
        "type": None,
        "genres": [],
        "langues": [],
        "lien": catalogue_url,
        "image": None,
        "noms_alt": [],
        "synopsis": None,
        "bande_annonce": None,
        "ids": {"jikan_id": None, "tmdb_id": None, "kitsu_id": None},
        "saisons": [],
    }

    detail = await sama.scrape_detail(browser, catalogue_url)
    anime["image"] = detail.get("image")
    anime["noms_alt"] = detail.get("nomsAlt", [])
    anime["synopsis"] = detail.get("synopsis")
    anime["bande_annonce"] = detail.get("bandeAnnonce")

    if not card_name and detail.get("saisons"):
        pass  # nom reste slug humanisé si pas de titre sur carte

    saisons = detail.get("saisons", [])
    for s in saisons:
        s["ids"] = {"jikan_id": None, "tmdb_id": None, "kitsu_id": None}
        s["langue"] = None
        s["lien_vf"] = None
        s["lien_vostfr"] = None
        s["episodes"] = []
    anime["saisons"] = saisons

    anime["ids"] = await sama.fetch_ids(session, anime["nom"])
    langues = anime.get("langues") or []
    if not langues:
        langues = ["VF"] if prefer_langue == "vf" else ["VOSTFR"]
        if prefer_langue == "vf":
            langues.append("VOSTFR")
        else:
            langues.append("VF")
    anime["langues"] = langues

    for s in anime["saisons"]:
        await sama.process_saison(browser, session, anime["nom"], anime["lien"], s, langues)

    return anime


async def process_release(
    browser,
    session,
    fs: FirestoreSync,
    release: dict,
    state: dict,
    force: bool,
) -> dict:
    """Traite une sortie : mise à jour partielle ou création complète."""
    url = release["url"]
    anime_id = release["anime_id"]
    state_key = f"{anime_id}|{release['segment']}|{release['langue']}"

    if not force and state.get("processed", {}).get(state_key) == url:
        log(f"  SKIP (déjà traité): {state_key}")
        return {"status": "skipped", "anime_id": anime_id}

    saison_id = build_saison_id(anime_id, release["titre_vignette"], release["langue"])
    existing = fs.get_anime(anime_id)

    if existing:
        log(f"  EXISTE dans Firebase: {anime_id} — sync épisodes {saison_id}")
        episodes = await sama.scrape_saison_episodes(browser, url)
        if not episodes:
            return {"status": "no_episodes", "anime_id": anime_id}

        added = fs.append_episodes(
            anime_id,
            saison_id,
            release["titre_vignette"],
            release["titre_complet"],
            release["langue"],
            episodes,
        )
        log(f"  +{added} épisode(s) pour {saison_id}")
        state.setdefault("processed", {})[state_key] = url
        return {"status": "updated", "anime_id": anime_id, "added": added}
    else:
        log(f"  NOUVEAU: {anime_id} — scrape complet")
        anime_data = await scrape_full_anime(
            browser,
            session,
            release["catalogue_url"],
            release.get("card_name", ""),
            prefer_langue=release["langue"],
        )
        if release.get("card_name"):
            anime_data["nom"] = release["card_name"]

        # Infos type/genres depuis la page catalogue si possible
        if not anime_data.get("type"):
            anime_data["type"] = "Anime"

        fs.write_full_anime(anime_id, anime_data)
        nb_eps = sum(len(s.get("episodes", [])) for s in anime_data.get("saisons", []))
        log(f"  créé {anime_id} — {len(anime_data.get('saisons', []))} saison(s), {nb_eps} ep")
        state.setdefault("processed", {})[state_key] = url
        return {
            "status": "created",
            "anime_id": anime_id,
            "saisons": len(anime_data.get("saisons", [])),
            "episodes": nb_eps,
        }


async def run(dry_run: bool, force: bool):
    t0 = time.time()
    log("=== SYNC NOUVELLES SORTIES ===")
    if dry_run:
        log("MODE dry-run (aucune écriture Firestore)")

    state = load_state()
    results: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        connector = aiohttp.TCPConnector(limit=10)
        async with aiohttp.ClientSession(connector=connector) as session:
            releases = await scrape_home_releases(browser)

            if dry_run:
                for r in releases:
                    exists = "?"
                    log(
                        f"  - {r['anime_id']} | {r['titre_vignette']} ({r['langue']}) -> {r['url']}"
                    )
                await browser.close()
                return

            db = init_firestore()
            fs = FirestoreSync(db)

            for i, release in enumerate(releases, 1):
                log(f"[{i}/{len(releases)}] {release['anime_id']}")
                try:
                    res = await process_release(
                        browser, session, fs, release, state, force
                    )
                    results.append(res)
                except Exception as e:
                    log(f"  ERROR: {e}")
                    results.append(
                        {"status": "error", "anime_id": release["anime_id"], "error": str(e)}
                    )

        await browser.close()

    if not dry_run:
        save_state(state)
    elapsed = int(time.time() - t0)
    created = sum(1 for r in results if r.get("status") == "created")
    updated = sum(1 for r in results if r.get("status") == "updated")
    log(f"=== DONE {elapsed}s | créés={created} mis_à_jour={updated} ===")


def main():
    parser = argparse.ArgumentParser(description="Sync nouvelles sorties anime-sama → Firestore")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Affiche les sorties sans écrire dans Firestore",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Retraite même si l'URL a déjà été synchronisée",
    )
    args = parser.parse_args()

    if not args.dry_run and not (
        os.environ.get("FIREBASE_SERVICE_ACCOUNT")
        or os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
        or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    ):
        print(
            "ERROR: configurez FIREBASE_SERVICE_ACCOUNT (chemin) ou "
            "FIREBASE_SERVICE_ACCOUNT_JSON (contenu JSON) ou GOOGLE_APPLICATION_CREDENTIALS",
            file=sys.stderr,
        )
        return 2

    try:
        asyncio.run(run(args.dry_run, args.force))
    except Exception as e:
        log(f"FATAL: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
