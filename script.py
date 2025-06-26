import discord
from discord.ext import commands, tasks
import asyncio
import datetime
import json
import os
import csv

TOKEN = os.getenv("TOKEN")
DATA_FILE = "snusstopp_data.json"
LOG_FILE = "snusstopp_log.csv"
MAX_LOG_DAYS = 4

intents = discord.Intents.default()
intents.messages = True
intents.reactions = True
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

snusstopp_message_id = None
registered_users = set()
streaks = {}  # {user_id: {"streak": int, "almost_count": int}}
today_checkins = {}  # {user_id: emoji}
latest_checkin_message_id = None
latest_checkin_date = None

# ------------------ LAGRING ------------------
def save_data():
    with open(DATA_FILE, 'w') as f:
        json.dump({
            "registered_users": list(registered_users),
            "streaks": streaks
        }, f)

def load_data():
    global registered_users, streaks
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r') as f:
                data = json.load(f)
                registered_users = set(data.get("registered_users", []))
                streaks.clear()
                for uid, val in data.get("streaks", {}).items():
                    # sikkerhetssjekk: val kan være int eller dict
                    if isinstance(val, dict):
                        streaks[uid] = {
                            "streak": val.get("streak", 0),
                            "almost_count": val.get("almost_count", 0)
                        }
                    else:
                        # fallback hvis eldre data
                        streaks[uid] = {"streak": val, "almost_count": 0}
        except Exception as e:
            print(f"Feil ved lasting av data: {e}")
            registered_users = set()
            streaks.clear()

# ------------------ LOGGING ------------------
def log_event(event_type, user_id, extra=""):
    """Logger event til CSV med timestamp, event, user_id og ekstra info."""
    now = datetime.datetime.utcnow().isoformat()
    row = [now, event_type, user_id, extra]
    # Sjekk om fil finnes, hvis ikke lag med header
    file_exists = os.path.exists(LOG_FILE)
    with open(LOG_FILE, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "event", "user_id", "extra"])
        writer.writerow(row)
    prune_logs()

