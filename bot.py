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

# === KONFIG ===
CHANNEL_ID = 1197586532396171334
URL = "https://kopernikus.pl/"
CHECK_INTERVAL = 60 * 5  # 5 minut
SEEN_FILE = "last_pdf.json"
IMAGES_DIR = "images"

# === TOKEN Z ENV ===
TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    print("❌ Brak zmiennej środowiskowej DISCORD_TOKEN")
    exit(1)

os.makedirs(IMAGES_DIR, exist_ok=True)

def load_last():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r") as f:
                data = json.load(f)
                return data.get("hash", "")
        except:
            return ""
    return ""

def save_last(pdf_hash):
    with open(SEEN_FILE, "w") as f:
        json.dump({"hash": pdf_hash, "time": datetime.now().isoformat()}, f)

def calculate_pdf_hash(pdf_data):
    return hashlib.sha256(pdf_data).hexdigest()

def extract_date_from_filename(filename):
    if not filename:
        return None
    patterns = [
        r'(\d{2})-(\d{2})-(\d{4})',
        r'(\d{4})-(\d{2})-(\d{2})',
        r'(\d{2})\.(\d{2})\.(\d{4})',
        r'(\d{2})/(\d{2})/(\d{4})',
    ]
    for p in patterns:
        m = re.search(p, filename)
        if m:
            if len(m.group(1)) == 4:
                return f"{m.group(3)}.{m.group(2)}.{m.group(1)}"
            return f"{m.group(1)}.{m.group(2)}.{m.group(3)}"
    return datetime.now().strftime("%Y-%m-%d")

async def fetch_pdf_link(session):
    async with session.get(URL, timeout=30) as r:
        soup = BeautifulSoup(await r.text(), "html.parser")
        link = soup.find("a", string="Zastępstwa")
        if not link:
            return None
        href = link.get("href")
        if href.startswith("/"):
            return f"https://kopernikus.pl{href}"
        return href

async def download_pdf(url, filepath):
    try:
        # Tworzy foldery nadrzędne (upload/zastepstwa), jeśli nie istnieją
        directory = os.path.dirname(filepath)
        if not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)
            print(f"📁 Utworzono katalog: {directory}")

        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as response:
                if response.status == 200:
                    with open(filepath, 'wb') as f:
                        f.write(await response.read())
                    return True
                else:
                    print(f"❌ Błąd HTTP {response.status} dla URL: {url}")
                    return False
    except Exception as e:
        print(f"❌ Błąd podczas pobierania {filepath}: {e}")
        return False

async def convert_pdf_to_images(pdf_data, date_str):
    folder = os.path.join(IMAGES_DIR, date_str)
    os.makedirs(folder, exist_ok=True)
    doc = fitz.open("pdf", pdf_data)
    images = []

    for i, page in enumerate(doc):
        pix = page.get_pixmap(matrix=fitz.Matrix(3, 3))
        img = Image.open(io.BytesIO(pix.tobytes("png")))

        if img.width > 1920:
            ratio = 1920 / img.width
            img = img.resize((1920, int(img.height * ratio)))

        path = os.path.join(folder, f"strona_{i+1}.png")
        img.save(path, "PNG", optimize=True)
        images.append(path)
        img.close()

    doc.close()
    return images

intents = discord.Intents.default()
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f"🤖 Zalogowany jako {client.user}")
    client.loop.create_task(watch_loop())

async def watch_loop():
    last_hash = load_last()
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                pdf_link = await fetch_pdf_link(session)
                if not pdf_link:
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue

                pdf_data = await download_pdf(session, pdf_link)
                if not pdf_data:
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue

                current_hash = calculate_pdf_hash(pdf_data)
                if current_hash == last_hash:
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue

                filename = pdf_link.split("/")[-1]
                date_str = extract_date_from_filename(filename)

                images = await convert_pdf_to_images(pdf_data, date_str)
                channel = client.get_channel(CHANNEL_ID)

                embed = discord.Embed(
                    title="📋 Nowe zastępstwa",
                    description=f"[Pobierz PDF]({pdf_link})",
                    color=0x00ff00,
                    timestamp=datetime.now(timezone.utc)
                )
                embed.add_field(name="📅 Data", value=date_str)
                embed.add_field(name="🖼️ Strony", value=str(len(images)))

                await channel.send(embed=embed)

                for i, img_path in enumerate(images, 1):
                    file = discord.File(img_path, filename=f"strona_{i}.png")
                    page_embed = discord.Embed(
                        title=f"📄 Strona {i}/{len(images)}",
                        color=0x0099ff
                    )
                    page_embed.set_image(url=f"attachment://strona_{i}.png")
                    await channel.send(embed=page_embed, file=file)
                    await asyncio.sleep(0.5)

                save_last(current_hash)
                last_hash = current_hash

            except Exception as e:
                print("❌ Błąd:", e)

            await asyncio.sleep(CHECK_INTERVAL)

client.run(TOKEN)
