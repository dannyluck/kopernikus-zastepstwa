import discord
from discord.ext import commands, tasks
import aiohttp
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import os
import json
import psycopg2
from psycopg2.extras import Json, RealDictCursor
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
DATABASE_URL = os.getenv('DATABASE_URL')

# Connection pool dla bazy danych
db_pool = None

# ===== FUNKCJE BAZY DANYCH =====

def init_db_pool():
    """Inicjalizuj connection pool"""
    global db_pool
    try:
        db_pool = psycopg2.pool.SimpleConnectionPool(
            1, 10,  # min 1, max 10 połączeń
            DATABASE_URL,
            connect_timeout=10
        )
        print("✅ Pool połączeń z bazą danych zainicjalizowany")
    except Exception as e:
        print(f"❌ Błąd inicjalizacji pool: {e}")

def get_db_connection():
    """Pobierz połączenie z pool"""
    try:
        return db_pool.getconn()
    except Exception as e:
        print(f"❌ Błąd pobierania połączenia: {e}")
        return None

def return_db_connection(conn):
    """Zwróć połączenie do pool"""
    try:
        if conn:
            db_pool.putconn(conn)
    except Exception as e:
        print(f"❌ Błąd zwracania połączenia: {e}")

def init_db():
    """Inicjalizacja tabel w bazie danych"""
    conn = get_db_connection()
    if not conn:
        print("❌ Nie można połączyć z bazą danych!")
        return
    
    try:
        cur = conn.cursor()
        
        # Usuń stare tabele jeśli istnieją (migracja)
        try:
            cur.execute('DROP TABLE IF EXISTS zastepstwa CASCADE')
            print("🗑️ Usunięto stare tabele (migracja)")
        except Exception as e:
            print(f"⚠️ Błąd podczas usuwania starych tabel: {e}")
        
        # Tabela z aktualnymi zastępstwami - przechowuje tylko metadane
        cur.execute('''
            CREATE TABLE IF NOT EXISTS zastepstwa (
                id SERIAL PRIMARY KEY,
                date DATE NOT NULL,
                version INTEGER NOT NULL DEFAULT 0,
                pdf_url TEXT NOT NULL,
                num_pages INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(date, version)
            )
        ''')
        
        # Indeksy dla lepszej wydajności
        cur.execute('''
            CREATE INDEX IF NOT EXISTS idx_zastepstwa_date 
            ON zastepstwa(date, version DESC)
        ''')
        
        conn.commit()
        cur.close()
        print("✅ Baza danych zainicjalizowana")
    except Exception as e:
        print(f"❌ Błąd inicjalizacji bazy: {e}")
    finally:
        return_db_connection(conn)

def check_pdf_exists(date_str, version):
    """Sprawdź czy PDF już istnieje w bazie"""
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        cur = conn.cursor()
        cur.execute('SELECT pdf_url FROM zastepstwa WHERE date = %s AND version = %s', (date_str, version))
        result = cur.fetchone()
        cur.close()
        return result is not None
    except Exception as e:
        print(f"❌ Błąd podczas sprawdzania bazy: {e}")
        return False
    finally:
        return_db_connection(conn)

