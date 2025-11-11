# ---------------------- IMPORTY ----------------------
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
CHECK_INTERVAL = 60 * 5  # co 5 minut
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
        except:
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
    patterns = [r'(\d{2})-(\d{2})-(\d{4})', r'(\d{4})-(\d{2})-(\d{2})', r'(\d{2})\.(\d{2})\.(\d{4})']
    for p in patterns:
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

async def download_pdf(session, pdf_url):
    try:
        async with session.get(pdf_url, timeout=30) as r:
            if r.status == 200:
                return await r.read()
    except:
        pass
    return None

async def convert_pdf_to_images(pdf_data, date_str):
    try:
        folder = os.path.join(IMAGES_DIR, date_str or "unknown")
        if not os.path.exists(folder):
            os.makedirs(folder)
        doc = fitz.open("pdf", pdf_data)
        saved = []
        for i in range(doc.page_count):
            page = doc[i]
            pix = page.get_pixmap(matrix=fitz.Matrix(3,3), alpha=False)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            if img.width > 1920:
                ratio = 1920/img.width
                img = img.resize((1920,int(img.height*ratio)), Image.Resampling.LANCZOS)
            path = os.path.join(folder,f"strona_{i+1}.png")
            img.save(path, "PNG", optimize=True)
            saved.append(path)
            img.close()
        doc.close()
        return saved
    except:
        return []

async def fetch_pdf_link(session):
    try:
        async with session.get(URL, timeout=30) as r:
            if r.status != 200:
                return None
            soup = BeautifulSoup(await r.text(), "html.parser")
            a = soup.find("a", string="Zastępstwa")
            if a and a.get("href"):
                href = a["href"]
                if href.startswith("/"):
                    return f"https://kopernikus.pl{href}"
                elif not href.startswith("http"):
                    return f"https://kopernikus.pl/{href}"
                return href
    except:
        pass
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

# ---------------------- BOT ----------------------
intents = discord.Intents.default()
client = discord.Client(intents=intents)
_watch_loop_started = False

@client.event
async def on_ready():
    print(f"🤖 Bot zalogowany jako {client.user}")
    client.loop.create_task(watch_loop())
    client.loop.create_task(start_web_app())

async def watch_loop():
    last_url, last_hash = load_last()
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                link = await fetch_pdf_link(session)
                if not link:
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue
                pdf = await download_pdf(session, link)
                if not pdf:
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue
                h = calculate_pdf_hash(pdf)
                if h == last_hash:
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue
                channel = client.get_channel(CHANNEL_ID)
                if not channel:
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue
                filename = link.split("/")[-1]
                date_str = extract_date_from_filename(filename) or datetime.now().strftime("%Y-%m-%d")
                images = await convert_pdf_to_images(pdf, date_str)
                if images:
                    main_embed = await create_main_embed(link, len(images), date_str)
                    await channel.send(embed=main_embed)
                    for i,img_path in enumerate(images,1):
                        page_embed = await create_page_embed(i,len(images),date_str)
                        with open(img_path,'rb') as f:
                            file_data=f.read()
                        file=discord.File(io.BytesIO(file_data),filename=f"strona_{i}.png")
                        await channel.send(embed=page_embed,file=file)
                        await asyncio.sleep(0.5)
                last_url = link
                last_hash = h
                save_last(last_url,last_hash)
            except Exception as e:
                print("Błąd w watch_loop:",e)
            await asyncio.sleep(CHECK_INTERVAL)

# ---------------------- WEB PANEL ----------------------
# Prosty login sesyjny w pamięci
sessions = set()

async def handle(request):
    # Sprawdzamy sesję
    cookie = request.cookies.get("session")
    if cookie in sessions:
        authenticated = True
    else:
        authenticated = False

    html_form = f"""
    <html>
    <head>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Panel Bota</title>
      <style>
        body{{font-family:sans-serif;background:#f4f4f9;margin:0;padding:0;display:flex;justify-content:center;align-items:center;height:100vh;}}
        .container{{background:white;padding:20px;border-radius:10px;box-shadow:0 0 15px rgba(0,0,0,0.2);width:90%;max-width:500px;}}
        input[type=text],input[type=password]{{width:100%;padding:10px;margin:5px 0;border-radius:5px;border:1px solid #ccc;}}
        input[type=submit]{{padding:10px 20px;border:none;background:#4CAF50;color:white;border-radius:5px;cursor:pointer;}}
        input[type=submit]:hover{{background:#45a049;}}
        p.msg{{color:red;text-align:center;}}
      </style>
    </head>
    <body>
      <div class="container">
        <h2 style="text-align:center;">Panel Bota Discord</h2>
        <form method="post">
          <input type="password" name="password" placeholder="Hasło"><br>
          <input type="text" name="command" placeholder="Komenda do wysłania">
          <input type="submit" value="Wyślij">
        </form>
        <p class="msg">{""}</p>
      </div>
    </body>
    </html>
    """

    if request.method == "POST":
        data = await request.post()
        password = data.get("password","")
        command = data.get("command","").strip()

        if password != WEB_PASSWORD and not authenticated:
            return web.Response(text=html_form.replace('>{""}<','>❌ Błędne hasło!<'),content_type="text/html")
        # dodajemy sesję
        if not authenticated:
            import secrets
            session_id = secrets.token_hex(16)
            sessions.add(session_id)
            response = web.HTTPFound('/')
            response.set_cookie("session",session_id)
            return response
        if command:
            channel = client.get_channel(CHANNEL_ID)
            if channel:
                await channel.send(command)
            # Po wysłaniu odświeżamy stronę (GET), żeby przy reload nie wysyłało komendy
            return web.HTTPFound('/')

    return web.Response(text=html_form,content_type="text/html")

async def start_web_app():
    app = web.Application()
    app.add_routes([web.get('/',handle),web.post('/',handle)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner,'0.0.0.0',8000)
    await site.start()
    print("🌐 Webowy panel działa na porcie 8000")

# ---------------------- URUCHOMIENIE ----------------------
if __name__=="__main__":
    try:
        client.run(TOKEN)
    except Exception as e:
        print("❌ Krytyczny błąd:",e)
