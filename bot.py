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
    
    # Tabela z aktualnymi zastępstwami
    cur.execute('''
        CREATE TABLE IF NOT EXISTS zastepstwa (
            id SERIAL PRIMARY KEY,
            date DATE UNIQUE NOT NULL,
            data JSONB NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Tabela z historią zmian
    cur.execute('''
        CREATE TABLE IF NOT EXISTS zastepstwa_history (
            id SERIAL PRIMARY KEY,
            date DATE NOT NULL,
            data JSONB NOT NULL,
            saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Indeksy dla lepszej wydajności
    cur.execute('''
        CREATE INDEX IF NOT EXISTS idx_zastepstwa_date 
        ON zastepstwa(date)
    ''')
    cur.execute('''
        CREATE INDEX IF NOT EXISTS idx_history_date 
        ON zastepstwa_history(date, saved_at DESC)
    ''')
    
    conn.commit()
    cur.close()
    conn.close()
    print("✅ Baza danych zainicjalizowana")

def load_json_from_db(date_str):
    """Załaduj dane zastępstw z bazy danych"""
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('SELECT data FROM zastepstwa WHERE date = %s', (date_str,))
        result = cur.fetchone()
        cur.close()
        conn.close()
        
        if result:
            return result['data']
        return None
    except Exception as e:
        print(f"❌ Błąd podczas wczytywania z bazy: {e}")
        return None

def save_json_to_db(date_str, data):
    """Zapisz dane zastępstw do bazy danych"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO zastepstwa (date, data, updated_at) 
            VALUES (%s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (date) 
            DO UPDATE SET 
                data = EXCLUDED.data, 
                updated_at = CURRENT_TIMESTAMP
        ''', (date_str, Json(data)))
        conn.commit()
        cur.close()
        conn.close()
        print(f"💾 Zapisano dane dla {date_str} do bazy")
    except Exception as e:
        print(f"❌ Błąd podczas zapisywania do bazy: {e}")

def save_history_to_db(date_str, data):
    """Zapisz starą wersję zastępstw do historii"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO zastepstwa_history (date, data) 
            VALUES (%s, %s)
        ''', (date_str, Json(data)))
        conn.commit()
        cur.close()
        conn.close()
        print(f"📜 Zapisano historię dla {date_str}")
    except Exception as e:
        print(f"❌ Błąd podczas zapisywania historii: {e}")

def get_all_dates_from_db():
    """Pobierz wszystkie daty z bazy danych"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT date FROM zastepstwa ORDER BY date')
        dates = [row[0].strftime('%Y-%m-%d') for row in cur.fetchall()]
        cur.close()
        conn.close()
        return dates
    except Exception as e:
        print(f"❌ Błąd podczas pobierania dat: {e}")
        return []

# ===== FUNKCJE SCRAPOWANIA =====

async def fetch_zastepstwa():
    """Pobierz zastępstwa ze strony szkoły"""
    url = "https://www.1lokoperniku.pl/zastepstwa/"
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=30) as response:
                if response.status == 200:
                    html = await response.text()
                    soup = BeautifulSoup(html, 'html.parser')
                    
                    # Znajdź wszystkie tabele z zastępstwami
                    tables = soup.find_all('table', class_='zastepstwa')
                    
                    zastepstwa_data = {}
                    
                    for table in tables:
                        # Znajdź datę
                        date_header = table.find_previous('h3')
                        if not date_header:
                            continue
                        
                        date_text = date_header.get_text(strip=True)
                        
                        # Parsuj zastępstwa z tabeli
                        rows = table.find_all('tr')[1:]  # Pomiń nagłówek
                        
                        zastepstwa_list = []
                        for row in rows:
                            cols = row.find_all('td')
                            if len(cols) >= 5:
                                zastepstwo = {
                                    'lekcja': cols[0].get_text(strip=True),
                                    'klasa': cols[1].get_text(strip=True),
                                    'przedmiot': cols[2].get_text(strip=True),
                                    'nauczyciel': cols[3].get_text(strip=True),
                                    'uwagi': cols[4].get_text(strip=True)
                                }
                                zastepstwa_list.append(zastepstwo)
                        
                        if zastepstwa_list:
                            zastepstwa_data[date_text] = zastepstwa_list
                    
                    return zastepstwa_data
                else:
                    print(f"❌ Błąd HTTP: {response.status}")
                    return None
        except Exception as e:
            print(f"❌ Błąd podczas pobierania zastępstw: {e}")
            return None

def parse_date(date_str):
    """Parsuj datę z polskiego formatu"""
    # Przykład: "Poniedziałek, 10 lutego 2025"
    try:
        # Usuń dzień tygodnia
        date_parts = date_str.split(',', 1)
        if len(date_parts) > 1:
            date_only = date_parts[1].strip()
        else:
            date_only = date_str.strip()
        
        # Mapowanie polskich miesięcy
        months = {
            'stycznia': '01', 'lutego': '02', 'marca': '03',
            'kwietnia': '04', 'maja': '05', 'czerwca': '06',
            'lipca': '07', 'sierpnia': '08', 'września': '09',
            'października': '10', 'listopada': '11', 'grudnia': '12'
        }
        
        parts = date_only.split()
        if len(parts) == 3:
            day = parts[0]
            month = months.get(parts[1].lower())
            year = parts[2]
            
            if month:
                return f"{year}-{month}-{day.zfill(2)}"
        
        return None
    except Exception as e:
        print(f"❌ Błąd parsowania daty '{date_str}': {e}")
        return None

# ===== FUNKCJE PORÓWNYWANIA I POWIADOMIEŃ =====

def compare_zastepstwa(old_data, new_data):
    """Porównaj stare i nowe zastępstwa"""
    if old_data is None:
        return "new", new_data
    
    if old_data == new_data:
        return "no_change", None
    
    return "changed", new_data

def format_zastepstwa_message(date_str, zastepstwa_list, change_type):
    """Formatuj wiadomość Discord"""
    if change_type == "new":
        title = f"🆕 Nowe zastępstwa na {date_str}"
    elif change_type == "changed":
        title = f"🔄 Zaktualizowano zastępstwa na {date_str}"
    else:
        title = f"📋 Zastępstwa na {date_str}"
    
    embed = discord.Embed(
        title=title,
        color=discord.Color.blue() if change_type == "new" else discord.Color.orange(),
        timestamp=datetime.now(TIMEZONE)
    )
    
    # Grupuj zastępstwa po klasach
    klasy = {}
    for z in zastepstwa_list:
        klasa = z['klasa']
        if klasa not in klasy:
            klasy[klasa] = []
        klasy[klasa].append(z)
    
    # Dodaj pola dla każdej klasy
    for klasa, zastepstwa in sorted(klasy.items()):
        zastepstwa_text = ""
        for z in zastepstwa:
            zastepstwa_text += f"**Lekcja {z['lekcja']}**: {z['przedmiot']}\n"
            zastepstwa_text += f"Nauczyciel: {z['nauczyciel']}\n"
            if z['uwagi']:
                zastepstwa_text += f"_{z['uwagi']}_\n"
            zastepstwa_text += "\n"
        
        # Discord ma limit 1024 znaków na pole
        if len(zastepstwa_text) > 1024:
            zastepstwa_text = zastepstwa_text[:1020] + "..."
        
        embed.add_field(name=f"Klasa {klasa}", value=zastepstwa_text, inline=False)
    
    if not klasy:
        embed.description = "Brak zastępstw"
    
    return embed

async def check_for_changes():
    """Sprawdź zmiany w zastępstwach"""
    print(f"🔍 Sprawdzam zastępstwa... ({datetime.now(TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')})")
    
    zastepstwa_data = await fetch_zastepstwa()
    
    if not zastepstwa_data:
        print("⚠️ Nie udało się pobrać zastępstw")
        return
    
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        print(f"❌ Nie znaleziono kanału o ID: {CHANNEL_ID}")
        return
    
    # Sprawdź każdą datę
    for date_text, new_zastepstwa in zastepstwa_data.items():
        target_date = parse_date(date_text)
        
        if not target_date:
            print(f"⚠️ Nie można sparsować daty: {date_text}")
            continue
        
        # Pobierz stare dane z bazy
        old_data = load_json_from_db(target_date)
        
        # Porównaj
        change_type, changed_data = compare_zastepstwa(old_data, new_zastepstwa)
        
        if change_type == "no_change":
            print(f"✅ Brak zmian dla {date_text}")
            continue
        
        # Zapisz stare dane do historii (jeśli istnieją)
        if old_data and change_type == "changed":
            save_history_to_db(target_date, old_data)
        
        # Zapisz nowe dane
        save_json_to_db(target_date, new_zastepstwa)
        
        # Wyślij powiadomienie
        embed = format_zastepstwa_message(date_text, new_zastepstwa, change_type)
        await channel.send(embed=embed)
        
        print(f"📤 Wysłano powiadomienie dla {date_text} (typ: {change_type})")

# ===== EVENTY I TASKI BOTA =====

@bot.event
async def on_ready():
    print(f'✅ Bot zalogowany jako {bot.user}')
    print(f'📊 Połączono z {len(bot.guilds)} serwerami')
    
    # Inicjalizacja bazy danych
    init_db()
    
    # Uruchom task sprawdzający zastępstwa
    if not check_zastepstwa_task.is_running():
        check_zastepstwa_task.start()
        print("🔄 Uruchomiono automatyczne sprawdzanie zastępstw")

@tasks.loop(minutes=30)  # Sprawdzaj co 30 minut
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
async def pokaz_command(ctx, *, date_text: str = None):
    """Pokaż zastępstwa dla konkretnej daty"""
    if not date_text:
        # Pokaż dostępne daty
        dates = get_all_dates_from_db()
        if dates:
            dates_str = "\n".join(dates)
            await ctx.send(f"📅 Dostępne daty w bazie:\n```{dates_str}```\nUżyj: `!pokaz YYYY-MM-DD`")
        else:
            await ctx.send("❌ Brak danych w bazie")
        return
    
    # Spróbuj sparsować datę
    target_date = date_text if '-' in date_text else parse_date(date_text)
    
    if not target_date:
        await ctx.send("❌ Nieprawidłowy format daty. Użyj: `!pokaz YYYY-MM-DD`")
        return
    
    data = load_json_from_db(target_date)
    
    if not data:
        await ctx.send(f"❌ Brak danych dla daty: {target_date}")
        return
    
    embed = format_zastepstwa_message(target_date, data, "show")
    await ctx.send(embed=embed)

@bot.command(name='status')
async def status_command(ctx):
    """Pokaż status bota i bazy danych"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Policz rekordy
        cur.execute('SELECT COUNT(*) FROM zastepstwa')
        count_current = cur.fetchone()[0]
        
        cur.execute('SELECT COUNT(*) FROM zastepstwa_history')
        count_history = cur.fetchone()[0]
        
        # Najnowsza aktualizacja
        cur.execute('SELECT MAX(updated_at) FROM zastepstwa')
        last_update = cur.fetchone()[0]
        
        cur.close()
        conn.close()
        
        embed = discord.Embed(
            title="📊 Status Bota",
            color=discord.Color.green(),
            timestamp=datetime.now(TIMEZONE)
        )
        embed.add_field(name="Aktywnych dat", value=str(count_current), inline=True)
        embed.add_field(name="Wpisów w historii", value=str(count_history), inline=True)
        embed.add_field(name="Ostatnia aktualizacja", value=str(last_update) if last_update else "Brak", inline=False)
        embed.add_field(name="Task aktywny", value="✅ Tak" if check_zastepstwa_task.is_running() else "❌ Nie", inline=True)
        
        await ctx.send(embed=embed)
        
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
