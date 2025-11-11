#!/usr/bin/env python3
import asyncio
import aiohttp
from aiohttp import web
from bs4 import BeautifulSoup
import discord
import os
import json
from datetime import datetime, timezone
import re
import fitz  # PyMuPDF
from PIL import Image
import io
import hashlib
import secrets
import sys

# ---------------------- KONFIG (bez tajnych danych w kodzie) ----------------------
TOKEN = os.getenv("DISCORD_TOKEN")            # MUST be set in Koyeb env vars
WEB_PASSWORD = os.getenv("WEB_PASSWORD")      # MUST be set in Koyeb env vars
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "1197586532396171334"))  # możesz zmienić jako env var
URL = os.getenv("TARGET_URL", "https://kopernikus.pl/")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 60*5))
SEEN_FILE = "last_pdf.json"
IMAGES_DIR = "images"
WEB_PORT = int(os.getenv("PORT", "8000"))    # Koyeb uses 8000

# safety checks
if not TOKEN:
    print("❌ DISCORD_TOKEN not set. Set environment variable DISCORD_TOKEN in Koyeb.")
    sys.exit(1)

# If WEB_PASSWORD not set -> do not start web panel (bot still runs)
WEB_PANEL_ENABLED = bool(WEB_PASSWORD)
if not WEB_PANEL_ENABLED:
    print("⚠️ WEB_PASSWORD not set. Web panel will be disabled. Set WEB_PASSWORD in env vars to enable it.")

# Ensure images directory exists
os.makedirs(IMAGES_DIR, exist_ok=True)

# ---------------------- LAST HASH PERSISTENCE ----------------------
def load_last():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("last", ""), data.get("hash", "")
        except Exception:
            return "", ""
    return "", ""

def save_last(name, pdf_hash):
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump({"last": name, "hash": pdf_hash, "timestamp": datetime.now(timezone.utc).isoformat()}, f)
    except Exception as e:
        print("⚠️ Nie udało się zapisać last_pdf.json:", e)

def calculate_pdf_hash(pdf_data: bytes) -> str:
    return hashlib.sha256(pdf_data).hexdigest()

# ---------------------- PDF / obrazki ----------------------
def extract_date_from_filename(filename: str):
    if not filename:
        return None
    date_patterns = [
        r'(\d{2})-(\d{2})-(\d{4})',
        r'(\d{4})-(\d{2})-(\d{2})',
        r'(\d{2})\.(\d{2})\.(\d{4})',
    ]
    for p in date_patterns:
        m = re.search(p, filename)
        if m:
            try:
                if len(m.group(1)) == 4:
                    return f"{m.group(3)}.{m.group(2)}.{m.group(1)}"
                else:
                    return f"{m.group(1)}.{m.group(2)}.{m.group(3)}"
            except:
                continue
    return None

async def download_pdf(session: aiohttp.ClientSession, pdf_url: str):
    try:
        async with session.get(pdf_url, timeout=30) as resp:
            if resp.status == 200:
                return await resp.read()
    except Exception as e:
        print("Błąd pobierania PDF:", e)
    return None

async def convert_pdf_to_images(pdf_data: bytes, date_str: str):
    try:
        date_folder = os.path.join(IMAGES_DIR, date_str or "unknown")
        os.makedirs(date_folder, exist_ok=True)
        pdf_document = fitz.open("pdf", pdf_data)
        saved = []
        for page_num in range(pdf_document.page_count):
            page = pdf_document[page_num]
            matrix = fitz.Matrix(2.0, 2.0)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            img_bytes = pix.tobytes("png")
            image = Image.open(io.BytesIO(img_bytes))
            if image.width > 1920:
                ratio = 1920 / image.width
                image = image.resize((1920, int(image.height * ratio)), Image.Resampling.LANCZOS)
            filename = f"strona_{page_num+1}.png"
            filepath = os.path.join(date_folder, filename)
            image.save(filepath, "PNG", optimize=True)
            saved.append(filepath)
            image.close()
            pix = None
        pdf_document.close()
        return saved
    except Exception as e:
        print("Błąd konwersji PDF:", e)
        return []

async def fetch_pdf_link(session: aiohttp.ClientSession):
    try:
        async with session.get(URL, timeout=30) as r:
            if r.status != 200:
                return None
            html = await r.text()
        soup = BeautifulSoup(html, "html.parser")
        link = soup.find("a", string="Zastępstwa")
        if link and link.get("href"):
            href = link["href"]
            if href.startswith("/"):
                return f"https://kopernikus.pl{href}"
            elif not href.startswith("http"):
                return f"https://kopernikus.pl/{href}"
            return href
    except Exception as e:
        print("Błąd fetch_pdf_link:", e)
    return None

# ---------------------- Discord bot ----------------------
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

_watch_loop_started = False

