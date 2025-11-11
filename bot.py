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
import sys
import hashlib

# Token bota z zmiennych środowiskowych (Koyeb → Environment Variables)
TOKEN = os.getenv("DISCORD_TOKEN")

# Ustawienia (zostawione zgodnie z Twoją prośbą)
CHANNEL_ID = 1197586532396171334
URL = "https://kopernikus.pl/"
CHECK_INTERVAL = 60 * 5  # co 5 minut
SEEN_FILE = "last_pdf.json"
IMAGES_DIR = "images"

# Hasło do panelu webowego (zmień jeśli chcesz)
WEB_PANEL_PASSWORD = "piesfiga1"

# Tworzymy folder na obrazy jeśli nie istnieje
if not os.path.exists(IMAGES_DIR):
    os.makedirs(IMAGES_DIR)

def load_last():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r") as f:
                data = json.load(f)
                return data.get("last", ""), data.get("hash", "")
        except (json.JSONDecodeError, ValueError):
            print("⚠️ Uszkodzony plik last_pdf.json - resetowanie...")
            return "", ""
    return "", ""

def save_last(name, pdf_hash):
    with open(SEEN_FILE, "w") as f:
        json.dump({"last": name, "hash": pdf_hash, "timestamp": datetime.now().isoformat()}, f)

def calculate_pdf_hash(pdf_data):
    """Oblicza hash PDF, aby wykryć czy to ten sam plik"""
    return hashlib.sha256(pdf_data).hexdigest()

def extract_date_from_filename(filename):
    if not filename:
        return None
    date_patterns = [
        r'(\\d{2})-(\\d{2})-(\\d{4})',
        r'(\\d{4})-(\\d{2})-(\\d{2})',
        r'(\\d{2})\\.(\\d{2})\\.(\\d{4})',
        r'(\\d{2})/(\\d{2})/(\\d{4})',
    ]
    for pattern in date_patterns:
        match = re.search(pattern, filename)
        if match:
            try:
                if len(match.group(1)) == 4:
                    return f\"{match.group(3)}.{match.group(2)}.{match.group(1)}\"
                else:
                    return f\"{match.group(1)}.{match.group(2)}.{match.group(3)}\"
            except:
                continue
    return None

async def download_pdf(session, pdf_url):
    try:
        async with session.get(pdf_url, timeout=30) as response:
            if response.status == 200:
                return await response.read()
    except Exception as e:
        print(f"Błąd podczas pobierania PDF: {e}")
    return None

async def convert_pdf_to_images(pdf_data, date_str):
    try:
        date_folder = os.path.join(IMAGES_DIR, date_str or "unknown")
        if not os.path.exists(date_folder):
            os.makedirs(date_folder)
        pdf_document = fitz.open("pdf", pdf_data)
        saved_images = []
        for page_num in range(pdf_document.page_count):
            page = pdf_document[page_num]
            matrix = fitz.Matrix(3.0, 3.0)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            img_bytes = pix.tobytes("png")
            image = Image.open(io.BytesIO(img_bytes))
            if image.width > 1920:
                ratio = 1920 / image.width
                new_height = int(image.height * ratio)
                image = image.resize((1920, new_height), Image.Resampling.LANCZOS)
            filename = f"strona_{page_num + 1}.png"
            filepath = os.path.join(date_folder, filename)
            image.save(filepath, 'PNG', optimize=True)
            saved_images.append(filepath)
            print(f"✅ Zapisano obraz: {filepath}")
            pix = None
            image.close()
        pdf_document.close()
        return saved_images
    except Exception as e:
        print(f"❌ Błąd podczas konwersji PDF: {e}")
        return []

async def fetch_pdf_link(session):
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
        return None
    except Exception as e:
        print(f"Błąd podczas pobierania linku: {e}")
        return None

async def create_main_embed(pdf_link, total_pages, date_str):
    embed = discord.Embed(
        title="📋 Nowe zastępstwa",
        description="Dostępne są nowe zastępstwa.",
        color=0x00ff00,
        timestamp=datetime.now(timezone.utc)
    )
    if date_str:
        embed.add_field(name="📅 Data zastępstw", value=date_str, inline=True)
    embed.add_field(name="🔗 Link do pobrania", value=f"[Otwórz PDF]({pdf_link})", inline=False)
    if total_pages:
        embed.add_field(name="🖼️ Liczba stron", value=f"{total_pages} stron", inline=True)
    embed.set_footer(text="Zastępstwa | Kopernikus", icon_url="https://cdn.discordapp.com/embed/avatars/0.png")
    return embed

