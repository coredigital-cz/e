"""
scraper_publi24.py — Publi24.ro Lead Scraper
==============================================
URL-uri verificate prin cautare reala (nu ghicite):

  Categorie confirmata 100%:
    https://www.publi24.ro/anunturi/servicii/constructii-amenajari/
    https://www.publi24.ro/anunturi/servicii/constructii-amenajari/prahova/
    https://www.publi24.ro/anunturi/servicii/constructii-amenajari/dolj/craiova/

  Anunt individual confirmat 100% (3 exemple reale gasite):
    /anunturi/servicii/constructii-amenajari/anunt/electrician/78086477796f6050.html
    /anunturi/servicii/constructii-amenajari/anunt/electrician-autorizat/78086073786b6151.html
    /anunturi/servicii/constructii-amenajari/anunt/electricianinstalator/e8d60438dd1h78gidg38eh07h39951h2.html

  Pattern: /anunturi/servicii/{subcategorie}/anunt/{slug}/{hash}.html

IMPORTANT — telefon:
  Publi24 cere LOGIN pentru butonul oficial de "Contacteaza":
  "Pentru a contacta acest utilizator, intra in contul tau Publi24.ro"
  DAR majoritatea meseriasilor scriu numarul direct in textul
  anuntului (ex: "Tel: 0766361596", "Contact: 0722.684.285") ca
  sa evite exact acest gate. De-aceea extragem telefonul prin
  regex din TEXTUL anuntului, nu din mecanismul oficial de contact.
  Anunturile fara telefon in text se sar (la fel ca la bazos.cz).

ATENTIE — subcategorii NEVERIFICATE individual:
  Doar "constructii-amenajari" e confirmat 100% printr-un URL real
  vazut in cautare. Restul slug-urilor de mai jos sunt deduse prin
  acelasi pattern de denumire (lowercase, spatii->liniute), dar NU
  au fost vazute live. Daca una din ele da 404 constant in log,
  verifica manual URL-ul corect din browser si actualizeaza
  _NICHES_PUBLI24 mai jos.
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup, Tag
from sqlalchemy import select

from database import AsyncSessionLocal, Job, JobStatus, init_db

logger = logging.getLogger(__name__)

_PUBLI24_BASE = "https://www.publi24.ro"

# Pattern URL anunt individual — CONFIRMAT prin 3 exemple reale
_LISTING_RE = re.compile(
    r"/anunturi/servicii/[a-z0-9\-]+/anunt/[a-zA-Z0-9\-]+/[a-zA-Z0-9]+\.html"
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ro-RO,ro;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Connection": "keep-alive",
    "DNT": "1",
}

# ================================================================
# Telefoane Romania: +40 7xx xxx xxx
# ================================================================

_PHONE_RE_RO = re.compile(
    r"(?:\+?40[\s\-\.]?)?"
    r"0?"
    r"7[0-9]{2}"
    r"[\s\-\.]?"
    r"[0-9]{3}"
    r"[\s\-\.]?"
    r"[0-9]{3}"
)


def _norm_ro(raw: str) -> str:
    d = re.sub(r"[^\d]", "", raw).lstrip("0")
    if not d.startswith("40"):
        d = "40" + d
    return "+" + d


def _valid_ro(phone: str) -> bool:
    d = re.sub(r"[^\d]", "", phone)
    return len(d) == 11 and d.startswith("40") and d[2] == "7"


def _find_phone_ro(text: str) -> str | None:
    for m in _PHONE_RE_RO.finditer(text):
        p = _norm_ro(m.group(0))
        if _valid_ro(p):
            return p
    return None


# ================================================================
# Lead
# ================================================================

@dataclass
class Lead:
    business_name: str
    phone_number: str
    niche: str
    url: str


# ================================================================
# Niche -> subcategorie slug Publi24
# "constructii-amenajari" = CONFIRMAT printr-un URL real vazut.
# Restul = deduse prin acelasi pattern, NEVERIFICATE individual.
# ================================================================

_NICHES_PUBLI24: list[tuple[str, str]] = [
    ("Constructii si renovari",  "constructii-amenajari"),      # CONFIRMAT
    ("Reparatii si meserii",     "reparatii-electronice-electrocasnice-pc"),
    ("Servicii curatenie",       "menaj-ingrijire-persoane"),
    ("Servicii IT",              "servicii-it"),
    ("Transport si mutari",      "auto-transporturi"),
    ("Contabilitate si juridic", "contabilitate-juridic"),
    ("Cursuri si meditatii",     "cursuri-meditatii"),
    ("Alte servicii",            "alte-servicii"),
]


# ================================================================
# Extrage URL-uri de anunturi dintr-o pagina de categorie
# ================================================================

def _get_listing_urls(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    results: list[str] = []
    seen: set[str] = set()

    for a_tag in soup.find_all("a", href=True):
        if not isinstance(a_tag, Tag):
            continue
        href = str(a_tag.get("href", "")).split("?")[0].split("#")[0]

        if not _LISTING_RE.search(href):
            continue

        if href.startswith("http"):
            url = href
        elif href.startswith("/"):
            url = _PUBLI24_BASE + href
        else:
            continue

        if "publi24.ro" not in url or url in seen:
            continue

        seen.add(url)
        results.append(url)

    logger.info("Gasit %d URL-uri anunt pe pagina", len(results))
    return results


def _find_next_page_url(html: str, current_url: str) -> str | None:
    """
    Cauta link-ul de 'pagina urmatoare' direct din HTML
    (mai sigur decat sa ghicim parametrul de query pentru paginare).
    """
    soup = BeautifulSoup(html, "lxml")

    # Metoda 1: <link rel="next">
    next_link = soup.find("link", rel="next")
    if next_link and isinstance(next_link, Tag):
        href = next_link.get("href", "")
        if href:
            return str(href) if str(href).startswith("http") \
                else _PUBLI24_BASE + str(href)

    # Metoda 2: <a> cu rel="next" sau text/aria-label de "urmatoare"
    for a in soup.find_all("a", href=True):
        if not isinstance(a, Tag):
            continue
        rel = a.get("rel", [])
        aria = str(a.get("aria-label", "")).lower()
        text = a.get_text(strip=True)
        if (
            "next" in rel
            or "urm" in aria
            or text in ("›", "»", "Următoarele anunturi »", "Urmatoarele anunturi »")
        ):
            href = str(a.get("href", ""))
            if href:
                return href if href.startswith("http") else _PUBLI24_BASE + href

    return None


# ================================================================
# Extrage titlu + telefon din pagina de detaliu
# ================================================================

def _parse_publi24_detail(html: str) -> tuple[str, str | None]:
    soup = BeautifulSoup(html, "lxml")

    title = "Prestator servicii"
    h1 = soup.find("h1")
    if h1 and isinstance(h1, Tag):
        t = h1.get_text(strip=True)
        if t and len(t) > 3:
            title = t

    if title == "Prestator servicii":
        og_title = soup.find("meta", {"property": "og:title"})
        if og_title and isinstance(og_title, Tag):
            c = og_title.get("content", "")
            if c:
                title = str(c).strip()

    # Telefon: in marea majoritate a cazurilor e scris direct in
    # textul anuntului de catre meserias (vezi nota din header).
    phone: str | None = None

    # Metoda 1: tel: link (rar, dar posibil)
    for a in soup.find_all("a", href=True):
        href = str(a.get("href", ""))
        if href.startswith("tel:"):
            p = _find_phone_ro(href.replace("tel:", ""))
            if p:
                phone = p
                break

    # Metoda 2: scan in zona descrierii anuntului
    if not phone:
        # cauta in apropierea cuvintelor cheie tipice
        full = soup.get_text(" ", strip=True)
        for kw in ["tel:", "tel.", "telefon", "contact:", "sunati",
                   "apelati", "mobil"]:
            idx = full.lower().find(kw)
            if idx >= 0:
                snippet = full[max(0, idx - 5): idx + 60]
                p = _find_phone_ro(snippet)
                if p:
                    phone = p
                    break

    # Metoda 3: scan complet pagina (fallback)
    if not phone:
        phone = _find_phone_ro(soup.get_text(" ", strip=True))

    return title, phone


# ================================================================
# Scrape o nisa
# ================================================================

async def _scrape_niche_publi24(
    client: httpx.AsyncClient,
    niche: str,
    subcategory_slug: str,
    target: int,
) -> list[Lead]:
    leads: list[Lead] = []
    seen_phones: set[str] = set()
    seen_urls: set[str] = set()

    current_url = f"{_PUBLI24_BASE}/anunturi/servicii/{subcategory_slug}/"
    pages_visited = 0
    max_pages = 6

    while current_url and pages_visited < max_pages:
        if len(seen_urls) >= target * 4:
            break

        try:
            r = await client.get(
                current_url,
                headers=_HEADERS,
                timeout=20.0,
                follow_redirects=True,
            )
            r.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error(
                "Eroare categorie Publi24 '%s' (slug='%s'): %s — "
                "VERIFICA manual URL-ul in browser, slug-ul poate fi gresit",
                niche, subcategory_slug, exc,
            )
            break

        pages_visited += 1
        page_urls = _get_listing_urls(r.text)

        if not page_urls:
            logger.info("Nicio listare pentru '%s' la pagina %d",
                        niche, pages_visited)
            break

        new_urls = [u for u in page_urls if u not in seen_urls]
        seen_urls.update(new_urls)
        logger.info("nisa='%s' pagina=%d -> %d URL-uri noi (total=%d)",
                    niche, pages_visited, len(new_urls), len(seen_urls))

        next_url = _find_next_page_url(r.text, current_url)
        if not next_url or next_url == current_url:
            logger.debug("Nicio pagina urmatoare gasita pentru '%s'", niche)
            break
        current_url = next_url

        await asyncio.sleep(2.5)

    logger.info("nisa='%s': %d URL-uri de vizitat", niche, len(seen_urls))

    # Viziteaza fiecare anunt
    for url in list(seen_urls):
        if len(leads) >= target:
            break

        try:
            dr = await client.get(
                url, headers=_HEADERS, timeout=15.0, follow_redirects=True,
            )
            dr.raise_for_status()
        except httpx.HTTPError as exc:
            logger.debug("Eroare detaliu Publi24 %s: %s", url, exc)
            await asyncio.sleep(1.0)
            continue

        title, phone = _parse_publi24_detail(dr.text)

        if phone and phone not in seen_phones:
            seen_phones.add(phone)
            leads.append(Lead(
                business_name=title,
                phone_number=phone,
                niche=niche,
                url=url,
            ))
            logger.info("LEAD_PUBLI24 [%s]: '%s' -> %s",
                        niche[:20], title[:40], phone)
        else:
            if not phone:
                logger.debug("Niciun telefon in text: %s", url)

        await asyncio.sleep(1.5)

    return leads[:target]


# ================================================================
# Insert in DB
# ================================================================

async def _insert_publi24(leads: list[Lead]) -> int:
    n = 0
    async with AsyncSessionLocal() as session:
        for lead in leads:
            exists = await session.scalar(
                select(Job).where(Job.phone_number == lead.phone_number)
            )
            if exists:
                continue
            session.add(Job(
                business_name=lead.business_name,
                phone_number=lead.phone_number,
                niche=lead.niche,
                language="Romanian",
                status=JobStatus.SCRAPED,
            ))
            n += 1
        await session.commit()
    return n


# ================================================================
# Main
# ================================================================

async def run_scraper_publi24(total: int = 200) -> None:
    await init_db()
    per_niche = max(5, total // len(_NICHES_PUBLI24))
    logger.info("Scraper Publi24: %d nise x %d = %d target",
                len(_NICHES_PUBLI24), per_niche, total)

    grand = 0

    async with httpx.AsyncClient(
        http2=False, follow_redirects=True, timeout=30.0,
    ) as client:
        for niche, slug in _NICHES_PUBLI24:
            logger.info("=== PUBLI24: %s (slug='%s') ===", niche, slug)
            leads = await _scrape_niche_publi24(client, niche, slug, per_niche)

            seen: set[str] = set()
            unique = [l for l in leads
                      if not (l.phone_number in seen
                              or seen.add(l.phone_number))]  # type: ignore

            inserted = await _insert_publi24(unique)
            grand += inserted
            logger.info("PUBLI24 %s: gasit=%d inserat=%d",
                        niche, len(unique), inserted)
            await asyncio.sleep(4.0)

    logger.info("DONE PUBLI24. Total lead-uri noi: %d", grand)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    asyncio.run(run_scraper_publi24(total=200))