@client.event
async def on_ready():
    global _watch_loop_started
    print(f"🤖 Bot zalogowany jako {client.user}")
    if not _watch_loop_started:
        client.loop.create_task(watch_loop())
        _watch_loop_started = True
    if WEB_PANEL_ENABLED:
        client.loop.create_task(start_web_app())

async def watch_loop():
    last_seen_url, last_seen_hash = load_last()
    consecutive_errors = 0
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                print(f"🔍 Sprawdzanie {URL} o {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")
                pdf_link = await fetch_pdf_link(session)
                if not pdf_link:
                    await asyncio.sleep(CHECK_INTERVAL); continue
                pdf_data = await download_pdf(session, pdf_link)
                if not pdf_data:
                    await asyncio.sleep(CHECK_INTERVAL); continue
                current_hash = calculate_pdf_hash(pdf_data)
                if current_hash == last_seen_hash:
                    # nic nowego
                    await asyncio.sleep(CHECK_INTERVAL); continue
                # nowy plik
                print("🆕 Nowy PDF:", pdf_link)
                # pobieramy kanał
                try:
                    channel = client.get_channel(CHANNEL_ID)
                    if channel is None:
                        channel = await client.fetch_channel(CHANNEL_ID)
                except Exception as e:
                    print("Nie można odnaleźć kanału:", e)
                    await asyncio.sleep(CHECK_INTERVAL); continue
                filename = pdf_link.split("/")[-1]
                date_str = extract_date_from_filename(filename) or datetime.now().strftime("%Y-%m-%d")
                image_paths = await convert_pdf_to_images(pdf_data, date_str)
                if image_paths:
                    main_embed = discord.Embed(title="📋 Nowe zastępstwa",
                                               description="Dostępne są nowe zastępstwa.",
                                               color=0x00ff00,
                                               timestamp=datetime.now(timezone.utc))
                    main_embed.add_field(name="📅 Data", value=date_str, inline=True)
                    main_embed.add_field(name="🔗 Link", value=pdf_link, inline=False)
                    main_embed.add_field(name="🖼️ Liczba stron", value=f"{len(image_paths)}", inline=True)
                    await channel.send(embed=main_embed)
                    for i, path in enumerate(image_paths, start=1):
                        try:
                            with open(path, "rb") as f:
                                file = discord.File(io.BytesIO(f.read()), filename=f"strona_{i}.png")
                            page_embed = discord.Embed(title=f"📄 Strona {i}/{len(image_paths)}", color=0x0099ff,
                                                      timestamp=datetime.now(timezone.utc))
                            page_embed.set_image(url=f"attachment://strona_{i}.png")
                            await channel.send(embed=page_embed, file=file)
                            await asyncio.sleep(0.5)
                        except Exception as e:
                            print("Błąd wysyłania obrazka:", e)
                else:
                    # wyślij sam embed informacyjny
                    main_embed = discord.Embed(title="📋 Nowe zastępstwa", description="Nowy plik, lecz brak obrazów.",
                                               color=0x00ff00, timestamp=datetime.now(timezone.utc))
                    main_embed.add_field(name="🔗 Link", value=pdf_link, inline=False)
                    await channel.send(embed=main_embed)
                # zapisz hash
                last_seen_url = pdf_link
                last_seen_hash = current_hash
                save_last(last_seen_url, last_seen_hash)
            except Exception as e:
                consecutive_errors += 1
                print("Błąd w watch_loop:", e)
                if consecutive_errors > 5:
                    consecutive_errors = 0
            await asyncio.sleep(CHECK_INTERVAL)

# ---------------------- WEB PANEL (login once, then command only) ----------------------
# sessions stored in memory (simple). session IDs are random tokens stored server-side.
sessions = set()

LOGIN_HTML = """
<!doctype html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Login - Panel Bota</title>
<style>
  body{font-family:Inter,system-ui,Arial,sans-serif;background:#f0f4f8;margin:0;padding:0;display:flex;align-items:center;justify-content:center;height:100vh}
  .card{width:94%;max-width:420px;background:#fff;padding:22px;border-radius:12px;box-shadow:0 6px 24px rgba(15,23,42,0.08)}
  h2{margin:0 0 10px;font-weight:600;color:#0f172a}
  p.lead{margin:0 0 16px;color:#475569;font-size:14px}
  input{width:100%;padding:12px 10px;margin:8px 0;border:1px solid #e2e8f0;border-radius:8px;font-size:15px}
  button{width:100%;padding:12px;border-radius:8px;border:0;background:#0ea5a4;color:#fff;font-weight:600;cursor:pointer}
  small.info{display:block;margin-top:10px;color:#94a3b8;text-align:center}
</style>
</head>
<body>
  <div class="card">
    <h2>Zaloguj do panelu</h2>
    <p class="lead">Wpisz hasło, aby wysyłać komendy do Discorda.</p>
    <form method="post" action="/login">
      <input name="password" type="password" placeholder="Hasło" required/>
      <button type="submit">Zaloguj</button>
    </form>
    <small class="info">Panel chroniony. Sesja zapamiętywana w ciasteczku.</small>
  </div>
</body>
</html>
"""

