import discord
from discord.ext import commands, tasks
import aiohttp
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import os
import json
import pytz
import re
import io
from pdf2image import convert_from_bytes
from PIL import Image

# Konfiguracja
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

TIMEZONE = pytz.timezone('Europe/Warsaw')
CHANNEL_ID = int(os.getenv('DISCORD_CHANNEL_ID'))
SENT_PDFS_FILE = '/tmp/sent_pdfs.json'  # Plik z listą wysłanych PDF-ów

# ===== FUNKCJE ZARZĄDZANIA WYSŁANYMI PDF-AMI =====

def load_sent_pdfs():
    """Załaduj listę wysłanych PDF-ów"""
    try:
        if os.path.exists(SENT_PDFS_FILE):
            with open(SENT_PDFS_FILE, 'r') as f:
                return json.load(f)
        return {}
    except Exception as e:
        print(f"❌ Błąd wczytywania listy: {e}")
        return {}

def save_sent_pdf(date_str, version):
    """Zapisz informację że PDF został wysłany"""
    try:
        sent_pdfs = load_sent_pdfs()
        key = f"{date_str}-v{version}"
        sent_pdfs[key] = {
            'date': date_str,
            'version': version,
            'sent_at': datetime.now(TIMEZONE).isoformat()
        }
        
        with open(SENT_PDFS_FILE, 'w') as f:
            json.dump(sent_pdfs, f, indent=2)
        
        print(f"💾 Zapisano: {key}")
    except Exception as e:
        print(f"❌ Błąd zapisywania: {e}")

def was_pdf_sent(date_str, version):
    """Sprawdź czy PDF już został wysłany"""
    sent_pdfs = load_sent_pdfs()
    key = f"{date_str}-v{version}"
    return key in sent_pdfs

# ===== FUNKCJE SCRAPOWANIA =====

async def fetch_pdf_links():
    """Pobierz linki do PDF-ów ze strony zastępstw"""
    url = "https://www.kopernikus.pl/zastepstwa"
    
    # Sprawdzaj TYLKO JUTRO
    today = datetime.now(TIMEZONE).date()
    tomorrow = today + timedelta(days=1)
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=30) as response:
                if response.status == 200:
                    html = await response.text()
                    soup = BeautifulSoup(html, 'html.parser')
                    
                    pdf_links = []
                    
                    for link in soup.find_all('a', href=True):
                        href = link['href']
                        match = re.search(r'/?upload/zastepstwa/(\d{4}-\d{2}-\d{2})-(\d+)\.pdf', href)
                        if match:
                            date_str = match.group(1)
                            version = int(match.group(2))
                            
                            try:
                                pdf_date = datetime.strptime(date_str, '%Y-%m-%d').date()
                                
                                # TYLKO JUTRO
                                if pdf_date == tomorrow:
                                    if href.startswith('http'):
                                        full_url = href
                                    elif href.startswith('/'):
                                        full_url = f"https://www.kopernikus.pl{href}"
                                    else:
                                        full_url = f"https://www.kopernikus.pl/{href}"
                                    
                                    pdf_links.append({
                                        'date': date_str,
                                        'version': version,
                                        'url': full_url
                                    })
                            except ValueError:
                                continue
                    
                    print(f"🔍 Znaleziono {len(pdf_links)} PDF-ów na JUTRO ({tomorrow})")
                    return pdf_links
                else:
                    print(f"❌ Błąd HTTP: {response.status}")
                    return []
        except Exception as e:
            print(f"❌ Błąd pobierania strony: {e}")
            return []

async def download_and_convert_pdf(pdf_url):
    """Pobierz PDF i konwertuj na obrazki"""
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(pdf_url, timeout=30) as response:
                if response.status == 200:
                    pdf_content = await response.read()
                    images = convert_from_bytes(pdf_content, dpi=200)
                    print(f"   ✅ Skonwertowano {len(images)} stron")
                    return images
                else:
                    print(f"   ❌ Błąd HTTP: {response.status}")
                    return None
        except Exception as e:
            print(f"   ❌ Błąd konwersji: {e}")
            return None

# ===== WYSYŁANIE POWIADOMIEŃ =====

async def send_zastepstwa_notification(channel, date_str, version, pdf_url, images):
    """Wyślij powiadomienie z obrazkami"""
    try:
        # Parsuj datę
        pdf_date = datetime.strptime(date_str, '%Y-%m-%d')
        days_pl = ['Poniedziałek', 'Wtorek', 'Środa', 'Czwartek', 'Piątek', 'Sobota', 'Niedziela']
        day_name = days_pl[pdf_date.weekday()]
        formatted_date = f"{day_name}, {pdf_date.strftime('%d.%m.%Y')}"
        
        # Główny embed
        if version == 0:
            embed = discord.Embed(
                title="📋 Nowe zastępstwa",
                description=f"Dostępne są nowe zastępstwa.",
                color=discord.Color.blue(),
                timestamp=datetime.now(TIMEZONE)
            )
        else:
            embed = discord.Embed(
                title="🔄 Zaktualizowano zastępstwa",
                description=f"Zastępstwa zostały zaktualizowane (wersja {version}).",
                color=discord.Color.orange(),
                timestamp=datetime.now(TIMEZONE)
            )
        
        embed.add_field(name="📅 Data zastępstw", value=formatted_date, inline=False)
        embed.add_field(name="📄 Link do pobrania", value=f"[Otwórz PDF]({pdf_url})", inline=False)
        embed.add_field(name="📊 Liczba stron", value=f"{len(images)} stron", inline=False)
        embed.set_footer(text="Zastępstwa | Kopernikus")
        
        await channel.send(embed=embed)
        
        # Wyślij każdą stronę
        for i, image in enumerate(images, 1):
            img_byte_arr = io.BytesIO()
            image.save(img_byte_arr, format='PNG')
            img_byte_arr.seek(0)
            
            page_embed = discord.Embed(
                title=f"📄 Strona {i}/{len(images)}",
                color=discord.Color.green(),
                timestamp=datetime.now(TIMEZONE)
            )
            page_embed.add_field(name="📅 Data", value=formatted_date, inline=True)
            page_embed.add_field(name="🔢 Strona", value=f"{i} z {len(images)}", inline=True)
            
            file = discord.File(img_byte_arr, filename=f"zastepstwa_{date_str}_strona_{i}.png")
            page_embed.set_image(url=f"attachment://zastepstwa_{date_str}_strona_{i}.png")
            
            await channel.send(embed=page_embed, file=file)
        
        print(f"   📤 Wysłano {len(images)} stron")
        
    except Exception as e:
        print(f"   ❌ Błąd wysyłania: {e}")

