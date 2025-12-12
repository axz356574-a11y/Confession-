# -------------------------
# PART 1 — MAIN SETUP + CONFESSION SYSTEM
# -------------------------

import os
import nextcord
from nextcord.ext import commands
from nextcord import Interaction, SlashOption, Embed
from nextcord.ui import View, Button
import datetime
import threading
from flask import Flask

TOKEN = os.environ.get("TOKEN")

AUTHORIZED_CHECKERS = [
    1438813958848122924,
    933218750285639690,
    1441027710238588968,
    1355140133661184221
]

CONFESSION_ADMINS = AUTHORIZED_CHECKERS.copy()

intents = nextcord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot Running!"

def run_flask():
    app.run(host="0.0.0.0", port=8080)

threading.Thread(target=run_flask).start()

confession_count = 0


# -------------------------
# CONFESSION COMMAND
# -------------------------

@bot.slash_command(name="confess", description="Send an anonymous confession")
async def confess(interaction: Interaction,
                  message: str = SlashOption(description="Your confession text")):

    global confession_count
    confession_count += 1

    embed = Embed(
        title=f"Anonymous Confession #{confession_count}",
        description=message,
        color=0x5865F2
    )
    embed.set_footer(text="Submitted Anonymously")

    class ConfessButtons(View):
        def __init__(self):
            super().__init__(timeout=None)

        @nextcord.ui.button(label="Submit Confession", style=nextcord.ButtonStyle.primary)
        async def submit_confession(self, btn: Button, itx: Interaction):
            await itx.response.send_modal(ConfessModal())

        @nextcord.ui.button(label="Reply Anonymously", style=nextcord.ButtonStyle.secondary)
        async def reply_confession(self, btn: Button, itx: Interaction):
            await itx.response.send_modal(ReplyModal())

    await interaction.response.send_message(embed=embed, view=ConfessButtons())

    # DM the confession to admins
    alert = Embed(
        title="New Confession Logged",
        description=f"**Confession #{confession_count}**\n{message}",
        color=0xFF0000
    )
    alert.add_field(name="User", value=f"{interaction.user} (`{interaction.user.id}`)", inline=False)

    for admin_id in CONFESSION_ADMINS:
        user = await bot.fetch_user(admin_id)
        try:
            await user.send(embed=alert)
        except:
            pass


# -------------------------
# MODALS
# -------------------------

class ConfessModal(nextcord.ui.Modal):
    def __init__(self):
        super().__init__("Submit Confession")

    message = nextcord.ui.TextInput(label="Your Confession", style=nextcord.TextInputStyle.paragraph)

    async def callback(self, interaction: Interaction):
        global confession_count
        confession_count += 1

        embed = Embed(
            title=f"Anonymous Confession #{confession_count}",
            description=self.message.value,
            color=0x5865F2
        )
        embed.set_footer(text="Submitted Anonymously")

        await interaction.response.send_message(embed=embed)

        alert = Embed(
            title="New Confession Logged",
            description=f"**Confession #{confession_count}**\n{self.message.value}",
            color=0xFF0000
        )
        alert.add_field(name="User", value=f"{interaction.user} (`{interaction.user.id}`)", inline=False)

        for admin_id in CONFESSION_ADMINS:
            user = await bot.fetch_user(admin_id)
            try:
                await user.send(embed=alert)
            except:
                pass


class ReplyModal(nextcord.ui.Modal):
    def __init__(self):
        super().__init__("Anonymous Reply")

    message = nextcord.ui.TextInput(label="Your Reply", style=nextcord.TextInputStyle.paragraph)

    async def callback(self, interaction: Interaction):
        embed = Embed(
            title="Anonymous Reply",
            description=self.message.value,
            color=0x2ECC71
        )
        embed.set_footer(text="Sent Anonymously")

        await interaction.response.send_message(embed=embed)

  # -------------------------
# TIMEZONE CHECK COMMAND (48+ HOURS JOIN REQUIREMENT)
# -------------------------

@bot.slash_command(name="checktimezone", description="Check estimated timezone of a member")
async def checktimezone(interaction: Interaction,
                        member: nextcord.Member = SlashOption(description="User to check")):

    if interaction.user.id not in AUTHORIZED_CHECKERS:
        await interaction.response.send_message("❌ You are not allowed to use this command.", ephemeral=True)
        return

    now = datetime.datetime.utcnow()
    joined_at = member.joined_at.replace(tzinfo=None)

    time_in_server = now - joined_at
    hours = time_in_server.total_seconds() / 3600

    if hours < 48:
        await interaction.response.send_message(
            f"❌ {member.mention} has not been in the server for 48 hours.\n"
            f"Time in server: **{round(hours, 1)} hours**",
            ephemeral=True
        )
        return

    # Fake estimation based on ID timestamp
    discord_epoch = ((member.id >> 22) + 1420070400000) / 1000
    account_time = datetime.datetime.utcfromtimestamp(discord_epoch)
    hour_guess = account_time.hour

    embed = Embed(
        title=f"Timezone Estimate for {member}",
        color=0x3498DB
    )
    embed.add_field(name="Estimated Hour", value=f"**UTC+{(hour_guess - now.hour)}**", inline=False)
    embed.add_field(name="Time in Server", value=f"**{round(hours, 1)} hours**", inline=False)

    await interaction.response.send_message(embed=embed)


# -------------------------
# BOT READY
# -------------------------

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


# -------------------------
# RUN BOT
# -------------------------

bot.run(TOKEN)
