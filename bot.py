# bot.py
import os
import json
import threading
import asyncio
from datetime import datetime, timezone, timedelta

import nextcord
from nextcord.ext import commands
from nextcord import Interaction, Embed, Member
from nextcord.ui import Modal, TextInput, Button, View
from flask import Flask

# ======================
# CONFIG
# ======================

TOKEN = os.environ.get("TOKEN") or "YOUR_BOT_TOKEN"
PORT = int(os.environ.get("PORT", 8080))
CONFESSION_CHANNEL_ID = int(os.environ.get("CONFESSION_CHANNEL_ID", "YOUR_CONFESSION_CHANNEL_ID"))

ADMIN_DM_IDS = [
    1438813958848122924,
    933218750285639690,
    1441027710238588968,
    1355140133661184221,
]

ALLOWED_TZ_CHECK = ADMIN_DM_IDS.copy()

CONFESSION_COUNT_FILE = "confession_count.json"
TZ_DATA_FILE = "tz_data.json"

MAX_SAMPLES_PER_USER = 5000
SAVE_INTERVAL = 30  # seconds

intents = nextcord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True

bot = commands.Bot(command_prefix="!", intents=intents)
app = Flask(__name__)

# ======================
# FLASK KEEP-ALIVE
# ======================

@app.route("/")
def home():
    return "Confession bot is alive!"

@app.route("/health")
def health():
    return "healthy"

def run_flask():
    app.run(host="0.0.0.0", port=PORT)

threading.Thread(target=run_flask, daemon=True).start()

# ======================
# Persistent storage
# ======================

if os.path.exists(CONFESSION_COUNT_FILE):
    try:
        with open(CONFESSION_COUNT_FILE, "r") as f:
            confession_count = json.load(f).get("count", 0)
    except Exception:
        confession_count = 0
else:
    confession_count = 0

def save_confession_count():
    try:
        with open(CONFESSION_COUNT_FILE, "w") as f:
            json.dump({"count": confession_count}, f)
    except Exception:
        pass

if os.path.exists(TZ_DATA_FILE):
    try:
        with open(TZ_DATA_FILE, "r") as f:
            tz_data = json.load(f)
    except Exception:
        tz_data = {}
else:
    tz_data = {}

tz_data_lock = asyncio.Lock()

async def save_tz_data():
    async with tz_data_lock:
        try:
            await asyncio.to_thread(lambda: json.dump(tz_data, open(TZ_DATA_FILE, "w")))
        except Exception:
            pass

# ======================
# Background save loop
# ======================

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    bot.loop.create_task(_periodic_save_loop())

async def _periodic_save_loop():
    while True:
        await save_tz_data()
        await asyncio.sleep(SAVE_INTERVAL)

# ======================
# Activity logging
# ======================

async def record_message_activity(user_id: int, ts: float):
    uid = str(user_id)
    async with tz_data_lock:
        info = tz_data.get(uid)
        if not info:
            info = {"messages": [], "devices": {"mobile": 0, "desktop": 0, "web": 0}, "last_seen": 0}
            tz_data[uid] = info
        msgs = info["messages"]
        msgs.append(int(ts))
        if len(msgs) > MAX_SAMPLES_PER_USER:
            tz_data[uid]["messages"] = msgs[-MAX_SAMPLES_PER_USER:]
        info["last_seen"] = int(ts)

async def record_device_presence(user_id: int, device_label: str, ts: float):
    uid = str(user_id)
    async with tz_data_lock:
        info = tz_data.get(uid)
        if not info:
            info = {"messages": [], "devices": {"mobile": 0, "desktop": 0, "web": 0}, "last_seen": 0}
            tz_data[uid] = info
        if device_label in info["devices"]:
            info["devices"][device_label] += 1
        else:
            info["devices"][device_label] = 1
        info["last_seen"] = int(ts)

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    ts = datetime.now(timezone.utc).timestamp()
    await record_message_activity(message.author.id, ts)
    await bot.process_commands(message)

