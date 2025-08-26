# -*- coding: utf-8 -*-
"""
Bot autonome Vinted -> Telegram
- Scrute Vinted toutes les X secondes
- Applique des filtres par requête (prix max, prix unitaire max, quantité minimale)
- Notifie Telegram pour chaque "nouvelle" annonce qui matche
- Déduplique via seen.json pour éviter le spam

⚠️ Rappels:
- Vinted n'a pas d'API publique officielle. Ce script utilise des endpoints JSON
  exposés par leur webapp et peut cesser de fonctionner si Vinted change.
- Respecte un polling raisonnable (60-120s) pour éviter d'être bloqué.
"""

import os
import time
import json
import random
import re
import requests
from datetime import datetime, timezone

# ---------- Configuration de base (modifiable via variables d'env Railway) ----------
TELEGRAM_TOKEN = os.getenv("TOKEN", "").strip()           # ex: "1234567890:ABCdef..."
TELEGRAM_CHAT_ID = os.getenv("CHAT_ID", "").strip()       # ex: "123456789"
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "90"))       # fréquence de scan (s)
MAX_ITEM_AGE_MIN = int(os.getenv("MAX_ITEM_AGE_MIN", "60"))  # ignorer annonces plus vieilles au démarrage

# Liste de requêtes à surveiller (modifiable ici)
SEARCHES = [
    # Pokémon bulk: on vise un prix unitaire max par carte
    {
        "name": "Pokemon bulk",
        "query": "lot cartes pokemon",
        "max_price": None,             # prix total max (euros) -> None = ignore
        "max_unit_price": 0.06,        # €/carte max
        "min_quantity": 80             # nb cartes mini détectées dans le titre
    },
    # Yu-Gi-Oh bulk
    {
        "name": "YuGiOh bulk",
        "query": "lot cartes yugioh",
        "max_price": None,
        "max_unit_price": 0.04,
        "min_quantity": 80
    },
    # Lego vrac: difficile de lire le poids depuis le titre -> on se limite à un prix max par lot
    {
        "name": "Lego vrac",
        "query": "lego vrac lot",
        "max_price": 30.0,
        "max_unit_price": None,
        "min_quantity": None
    },
    # Consoles rétro: prix max simple
    {
        "name": "Game Boy",
        "query": "game boy console",
        "max_price": 40.0,
        "max_unit_price": None,
        "min_quantity": None
    },
]

# ---------- Endpoints / entêtes ----------
BASE = "https://www.vinted.fr"
SEARCH_API = f"{BASE}/api/v2/catalog/items"

