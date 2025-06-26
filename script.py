import discord
from discord.ext import commands, tasks
import asyncio
import datetime
import os
import uuid
from supabase import create_client, Client

TOKEN = os.getenv("TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

intents = discord.Intents.default()
intents.messages = True
intents.reactions = True
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

snusstopp_message_id = None
latest_checkin_message_id = None
latest_checkin_date = None
today_checkins = {}  # {user_id: emoji}

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- SUPABASE HJELPEFUNKSJONER ---

async def fetch_registered_users():
    """Hent registrerte brukere fra Supabase."""
    def _fetch():
        res = supabase.table("registered_users").select("user_id").execute()
        return res
    res = await asyncio.to_thread(_fetch)
    if res.error:
        print(f"Feil ved henting av registrerte brukere: {res.error.message}")
        return set()
    return set([row["user_id"] for row in res.data])

async def register_user(user_id: int):
    def _insert():
        return supabase.table("registered_users").insert({"user_id": user_id}).execute()
    # sjekk først om allerede registrert
    users = await fetch_registered_users()
    if user_id in users:
        return
    res = await asyncio.to_thread(_insert)
    if res.error:
        print(f"Feil ved registrering av bruker: {res.error.message}")

async def unregister_user(user_id: int):
    def _delete():
        return supabase.table("registered_users").delete().eq("user_id", user_id).execute()
    res = await asyncio.to_thread(_delete)
    if res.error:
        print(f"Feil ved avregistrering av bruker: {res.error.message}")

async def get_streak(user_id: int):
    def _fetch():
        return supabase.table("streaks").select("*").eq("user_id", user_id).single().execute()
    res = await asyncio.to_thread(_fetch)
    if res.error:
        print(f"Feil ved henting av streak: {res.error.message}")
        return {"streak": 0, "almost_count": 0}
    if res.data:
        return res.data
    return {"streak": 0, "almost_count": 0}

async def save_streak(user_id: int, streak: int, almost_count: int):
    existing = await get_streak(user_id)
    def _upsert():
        if existing and "user_id" in existing:
            return supabase.table("streaks").update({
                "streak": streak,
                "almost_count": almost_count
            }).eq("user_id", user_id).execute()
        else:
            return supabase.table("streaks").insert({
                "user_id": user_id,
                "streak": streak,
                "almost_count": almost_count
            }).execute()
    res = await asyncio.to_thread(_upsert)
    if res.error:
        print(f"Feil ved lagring av streak: {res.error.message}")

async def save_checkin(user_id: int, status: str):
    today = datetime.datetime.utcnow().date().isoformat()
    def _upsert_checkin():
        existing = supabase.table("checkins").select("*")\
            .eq("user_id", user_id)\
            .eq("date", today).execute()
        if existing.data and len(existing.data) > 0:
            checkin_id = existing.data[0]["id"]
            return supabase.table("checkins").update({"status": status}).eq("id", checkin_id).execute()
        else:
            return supabase.table("checkins").insert({
                "id": str(uuid.uuid4()),
                "user_id": user_id,
                "date": today,
                "status": status
            }).execute()
    res = await asyncio.to_thread(_upsert_checkin)
    if res.error:
        print(f"Feil ved lagring av checkin: {res.error.message}")

async def log_event(event_type, user_id, extra=""):
    now = datetime.datetime.utcnow().isoformat()
    data = {
        "event": event_type,
        "user_id": user_id,
        "timestamp": now,
        "extra": extra
    }
    def _insert():
        return supabase.table("logs").insert(data).execute()
    res = await asyncio.to_thread(_insert)
    if res.error:
        print(f"Supabase log error: {res.error.message}")

# --- HJELPEFUNKSJONER ---

def can_checkin_today():
    if latest_checkin_date is None:
        return False
    return datetime.datetime.utcnow().date() == latest_checkin_date

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
    # Hent registrerte brukere fra Supabase for oppdatert liste
    registered_users = await fetch_registered_users()
    if not registered_users:
        user_list = "Ingen deltakere ennå."
    else:
        names = []
        for uid in sorted(registered_users):
            names.append(f"- {get_user_display_name(uid)}")
        user_list = "\n".join(names)
    new_content = f"Trykk ❌ for å bli med i snusstopputfordringen! Fjern ❌ for å melde deg av.\n\n**Deltakere:**\n{user_list}"
    try:
        await message.edit(content=new_content)
    except discord.errors.Forbidden:
        print("Manglende tillatelse til å oppdatere snusstopp-meldingen.")

# --- BOT EVENTS ---

@bot.event
async def on_ready():
    print(f'Logget inn som {bot.user}')
    global snusstopp_message_id
    # Hvis du vil kan du her hente og cache snusstopp_message_id fra supabase eller minne
    daily_checkin.start()
    evening_reminder.start()

# --- BOT COMMANDS ---

@bot.command()
async def snusstopp(ctx):
    global snusstopp_message_id
    registered_users = await fetch_registered_users()
    if not registered_users:
        user_list = "Ingen deltakere ennå."
    else:
        names = []
        for uid in sorted(registered_users):
            names.append(f"- {get_user_display_name(uid)}")
        user_list = "\n".join(names)
    msg = await ctx.send(f"Trykk ❌ for å bli med i snusstopputfordringen! Fjern ❌ for å melde deg av.\n\n**Deltakere:**\n{user_list}")
    await msg.add_reaction("❌")
    snusstopp_message_id = msg.id

@bot.command()
async def streak(ctx):
    data = await get_streak(ctx.author.id)
    streak_count = data.get("streak", 0)
    nesten_count = data.get("almost_count", 0)
    await ctx.send(f"{ctx.author.display_name}, din streak er {streak_count} dager snusfri 🔥 og {nesten_count} 'nesten' dager 🟡.")

# --- REACTION HANDLERS ---

@bot.event
async def on_reaction_add(reaction, user):
    global latest_checkin_message_id
    if user.bot:
        return

    if reaction.message.id == snusstopp_message_id and str(reaction.emoji) == "❌":
        await register_user(user.id)
        await update_snusstopp_message(reaction.message)
        return

    if reaction.message.id == latest_checkin_message_id:
        registered_users = await fetch_registered_users()
        if user.id not in registered_users:
            return

        if not can_checkin_today():
            return

        emoji = str(reaction.emoji)
        if emoji not in ["✅", "🟡", "🔴"]:
            return

        if user.id in today_checkins:
            return  # allerede sjekket inn

        today_checkins[user.id] = emoji

        current = await get_streak(user.id)
        if emoji == "✅":
            current["streak"] = current.get("streak", 0) + 1
        elif emoji == "🟡":
            current["almost_count"] = current.get("almost_count", 0) + 1
        elif emoji == "🔴":
            current["streak"] = 0

        await save_streak(user.id, current["streak"], current["almost_count"])
        await save_checkin(user.id, emoji)
        await log_event("checkin", user.id, emoji)
        return

@bot.event
async def on_reaction_remove(reaction, user):
    if user.bot:
        return

    if reaction.message.id == snusstopp_message_id and str(reaction.emoji) == "❌":
        await unregister_user(user.id)
        await update_snusstopp_message(reaction.message)
        return

    if reaction.message.id == latest_checkin_message_id:
        registered_users = await fetch_registered_users()
        if user.id not in registered_users:
            return

        if not can_checkin_today():
            return

        emoji = str(reaction.emoji)
        if emoji not in ["✅", "🟡", "🔴"]:
            return

        if user.id not in today_checkins:
            return

        del today_checkins[user.id]

        current = await get_streak(user.id)

        # reverser effekten av fjernet reaksjon
        if emoji == "✅" and current.get("streak", 0) > 0:
            current["streak"] = max(0, current["streak"] - 1)
        elif emoji == "🟡" and current.get("almost_count", 0) > 0:
            current["almost_count"] = max(0, current["almost_count"] - 1)
        elif emoji == "🔴":
            pass  # ikke reverser streak på rød fjernet

        await save_streak(user.id, current["streak"], current["almost_count"])
        await log_event("checkin_removed", user.id, emoji)
        return

# --- DAGLIG MELDING KL. 16 ---

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
        await log_event("daily_checkin_sent", "bot")

# --- PÅMINNELSE KL. 21 ---

@tasks.loop(minutes=1)
async def evening_reminder():
    now = datetime.datetime.utcnow()
    if now.hour == 21 and now.minute == 0:
        channel = discord.utils.get(bot.get_all_channels(), name='generelt')
        if channel:
            registered_users = await fetch_registered_users()
            not_checked_in = [uid for uid in registered_users if uid not in today_checkins]
            if not_checked_in:
                mentions = " ".join(f"<@{uid}>" for uid in not_checked_in)
                await channel.send(f"Påminnelse til dere som ikke har sjekket inn i dag: {mentions}")
                await log_event("reminder_sent", "bot", ",".join(str(uid) for uid in not_checked_in))

@daily_checkin.before_loop
@evening_reminder.before_loop
async def before_tasks():
    await bot.wait_until_ready()

bot.run(TOKEN)