def prune_logs():
    """Fjern logg-linjer eldre enn MAX_LOG_DAYS dager."""
    if not os.path.exists(LOG_FILE):
        return
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=MAX_LOG_DAYS)
    with open(LOG_FILE, 'r', encoding='utf-8') as f:
        lines = list(csv.reader(f))
    header = lines[0]
    filtered = [header]
    for row in lines[1:]:
        try:
            ts = datetime.datetime.fromisoformat(row[0])
            if ts >= cutoff:
                filtered.append(row)
        except Exception:
            # ved feil i tidsformat, behold linjen for sikkerhet
            filtered.append(row)
    with open(LOG_FILE, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerows(filtered)

# ------------------ HJELPEFUNKSJONER ------------------
def can_checkin_today():
    """Returnerer True hvis det er samme dag som latest_checkin_date."""
    if latest_checkin_date is None:
        return False
    now = datetime.datetime.utcnow().date()
    return now == latest_checkin_date

def reset_checkins_for_new_day():
    global today_checkins, latest_checkin_date
    today_checkins = {}
    latest_checkin_date = datetime.datetime.utcnow().date()

def get_user_display_name(user_id):
    user = bot.get_user(int(user_id))
    if user:
        return user.display_name
    return f"User({user_id})"

async def update_snusstopp_message(message):
    user_list = "\n".join(f"- {get_user_display_name(uid)}" for uid in sorted(registered_users)) or "Ingen deltakere ennå."
    new_content = f"Trykk ❌ for å bli med i snusstopputfordringen! Fjern ❌ for å melde deg av.\n\n**Deltakere:**\n{user_list}"
    try:
        await message.edit(content=new_content)
    except discord.errors.Forbidden:
        print("Manglende tillatelse til å oppdatere meldingen.")

# ------------------ EVENT ------------------
@bot.event
async def on_ready():
    print(f'Logget inn som {bot.user}')
    load_data()
    daily_checkin.start()
    evening_reminder.start()

# ------------------ KOMMANDOER ------------------
@bot.command()
async def snusstopp(ctx):
    global snusstopp_message_id
    user_list = "\n".join(f"- {get_user_display_name(uid)}" for uid in sorted(registered_users)) or "Ingen deltakere ennå."
    msg = await ctx.send(f"Trykk ❌ for å bli med i snusstopputfordringen! Fjern ❌ for å melde deg av.\n\n**Deltakere:**\n{user_list}")
    await msg.add_reaction("❌")
    snusstopp_message_id = msg.id

@bot.command()
async def streak(ctx):
    user_id = str(ctx.author.id)
    data = streaks.get(user_id, {"streak": 0, "almost_count": 0})
    streak_count = data.get("streak", 0)
    nesten_count = data.get("almost_count", 0)
    await ctx.send(f"{ctx.author.display_name}, din streak er {streak_count} dager snusfri 🔥 og {nesten_count} 'nesten' dager 🟡.")

@bot.command()
@commands.has_permissions(administrator=True)
async def triggercheckin(ctx):
    """Administrator-kommando for å trigge dagens innsjekk manuelt."""
    await send_daily_checkin()

# ------------------ REAKSJONSHÅNDTERING ------------------
@bot.event
async def on_reaction_add(reaction, user):
    global latest_checkin_message_id
    if user.bot:
        return

    if reaction.message.id == snusstopp_message_id and str(reaction.emoji) == "❌":
        # Påmelding
        if user.id not in registered_users:
            registered_users.add(user.id)
            save_data()
            await update_snusstopp_message(reaction.message)
            log_event("register", user.id)
        return

    if reaction.message.id == latest_checkin_message_id:
        if user.id not in registered_users:
            return  # Ikke registrert, ignorer

        if not can_checkin_today():
            # Svar med pm? Ignorer
            return

        emoji = str(reaction.emoji)
        if emoji not in ["✅", "🟡", "🔴"]:
            return

        if user.id in today_checkins:
            # Bruker har allerede sjekket inn i dag - ignorér for å unngå spam
            return

        today_checkins[user.id] = emoji

        user_id_str = str(user.id)

        # Oppdater streaks og almost_counts
        current = streaks.get(user_id_str, {"streak": 0, "almost_count": 0})
        if emoji == "✅":
            current["streak"] = current.get("streak", 0) + 1
        elif emoji == "🟡":
            current["almost_count"] = current.get("almost_count", 0) + 1
        elif emoji == "🔴":
            current["streak"] = 0
            # almost_count uendret

        streaks[user_id_str] = current
        save_data()
        log_event("checkin", user.id, emoji)
        return

@bot.event
async def on_reaction_remove(reaction, user):
    if user.bot:
        return

    if reaction.message.id == snusstopp_message_id and str(reaction.emoji) == "❌":
        if user.id in registered_users:
            registered_users.remove(user.id)
            save_data()
            await update_snusstopp_message(reaction.message)
            log_event("unregister", user.id)
        return

    if reaction.message.id == latest_checkin_message_id:
        if user.id not in registered_users:
            return

        if not can_checkin_today():
            return

        emoji = str(reaction.emoji)
        if emoji not in ["✅", "🟡", "🔴"]:
            return

        if user.id not in today_checkins:
            return  # Har ikke sjekket inn - ignorér

        # Fjern innsjekk
        del today_checkins[user.id]

        user_id_str = str(user.id)
        current = streaks.get(user_id_str, {"streak": 0, "almost_count": 0})

        # Reverser effekten av fjernet reaksjon
        if emoji == "✅" and current.get("streak", 0) > 0:
            current["streak"] = max(0, current["streak"] - 1)
        elif emoji == "🟡" and current.get("almost_count", 0) > 0:
            current["almost_count"] = max(0, current["almost_count"] - 1)
        elif emoji == "🔴":
            # Hvis rød fjernes, gir vi ikke tilbake streak, men la den stå
            pass

        streaks[user_id_str] = current
        save_data()
        log_event("checkin_removed", user.id, emoji)
        return

# ------------------ DAGLIG MELDING KL. 16 ------------------
@tasks.loop(minutes=1)
async def daily_checkin():
    now = datetime.datetime.utcnow()
    if now.hour == 16 and now.minute == 0:
        await send_daily_checkin()

async def send_daily_checkin():
    global latest_checkin_message_id
    global latest_checkin_date
    reset_checkins_for_new_day()
    channel = discord.utils.get(bot.get_all_channels(), name='generelt')
    if channel:
        msg = await channel.send("Har du snuset i dag? Reager med:\n✅ for nei\n🟡 for nesten\n🔴 for ja")
        for emoji in ["✅", "🟡", "🔴"]:
            await msg.add_reaction(emoji)
        latest_checkin_message_id = msg.id
        latest_checkin_date = datetime.datetime.utcnow().date()
        log_event("daily_checkin_sent", "bot")

# ------------------ PÅMINNELSE KL. 21 ------------------
@tasks.loop(minutes=1)
async def evening_reminder():
    now = datetime.datetime.utcnow()
    if now.hour == 21 and now.minute == 0:
        channel = discord.utils.get(bot.get_all_channels(), name='generelt')
        if channel:
            not_checked_in = [uid for uid in registered_users if uid not in today_checkins]
            if not_checked_in:
                mentions = " ".join(f"<@{uid}>" for uid in not_checked_in)
                await channel.send(f"Påminnelse til dere som ikke har sjekket inn i dag: {mentions}")
                log_event("reminder_sent", "bot", ",".join(str(uid) for uid in not_checked_in))

# ------------------ BEFORE LOOPS ------------------
@daily_checkin.before_loop
@evening_reminder.before_loop
async def before_loops():
    await bot.wait_until_ready()

bot.run(TOKEN)
