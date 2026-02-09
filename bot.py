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
import PyPDF2

# Konfiguracja
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

TIMEZONE = pytz.timezone('Europe/Warsaw')
CHANNEL_ID = int(os.getenv('DISCORD_CHANNEL_ID'))  # ID kanału Discord gdzie bot będzie wysyłał wiadomości
DATABASE_URL = os.getenv('DATABASE_URL')

# ===== FUNKCJE BAZY DANYCH =====

def get_db_connection():
    """Połączenie z bazą danych PostgreSQL"""
    return psycopg2.connect(DATABASE_URL)

def init_db():
    """Inicjalizacja tabel w bazie danych"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Usuń stare tabele jeśli istnieją (migracja)
    try:
        cur.execute('DROP TABLE IF EXISTS zastepstwa CASCADE')
        cur.execute('DROP TABLE IF EXISTS zastepstwa_history CASCADE')
        print("🗑️ Usunięto stare tabele (migracja)")
    except Exception as e:
        print(f"⚠️ Błąd podczas usuwania starych tabel: {e}")
    
    # Tabela z aktualnymi zastępstwami
    cur.execute('''
        CREATE TABLE IF NOT EXISTS zastepstwa (
            id SERIAL PRIMARY KEY,
            date DATE NOT NULL,
            version INTEGER NOT NULL DEFAULT 0,
            pdf_url TEXT NOT NULL,
            data JSONB NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(date, version)
        )
    ''')
    
    # Tabela z historią zmian
    cur.execute('''
        CREATE TABLE IF NOT EXISTS zastepstwa_history (
            id SERIAL PRIMARY KEY,
            date DATE NOT NULL,
            version INTEGER NOT NULL,
            pdf_url TEXT NOT NULL,
            data JSONB NOT NULL,
            saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Indeksy dla lepszej wydajności
    cur.execute('''
        CREATE INDEX IF NOT EXISTS idx_zastepstwa_date 
        ON zastepstwa(date, version DESC)
    ''')
    cur.execute('''
        CREATE INDEX IF NOT EXISTS idx_history_date 
        ON zastepstwa_history(date, version DESC, saved_at DESC)
    ''')
    
    conn.commit()
    cur.close()
    conn.close()
    print("✅ Baza danych zainicjalizowana")

def load_json_from_db(date_str, version=None):
    """Załaduj dane zastępstw z bazy danych"""
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        if version is not None:
            cur.execute('SELECT data, version, pdf_url FROM zastepstwa WHERE date = %s AND version = %s', (date_str, version))
        else:
            # Pobierz najnowszą wersję
            cur.execute('SELECT data, version, pdf_url FROM zastepstwa WHERE date = %s ORDER BY version DESC LIMIT 1', (date_str,))
        
        result = cur.fetchone()
        cur.close()
        conn.close()
        
        if result:
            return {
                'data': result['data'],
                'version': result['version'],
                'pdf_url': result['pdf_url']
            }
        return None
    except Exception as e:
        print(f"❌ Błąd podczas wczytywania z bazy: {e}")
        return None

def save_json_to_db(date_str, version, pdf_url, data):
    """Zapisz dane zastępstw do bazy danych"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO zastepstwa (date, version, pdf_url, data, updated_at) 
            VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (date, version) 
            DO UPDATE SET 
                data = EXCLUDED.data,
                pdf_url = EXCLUDED.pdf_url,
                updated_at = CURRENT_TIMESTAMP
        ''', (date_str, version, pdf_url, Json(data)))
        conn.commit()
        cur.close()
        conn.close()
        print(f"💾 Zapisano dane dla {date_str} wersja {version} do bazy")
    except Exception as e:
        print(f"❌ Błąd podczas zapisywania do bazy: {e}")

def save_history_to_db(date_str, version, pdf_url, data):
    """Zapisz starą wersję zastępstw do historii"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO zastepstwa_history (date, version, pdf_url, data) 
            VALUES (%s, %s, %s, %s)
        ''', (date_str, version, pdf_url, Json(data)))
        conn.commit()
        cur.close()
        conn.close()
        print(f"📜 Zapisano historię dla {date_str} wersja {version}")
    except Exception as e:
        print(f"❌ Błąd podczas zapisywania historii: {e}")

