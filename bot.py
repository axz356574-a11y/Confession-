# bot.py
import os
import json
import threading
import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

import nextcord
from nextcord.ext import commands, tasks
from nextcord import Interaction, Embed, Member
from nextcord.ui import Modal, TextInput, Button, View
from flask import Flask

# ======================
# CONFIG
# ======================

TOKEN = os.environ.get("TOKEN") or "YOUR_BOT_TOKEN"
PORT = int(os.environ.get("PORT", 8080))
CONFESSION_CHANNEL_ID = int(os.environ.get("CONFESSION_CHANNEL_ID", "YOUR_CONFESSION_CHANNEL_ID"))

# Admins who receive DMs about identities and who can use /tzcheck
ADMIN_DM_IDS = [
    1438813958848122924,
    933218750285639690,
    1441027710238588968,
    1355140133661184221,
]

# These four are allowed to run /tzcheck (same as ADMIN_DM_IDS per your request)
ALLOWED_TZ_CHECK = ADMIN_DM_IDS.copy()

# Files
CONFESSION_COUNT_FILE = "confession_count.json"
TZ_DATA_FILE = "tz_data.json"

# Activity data retention / params
MAX_SAMPLES_PER_USER = 5000  # cap per user to prevent blowup
SAVE_INTERVAL = 30  # seconds

# Intents and bot
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

# Start flask in background so hosters don't mark app as unresponsive
threading.Thread(target=run_flask, daemon=True).start()

# ======================
# Persistent counters & tz data load/save
# ======================

# Confession counter
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

# Timezone/activity store
# Structure:
# {
#   "<user_id>": {
#       "messages": [unix_ts, ...],
#       "devices": {"mobile": N, "desktop": N, "web": N},
#       "last_seen": unix_ts
#   },
#   ...
# }
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
            with open(TZ_DATA_FILE, "w") as f:
                json.dump(tz_data, f)
        except Exception:
            pass

# Periodic save task
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    # start background saver loop
    bot.loop.create_task(_periodic_save_loop())

async def _periodic_save_loop():
    while True:
        await save_tz_data()
        await asyncio.sleep(SAVE_INTERVAL)

# Helper to record activity
async def record_message_activity(user_id: int, ts: float):
    uid = str(user_id)
    async with tz_data_lock:
        info = tz_data.get(uid)
        if not info:
            info = {"messages": [], "devices": {"mobile": 0, "desktop": 0, "web": 0}, "last_seen": 0}
            tz_data[uid] = info
        msgs = info["messages"]
        msgs.append(int(ts))
        # cap size
        if len(msgs) > MAX_SAMPLES_PER_USER:
            # keep most recent
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

# ======================
# Activity events (log messages & presence)
# ======================

@bot.event
async def on_message(message):
    # do not record bot messages
    if message.author.bot:
        return
    # record timestamp for message author
    ts = datetime.now(timezone.utc).timestamp()
    await record_message_activity(message.author.id, ts)

    # process commands after our logging
    await bot.process_commands(message)

@bot.event
async def on_presence_update(before: Member, after: Member):
    # presence updates may be frequent; only record when moving online or device statuses change
    try:
        if after.bot:
            return
        ts = datetime.now(timezone.utc).timestamp()
        # device presence checks; nextcord provides mobile_status, desktop_status, web_status attributes on Member
        try:
            if getattr(after, "mobile_status", None) and getattr(after, "mobile_status").value != "offline":
                await record_device_presence(after.id, "mobile", ts)
            if getattr(after, "desktop_status", None) and getattr(after, "desktop_status").value != "offline":
                await record_device_presence(after.id, "desktop", ts)
            if getattr(after, "web_status", None) and getattr(after, "web_status").value != "offline":
                await record_device_presence(after.id, "web", ts)
        except Exception:
            # If attributes differ by version, ignore device logging for this event
            pass
    except Exception:
        pass

@bot.event
async def on_voice_state_update(member, before, after):
    # track when user joins voice as activity (likely indicative of timezone)
    if member.bot:
        return
    ts = datetime.now(timezone.utc).timestamp()
    # treat voice as desktop unless we detect mobile presence
    await record_device_presence(member.id, "desktop", ts)
    await record_message_activity(member.id, ts)

