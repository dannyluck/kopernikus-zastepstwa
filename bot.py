import asyncio
import aiohttp
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
from aiohttp import web

# ---------------------- KONFIG ----------------------
TOKEN = os.getenv("DISCORD_TOKEN")  # Token bota Discord
CHANNEL_ID = 1197586532396171334    # ID kanału do wysyłania wiadomości
URL = "https://kopernikus.pl/"
CHECK_INTERVAL = 60 * 5  # Sprawdzanie co 5 minut
SEEN_FILE = "last_pdf.json"
IMAGES_DIR = "images"

WEB_PASSWORD = "piesfiga1"  # Hasło do web panelu

# Tworzymy folder na obrazy jeśli nie istnieje
if not os.path.exists(IMAGES_DIR):
    os.makedirs(IMAGES_DIR)

# ---------------------- FUNKCJE ----------------------
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
    return hashlib.sha256(pdf_data).hexdigest()

def extract_date_from_filename(filename):
    if not filename:
        return None
    date_patterns = [
        r'(\d{2})-(\d{2})-(\d{4})',
        r'(\d{4})-(\d{2})-(\d{2})',
        r'(\d{2})\.(\d{2})\.(\d{4})',
        r'(\d{2})/(\d{2})/(\d{4})',
    ]
    for pattern in date_patterns:
        match = re.search(pattern, filename)
        if match:
            try:
                if len(match.group(1)) == 4:
                    return f"{match.group(3)}.{match.group(2)}.{match.group(1)}"
                else:
                    return f"{match.group(1)}.{match.group(2)}.{match.group(3)}"
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

# ---------------------- BOT DISCORD ----------------------
intents = discord.Intents.default()
intents.message_content = False
client = discord.Client(intents=intents)

_watch_loop_started = False

@client.event
async def on_ready():
    print(f"🤖 Bot zalogowany jako {client.user}")
    print(f"📡 Monitorowanie: {URL}")
    print(f"💬 Kanał: {CHANNEL_ID}")
    print(f"📁 Folder obrazów: {IMAGES_DIR}")
    print("✅ Rozpoczynanie monitorowania...")
    client.loop.create_task(watch_loop())
    client.loop.create_task(start_web_app())

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
                
                pdf_data = await download_pdf(session, pdf_link)
                if not pdf_data:
                    print("❌ Nie udało się pobrać pliku PDF")
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue
                
                current_hash = calculate_pdf_hash(pdf_data)
                if current_hash == last_seen_hash:
                    print(f"📋 Brak nowych plików (ten sam hash).")
                    consecutive_errors = 0
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue
                
                print(f"🆕 NOWY PLIK! Hash się zmienił.")
                channel = client.get_channel(CHANNEL_ID)
                if not channel:
                    print(f"❌ Nie można znaleźć kanału o ID: {CHANNEL_ID}")
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue
                
                filename = pdf_link.split("/")[-1]
                date_str = extract_date_from_filename(filename) or datetime.now().strftime("%Y-%m-%d")
                
                image_paths = await convert_pdf_to_images(pdf_data, date_str)
                
                if image_paths:
                    main_embed = await create_main_embed(pdf_link, len(image_paths), date_str)
                    await channel.send(embed=main_embed)
                    for i, image_path in enumerate(image_paths, start=1):
                        page_embed = await create_page_embed(i, len(image_paths), date_str)
                        with open(image_path, 'rb') as f:
                            file_data = f.read()
                        image_file = discord.File(io.BytesIO(file_data), filename=f"strona_{i}.png")
                        await channel.send(embed=page_embed, file=image_file)
                        await asyncio.sleep(0.5)
                
                last_seen_url = pdf_link
                last_seen_hash = current_hash
                save_last(last_seen_url, last_seen_hash)
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                print(f"❌ Błąd ({consecutive_errors}/{max_errors}): {e}")
            await asyncio.sleep(CHECK_INTERVAL)

# ---------------------- WEB PANEL ----------------------
async def handle(request):
    data = await request.post()
    command = data.get("command", "")
    password = data.get("password", "")
    html_form = """
    <html>
    <head><title>Discord Bot Panel</title></head>
    <body>
    <h2>Panel Bota Discord</h2>
    <form method="post">
        Hasło: <input type="password" name="password"><br>
        Komenda: <input type="text" name="command" style="width:400px">
        <input type="submit" value="Wyślij">
    </form>
    <p style="color:red;">{}</p>
    </body>
    </html>
    """
    if request.method == "POST":
        if password != WEB_PASSWORD:
            return web.Response(text=html_form.format("❌ Błędne hasło!"), content_type='text/html')
        if command.strip() == "":
            return web.Response(text=html_form.format("⚠️ Nie wpisano komendy!"), content_type='text/html')
        channel = client.get_channel(CHANNEL_ID)
        if channel:
            await channel.send(command)
            return web.Response(text=html_form.format(f"✅ Wysłano: {command}"), content_type='text/html')
        else:
            return web.Response(text=html_form.format("❌ Nie znaleziono kanału!"), content_type='text/html')
    return web.Response(text=html_form.format(""), content_type='text/html')

async def start_web_app():
    app = web.Application()
    app.add_routes([web.get('/', handle), web.post('/', handle)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8000)  # port Koyeb wymusza 8000
    await site.start()
    print("🌐 Webowy panel działa na porcie 8000")

# ---------------------- URUCHOMIENIE ----------------------
if __name__ == "__main__":
    try:
        client.run(TOKEN)
    except discord.LoginFailure:
        print("❌ Błędny token Discord! Sprawdź DISCORD_TOKEN.")
    except Exception as e:
        print(f"❌ Krytyczny błąd: {e}")