# ===== GŁÓWNA LOGIKA =====

async def check_for_changes():
    """Sprawdź zmiany w zastępstwach"""
    print(f"🔍 Sprawdzam zastępstwa... ({datetime.now(TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')})")
    
    pdf_links = await fetch_pdf_links()
    
    if not pdf_links:
        print("⚠️ Nie znaleziono PDF-ów na jutro")
        return
    
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        print(f"❌ Nie znaleziono kanału o ID: {CHANNEL_ID}")
        return
    
    # Sortuj po wersji - najnowsza najpierw
    # WAŻNE: Wysyłamy TYLKO najnowszą wersję dla danej daty!
    pdf_by_date = {}
    for pdf_info in pdf_links:
        date_str = pdf_info['date']
        version = pdf_info['version']
        
        if date_str not in pdf_by_date or version > pdf_by_date[date_str]['version']:
            pdf_by_date[date_str] = pdf_info
    
    # Teraz mamy tylko najnowszą wersję każdej daty
    for date_str, pdf_info in pdf_by_date.items():
        version = pdf_info['version']
        pdf_url = pdf_info['url']
        
        print(f"📄 {date_str} v{version}")
        
        # Sprawdź czy już wysłano
        if was_pdf_sent(date_str, version):
            print(f"   ✅ Już wysłane, pomijam")
            continue
        
        # Nowy PDF!
        try:
            print(f"   🆕 Nowy! Pobieram...")
            images = await download_and_convert_pdf(pdf_url)
            
            if images is None or len(images) == 0:
                print(f"   ❌ Nie udało się skonwertować")
                continue
            
            # Wyślij
            print(f"   📤 Wysyłam...")
            await send_zastepstwa_notification(channel, date_str, version, pdf_url, images)
            
            # Zapisz że wysłano
            save_sent_pdf(date_str, version)
            
            print(f"   ✅ Gotowe!")
            
        except Exception as e:
            print(f"   ❌ Błąd: {e}")
    
    print(f"✅ Zakończono")

# ===== EVENTY =====

@bot.event
async def on_ready():
    print(f'✅ Bot: {bot.user}')
    print(f'📊 Serwery: {len(bot.guilds)}')
    print(f'📁 Lista wysłanych: {SENT_PDFS_FILE}')
    
    # Pokaż co już wysłano
    sent = load_sent_pdfs()
    if sent:
        print(f"📋 Już wysłane PDF-y: {len(sent)}")
        for key in list(sent.keys())[-5:]:  # Ostatnie 5
            print(f"   - {key}")
    
    if not check_zastepstwa_task.is_running():
        check_zastepstwa_task.start()
        print("🔄 Automatyczne sprawdzanie: co 15 min")

@tasks.loop(minutes=15)
async def check_zastepstwa_task():
    await check_for_changes()

@check_zastepstwa_task.before_loop
async def before_check():
    await bot.wait_until_ready()

# ===== KOMENDY =====

@bot.command(name='sprawdz')
async def sprawdz_command(ctx):
    """Ręczne sprawdzenie"""
    await ctx.send("🔍 Sprawdzam...")
    await check_for_changes()

@bot.command(name='status')
async def status_command(ctx):
    """Status bota"""
    sent = load_sent_pdfs()
    
    embed = discord.Embed(
        title="📊 Status Bota",
        color=discord.Color.green(),
        timestamp=datetime.now(TIMEZONE)
    )
    embed.add_field(name="Wysłanych PDF-ów", value=str(len(sent)), inline=True)
    embed.add_field(name="Task aktywny", value="✅ Tak" if check_zastepstwa_task.is_running() else "❌ Nie", inline=True)
    
    if sent:
        latest = list(sent.values())[-3:]
        latest_str = "\n".join([f"{p['date']} v{p['version']}" for p in latest])
        embed.add_field(name="Ostatnio wysłane", value=latest_str, inline=False)
    
    await ctx.send(embed=embed)

@bot.command(name='reset')
async def reset_command(ctx):
    """Wyczyść listę wysłanych (ADMIN)"""
    try:
        if os.path.exists(SENT_PDFS_FILE):
            os.remove(SENT_PDFS_FILE)
        await ctx.send("✅ Lista wysłanych została wyczyszczona!")
    except Exception as e:
        await ctx.send(f"❌ Błąd: {e}")

# ===== START =====

if __name__ == "__main__":
    TOKEN = os.getenv('DISCORD_BOT_TOKEN')
    
    if not TOKEN:
        print("❌ Brak DISCORD_BOT_TOKEN!")
        exit(1)
    
    if not CHANNEL_ID:
        print("❌ Brak DISCORD_CHANNEL_ID!")
        exit(1)
    
    print("🚀 Uruchamiam...")
    bot.run(TOKEN)
