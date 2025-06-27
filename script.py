import discord
from discord.ext import commands, tasks
import asyncio
import datetime
import os
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
registered_users = set()
today_checkins = {}  # {user_id: emoji}
latest_checkin_message_id = None
latest_checkin_date = None

# Opprett supabase klient
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- ASYNKRONE SUPABASE FUNKSJONER ---

async def log_event(event_type, user_id, extra=""):
    now = datetime.datetime.utcnow().isoformat()
    data = {
        "event": event_type,
        "user_id": str(user_id),
        "timestamp": now,
        "extra": extra
    }
    def insert_log():
        return supabase.table("logs").insert(data).execute()
    result = await asyncio.to_thread(insert_log)
    if result.error:
        print(f"Supabase log error: {result.error.message}")

async def register_user(user_id: int):
    if user_id in registered_users:
        return
    registered_users.add(user_id)
    await log_event("register", user_id)

async def unregister_user(user_id: int):
    if user_id in registered_users:
        registered_users.remove(user_id)
        await log_event("unregister", user_id)

async def get_streak(user_id: int):
    def fetch_streak():
        return supabase.table("streaks").select("*").eq("user_id", str(user_id)).execute()
    result = await asyncio.to_thread(fetch_streak)
    if result.error:
        print(f"Feil ved henting streak: {result.error.message}")
        return {"streak": 0, "almost_count": 0}
    if result.data and len(result.data) > 0:
        return result.data[0]
    return {"streak": 0, "almost_count": 0}

async def save_streak(user_id: int, streak: int, almost_count: int):
    existing = await get_streak(user_id)
    def upsert():
        if existing and "user_id" in existing:
            return supabase.table("streaks").update({
                "streak": streak,
                "almost_count": almost_count
            }).eq("user_id", str(user_id)).execute()
        else:
            return supabase.table("streaks").insert({
                "user_id": str(user_id),
                "streak": streak,
                "almost_count": almost_count
            }).execute()
    result = await asyncio.to_thread(upsert)
    if result.error:
        print(f"Feil ved lagring av streak: {result.error.message}")

async def save_checkin(user_id: int, status: str):
    today = datetime.datetime.utcnow().date().isoformat()
    def upsert_checkin():
        existing = supabase.table("checkins").select("*")\
            .eq("user_id", str(user_id))\
            .eq("date", today).execute()
        if existing.data and len(existing.data) > 0:
            checkin_id = existing.data[0]["id"]
            return supabase.table("checkins").update({"status": status}).eq("id", checkin_id).execute()
        else:
            return supabase.table("checkins").insert({
                "user_id": str(user_id),
                "date": today,
                "status": status
            }).execute()
    result = await asyncio.to_thread(upsert_checkin)
    if result.error:
        print(f"Feil ved lagring av checkin: {result.error.message}")

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
    return user.display_name if user else f"User({user_id})"

async def update_snusstopp_message(message):
    user_list = "\n".join(f"- {get_user_display_name(uid)}" for uid in sorted(registered_users)) or "Ingen deltakere ennå."
    content = f"Trykk ❌ for å bli med i snusstopputfordringen! Fjern ❌ for å melde deg av.\n\n**Deltakere:**\n{user_list}"
    try:
        await message.edit(content=content)
    except discord.errors.Forbidden:
        print("Manglende tillatelse til å oppdatere meldingen.")

# --- BOT EVENTS ---

@bot.event
async def on_ready():
    print(f'Logget inn som {bot.user}')
    daily_checkin.start()
    evening_reminder.start()

# --- KOMMANDOER ---

@bot.command()
async def snusstopp(ctx):
    global snusstopp_message_id
    user_list = "\n".join(f"- {get_user_display_name(uid)}" for uid in sorted(registered_users)) or "Ingen deltakere ennå."
    msg = await ctx.send(f"Trykk ❌ for å bli med i snusstopputfordringen! Fjern ❌ for å melde deg av.\n\n**Deltakere:**\n{user_list}")
    await msg.add_reaction("❌")
    snusstopp_message_id = msg.id

@bot.command()
async def streak(ctx):
    data = await get_streak(ctx.author.id)
    streak_count = data.get("streak", 0)
    nesten_count = data.get("almost_count", 0)
    await ctx.send(f"{ctx.author.display_name}, din streak er {streak_count} dager snusfri 🔥 og {nesten_count} 'nesten' dager 🟡.")

@bot.command()
@commands.has_permissions(administrator=True)
async def triggercheckin(ctx):
    await send_daily_checkin()

# --- REAKSJONSHÅNDTERING ---

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
        if user.id not in registered_users or not can_checkin_today():
            return
        emoji = str(reaction.emoji)
        if emoji not in ["✅", "🟡", "🔴"] or user.id in today_checkins:
            return

        today_checkins[user.id] = emoji
        current = await get_streak(user.id)
        if emoji == "✅":
            current["streak"] += 1
        elif emoji == "🟡":
            current["almost_count"] += 1
        elif emoji == "🔴":
            current["streak"] = 0

        await save_streak(user.id, current["streak"], current["almost_count"])
        await save_checkin(user.id, emoji)
        await log_event("checkin", user.id, emoji)

@bot.event
async def on_reaction_remove(reaction, user):
    if user.bot:
        return

    if reaction.message.id == snusstopp_message_id and str(reaction.emoji) == "❌":
        await unregister_user(user.id)
        await update_snusstopp_message(reaction.message)
        return

    if reaction.message.id == latest_checkin_message_id:
        if user.id not in registered_users or not can_checkin_today():
            return
        emoji = str(reaction.emoji)
        if emoji not in ["✅", "🟡", "🔴"] or user.id not in today_checkins:
            return

        del today_checkins[user.id]
        current = await get_streak(user.id)

        if emoji == "✅":
            current["streak"] = max(0, current["streak"] - 1)
        elif emoji == "🟡":
            current["almost_count"] = max(0, current["almost_count"] - 1)

        await save_streak(user.id, current["streak"], current["almost_count"])
        await log_event("checkin_removed", user.id, emoji)

# --- DAGLIG MELDING KL. 16 ---

@tasks.loop(minutes=1)
async def daily_checkin():
    now = datetime.datetime.utcnow()
    if now.hour == 16 and now.minute == 0:
        await send_daily_checkin()

async def send_daily_checkin():
    global latest_checkin_message_id, latest_checkin_date
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