PANEL_HTML = """
<!doctype html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Panel Bota</title>
<style>
  body{font-family:Inter,system-ui,Arial,sans-serif;background:linear-gradient(180deg,#f7f9fb,#eef2f6);margin:0;padding:20px}
  .top{max-width:900px;margin:12px auto 18px;display:flex;justify-content:space-between;align-items:center}
  .card{max-width:900px;margin:0 auto;background:#fff;padding:20px;border-radius:12px;box-shadow:0 8px 30px rgba(2,6,23,0.06)}
  h2{margin:0 0 8px;color:#0f172a}
  p.sub{color:#64748b;margin:0 0 18px}
  textarea{width:100%;height:110px;padding:12px;border-radius:10px;border:1px solid #e2e8f0;font-size:15px;resize:vertical}
  .row{display:flex;gap:10px}
  .btn{padding:12px 18px;border-radius:10px;border:0;background:#0ea5a4;color:#fff;font-weight:600;cursor:pointer}
  .btn.secondary{background:#64748b}
  @media(max-width:600px){textarea{height:90px}}
</style>
</head>
<body>
  <div class="top"><div style="max-width:900px;margin:0 auto;width:100%"></div></div>
  <div class="card">
    <h2>Wyślij wiadomość</h2>
    <p class="sub">Wpisz treść i kliknij Wyślij. Odświeżenie strony nie wyśle ponownie wiadomości.</p>
    <form method="post" action="/send" id="sendForm">
      <textarea name="message" placeholder="Wpisz wiadomość..." required></textarea>
      <div style="display:flex;gap:10px;margin-top:12px">
        <button class="btn" type="submit">Wyślij</button>
        <a href="/logout"><button class="btn secondary" type="button">Wyloguj</button></a>
      </div>
    </form>
  </div>
</body>
</html>
"""

async def login_get(request):
    return web.Response(text=LOGIN_HTML, content_type="text/html")

async def login_post(request):
    data = await request.post()
    pw = data.get("password","")
    if not WEB_PASSWORD:
        return web.Response(text="Panel wyłączony na serwerze.", status=503)
    if pw != WEB_PASSWORD:
        # stale pokazujemy login z prostym komunikatem
        body = LOGIN_HTML.replace("<form", '<p style="color:#ef4444">Błędne hasło</p><form', 1)
        return web.Response(text=body, content_type="text/html")
    # ok — wygeneruj sesję i ustaw cookie (secure, httponly, samesite)
    session_id = secrets.token_hex(24)
    sessions.add(session_id)
    resp = web.HTTPFound('/panel')
    resp.set_cookie("session", session_id, httponly=True, secure=True, samesite='Lax', max_age=30*24*3600, path='/')
    return resp

async def panel_get(request):
    cookie = request.cookies.get("session")
    if cookie is None or cookie not in sessions:
        return web.HTTPFound('/')
    return web.Response(text=PANEL_HTML, content_type="text/html")

async def send_post(request):
    cookie = request.cookies.get("session")
    if cookie is None or cookie not in sessions:
        return web.HTTPFound('/')
    data = await request.post()
    msg = data.get("message","").strip()
    if not msg:
        return web.HTTPFound('/panel')
    try:
        channel = client.get_channel(CHANNEL_ID)
        if channel is None:
            channel = await client.fetch_channel(CHANNEL_ID)
        await channel.send(msg)
    except Exception as e:
        print("Błąd wysyłania z panelu:", e)
    # redirect to panel (prevents re-POST on refresh)
    return web.HTTPFound('/panel')

async def logout(request):
    cookie = request.cookies.get("session")
    if cookie and cookie in sessions:
        sessions.discard(cookie)
    resp = web.HTTPFound('/')
    resp.del_cookie("session", path='/')
    return resp

async def start_web_app():
    if not WEB_PANEL_ENABLED:
        print("⚠️ Web panel disabled (WEB_PASSWORD not set).")
        return
    app = web.Application()
    app.add_routes([
        web.get('/', login_get),
        web.post('/login', login_post),
        web.get('/panel', panel_get),
        web.post('/send', send_post),
        web.get('/logout', logout),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEB_PORT)
    await site.start()
    print(f"🌐 Web panel started on port {WEB_PORT}")

# ---------------------- START ----------------------
if __name__ == "__main__":
    try:
        client.run(TOKEN)
    except discord.LoginFailure:
        print("❌ Błędny DISCORD_TOKEN")
    except Exception as e:
        print("❌ Krytyczny błąd:", e)