async def create_page_embed(page_number, total_pages, date_str):
    embed = discord.Embed(
        title=f"📄 Strona {page_number}/{total_pages}",
        color=0x0099ff,
        timestamp=datetime.now(timezone.utc)
    )
    if date_str:
        embed.add_field(name="📅 Data", value=date_str, inline=True)
    embed.add_field(name="📄 Strona", value=f"{page_number} z {total_pages}", inline=True)
    embed.set_image(url=f"attachment://strona_{page_number}.png")
    embed.set_footer(text=f"Zastępstwa | Strona {page_number}",
                     icon_url="https://cdn.discordapp.com/embed/avatars/0.png")
    return embed

intents = discord.Intents.default()
intents.message_content = True  # potrzebne do nasłuchiwania treści wiadomości
intents.members = False
intents.presences = False
client = discord.Client(intents=intents)

# Flaga zapobiegająca wielokrotnemu uruchomieniu watch_loop
_watch_loop_started = False

@client.event
async def on_ready():
    global _watch_loop_started
    print(f"🤖 Bot zalogowany jako {client.user}")
    print(f"📡 Monitorowanie: {URL}")
    print(f"💬 Kanał: {CHANNEL_ID}")
    print(f"📁 Folder obrazów: {IMAGES_DIR}")
    print("✅ Rozpoczynanie monitorowania...")
    if not _watch_loop_started:
        client.loop.create_task(watch_loop())
        _watch_loop_started = True
    # Uruchamiamy prosty panel webowy (aiohttp) na porcie 8080
    client.loop.create_task(start_web_panel())

async def watch_loop():
    last_seen_url, last_seen_hash = load_last()
    consecutive_errors = 0
    max_errors = 5
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                print(f"🔍 Sprawdzanie o {datetime.now().strftime('%H:%M:%S')}...")
                pdf_link = await fetch_pdf_link(session)
                
                if not pdf_link:
                    print("📋 Brak linku do PDF.")
                    consecutive_errors = 0
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue
                
                # Pobierz PDF i oblicz hash
                print(f"📥 Pobieranie PDF: {pdf_link}")
                pdf_data = await download_pdf(session, pdf_link)
                
                if not pdf_data:
                    print("❌ Nie udało się pobrać pliku PDF")
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue
                
                current_hash = calculate_pdf_hash(pdf_data)
                print(f"🔐 Hash PDF: {current_hash[:16]}...")
                
                # Sprawdź czy to nowy plik (porównanie po hash, nie po URL)
                if current_hash == last_seen_hash:
                    print(f"📋 Brak nowych plików (ten sam hash).")
                    consecutive_errors = 0
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue
                
                # Nowy plik wykryty!
                print(f"🆕 NOWY PLIK! Hash się zmienił.")
                try:
                    channel = client.get_channel(CHANNEL_ID)
                    if channel is None:
                        channel = await client.fetch_channel(CHANNEL_ID)
                except Exception as e:
                    print(f"❌ Nie można znaleźć kanału o ID: {CHANNEL_ID} - {e}")
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue
                
                filename = pdf_link.split("/")[-1]
                date_str = extract_date_from_filename(filename)
                if not date_str:
                    date_str = datetime.now().strftime("%Y-%m-%d")
                
                print("🔄 Konwertowanie PDF na obrazy...")
                image_paths = await convert_pdf_to_images(pdf_data, date_str)
                
                if image_paths:
                    print("📤 Wysyłanie głównego embeda...")
                    main_embed = await create_main_embed(pdf_link, len(image_paths), date_str)
                    await channel.send(embed=main_embed)
                    
                    print(f"📤 Wysyłanie {len(image_paths)} stron jako embedy...")
                    for i, image_path in enumerate(image_paths, start=1):
                        try:
                            page_embed = await create_page_embed(i, len(image_paths), date_str)
                            filename = f"strona_{i}.png"
                            with open(image_path, 'rb') as f:
                                file_data = f.read()
                            image_file = discord.File(io.BytesIO(file_data), filename=filename)
                            await channel.send(embed=page_embed, file=image_file)
                            print(f"✅ Wysłano stronę {i}/{len(image_paths)}")
                            if i < len(image_paths):
                                await asyncio.sleep(0.5)
                        except Exception as e:
                            print(f"❌ Błąd wysyłania strony {i}: {e}")
                    print(f"✅ Wysłano wszystkie {len(image_paths)} stron")
                else:
                    main_embed = await create_main_embed(pdf_link, 0, date_str)
                    await channel.send(embed=main_embed)
                    print("✅ Wysłano główny embed bez obrazów")
                
                print(f"✅ Wysłano powiadomienie o nowym pliku")
                
                # Zapisz nowy hash
                last_seen_url = pdf_link
                last_seen_hash = current_hash
                save_last(last_seen_url, last_seen_hash)
                consecutive_errors = 0
                
            except Exception as e:
                consecutive_errors += 1
                print(f"❌ Błąd ({consecutive_errors}/{max_errors}): {e}")
                if consecutive_errors >= max_errors:
                    try:
                        channel = client.get_channel(CHANNEL_ID)
                        if channel:
                            error_embed = discord.Embed(
                                title="⚠️ Problem z Botem",
                                description=f"Bot napotkał {consecutive_errors} błędów z rzędu.",
                                color=0xff0000,
                                timestamp=datetime.now(timezone.utc)
                            )
                            error_embed.add_field(name="Ostatni błąd:", value=f"`{str(e)[:1000]}`", inline=False)
                            await channel.send(embed=error_embed)
                            consecutive_errors = 0
                    except:
                        pass
            
            await asyncio.sleep(CHECK_INTERVAL)