def save_pdf_metadata(date_str, version, pdf_url, num_pages):
    """Zapisz metadane PDF do bazy danych"""
    conn = get_db_connection()
    if not conn:
        print("❌ Nie można połączyć z bazą")
        return
    
    try:
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO zastepstwa (date, version, pdf_url, num_pages, updated_at) 
            VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (date, version) 
            DO UPDATE SET 
                pdf_url = EXCLUDED.pdf_url,
                num_pages = EXCLUDED.num_pages,
                updated_at = CURRENT_TIMESTAMP
        ''', (date_str, version, pdf_url, num_pages))
        conn.commit()
        cur.close()
        print(f"💾 Zapisano metadane dla {date_str} wersja {version} do bazy")
    except Exception as e:
        print(f"❌ Błąd podczas zapisywania do bazy: {e}")
    finally:
        return_db_connection(conn)

def get_all_dates_from_db():
    """Pobierz wszystkie daty z bazy danych"""
    conn = get_db_connection()
    if not conn:
        return []
    
    try:
        cur = conn.cursor()
        cur.execute('SELECT DISTINCT date, MAX(version) as latest_version FROM zastepstwa GROUP BY date ORDER BY date DESC')
        dates = [(row[0].strftime('%Y-%m-%d'), row[1]) for row in cur.fetchall()]
        cur.close()
        return dates
    except Exception as e:
        print(f"❌ Błąd podczas pobierania dat: {e}")
        return []
    finally:
        return_db_connection(conn)

# ===== FUNKCJE SCRAPOWANIA I PARSOWANIA PDF =====

async def fetch_pdf_links():
    """Pobierz linki do PDF-ów ze strony zastępstw"""
    url = "https://kopernikus.pl/zastepstwa"
    
    # Sprawdzaj tylko JUTRO i POJUTRZE (max 2 dni do przodu)
    today = datetime.now(TIMEZONE).date()
    tomorrow = today + timedelta(days=1)
    max_date = today + timedelta(days=2)  # Jutro + pojutrze
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=30) as response:
                if response.status == 200:
                    html = await response.text()
                    soup = BeautifulSoup(html, 'html.parser')
                    
                    # Znajdź wszystkie linki do PDF-ów
                    pdf_links = []
                    
                    # Szukaj linków w formacie: /upload/zastepstwa/YYYY-MM-DD-V.pdf lub upload/zastepstwa/YYYY-MM-DD-V.pdf
                    for link in soup.find_all('a', href=True):
                        href = link['href']
                        # Sprawdź czy to link do PDF zastępstw (może być z / lub bez)
                        match = re.search(r'/?upload/zastepstwa/(\d{4}-\d{2}-\d{2})-(\d+)\.pdf', href)
                        if match:
                            date_str = match.group(1)
                            version = int(match.group(2))
                            
                            # Parsuj datę
                            try:
                                pdf_date = datetime.strptime(date_str, '%Y-%m-%d').date()
                                
                                # Sprawdź czy to JUTRO lub POJUTRZE (max 2 dni do przodu)
                                if tomorrow <= pdf_date <= max_date:
                                    # Buduj pełny URL
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
                                else:
                                    if pdf_date < tomorrow:
                                        print(f"⏭️ Pomijam starą/dzisiejszą datę: {date_str}")
                                    else:
                                        print(f"⏭️ Pomijam zbyt odległą datę: {date_str} (max: {max_date})")
                            except ValueError:
                                print(f"⚠️ Nie można sparsować daty: {date_str}")
                                continue
                    
                    print(f"🔍 Znaleziono {len(pdf_links)} PDF-ów (jutro-pojutrze: {tomorrow} - {max_date})")
                    return pdf_links
                else:
                    print(f"❌ Błąd HTTP: {response.status}")
                    return []
        except Exception as e:
            print(f"❌ Błąd podczas pobierania listy PDF-ów: {e}")
            return []

async def download_and_convert_pdf(pdf_url):
    """Pobierz PDF i konwertuj na obrazki"""
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(pdf_url, timeout=30) as response:
                if response.status == 200:
                    pdf_content = await response.read()
                    
                    # Konwertuj PDF na obrazki (PIL Images)
                    images = convert_from_bytes(pdf_content, dpi=200)
                    
                    print(f"✅ Skonwertowano PDF na {len(images)} stron")
                    return images
                else:
                    print(f"❌ Błąd pobierania PDF: {response.status}")
                    return None
        except Exception as e:
            print(f"❌ Błąd podczas konwersji PDF {pdf_url}: {e}")
            return None

# ===== FUNKCJE PORÓWNYWANIA I POWIADOMIEŃ =====

async def send_zastepstwa_notification(channel, date_str, version, pdf_url, images):
    """Wyślij powiadomienie z obrazkami ze zastępstw"""
    try:
        # Informacja o nowych zastępstwach
        if version == 0:
            message = f"🆕 **Nowe zastępstwa na {date_str}**"
        else:
            message = f"🔄 **Zaktualizowano zastępstwa na {date_str}** (wersja {version})"
        
        message += f"\n📄 [Pobierz PDF]({pdf_url})"
        message += f"\n📄 Liczba stron: {len(images)}"
        
        await channel.send(message)
        
        # Wyślij każdą stronę jako osobny obrazek
        for i, image in enumerate(images, 1):
            # Konwertuj PIL Image do bytes
            img_byte_arr = io.BytesIO()
            image.save(img_byte_arr, format='PNG')
            img_byte_arr.seek(0)
            
            # Wyślij jako załącznik
            file = discord.File(img_byte_arr, filename=f"zastepstwa_{date_str}_v{version}_strona_{i}.png")
            await channel.send(f"📄 Strona {i}/{len(images)}", file=file)
        
        print(f"📤 Wysłano {len(images)} stron dla {date_str} wersja {version}")
        
    except Exception as e:
        print(f"❌ Błąd podczas wysyłania powiadomienia: {e}")

async def check_for_changes():
    """Sprawdź zmiany w zastępstwach"""
    print(f"🔍 Sprawdzam zastępstwa... ({datetime.now(TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')})")
    
    pdf_links = await fetch_pdf_links()
    
    if not pdf_links:
        print("⚠️ Nie znaleziono PDF-ów na jutro/pojutrze")
        return
    
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        print(f"❌ Nie znaleziono kanału o ID: {CHANNEL_ID}")
        return
    
    # Sprawdź każdy PDF (posortowane od najnowszych)
    pdf_links_sorted = sorted(pdf_links, key=lambda x: (x['date'], x['version']), reverse=True)
    
    for pdf_info in pdf_links_sorted:
        date_str = pdf_info['date']
        version = pdf_info['version']
        pdf_url = pdf_info['url']
        
        print(f"📄 Sprawdzam {date_str} wersja {version}")
        
        # Sprawdź czy już mamy ten PDF w bazie
        if check_pdf_exists(date_str, version):
            print(f"✅ PDF już istnieje w bazie: {date_str} v{version}, pomijam")
            continue
        
        # Nowy PDF! Pobierz i konwertuj
        try:
            print(f"🆕 Nowy PDF! Pobieram {date_str} v{version}...")
            images = await download_and_convert_pdf(pdf_url)
            
            if images is None or len(images) == 0:
                print(f"⚠️ Nie udało się skonwertować PDF: {pdf_url}")
                continue
            
            # Zapisz metadane do bazy
            save_pdf_metadata(date_str, version, pdf_url, len(images))
            
            # Wyślij powiadomienie z obrazkami
            await send_zastepstwa_notification(channel, date_str, version, pdf_url, images)
            
            print(f"📤 ✅ Wysłano powiadomienie dla {date_str} wersja {version}")
            
        except Exception as e:
            print(f"❌ Błąd podczas przetwarzania PDF {pdf_url}: {e}")
            continue
    
    print(f"✅ Zakończono sprawdzanie")

# ===== EVENTY I TASKI BOTA =====

@bot.event
async def on_ready():
    print(f'✅ Bot zalogowany jako {bot.user}')
    print(f'📊 Połączono z {len(bot.guilds)} serwerami')
    
    # Inicjalizacja bazy danych
    init_db()
    
    print("🔍 Bot gotowy - będzie wysyłał powiadomienia tylko o PRZYSZŁYCH zastępstwach (od jutra)")
    
    # Uruchom task sprawdzający zastępstwa
    if not check_zastepstwa_task.is_running():
        check_zastepstwa_task.start()
        print("🔄 Uruchomiono automatyczne sprawdzanie zastępstw co 15 minut")

@tasks.loop(minutes=15)  # Sprawdzaj co 15 minut
async def check_zastepstwa_task():
    await check_for_changes()

@check_zastepstwa_task.before_loop
async def before_check_zastepstwa():
    await bot.wait_until_ready()
    print("⏳ Czekam na gotowość bota...")

# ===== KOMENDY =====

@bot.command(name='sprawdz')
async def sprawdz_command(ctx):
    """Ręczne sprawdzenie zastępstw"""
    await ctx.send("🔍 Sprawdzam zastępstwa...")
    await check_for_changes()

@bot.command(name='pokaz')
async def pokaz_command(ctx):
    """Pokaż zastępstwa zapisane w bazie"""
    dates = get_all_dates_from_db()
    if dates:
        dates_str = "\n".join([f"{d[0]} (wersja {d[1]})" for d in dates])
        await ctx.send(f"📅 Zastępstwa w bazie:\n```{dates_str}```")
    else:
        await ctx.send("❌ Brak danych w bazie")

@bot.command(name='status')
async def status_command(ctx):
    """Pokaż status bota i bazy danych"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Policz rekordy
        cur.execute('SELECT COUNT(DISTINCT date) FROM zastepstwa')
        count_dates = cur.fetchone()[0]
        
        cur.execute('SELECT COUNT(*) FROM zastepstwa')
        count_versions = cur.fetchone()[0]
        
        # Najnowsza aktualizacja
        cur.execute('SELECT MAX(updated_at) FROM zastepstwa')
        last_update = cur.fetchone()[0]
        
        # Najnowsze zastępstwa
        cur.execute('SELECT date, version, num_pages FROM zastepstwa ORDER BY date DESC, version DESC LIMIT 5')
        latest = cur.fetchall()
        
        cur.close()
        conn.close()
        
        embed = discord.Embed(
            title="📊 Status Bota",
            color=discord.Color.green(),
            timestamp=datetime.now(TIMEZONE)
        )
        embed.add_field(name="Różnych dat", value=str(count_dates), inline=True)
        embed.add_field(name="Wersji łącznie", value=str(count_versions), inline=True)
        embed.add_field(name="Ostatnia aktualizacja", value=str(last_update) if last_update else "Brak", inline=False)
        embed.add_field(name="Task aktywny", value="✅ Tak" if check_zastepstwa_task.is_running() else "❌ Nie", inline=True)
        
        if latest:
            latest_str = "\n".join([f"{row[0]} (v{row[1]}, {row[2]} stron)" for row in latest])
            embed.add_field(name="Najnowsze zastępstwa", value=latest_str, inline=False)
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"❌ Błąd: {e}")

@bot.command(name='debug')
async def debug_command(ctx):
    """Debug: pokaż wszystkie znalezione PDF-y na stronie"""
    await ctx.send("🔍 Szukam PDF-ów na stronie...")
    
    url = "https://kopernikus.pl/zastepstwa"
    today = datetime.now(TIMEZONE).date()
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=30) as response:
                if response.status == 200:
                    html = await response.text()
                    soup = BeautifulSoup(html, 'html.parser')
                    
                    all_pdfs = []
                    for link in soup.find_all('a', href=True):
                        href = link['href']
                        match = re.search(r'/?upload/zastepstwa/(\d{4}-\d{2}-\d{2})-(\d+)\.pdf', href)
                        if match:
                            date_str = match.group(1)
                            version = int(match.group(2))
                            pdf_date = datetime.strptime(date_str, '%Y-%m-%d').date()
                            is_future = "✅" if pdf_date >= today else "⏭️"
                            all_pdfs.append(f"{is_future} {date_str} v{version}")
                    
                    if all_pdfs:
                        pdfs_text = "\n".join(all_pdfs[:20])  # Max 20
                        await ctx.send(f"📄 Znalezione PDF-y:\n```\n{pdfs_text}\n```\n✅ = aktualny/przyszły | ⏭️ = przeszły\nDzisiaj: {today}")
                    else:
                        await ctx.send("❌ Nie znaleziono żadnych PDF-ów")
                else:
                    await ctx.send(f"❌ Błąd HTTP: {response.status}")
        except Exception as e:
            await ctx.send(f"❌ Błąd: {e}")

# ===== URUCHOMIENIE BOTA =====

if __name__ == "__main__":
    TOKEN = os.getenv('DISCORD_BOT_TOKEN')
    
    if not TOKEN:
        print("❌ Brak tokena Discord! Ustaw zmienną DISCORD_BOT_TOKEN")
        exit(1)
    
    if not DATABASE_URL:
        print("❌ Brak URL bazy danych! Ustaw zmienną DATABASE_URL")
        exit(1)
    
    if not CHANNEL_ID:
        print("❌ Brak ID kanału! Ustaw zmienną DISCORD_CHANNEL_ID")
        exit(1)
    
    print("🚀 Uruchamiam bota...")
    bot.run(TOKEN)
