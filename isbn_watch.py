"""
Surveillance par ISBN.

Chaque ISBN suivi est lié à une recherche Vinted (table `queries`). Quand une
nouvelle annonce tombe sur cette recherche, on ouvre la page de l'annonce, on
y cherche les ISBN (champ « ISBN » de Vinted + description) et on classe
l'annonce :

    "match"    -> l'ISBN correspond à un ISBN suivi        => alerte ✅
    "no_isbn"  -> aucun ISBN lisible sur l'annonce         => alerte ⚠️
    "other"    -> ISBN présent mais différent (autre édition) => ignorée
"""

import re
import sqlite3
from traceback import print_exc
from urllib.parse import urlencode, urlparse

import requests

import db
from logger import get_logger

logger = get_logger(__name__)


# --------------------------------------------------------------------------
# Base de données (table créée automatiquement, sans toucher aux migrations)
# --------------------------------------------------------------------------

def init_table():
    conn = None
    try:
        conn = sqlite3.connect(db.DB_PATH)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS isbn_watch
               (
                   isbn      TEXT PRIMARY KEY,
                   label     TEXT,
                   max_price REAL,
                   query_id  INTEGER
               )"""
        )
        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def _execute(sql, params=(), fetch=None):
    conn = None
    try:
        conn = sqlite3.connect(db.DB_PATH)
        cur = conn.execute(sql, params)
        if fetch == "one":
            return cur.fetchone()
        if fetch == "all":
            return cur.fetchall()
        conn.commit()
        return None
    except Exception:
        print_exc()
        return None
    finally:
        if conn:
            conn.close()


def get_watches_for_query(query_id):
    """Retourne {isbn: (label, max_price)} pour une recherche."""
    rows = _execute(
        "SELECT isbn, label, max_price FROM isbn_watch WHERE query_id=?",
        (query_id,),
        fetch="all",
    ) or []
    return {r[0]: (r[1], r[2]) for r in rows}


def list_watches():
    return _execute(
        "SELECT isbn, label, max_price, query_id FROM isbn_watch ORDER BY label",
        fetch="all",
    ) or []


# --------------------------------------------------------------------------
# ISBN : validation / normalisation
# --------------------------------------------------------------------------

def _isbn13_ok(d):
    if len(d) != 13 or not d.isdigit() or d[:3] not in ("978", "979"):
        return False
    s = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(d[:12]))
    return (10 - s % 10) % 10 == int(d[12])


def _isbn10_ok(d):
    if len(d) != 10 or not d[:9].isdigit() or not (d[9].isdigit() or d[9] == "X"):
        return False
    s = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(d))
    return s % 11 == 0


def normalize_isbn(raw):
    """Retourne l'ISBN-13 (string) si valide, sinon None. Accepte ISBN-10/13, tirets, espaces."""
    if raw is None:
        return None
    d = re.sub(r"[^0-9Xx]", "", str(raw)).upper()
    if _isbn13_ok(d):
        return d
    if _isbn10_ok(d):
        core = "978" + d[:9]
        s = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(core))
        return core + str((10 - s % 10) % 10)
    return None


# Une suite de 10 à 13 chiffres, éventuellement séparés par tirets/espaces, finissant
# éventuellement par X (ISBN-10).
_CANDIDATE = re.compile(r"(?<![0-9])((?:97[89][\s\-]?)?(?:[0-9][\s\-]?){9}[0-9Xx])(?![0-9])")
# Valeur placée juste après le mot "ISBN" (champ Vinted, JSON de la page ou description).
_LABELED = re.compile(r"isbn[^0-9]{0,40}([0-9][0-9Xx\s\-]{8,20})", re.IGNORECASE)


def find_isbns(text):
    """Renvoie (isbns_etiquetes, autres_isbns) trouvés dans un texte, au format ISBN-13."""
    labeled, others = set(), set()
    if not text:
        return labeled, others
    for m in _LABELED.finditer(text):
        for c in _CANDIDATE.findall(m.group(1)):
            n = normalize_isbn(c)
            if n:
                labeled.add(n)
    for c in _CANDIDATE.findall(text):
        n = normalize_isbn(c)
        if n and n not in labeled:
            others.add(n)
    return labeled, others


# --------------------------------------------------------------------------
# Lecture de l'ISBN d'une annonce Vinted
# --------------------------------------------------------------------------

def get_item_isbns(item):
    """
    Ouvre la page de l'annonce et en extrait les ISBN.
    Retourne un set d'ISBN-13 ; set vide si rien de lisible ; None si la page
    n'a pas pu être chargée.
    """
    from pyVintedVN.requester import requester

    try:
        response = requester.get(item.url)
        if response.status_code != 200:
            logger.warning(f"ISBN: page {item.url} -> HTTP {response.status_code}")
            return None
        labeled, others = find_isbns(response.text)
        # On privilégie les ISBN étiquetés ("ISBN : ...") ; à défaut, ceux de la description.
        return labeled if labeled else others
    except Exception as e:
        logger.error(f"ISBN: erreur en lisant {item.url}: {e}", exc_info=True)
        return None