def get_all_dates_from_db():
    """Pobierz wszystkie daty z bazy danych"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT DISTINCT date, MAX(version) as latest_version FROM zastepstwa GROUP BY date ORDER BY date DESC')
        dates = [(row[0].strftime('%Y-%m-%d'), row[1]) for row in cur.fetchall()]
        cur.close()
        conn.close()
        return dates
    except Exception as e:
        print(f"❌ Błąd podczas pobierania dat: {e}")
        return []

# ===== FUNKCJE SCRAPOWANIA I PARSOWANIA PDF =====

async def fetch_pdf_links():
    """Pobierz linki do PDF-ów ze strony zastępstw"""
    url = "https://kopernikus.pl/zastepstwa"
    
    # JUTRO (nie dziś!) - chcemy tylko przyszłe zastępstwa
    today = datetime.now(TIMEZONE).date()
    tomorrow = today + timedelta(days=1)
    
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
                                
                                # Sprawdź czy to PRZYSZŁA data (od jutra)
                                if pdf_date >= tomorrow:
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
                                    print(f"⏭️ Pomijam starą/dzisiejszą datę: {date_str}")
                            except ValueError:
                                print(f"⚠️ Nie można sparsować daty: {date_str}")
                                continue
                    
                    print(f"🔍 Znaleziono {len(pdf_links)} przyszłych plików PDF (od jutra: {tomorrow})")
                    return pdf_links
                else:
                    print(f"❌ Błąd HTTP: {response.status}")
                    return []
        except Exception as e:
            print(f"❌ Błąd podczas pobierania listy PDF-ów: {e}")
            return []

async def download_and_parse_pdf(pdf_url):
    """Pobierz i sparsuj PDF z zastępstwami"""
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(pdf_url, timeout=30) as response:
                if response.status == 200:
                    pdf_content = await response.read()
                    
                    # Parsuj PDF
                    pdf_file = io.BytesIO(pdf_content)
                    pdf_reader = PyPDF2.PdfReader(pdf_file)
                    
                    # Wyciągnij tekst ze wszystkich stron
                    full_text = ""
                    for page in pdf_reader.pages:
                        full_text += page.extract_text() + "\n"
                    
                    # Parsuj tekst na strukturę danych
                    zastepstwa = parse_zastepstwa_from_text(full_text)
                    
                    return zastepstwa
                else:
                    print(f"❌ Błąd pobierania PDF: {response.status}")
                    return None
        except Exception as e:
            print(f"❌ Błąd podczas parsowania PDF {pdf_url}: {e}")
            return None

def parse_zastepstwa_from_text(text):
    """Parsuj tekst z PDF na strukturę zastępstw"""
    zastepstwa = []
    
    # Usuń zbędne białe znaki
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    
    # Pomiń nagłówki i znajdź dane
    i = 0
    while i < len(lines):
        line = lines[i]
        
        # Szukaj wzorca zastępstwa
        # Typowy format: "Lekcja Klasa Przedmiot Nauczyciel Uwagi"
        # Lub warianty z różnymi formatami
        
        # Prosty parser - można rozbudować w zależności od faktycznego formatu PDF
        # Zakładamy format: numer_lekcji klasa przedmiot nauczyciel uwagi
        parts = line.split()
        
        if len(parts) >= 4:
            # Spróbuj wyciągnąć dane
            try:
                # Pierwszy element to zazwyczaj numer lekcji
                lekcja = parts[0]
                
                # Jeśli pierwszy element to cyfra lub zakres (np. "1", "1-2")
                if re.match(r'^\d+(-\d+)?$', lekcja):
                    # Reszta danych
                    if len(parts) >= 4:
                        zastepstwo = {
                            'lekcja': lekcja,
                            'klasa': parts[1] if len(parts) > 1 else '',
                            'przedmiot': parts[2] if len(parts) > 2 else '',
                            'nauczyciel': parts[3] if len(parts) > 3 else '',
                            'uwagi': ' '.join(parts[4:]) if len(parts) > 4 else ''
                        }
                        zastepstwa.append(zastepstwo)
            except:
                pass
        
        i += 1
    
    return zastepstwa

# ===== FUNKCJE PORÓWNYWANIA I POWIADOMIEŃ =====

def compare_zastepstwa(old_record, new_data, new_version):
    """Porównaj stare i nowe zastępstwa"""
    if old_record is None:
        return "new", new_data, new_version
    
    old_data = old_record['data']
    old_version = old_record['version']
    
    # Jeśli to ta sama wersja i te same dane
    if old_version == new_version and old_data == new_data:
        return "no_change", None, None
    
    # Jeśli to nowa wersja
    if new_version > old_version:
        return "updated", new_data, new_version
    
    # Jeśli dane się zmieniły przy tej samej wersji
    if old_data != new_data:
        return "changed", new_data, new_version
    
    return "no_change", None, None

def format_zastepstwa_message(date_str, version, zastepstwa_list, change_type, pdf_url):
    """Formatuj wiadomość Discord"""
    if change_type == "new":
        title = f"🆕 Nowe zastępstwa na {date_str}"
        color = discord.Color.blue()
    elif change_type == "updated":
        title = f"🔄 Zaktualizowano zastępstwa na {date_str} (wersja {version})"
        color = discord.Color.orange()
    elif change_type == "changed":
        title = f"⚠️ Zmiana w zastępstwach na {date_str}"
        color = discord.Color.gold()
    else:
        title = f"📋 Zastępstwa na {date_str}"
        color = discord.Color.green()
    
    embed = discord.Embed(
        title=title,
        color=color,
        timestamp=datetime.now(TIMEZONE)
    )
    
    # Dodaj link do PDF
    embed.add_field(name="📄 Oryginalny PDF", value=f"[Pobierz PDF]({pdf_url})", inline=False)
    
    if not zastepstwa_list:
        embed.description = "Nie udało się sparsować zastępstw z PDF. Sprawdź oryginalny plik."
        return embed
    
    # Grupuj zastępstwa po klasach
    klasy = {}
    for z in zastepstwa_list:
        klasa = z.get('klasa', 'Nieznana')
        if klasa not in klasy:
            klasy[klasa] = []
        klasy[klasa].append(z)
    
    # Dodaj pola dla każdej klasy
    for klasa, zastepstwa in sorted(klasy.items()):
        zastepstwa_text = ""
        for z in zastepstwa:
            zastepstwa_text += f"**Lekcja {z.get('lekcja', '?')}**: {z.get('przedmiot', '?')}\n"
            if z.get('nauczyciel'):
                zastepstwa_text += f"Nauczyciel: {z['nauczyciel']}\n"
            if z.get('uwagi'):
                zastepstwa_text += f"_{z['uwagi']}_\n"
            zastepstwa_text += "\n"
        
        # Discord ma limit 1024 znaków na pole
        if len(zastepstwa_text) > 1024:
            zastepstwa_text = zastepstwa_text[:1020] + "..."
        
        embed.add_field(name=f"Klasa {klasa}", value=zastepstwa_text or "Brak danych", inline=False)
    
    if not klasy:
        embed.description = "Brak zastępstw"
    
    return embed

async def check_for_changes():
    """Sprawdź zmiany w zastępstwach"""
    print(f"🔍 Sprawdzam zastępstwa... ({datetime.now(TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')})")
    
    pdf_links = await fetch_pdf_links()
    
    if not pdf_links:
        print("⚠️ Nie znaleziono linków do przyszłych PDF-ów")
        return
    
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        print(f"❌ Nie znaleziono kanału o ID: {CHANNEL_ID}")
        return
    
    # Sprawdź każdy PDF
    for pdf_info in pdf_links:
        date_str = pdf_info['date']
        version = pdf_info['version']
        pdf_url = pdf_info['url']
        
        print(f"📄 Sprawdzam {date_str} wersja {version}")
        
        # Pobierz i sparsuj PDF
        new_zastepstwa = await download_and_parse_pdf(pdf_url)
        
        if new_zastepstwa is None:
            print(f"⚠️ Nie udało się pobrać PDF: {pdf_url}")
            continue
        
        # Pobierz stare dane z bazy
        old_record = load_json_from_db(date_str)
        
        # Porównaj
        change_type, changed_data, changed_version = compare_zastepstwa(old_record, new_zastepstwa, version)
        
        if change_type == "no_change":
            print(f"✅ Brak zmian dla {date_str} wersja {version}")
            continue
        
        # Zapisz stare dane do historii (jeśli istnieją)
        if old_record and change_type in ["updated", "changed"]:
            save_history_to_db(date_str, old_record['version'], old_record['pdf_url'], old_record['data'])
        
        # Zapisz nowe dane
        save_json_to_db(date_str, version, pdf_url, new_zastepstwa)
        
        # Wyślij powiadomienie
        embed = format_zastepstwa_message(date_str, version, new_zastepstwa, change_type, pdf_url)
        await channel.send(embed=embed)
        print(f"📤 Wysłano powiadomienie dla {date_str} wersja {version} (typ: {change_type})")

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
async def pokaz_command(ctx, date_str: str = None, version: int = None):
    """Pokaż zastępstwa dla konkretnej daty"""
    if not date_str:
        # Pokaż dostępne daty
        dates = get_all_dates_from_db()
        if dates:
            dates_str = "\n".join([f"{d[0]} (wersja {d[1]})" for d in dates])
            await ctx.send(f"📅 Dostępne daty w bazie:\n```{dates_str}```\nUżyj: `!pokaz YYYY-MM-DD [wersja]`")
        else:
            await ctx.send("❌ Brak danych w bazie")
        return
    
    record = load_json_from_db(date_str, version)
    
    if not record:
        await ctx.send(f"❌ Brak danych dla daty: {date_str}" + (f" wersja {version}" if version else ""))
        return
    
    embed = format_zastepstwa_message(
        date_str, 
        record['version'], 
        record['data'], 
        "show",
        record['pdf_url']
    )
    await ctx.send(embed=embed)

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
        
        cur.execute('SELECT COUNT(*) FROM zastepstwa_history')
        count_history = cur.fetchone()[0]
        
        # Najnowsza aktualizacja
        cur.execute('SELECT MAX(updated_at) FROM zastepstwa')
        last_update = cur.fetchone()[0]
        
        # Najnowsze zastępstwa
        cur.execute('SELECT date, version FROM zastepstwa ORDER BY date DESC, version DESC LIMIT 3')
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
        embed.add_field(name="Wpisów w historii", value=str(count_history), inline=True)
        embed.add_field(name="Ostatnia aktualizacja", value=str(last_update) if last_update else "Brak", inline=False)
        embed.add_field(name="Task aktywny", value="✅ Tak" if check_zastepstwa_task.is_running() else "❌ Nie", inline=True)
        
        if latest:
            latest_str = "\n".join([f"{row[0]} (v{row[1]})" for row in latest])
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
