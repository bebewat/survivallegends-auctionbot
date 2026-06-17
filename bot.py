import os
import logging
import time
import discord
from discord import app_commands
from discord.ext import commands, tasks
import sqlite3
import config
from mcrcon import MCRcon
from dotenv import load_dotenv

load_dotenv()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.config(
  level=getattr(logging, LOG_LEVEL, logging.INFO),
  format"%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
)
log = logging.getLogger("bot")

config = load_config("config.json")

TOKEN = os.getenv("TOKEN", "").strip()
if not TOKEN:
  raise Runtime Error("TOKEN is not set in .env")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)

def init_local_db():
  conn = sqlite3.connect("auctionbot.db")
  cursor = conn.cursor()
  cursor.execute('''CREATE TABLE IF NOT EXISTS
  active_auctions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, item_name TEXT, start_bid INTEGER,
    highest_bid INTEGER, highest_bidder TEXT, end_time REAL, 
    channel_id TEXT, message_id TEXT, description TEXT, image_url TEXT,
    creator_name TEXT, created_at REAL
  )''')
  conn.commit()
  conn.close()

def format_time(mins):
  days = mins // 1440
  hours = (mins % 1440) // 60
  minutes = mins % 60
  return f"📅 {days}d | 🕒 {hours}h | ⏳ {minutes}m"