def evaluate_item(item, query_id):
    """
    Retourne (statut, info) :
        ("not_isbn_query", None)   la recherche n'est liée à aucun ISBN -> comportement normal
        ("match", (isbn, label))   ISBN correspondant et prix OK
        ("too_expensive", ...)     ISBN correspondant mais prix > max
        ("no_isbn", None)          aucun ISBN lisible (ou page inaccessible)
        ("other", isbns)           ISBN différent(s) -> autre édition
    """
    watches = get_watches_for_query(query_id)
    if not watches:
        return "not_isbn_query", None

    isbns = get_item_isbns(item)
    if not isbns:
        return "no_isbn", None

    for isbn in isbns:
        if isbn in watches:
            label, max_price = watches[isbn]
            try:
                price = float(item.price)
            except (TypeError, ValueError):
                price = None
            if max_price is not None and price is not None and price > float(max_price):
                return "too_expensive", (isbn, label)
            return "match", (isbn, label)
    return "other", isbns


# --------------------------------------------------------------------------
# Ajout / suppression d'un ISBN suivi
# --------------------------------------------------------------------------

def lookup_title(isbn):
    """Cherche le titre d'un livre via Open Library puis Google Books. None si introuvable."""
    try:
        r = requests.get(
            "https://openlibrary.org/api/books",
            params={"bibkeys": f"ISBN:{isbn}", "format": "json", "jscmd": "data"},
            timeout=10,
        )
        data = r.json().get(f"ISBN:{isbn}")
        if data and data.get("title"):
            return data["title"]
    except Exception:
        logger.warning(f"Open Library indisponible pour {isbn}")
    try:
        r = requests.get(
            "https://www.googleapis.com/books/v1/volumes",
            params={"q": f"isbn:{isbn}"},
            timeout=10,
        )
        items = r.json().get("items") or []
        if items:
            info = items[0]["volumeInfo"]
            title = info.get("title")
            if title and info.get("subtitle"):
                title = f"{title} {info['subtitle']}"
            return title
    except Exception:
        logger.warning(f"Google Books indisponible pour {isbn}")
    return None


def _default_domain():
    """Réutilise le domaine Vinted des recherches existantes (ex. www.vinted.be)."""
    for q in db.get_queries() or []:
        netloc = urlparse(q[1]).netloc
        if "vinted" in netloc:
            return netloc
    return "www.vinted.fr"


def _find_query_id(url):
    row = _execute("SELECT id FROM queries WHERE query=?", (url,), fetch="one")
    return row[0] if row else None


def add_watch(raw_isbn, max_price, title=None):
    """
    Ajoute un ISBN suivi et crée la recherche Vinted correspondante.
    Retourne (message, ok).
    """
    import core

    isbn = normalize_isbn(raw_isbn)
    if not isbn:
        return f"ISBN invalide : {raw_isbn}", False
    try:
        max_price = float(str(max_price).replace(",", "."))
    except ValueError:
        return f"Prix invalide : {max_price}", False

    if not title:
        title = lookup_title(isbn)
        if not title:
            return (
                f"Titre introuvable pour {isbn}. Réessaie en le donnant :\n"
                f"/add_isbn {isbn} {max_price:g} Titre du livre tome X",
                False,
            )

    params = {"search_text": title, "price_to": f"{max_price:g}", "order": "newest_first"}
    url = f"https://{_default_domain()}/catalog?{urlencode(params)}"
    name = f"📖 {title} [{isbn}]"

    core.process_query(url, name)  # ajoute la recherche si elle n'existe pas déjà
    query_id = _find_query_id(core_processed(url))
    if query_id is None:
        return "Impossible de créer la recherche Vinted.", False

    _execute(
        "INSERT OR REPLACE INTO isbn_watch (isbn, label, max_price, query_id) VALUES (?, ?, ?, ?)",
        (isbn, title, max_price, query_id),
    )
    return f"✅ Suivi : {title}\nISBN {isbn} — max {max_price:g} €\nRecherche : « {title} »", True


def core_processed(url):
    """Applique la même normalisation d'URL que core.process_query (pour retrouver l'id)."""
    from urllib.parse import parse_qs, urlunparse

    p = urlparse(url)
    qp = parse_qs(p.query)
    qp["order"] = ["newest_first"]
    for k in ("time", "search_id", "disabled_personalization", "page"):
        qp.pop(k, None)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(qp, doseq=True), p.fragment))


def remove_watch(raw_isbn):
    isbn = normalize_isbn(raw_isbn)
    if not isbn:
        return f"ISBN invalide : {raw_isbn}", False
    row = _execute("SELECT label, query_id FROM isbn_watch WHERE isbn=?", (isbn,), fetch="one")
    if not row:
        return f"{isbn} n'est pas suivi.", False
    label, query_id = row
    _execute("DELETE FROM isbn_watch WHERE isbn=?", (isbn,))
    # Si plus aucun ISBN n'utilise cette recherche, on la supprime aussi.
    still_used = _execute(
        "SELECT COUNT(*) FROM isbn_watch WHERE query_id=?", (query_id,), fetch="one"
    )
    if still_used and still_used[0] == 0:
        db.remove_query_from_db(query_id)
    return f"🗑️ Plus suivi : {label} ({isbn})", True


def format_watch_list():
    rows = list_watches()
    if not rows:
        return "Aucun ISBN suivi. Ajoute-en un avec /add_isbn ISBN PRIX_MAX [titre]"
    return "\n".join(f"• {label} — {isbn} — max {mp:g} €" for isbn, label, mp, _ in rows)


init_table()