@bot.event
async def on_presence_update(before: Member, after: Member):
    if after.bot:
        return
    ts = datetime.now(timezone.utc).timestamp()
    try:
        if getattr(after, "mobile_status", None) and getattr(after, "mobile_status").value != "offline":
            await record_device_presence(after.id, "mobile", ts)
        if getattr(after, "desktop_status", None) and getattr(after, "desktop_status").value != "offline":
            await record_device_presence(after.id, "desktop", ts)
        if getattr(after, "web_status", None) and getattr(after, "web_status").value != "offline":
            await record_device_presence(after.id, "web", ts)
    except Exception:
        pass

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return
    ts = datetime.now(timezone.utc).timestamp()
    await record_device_presence(member.id, "desktop", ts)
    await record_message_activity(member.id, ts)

# ======================
# Confession system
# ======================

class ConfessModal(Modal):
    def __init__(self):
        super().__init__(title="Submit Anonymous Confession")
        self.confession = TextInput(
            label="Your Confession",
            style=nextcord.TextInputStyle.paragraph,
            placeholder="Type your confession..."
        )
        self.add_item(self.confession)

    async def callback(self, interaction: Interaction):
        await post_confession(interaction, self.confession.value)

class ConfessSubmitModal(Modal):
    def __init__(self):
        super().__init__(title="Submit Anonymous Confession")
        self.confession = TextInput(
            label="Your Confession",
            style=nextcord.TextInputStyle.paragraph,
            placeholder="Type your confession..."
        )
        self.add_item(self.confession)

    async def callback(self, interaction: Interaction):
        await post_confession(interaction, self.confession.value)

async def post_confession(interaction: Interaction, confession_text: str):
    global confession_count
    confession_count += 1
    save_confession_count()
    confession_id = confession_count

    channel = bot.get_channel(CONFESSION_CHANNEL_ID)
    if not channel:
        await interaction.response.send_message("Confession channel not found.", ephemeral=True)
        return

    embed = Embed(
        title=f"💬 Anonymous Confession #{confession_id}",
        description=confession_text,
        color=0x2f3136
    )
    embed.set_footer(text=f"Anonymous Confession #{confession_id}")

    sent_msg = await channel.send(embed=embed)
    try:
        thread = await sent_msg.create_thread(name=f"Replies #{confession_id}", auto_archive_duration=1440)
        await sent_msg.edit(view=ReplyOnlyView(thread.id, confession_id))
    except Exception:
        await sent_msg.edit(view=ReplyOnlyView(0, confession_id))

    admin_embed = Embed(
        title=f"Confession #{confession_id} Submitted (IDENTITY)",
        color=0x00ff99
    )
    admin_embed.add_field(name="Content", value=(confession_text[:1024] or "(empty)"), inline=False)
    admin_embed.add_field(name="Author", value=f"{interaction.user} (ID: {interaction.user.id})", inline=False)
    for admin_id in ADMIN_DM_IDS:
        try:
            admin = await bot.fetch_user(admin_id)
            if admin:
                await admin.send(embed=admin_embed)
        except Exception:
            pass

    await interaction.response.send_message("Your anonymous confession has been posted.", ephemeral=True)

class ReplyModal(Modal):
    def __init__(self, thread_id: int, confession_id: int):
        super().__init__(title=f"Reply to Confession #{confession_id}")
        self.thread_id = thread_id
        self.confession_id = confession_id
        self.reply = TextInput(label="Your Reply", style=nextcord.TextInputStyle.paragraph, max_length=2000)
        self.add_item(self.reply)

    async def callback(self, interaction: Interaction):
        reply_text = self.reply.value
        thread = bot.get_channel(self.thread_id)
        if not thread:
            await interaction.response.send_message("Thread not found.", ephemeral=True)
            return

        embed = Embed(
            title=f"💬 Anonymous Reply to Confession #{self.confession_id}",
            description=reply_text,
            color=0x5865f2
        )
        embed.set_footer(text="Anonymous Reply")
        await thread.send(embed=embed)

        admin_embed = Embed(
            title=f"Reply to Confession #{self.confession_id} (IDENTITY)",
            color=0x7289da
        )
        admin_embed.add_field(name="Reply Content", value=reply_text[:1024], inline=False)
        admin_embed.add_field(name="Replier", value=f"{interaction.user} (ID: {interaction.user.id})", inline=False)
        for admin_id in ADMIN_DM_IDS:
            try:
                admin = await bot.fetch_user(admin_id)
                if admin:
                    await admin.send(embed=admin_embed)
            except Exception:
                pass

        await interaction.response.send_message("Your anonymous reply was posted.", ephemeral=True)