def deduct_points(eos_id, amount):
  try: 
    with MCRcon(RCON_HOST, RCON_PASSWORD, port=RCON_PORT)
    as mcr:
      mcr.command(f"ChangePoints {eos_id} -{amount}
      ")
  except Exception as e:
    print(f"RCON Error: {e}")

class AuctionView(discord.ui.View):
  def __init__((self, message_id): 
        super().__init__(timeout=None)
        self.message_id = message_id

    async def update_bid(self, i: discord.Interaction, amount: int):
        conn = sqlite3.connect("AuctionBot.db")
        cursor = conn.cursor()
        cursor.execute("SELECT highest_bid, highest_bidder, creator_name FROM active_auctions WHERE message_id = ?", (self.message_id,))
        data = cursor.fetchone()
        
        # Security: Prevent creator from bidding on their own item
        if data[2] == i.user.name:
            return await i.response.send_message("❌ You cannot bid on your own auction!", ephemeral=True)
        # Security: Prevent outbidding self
        if data[1] == i.user.name:
            return await i.response.send_message("❌ You are already the highest bidder!", ephemeral=True)
            
        new_bid = (data[0] or 0) + amount
        bidder = i.user.name
        
        cursor.execute("UPDATE active_auctions SET highest_bid = ?, highest_bidder = ? WHERE message_id = ?", (new_bid, bidder, self.message_id))
        conn.commit()
        
        embed = i.message.embeds[0]
        embed.set_field_at(1, name="💰 Current Bid", value=f"✅ **{new_bid} Points**", inline=True)
        embed.set_field_at(2, name="👤 Highest Bidder", value=f"**{bidder}**", inline=True)
        await i.message.edit(embed=embed)
        await i.response.send_message(f"Bid of +{amount} placed! New total: {new_bid}", ephemeral=True)
        conn.close()

    @discord.ui.button(label="+100 🔨", style=discord.ButtonStyle.green, custom_id="bid_100")
    async def bid_100(self, i: discord.Interaction, b: discord.ui.Button): await self.update_bid(i, 100)
    @discord.ui.button(label="+500 🔨", style=discord.ButtonStyle.primary, custom_id="bid_500")
    async def bid_500(self, i: discord.Interaction, b: discord.ui.Button): await self.update_bid(i, 500)
    @discord.ui.button(label="+1000 🔨", style=discord.ButtonStyle.danger, custom_id="bid_1000")
    async def bid_1000(self, i: discord.Interaction, b: discord.ui.Button): await self.update_bid(i, 1000)

@tasks.loop(seconds=60)
async def dynamic_updater():
    conn = sqlite3.connect("AuctionBot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM active_auctions")
    auctions = cursor.fetchall()
    
    for row in auctions:
        remaining_mins = int((row[5] - time.time()) / 60)
        if remaining_mins <= 0:
            try:
                if row[4]: deduct_points(row[4], row[3])
                channel = await bot.fetch_channel(int(row[6]))
                msg = await channel.fetch_message(int(row[7]))
                exp_chan = bot.get_channel(EXPIRED_CHANNEL_ID)
                if exp_chan:
                    embed = discord.Embed(title=f"🚫 AUCTION CLOSED: {row[1].upper()} 🚫", color=discord.Color.light_gray())
                    embed.add_field(name="Final Bid", value=f"✅ **{row[3]} Points**", inline=True)
                    embed.add_field(name="Winner", value=row[4] or "No bids", inline=True)
                    if row[9]: embed.set_image(url=row[9])
                    await exp_chan.send(embed=embed)
                await msg.delete()
            except: pass
            cursor.execute("DELETE FROM active_auctions WHERE id = ?", (row[0],))
        else:
            try:
                channel = await bot.fetch_channel(int(row[6]))
                msg = await channel.fetch_message(int(row[7]))
                embed = msg.embeds[0]
                embed.set_field_at(4, name="⏰ Time Remaining", value=f"**{format_time(remaining_mins)}**", inline=False)
                await msg.edit(embed=embed)
            except: pass
    conn.commit()
    conn.close()

@bot.tree.command(name="launch_auction", description="Launch a new auction")
@app_commands.choices(duration=[
    app_commands.Choice(name="1 Minute Test", value=1),
    app_commands.Choice(name="24 Hours", value=1440),
    app_commands.Choice(name="48 Hours", value=2880),
    app_commands.Choice(name="5 Days", value=7200)
])
async def launch_auction(i: discord.Interaction, item: str, starting_bid: int, duration: int, description: str = None, image: discord.Attachment = None):
    user_role_ids = [role.id for role in i.user.roles]
    if not any(role_id in user_role_ids for role_id in ALLOWED_ROLE_IDS):
        return await i.response.send_message("❌ You are not authorized.", ephemeral=True)
    
    if i.channel_id != AUCTION_CHANNEL_ID:
        return await i.response.send_message("❌ Please use the correct auction channel.", ephemeral=True)
        
    # Check 24hr limit (2 per 24 hours)
    conn = sqlite3.connect("AuctionBot.db")
    cursor = conn.cursor()
    day_ago = time.time() - 86400
    cursor.execute("SELECT COUNT(*) FROM active_auctions WHERE creator_name = ? AND created_at > ?", (i.user.name, day_ago))
    if cursor.fetchone()[0] >= 2:
        conn.close()
        return await i.response.send_message("❌ You have reached your limit of 2 auctions per 24 hours.", ephemeral=True)

    end_time = time.time() + (duration * 60)
    created_at = time.time()
    
    embed = discord.Embed(title=f"🔹 {item.upper()} AUCTION 🔹", description=f"{description or 'No details provided.'}", color=discord.Color.blue())
    embed.set_thumbnail(url=SPINNING_LOGO_URL)
    embed.add_field(name="\u200b", value="\u200b", inline=False)
    embed.add_field(name="💰 Current Bid", value=f"✅ **{starting_bid} Points**", inline=True)
    embed.add_field(name="👤 Highest Bidder", value=f"**None yet**", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=False)
    embed.add_field(name="⏰ Time Remaining", value=f"**{format_time(duration)}**", inline=False)
    
    if image: embed.set_image(url=image.url)
    
    msg = await i.channel.send(embed=embed)
    await msg.edit(view=AuctionView(str(msg.id)))
    
    cursor.execute("INSERT INTO active_auctions (item_name, start_bid, highest_bid, end_time, channel_id, message_id, description, image_url, creator_name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", 
                   (item, starting_bid, starting_bid, end_time, str(i.channel_id), str(msg.id), description, image.url if image else None, i.user.name, created_at))
    conn.commit()
    conn.close()
    await i.response.send_message("Auction live!", ephemeral=True)

@bot.event
async def on_ready():
    init_local_db()
    await bot.tree.sync()
    dynamic_updater.start()
    print("🚀 Shop Tool is ONLINE (RCON Enabled, Security Active).")

bot.run(TOKEN)
    