# ======================
# Confessions system (modal, publish, threads, replies)
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
        global confession_count
        confession_count += 1
        save_confession_count()

        confession_text = self.confession.value
        confession_id = confession_count

        public_embed = Embed(
            title=f"💬 Anonymous Confession #{confession_id}",
            description=confession_text,
            color=0x2f3136
        )
        public_embed.set_footer(text=f"Anonymous Confession #{confession_id}")

        channel = bot.get_channel(CONFESSION_CHANNEL_ID)
        if not channel:
            await interaction.response.send_message("Confession channel not found or bot lacks access.", ephemeral=True)
            return

        # Buttons view with submit & reply (pending state)
        view = ConfessionButtonsPending(confession_text, confession_id, interaction.user)

        # send pending embed (this is anonymous in channel but admins are DM'ed)
        pending_msg = await channel.send(embed=public_embed, view=view)

        # DM admins with identity
        admin_embed = Embed(
            title=f"Confession #{confession_id} Submitted (IDENTITY)",
            color=0xff0000
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

        # respond to user immediately (avoid interaction timeouts)
        await interaction.response.send_message("Your anonymous confession has been posted (pending).", ephemeral=True)

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

        # post reply as embed in thread
        embed = Embed(title=f"💬 Anonymous Reply to Confession #{self.confession_id}", description=reply_text, color=0x5865f2)
        embed.set_footer(text="Anonymous Reply")
        await thread.send(embed=embed)

        # notify admins about reply identity
        admin_embed = Embed(title=f"Reply to Confession #{self.confession_id} (IDENTITY)", color=0x7289da)
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
    def __init__(self, confession_text: str, confession_id: int, confessor: nextcord.User):
        super().__init__(timeout=None)
        self.confession_text = confession_text
        self.confession_id = confession_id
        self.confessor = confessor

        submit_btn = Button(label="Submit Confession", style=nextcord.ButtonStyle.primary)
        submit_btn.callback = self.submit_confession
        self.add_item(submit_btn)

        reply_btn = Button(label="Reply Anonymously", style=nextcord.ButtonStyle.secondary)
        reply_btn.callback = self.reply_anonymous_pending
        self.add_item(reply_btn)

    async def submit_confession(self, interaction: Interaction):
        channel = bot.get_channel(CONFESSION_CHANNEL_ID)
        if not channel:
            await interaction.response.send_message("Confession channel not found.", ephemeral=True)
            return

        # publish confession publicly as embed
        embed = Embed(title=f"💬 Anonymous Confession #{self.confession_id}", description=self.confession_text, color=0x2f3136)
        embed.set_footer(text=f"Anonymous Confession #{self.confession_id}")
        sent_msg = await channel.send(embed=embed)

        # create replies thread under the confession message
        try:
            thread = await sent_msg.create_thread(name=f"Replies #{self.confession_id}", auto_archive_duration=1440)
            # set the view on the sent message so published confession has Reply button only
            await sent_msg.edit(view=ReplyOnlyView(thread.id, self.confession_id))
        except Exception:
            # If thread creation fails, still attach ReplyOnlyView without thread (replies won't thread)
            await sent_msg.edit(view=ReplyOnlyView(0, self.confession_id))

        # ack to clicker
        await interaction.response.send_message("Confession submitted anonymously.", ephemeral=True)

        # DM admins identity (again)
        admin_embed = Embed(title=f"Confession #{self.confession_id} Published (IDENTITY)", color=0x00ff99)
        admin_embed.add_field(name="Content", value=(self.confession_text[:1024] or "(empty)"), inline=False)
        admin_embed.add_field(name="Author", value=f"{self.confessor} (ID: {self.confessor.id})", inline=False)
        for admin_id in ADMIN_DM_IDS:
            try:
                admin = await bot.fetch_user(admin_id)
                if admin:
                    await admin.send(embed=admin_embed)
            except Exception:
                pass

    async def reply_anonymous_pending(self, interaction: Interaction):
        # cannot reply in pending mode (must publish first to have thread)
        await interaction.response.send_message("Confession must be submitted first before replies can be made.", ephemeral=True)

# Slash command to open confess modal
@bot.slash_command(name="confess", description="Submit an anonymous confession")
async def confess_command(interaction: Interaction):
    await interaction.response.send_modal(ConfessModal())

# ======================
# Timezone & device detection helpers
# ======================

def hourly_activity_from_timestamps(ts_list):
    # ts_list: list of unix timestamps (ints)
    # returns histogram dict hour(0-23) -> count
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
    return indexed[:n]  # list of (hour, count)

def guess_timezones_from_peak(peak_hour_utc):
    """
    Simple heuristic:
    We'll propose candidate UTC offsets (-12..+14) where the local hour of peak falls in evening window (18..2)
    The function returns list of offsets that make local_peak in evening window, ordered by closeness to 20:00 local.
    """
    candidates = []
    for offset in range(-12, 15):  # inclusive +14
        local_hour = (peak_hour_utc + offset) % 24
        # prefer evening/night peaks
        if local_hour >= 18 or local_hour <= 2:
            # score by closeness to 20:00 (preferred)
            distance = min((local_hour - 20) % 24, (20 - local_hour) % 24)
            candidates.append((offset, local_hour, distance))
    # sort by distance (smaller is better)
    candidates.sort(key=lambda x: x[2])
    # return top 5 candidates
    return candidates[:5]

def device_preference(dev_dict):
    # dev_dict like {"mobile": N, "desktop": N, "web": N}
    if not dev_dict:
        return "Unknown", {}
    sorted_dev = sorted(dev_dict.items(), key=lambda x: x[1], reverse=True)
    if sorted_dev[0][1] == 0:
        return "Unknown", dict(sorted_dev)
    primary = sorted_dev[0][0]
    return primary, dict(sorted_dev)

# ======================
# Slash command: /tzcheck restricted
# ======================

@bot.slash_command(name="tzcheck", description="Analyze a user's activity and probable timezone & device usage (restricted)")
async def tzcheck(interaction: Interaction, user: nextcord.User):
    # Restrict usage
    if interaction.user.id not in ALLOWED_TZ_CHECK:
        return await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)

    # Defer immediately to avoid timeout while we compute
    await interaction.response.defer(ephemeral=True)

    try:
        # require account age >48 hours
        account_age = datetime.now(timezone.utc) - user.created_at
        if account_age < timedelta(hours=48):
            await interaction.followup.send(f"User {user} is very new ({account_age}). Too new to analyze reliably.", ephemeral=True)
            return

        # get activity from tz_data
        uid = str(user.id)
        async with tz_data_lock:
            info = tz_data.get(uid, {"messages": [], "devices": {"mobile":0,"desktop":0,"web":0}, "last_seen": 0})

        msg_list = info.get("messages", [])
        if len(msg_list) < 5:
            await interaction.followup.send(f"Not enough data to analyze user {user}. Only {len(msg_list)} samples.", ephemeral=True)
            return

        # Build histogram of hours (UTC)
        hist = hourly_activity_from_timestamps(msg_list)
        top_hours = top_n_hours(hist, n=3)  # top 3 peak hours
        peak_hour = top_hours[0][0]

        # Determine active window: hours where count >= 15% of peak count
        peak_count = top_hours[0][1] or 1
        threshold = max(1, int(peak_count * 0.15))
        active_hours = [h for h,c in enumerate(hist) if c >= threshold]
        # summarize active window ranges
        def hour_ranges(hours):
            if not hours:
                return "None"
            hours_sorted = sorted(hours)
            ranges = []
            start = hours_sorted[0]
            prev = start
            for h in hours_sorted[1:]:
                if h == prev + 1:
                    prev = h
                    continue
                else:
                    ranges.append((start, prev))
                    start = h
                    prev = h
            ranges.append((start, prev))
            return ", ".join([f"{a:02d}:00-{(b+1)%24:02d}:00 UTC" for a,b in ranges])

        active_window_str = hour_ranges(active_hours)

        # Guess candidate offsets
        candidates = guess_timezones_from_peak(peak_hour)
        candidate_texts = []
        for off, local_hour, dist in candidates:
            sign = "+" if off >= 0 else ""
            candidate_texts.append(f"UTC{sign}{off} (local peak ~{local_hour:02d}:00)")

        # device preference
        primary_device, device_counts = device_preference(info.get("devices", {}))

        # Confidence metric: based on number of samples and peak prominence
        total_samples = len(msg_list)
        prominence = peak_count / max(1, sum(hist))
        confidence_score = min(100, int((min(total_samples, 1000) / 10) * prominence * 10))  # heuristic
        confidence = f"{confidence_score}%"

        # Build ephemeral reply
        embed = Embed(title=f"Activity & Timezone Analysis — {user}", color=0x2b90ff)
        embed.add_field(name="Account Created", value=user.created_at.strftime("%Y-%m-%d %H:%M:%S UTC"), inline=False)
        embed.add_field(name="Samples (messages logged)", value=str(total_samples), inline=True)
        embed.add_field(name="Primary Device (by events)", value=primary_device or "Unknown", inline=True)
        embed.add_field(name="Device counts", value=json.dumps(device_counts), inline=False)
        embed.add_field(name="Most active UTC hour(s)", value=", ".join([f"{h:02d}:00 ({c})" for h,c in top_hours]), inline=False)
        embed.add_field(name="Active window (UTC)", value=active_window_str, inline=False)
        if candidate_texts:
            embed.add_field(name="Likely timezone offsets (candidates)", value="\n".join(candidate_texts), inline=False)
        else:
            embed.add_field(name="Likely timezone offsets (candidates)", value="No strong evening/night peak found", inline=False)
        embed.add_field(name="Confidence (heuristic)", value=confidence, inline=True)
        embed.set_footer(text="This is probabilistic — treat as investigative lead, not proof.")

        # send ephemeral summary
        await interaction.followup.send(embed=embed, ephemeral=True)
        # send full report to admins (DM)
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
                    try:
                        await admin_user.send(f"TZCHECK run by {interaction.user} on {user}:\n\n{report_text}")
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception as e:
        # fail-safe reply
        try:
            await interaction.followup.send(f"An error occurred while analyzing: {e}", ephemeral=True)
        except Exception:
            pass
# ======================
# Utilities: account age check command (optional quick check)
# ======================
@bot.slash_command(name="check_account", description="Check account creation date")
async def check_account(interaction: Interaction, user: nextcord.User):
    await interaction.response.defer(ephemeral=True)
    created = user.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    await interaction.followup.send(f"**User:** {user}\n**Account Created:** {created}", ephemeral=True)
# ======================
# Run
# ======================
# save confession count on shutdown
def _atexit_save():
    try:
        save_confession_count()
    except Exception:
        pass
    try:
        # sync tz_data final save (blocking small IO)
        with open(TZ_DATA_FILE, "w") as f:
            json.dump(tz_data, f)
    except Exception:
        pass
import atexit
atexit.register(_atexit_save)
# Start the bot
if __name__ == "__main__":
    bot.run(TOKEN)