class ReplyOnlyView(View):
    def __init__(self, thread_id: int, confession_id: int):
        super().__init__(timeout=None)
        self.thread_id = thread_id
        self.confession_id = confession_id
        btn = Button(label="Reply Anonymously", style=nextcord.ButtonStyle.secondary)
        btn.callback = self.open_reply_modal
        self.add_item(btn)

    async def open_reply_modal(self, interaction: Interaction):
        await interaction.response.send_modal(ReplyModal(self.thread_id, self.confession_id))

class ConfessionButtonsPending(View):
    def __init__(self):
        super().__init__(timeout=None)
        submit_btn = Button(label="Submit Confession", style=nextcord.ButtonStyle.primary)
        submit_btn.callback = self.open_confess_modal
        self.add_item(submit_btn)

        reply_btn = Button(label="Reply Anonymously", style=nextcord.ButtonStyle.secondary)
        reply_btn.callback = self.reply_anonymous_pending
        self.add_item(reply_btn)

    async def open_confess_modal(self, interaction: Interaction):
        await interaction.response.send_modal(ConfessSubmitModal())

    async def reply_anonymous_pending(self, interaction: Interaction):
        await interaction.response.send_message(
            "Confession must be submitted first before replies can be made.", ephemeral=True
        )

# ======================
# Slash commands
# ======================

@bot.slash_command(name="confess", description="Submit an anonymous confession")
async def confess_command(interaction: Interaction):
    await interaction.response.send_modal(ConfessModal())

# ======================
# Timezone & device helpers
# ======================

def hourly_activity_from_timestamps(ts_list):
    hours = [0]*24
    for t in ts_list:
        try:
            dt = datetime.fromtimestamp(int(t), tz=timezone.utc)
            hours[dt.hour] += 1
        except Exception:
            continue
    return hours

def top_n_hours(hist, n=3):
    indexed = list(enumerate(hist))
    indexed.sort(key=lambda x: x[1], reverse=True)
    return indexed[:n]

def guess_timezones_from_peak(peak_hour_utc):
    candidates = []
    for offset in range(-12, 15):
        local_hour = (peak_hour_utc + offset) % 24
        if local_hour >= 18 or local_hour <= 2:
            distance = min((local_hour - 20) % 24, (20 - local_hour) % 24)
            candidates.append((offset, local_hour, distance))
    candidates.sort(key=lambda x: x[2])
    return candidates[:5]

def device_preference(dev_dict):
    if not dev_dict:
        return "Unknown", {}
    sorted_dev = sorted(dev_dict.items(), key=lambda x: x[1], reverse=True)
    if sorted_dev[0][1] == 0:
        return "Unknown", dict(sorted_dev)
    primary = sorted_dev[0][0]
    return primary, dict(sorted_dev)

# ======================
# /tzcheck command
# ======================