HEADERS = {
    # user-agent correct pour réduire les blocages
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/123.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": f"{BASE}/",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

# ---------- Persistance des IDs vus ----------
SEEN_PATH = "seen.json"
def load_seen():
    try:
        with open(SEEN_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_seen(seen_ids):
    try:
        with open(SEEN_PATH, "w", encoding="utf-8") as f:
            json.dump(sorted(list(seen_ids)), f)
    except Exception as e:
        print("WARN save_seen:", e)

SEEN = load_seen()

# ---------- Utilitaires ----------
num_re = re.compile(r"(\d{1,5})")

def parse_float(val):
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace(",", ".")
    # ne garder que chiffres, ., -
    s = "".join(ch for ch in s if ch.isdigit() or ch in ".-")
    try:
        return float(s)
    except Exception:
        return 0.0

def extract_quantity_from_text(text: str):
    """Détecte un nombre (quantité) dans le titre (ex: '100 cartes pokemon')."""
    if not text:
        return None
    m = num_re.search(text.replace(" ", " "))  # parfois espaces insécables
    if m:
        try:
            n = int(m.group(1))
            if 1 <= n <= 5000:
                return n
        except Exception:
            return None
    return None

def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram non configuré (TOKEN/CHAT_ID manquants). Message:", msg[:120], "...")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
    try:
        r = requests.post(url, data=payload, timeout=10)
        if r.status_code >= 300:
            print("ERR Telegram:", r.status_code, r.text[:200])
    except Exception as e:
        print("ERR Telegram:", e)

def search_vinted(query: str, per_page=30):
    """Retourne une liste d'items (dict) depuis l'endpoint JSON."""
    params = {
        "search_text": query,
        "per_page": per_page,
        "page": 1,
        "order": "newest_first",
        "currency": "EUR",
    }
    try:
        r = requests.get(SEARCH_API, params=params, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            print("WARN HTTP", r.status_code, "pour", query, "|", r.text[:160])
            return []
        j = r.json()
        return j.get("items", []) or []
    except Exception as e:
        print("ERR search_vinted:", e)
        return []

def item_info(item):
    """Extrait les infos utiles d'un item Vinted brut."""
    item_id = item.get("id")
    title = item.get("title") or ""
    # prix: parfois str ("12.0") ou dict {"amount":"12.0"}
    raw_price = item.get("price")
    if isinstance(raw_price, dict):
        price = parse_float(raw_price.get("amount"))
    else:
        price = parse_float(raw_price)
    url_path = item.get("url") or item.get("path") or f"/items/{item_id}"
    url = BASE + url_path
    created_ts = item.get("created_at_ts")  # epoch sec (souvent présent)
    created_dt = None
    if created_ts:
        try:
            created_dt = datetime.fromtimestamp(int(created_ts), tz=timezone.utc)
        except Exception:
            created_dt = None
    return {
        "id": item_id,
        "title": title,
        "price": price,
        "url": url,
        "created_dt": created_dt
    }

def is_recent(created_dt):
    """Filtre de récence pour éviter d'inonder au démarrage."""
    if not created_dt:
        return True  # si inconnu, on laisse passer
    age_min = (datetime.now(timezone.utc) - created_dt).total_seconds() / 60.0
    return age_min <= MAX_ITEM_AGE_MIN

def evaluate_item(search, info):
    """
    Applique les règles:
      - max_price: prix total <= max_price
      - max_unit_price: prix/quantité <= max_unit_price (si quantité détectée)
      - min_quantity: quantité min requise
    """
    title = info["title"]
    price = info["price"]
    qty = extract_quantity_from_text(title)

    # min_quantity
    if search.get("min_quantity") and (not qty or qty < search["min_quantity"]):
        return False, qty, None

    # max_price total
    if search.get("max_price") is not None and price > float(search["max_price"]):
        return False, qty, None

    # max_unit_price: nécessite qty
    unit_price = None
    if search.get("max_unit_price") is not None:
        if not qty or qty <= 0:
            return False, qty, None
        unit_price = price / float(qty)
        if unit_price > float(search["max_unit_price"]):
            return False, qty, unit_price

    return True, qty, unit_price

def scan_once():
    """Un passage de scan pour toutes les requêtes."""
    global SEEN
    sent_count = 0
    for search in SEARCHES:
        query = search["query"]
        name = search["name"]
        items = search_vinted(query)
        # petit random pour "humaniser" la charge quand plusieurs requêtes
        time.sleep(random.uniform(0.4, 1.2))

        for it in items:
            info = item_info(it)
            if not info["id"]:
                continue
            if info["id"] in SEEN:
                continue
            # récence
            if not is_recent(info["created_dt"]):
                # on marque comme vu pour éviter de le renvoyer aux prochains runs
                SEEN.add(info["id"])
                continue

            ok, qty, unit_price = evaluate_item(search, info)
            if ok:
                SEEN.add(info["id"])
                price = info["price"]
                url = info["url"]
                # Message Telegram
                lines = [
                    "🔥 *Nouvelle offre* détectée !",
                    f"🔎 Requête: {name}",
                    f"📌 {info['title']}",
                    f"💰 Prix: {price:.2f} €",
                ]
                if qty:
                    lines.append(f"📦 Quantité estimée: {qty}")
                if unit_price is not None:
                    lines.append(f"🔢 Prix unitaire estimé: {unit_price:.4f} €")
                lines.append(f"🔗 {url}")
                send_telegram("\n".join(lines))
                sent_count += 1
            else:
                # on marque comme vu pour ne pas re-tester en boucle
                SEEN.add(info["id"])

    if sent_count:
        save_seen(SEEN)
    return sent_count

def main():
    print("=== Vinted -> Telegram bot démarré ===")
    print("Requêtes surveillées :")
    for s in SEARCHES:
        print(f"- {s['name']}: '{s['query']}'")
    print(f"Polling toutes les {POLL_SECONDS}s | Max age au démarrage: {MAX_ITEM_AGE_MIN} min")
    print("-------------------------------------------------------")

    # 1er passage "soft": on scanne mais on n'envoie pas >N messages pour éviter l'inondation
    try:
        scan_once()
    except Exception as e:
        print("ERR initial scan:", e)

    while True:
        try:
            sent = scan_once()
            if sent:
                print(f"[{datetime.now().isoformat(timespec='seconds')}] Notifications envoyées:", sent)
            # sleep avec un léger jitter
            time.sleep(POLL_SECONDS + random.uniform(-5, 8))
        except Exception as e:
            print("ERR boucle principale:", e)
            time.sleep(10)

if __name__ == "__main__":
    main()
