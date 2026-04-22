#!/usr/bin/env python3
"""
ncanews_monitor.py
==================
Monitora il feed RSS di NCANews.it e invia push notification via Firebase FCM
ogni volta che viene pubblicato un articolo nuovo.

Requisiti:
  pip install feedparser firebase-admin schedule flask

Avvio:
  python ncanews_monitor.py
"""

import feedparser
import firebase_admin
from firebase_admin import credentials, messaging
import schedule
import time
import json
import os
import logging
from datetime import datetime
from flask import Flask, request, jsonify
from threading import Thread

# ─── CONFIGURAZIONE ───────────────────────────────────────────────
RSS_URL       = "https://ncanews.it/feed/"
CHECK_EVERY   = 5          # minuti tra un controllo e l'altro
SEEN_FILE     = "seen_articles.json"
SERVICE_ACCOUNT_FILE = "firebase-service-account.json"  # scarica da Firebase Console
# ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("ncanews")

# Inizializza Firebase Admin SDK
cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
firebase_admin.initialize_app(cred)

app = Flask(__name__)

# ─── GESTIONE TOKEN DISPOSITIVI ───────────────────────────────────

TOKENS_FILE = "tokens.json"

def load_tokens():
    if os.path.exists(TOKENS_FILE):
        with open(TOKENS_FILE) as f:
            return json.load(f)
    return []

def save_tokens(tokens):
    with open(TOKENS_FILE, "w") as f:
        json.dump(list(set(tokens)), f, indent=2)

# Endpoint Flask: la PWA chiama questo per registrare il token FCM
@app.route("/save-token", methods=["POST"])
def save_token():
    data = request.get_json()
    token = data.get("token", "").strip()
    if not token:
        return jsonify({"error": "token mancante"}), 400
    tokens = load_tokens()
    if token not in tokens:
        tokens.append(token)
        save_tokens(tokens)
        log.info(f"Nuovo token registrato. Totale dispositivi: {len(tokens)}")
    return jsonify({"status": "ok"})

@app.route("/")
def index():
    return app.send_static_file("index.html")

# ─── GESTIONE ARTICOLI GIÀ VISTI ──────────────────────────────────

def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()

def save_seen(seen: set):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)

# ─── INVIO NOTIFICA FCM ───────────────────────────────────────────

def send_notification(title: str, body: str, link: str, image: str = None):
    tokens = load_tokens()
    if not tokens:
        log.warning("Nessun dispositivo registrato. Notifica non inviata.")
        return

    notification = messaging.Notification(
        title=title,
        body=body,
        image=image
    )
    android_config = messaging.AndroidConfig(
        notification=messaging.AndroidNotification(
            icon="icon-192",
            color="#c0392b",
            click_action="OPEN_ARTICLE"
        )
    )
    apns_config = messaging.APNSConfig(
        payload=messaging.APNSPayload(
            aps=messaging.Aps(badge=1, sound="default")
        )
    )

    multicast = messaging.MulticastMessage(
        tokens=tokens,
        notification=notification,
        android=android_config,
        apns=apns_config,
        data={"link": link}
    )

    try:
        response = messaging.send_each_for_multicast(multicast)
        log.info(f"Notifiche inviate: {response.success_count} ok, {response.failure_count} fallite")

        # Rimuovi token non validi
        if response.failure_count > 0:
            valid_tokens = []
            for idx, resp in enumerate(response.responses):
                if resp.success:
                    valid_tokens.append(tokens[idx])
                else:
                    log.warning(f"Token rimosso (invalido): {tokens[idx][:20]}…")
            save_tokens(valid_tokens)

    except Exception as e:
        log.error(f"Errore invio notifica: {e}")

# ─── CONTROLLO RSS ────────────────────────────────────────────────

def check_rss():
    log.info(f"Controllo feed RSS: {RSS_URL}")
    try:
        feed = feedparser.parse(RSS_URL)
    except Exception as e:
        log.error(f"Errore parsing RSS: {e}")
        return

    if feed.bozo:
        log.warning("Feed RSS con errori di parsing (potrebbe funzionare lo stesso)")

    seen = load_seen()
    new_articles = []

    for entry in feed.entries:
        article_id = entry.get("id") or entry.get("link", "")
        if article_id not in seen:
            new_articles.append(entry)
            seen.add(article_id)

    if not new_articles:
        log.info("Nessun articolo nuovo.")
        return

    log.info(f"Trovati {len(new_articles)} articoli nuovi!")

    for entry in reversed(new_articles):  # dal più vecchio al più recente
        title = entry.get("title", "Nuovo articolo")
        link  = entry.get("link", "https://ncanews.it")

        # Breve estratto come corpo della notifica
        summary = entry.get("summary", "")
        if summary:
            import re
            summary = re.sub(r"<[^>]+>", "", summary)  # rimuovi HTML
            body = summary[:120] + "…" if len(summary) > 120 else summary
        else:
            body = "Leggi l'articolo su NCANews.it"

        # Immagine (se presente nel feed)
        image = None
        if hasattr(entry, "media_content") and entry.media_content:
            image = entry.media_content[0].get("url")
        elif hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
            image = entry.media_thumbnail[0].get("url")

        log.info(f"📰 Nuovo articolo: {title}")
        send_notification(title=title, body=body, link=link, image=image)
        time.sleep(1)  # piccola pausa tra notifiche multiple

    save_seen(seen)

# ─── AVVIO ────────────────────────────────────────────────────────

def run_scheduler():
    """Avvia il controllo periodico RSS in un thread separato."""
    check_rss()  # prima esecuzione immediata
    schedule.every(CHECK_EVERY).minutes.do(check_rss)
    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == "__main__":
    log.info("NCANews Monitor avviato")
    log.info(f"Controllo RSS ogni {CHECK_EVERY} minuti")

    # Avvia scheduler in background
    scheduler_thread = Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

    # Avvia server Flask (serve la PWA + endpoint /save-token)
    app.static_folder = "."  # serve i file della PWA
    app.run(host="0.0.0.0", port=8080, debug=False)