async def start_web_panel():
    # Prosty panel webowy (aiohttp) — dostępny na porcie 8080
    async def index(request):
        html = f"""
        <html><head><meta charset="utf-8"><title>Panel Bota</title></head><body>
        <h2>Panel Bota</h2>
        <form method="post" action="/send">
          <label>Hasło: <input name="password" type="password" /></label><br/><br/>
          <label>Treść wiadomości:<br/><textarea name="message" rows="4" cols="60"></textarea></label><br/><br/>
          <button type="submit">Wyślij do Discorda</button>
        </form>
        <hr/>
        <form method="post" action="/reset">
          <label>Hasło: <input name="password" type="password" /></label>
          <button type="submit">Resetuj zapamiętany hash (treat as new)</button>
        </form>
        <p>Uwaga: użyj hasła panelu, by wysłać wiadomość. Panel działa tylko po uruchomieniu bota.</p>
        </body></html>
        """
        return web.Response(text=html, content_type='text/html; charset=utf-8')

    async def send(request):
        data = await request.post()
        pw = data.get('password','')
        if pw != WEB_PANEL_PASSWORD:
            return web.Response(text="Błędne hasło", status=403)
        message = data.get('message','').strip()
        if not message:
            return web.Response(text="Brak wiadomości", status=400)
        # Wyślij wiadomość do kanału
        try:
            channel = client.get_channel(CHANNEL_ID)
            if channel is None:
                channel = await client.fetch_channel(CHANNEL_ID)
            await channel.send(message)
            return web.Response(text="Wysłano wiadomość!")
        except Exception as e:
            return web.Response(text=f"Błąd podczas wysyłania: {e}", status=500)

    async def reset(request):
        data = await request.post()
        pw = data.get('password','')
        if pw != WEB_PANEL_PASSWORD:
            return web.Response(text="Błędne hasło", status=403)
        save_last("", "")
        return web.Response(text="Zresetowano zapisany hash. Następne sprawdzenie potraktuje plik jako nowy.")

    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_post('/send', send)
    app.router.add_post('/reset', reset)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()
    print("🌐 Panel webowy uruchomiony na porcie 8080")

@client.event
async def on_message(message):
    if message.author == client.user:
        return
    content = message.content.strip()
    if content.lower() == "!status":
        embed = discord.Embed(
            title="🤖 Status Bota",
            color=0x0099ff,
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="✅ Status", value="Online i działający", inline=True)
        embed.add_field(name="🌐 URL", value=URL, inline=True)
        embed.add_field(name="⏱️ Interwał", value=f"{CHECK_INTERVAL}s", inline=True)
        embed.add_field(name="📁 Folder obrazów", value=IMAGES_DIR, inline=True)
        last_seen_url, last_seen_hash = load_last()
        if last_seen_url:
            embed.add_field(name="📄 Ostatni plik", value=f"`{last_seen_url.split('/')[-1]}`", inline=False)
        if last_seen_hash:
            embed.add_field(name="🔐 Hash", value=f"`{last_seen_hash[:16]}...`", inline=False)
        await message.reply(embed=embed)
    elif content.lower() == "!ping":
        await message.channel.send("Pong! 🏓")