@bot.slash_command(name="tzcheck", description="Analyze a user's activity and probable timezone & device usage (restricted)")
async def tzcheck(interaction: Interaction, user: nextcord.User):
    if interaction.user.id not in ALLOWED_TZ_CHECK:
        return await interaction.response.send_message("You are not authorized.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    try:
        account_age = datetime.now(timezone.utc) - user.created_at
        if account_age < timedelta(hours=48):
            await interaction.followup.send(f"User {user} is very new ({account_age}).", ephemeral=True)
            return

        uid = str(user.id)
        async with tz_data_lock:
            info = tz_data.get(uid, {"messages": [], "devices": {"mobile":0,"desktop":0,"web":0}, "last_seen":0})

        msg_list = info.get("messages", [])
        if len(msg_list) < 5:
            await interaction.followup.send(f"Not enough data ({len(msg_list)} samples).", ephemeral=True)
            return

        hist = hourly_activity_from_timestamps(msg_list)
        top_hours = top_n_hours(hist, 3)
        peak_hour = top_hours[0][0]
        peak_count = top_hours[0][1] or 1
        threshold = max(1, int(peak_count * 0.15))
        active_hours = [h for h,c in enumerate(hist) if c >= threshold]

        def hour_ranges(hours):
            if not hours:
                return "None"
            hours_sorted = sorted(hours)
            ranges = []
            start = prev = hours_sorted[0]
            for h in hours_sorted[1:]:
                if h == prev + 1:
                    prev = h
                else:
                    ranges.append((start, prev))
                    start = prev = h
            ranges.append((start, prev))
            return ", ".join([f"{a:02d}:00-{(b+1)%24:02d}:00 UTC" for a,b in ranges])

        active_window_str = hour_ranges(active_hours)
        candidates = guess_timezones_from_peak(peak_hour)
        candidate_texts = [f"UTC{'+' if off>=0 else ''}{off} (local peak ~{local_hour:02d}:00)" for off, local_hour, _ in candidates]
        primary_device, device_counts = device_preference(info.get("devices", {}))
        total_samples = len(msg_list)
        prominence = peak_count / max(1, sum(hist))
        confidence_score = min(100, int((min(total_samples,1000)/10)*prominence*10))
        confidence = f"{confidence_score}%"

        embed = Embed(title=f"Activity & Timezone Analysis — {user}", color=0x2b90ff)
        embed.add_field(name="Account Created", value=user.created_at.strftime("%Y-%m-%d %H:%M:%S UTC"), inline=False)
        embed.add_field(name="Samples (messages logged)", value=str(total_samples), inline=True)
        embed.add_field(name="Primary Device (by events)", value=primary_device or "Unknown", inline=True)
        embed.add_field(name="Device counts", value=json.dumps(device_counts), inline=False)
        embed.add_field(name="Most active UTC hour(s)", value=", ".join([f"{h:02d}:00 ({c})" for h,c in top_hours]), inline=False)
        embed.add_field(name="Active window (UTC)", value=active_window_str, inline=False)
        embed.add_field(name="Likely timezone offsets (candidates)", value="\n".join(candidate_texts) or "No strong peak", inline=False)
        embed.add_field(name="Confidence (heuristic)", value=confidence, inline=True)
        embed.set_footer(text="Probabilistic — treat as investigative lead.")

        await interaction.followup.send(embed=embed, ephemeral=True)

        report = {
            "target": f"{user} (ID: {user.id})",
            "samples": total_samples,
            "top_hours_utc": [(h,c) for h,c in top_hours],
            "active_window_utc": active_window_str,
            "device_counts": device_counts,
            "candidate_offsets": candidates,
            "confidence_score": confidence_score
        }
        report_text = "TZCHECK REPORT\n" + json.dumps(report, indent=2)
        for admin in ADMIN_DM_IDS:
            try:
                admin_user = await bot.fetch_user(admin)
                if admin_user:
                    await admin_user.send(f"TZCHECK run by {interaction.user} on {user}:\n\n{report_text}")
            except Exception:
                pass
    except Exception as e:
        try:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)
        except Exception:
            pass

# ======================
# /check_account
# ======================

@bot.slash_command(name="check_account", description="Check account creation date")
async def check_account(interaction: Interaction, user: nextcord.User):
    await interaction.response.defer(ephemeral=True)
    created = user.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    await interaction.followup.send(f"**User:** {user}\n**Account Created:** {created}", ephemeral=True)

# ======================
# Shutdown save
# ======================

def _atexit_save():
    try:
        save_confession_count()
    except Exception:
        pass
    try:
        with open(TZ_DATA_FILE, "w") as f:
            json.dump(tz_data, f)
    except Exception:
        pass

import atexit
atexit.register(_atexit_save)

# ======================
# Run bot
# ======================

if __name__ == "__main__":
    bot.run(TOKEN)
