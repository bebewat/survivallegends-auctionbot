import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from mcrcon import MCRcon

load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
log = logging.getLogger("auctionbot")

TOKEN = os.getenv("TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("TOKEN is not set in .env")

AUCTION_CHANNEL_ID = int(os.getenv("AUCTION_CHANNEL_ID", "0") or 0)
EXPIRED_CHANNEL_ID = int(os.getenv("EXPIRED_CHANNEL_ID", "0") or 0)
LOGO_URL = os.getenv("LOGO_URL", "").strip()
RCON_HOST = os.getenv("RCON_HOST", "127.0.0.1").strip()
RCON_PORT = int(os.getenv("RCON_PORT", "27020") or 27020)
RCON_PASSWORD = os.getenv("RCON_PASSWORD", "").strip()
ALLOWED_ROLE_IDS = {
    int(role_id.strip())
    for role_id in os.getenv("ALLOWED_ROLE_IDS", "").split(",")
    if role_id.strip().isdigit()
}

POINTS_DB_PATH = os.getenv("POINTS_DB_PATH", "").strip()
AUCTION_DB_PATH = os.getenv("AUCTION_DB_PATH", "AuctionBot.db").strip()

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)


def init_auction_db() -> None:
    with sqlite3.connect(AUCTION_DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS active_auctions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_name TEXT NOT NULL,
                start_bid INTEGER NOT NULL,
                highest_bid INTEGER NOT NULL,
                highest_bidder TEXT,
                highest_bidder_id TEXT,
                end_time REAL NOT NULL,
                channel_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                description TEXT,
                image_url TEXT,
                creator_name TEXT NOT NULL,
                creator_id TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        # Add new columns if this bot is being run against the old included DB.
        cursor.execute("PRAGMA table_info(active_auctions)")
        existing_columns = {row[1] for row in cursor.fetchall()}
        migrations = {
            "highest_bidder_id": "ALTER TABLE active_auctions ADD COLUMN highest_bidder_id TEXT",
            "creator_id": "ALTER TABLE active_auctions ADD COLUMN creator_id TEXT DEFAULT ''",
        }
        for column, sql in migrations.items():
            if column not in existing_columns:
                cursor.execute(sql)
        conn.commit()


def format_time(mins: int) -> str:
    days = mins // 1440
    hours = (mins % 1440) // 60
    minutes = mins % 60
    return f"📅 {days}d | 🕒 {hours}h | ⏳ {minutes}m"


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def _points_db_connection() -> Optional[sqlite3.Connection]:
    if not POINTS_DB_PATH:
        log.warning("POINTS_DB_PATH is not set; cannot check the second database")
        return None

    path = Path(POINTS_DB_PATH)
    if not path.exists():
        log.error("POINTS_DB_PATH does not exist: %s", path)
        return None

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def lookup_eos_id(discord_id: int) -> Optional[str]:
    """Pull the winner's eos_id from the second database using their Discord ID."""
    discord_id_text = str(discord_id)
    conn = _points_db_connection()
    if conn is None:
        log.warning("Cannot look up eos_id for Discord ID %s", discord_id)
        return None

    with conn:

        # Bone Depot/common normalized schema.
        if _table_exists(conn, "discord_links") and _table_exists(conn, "player_game_ids"):
            row = conn.execute(
                """
                SELECT pgi.eos_id
                FROM discord_links dl
                JOIN player_game_ids pgi ON pgi.player_id = dl.player_id
                WHERE dl.discord_id = ?
                  AND pgi.eos_id IS NOT NULL
                  AND pgi.eos_id != ''
                ORDER BY pgi.last_synced DESC
                LIMIT 1
                """,
                (discord_id_text,),
            ).fetchone()
            if row:
                return str(row["eos_id"])

        # Simple one-table schema fallback.
        if _table_exists(conn, "players"):
            columns = _table_columns(conn, "players")
            if {"discord_id", "eos_id"}.issubset(columns):
                row = conn.execute(
                    """
                    SELECT eos_id
                    FROM players
                    WHERE discord_id = ?
                      AND eos_id IS NOT NULL
                      AND eos_id != ''
                    LIMIT 1
                    """,
                    (discord_id_text,),
                ).fetchone()
                if row:
                    return str(row["eos_id"])

    log.warning("No eos_id found in second DB for Discord ID %s", discord_id)
    return None


def check_points(eos_id: str, amount: int) -> bool:
    """Return True only when the linked EOS account has at least amount points.

    The points DB is expected to have an eos_id column and one of these balance
    columns: amount, points, balance, point_balance, or current_points.
    This keeps the auction bot from accepting bids the player cannot afford.
    """
    if not eos_id or amount <= 0:
        return False

    conn = _points_db_connection()
    if conn is None:
        return False

    balance_columns = ("amount", "points", "balance", "point_balance", "current_points")
    direct_tables = ("players", "player_points", "points", "balances", "economy")

    with conn:
        # Prefer common/simple tables first.
        for table_name in direct_tables:
            if not _table_exists(conn, table_name):
                continue
            columns = _table_columns(conn, table_name)
            if "eos_id" not in columns:
                continue

            balance_column = next((column for column in balance_columns if column in columns), None)
            if not balance_column:
                continue

            row = conn.execute(
                f"SELECT {balance_column} AS held_points FROM {table_name} WHERE eos_id = ? LIMIT 1",
                (eos_id,),
            ).fetchone()
            if row is not None:
                held_points = int(row["held_points"] or 0)
                return held_points >= amount

        # Generic fallback: scan any table with eos_id + a known balance column.
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for table in tables:
            table_name = table["name"]
            columns = _table_columns(conn, table_name)
            if "eos_id" not in columns:
                continue

            balance_column = next((column for column in balance_columns if column in columns), None)
            if not balance_column:
                continue

            row = conn.execute(
                f"SELECT {balance_column} AS held_points FROM {table_name} WHERE eos_id = ? LIMIT 1",
                (eos_id,),
            ).fetchone()
            if row is not None:
                held_points = int(row["held_points"] or 0)
                return held_points >= amount

    log.warning("No points balance found in second DB for eos_id=%s", eos_id)
    return False


def deduct_points(eos_id: str, amount: int) -> bool:
    if not eos_id or amount <= 0:
        return False
    if not RCON_PASSWORD:
        log.error("RCON_PASSWORD is not set; cannot deduct points")
        return False

    try:
        with MCRcon(RCON_HOST, RCON_PASSWORD, port=RCON_PORT) as mcr:
            response = mcr.command(f"ChangePoints {eos_id} -{amount}")
            log.info("Deducted %s points from %s. RCON response: %s", amount, eos_id, response)
            return True
    except Exception:
        log.exception("RCON error while deducting points from eos_id=%s", eos_id)
        return False


class AuctionView(discord.ui.View):
    def __init__(self, message_id: str):
        super().__init__(timeout=None)
        self.message_id = message_id

    async def update_bid(self, interaction: discord.Interaction, amount: int):
        with sqlite3.connect(AUCTION_DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT highest_bid, highest_bidder_id, creator_id
                FROM active_auctions
                WHERE message_id = ?
                """,
                (self.message_id,),
            )
            data = cursor.fetchone()
            if not data:
                return await interaction.response.send_message("❌ Auction not found.", ephemeral=True)

            highest_bid, highest_bidder_id, creator_id = data
            user_id_text = str(interaction.user.id)

            if creator_id == user_id_text:
                return await interaction.response.send_message("❌ You cannot bid on your own auction!", ephemeral=True)
            if highest_bidder_id == user_id_text:
                return await interaction.response.send_message("❌ You are already the highest bidder!", ephemeral=True)

            new_bid = int(highest_bid or 0) + amount
            eos_id = lookup_eos_id(interaction.user.id)
            if not eos_id:
                return await interaction.response.send_message(
                    "❌ I could not find your linked EOS ID, so I cannot verify your points.",
                    ephemeral=True,
                )
            if not check_points(eos_id, new_bid):
                return await interaction.response.send_message(
                    f"❌ You do not have enough points for that bid. Required: {new_bid} points.",
                    ephemeral=True,
                )

            bidder_name = interaction.user.display_name
            cursor.execute(
                """
                UPDATE active_auctions
                SET highest_bid = ?, highest_bidder = ?, highest_bidder_id = ?
                WHERE message_id = ?
                """,
                (new_bid, bidder_name, user_id_text, self.message_id),
            )
            conn.commit()

        embed = interaction.message.embeds[0]
        embed.set_field_at(1, name="💰 Current Bid", value=f"✅ **{new_bid} Points**", inline=True)
        embed.set_field_at(2, name="👤 Highest Bidder", value=f"**{bidder_name}**", inline=True)
        await interaction.message.edit(embed=embed, view=self)
        await interaction.response.send_message(f"Bid of +{amount} placed! New total: {new_bid}", ephemeral=True)

    @discord.ui.button(label="+100 🔨", style=discord.ButtonStyle.green, custom_id="bid_100")
    async def bid_100(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.update_bid(interaction, 100)

    @discord.ui.button(label="+500 🔨", style=discord.ButtonStyle.primary, custom_id="bid_500")
    async def bid_500(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.update_bid(interaction, 500)

    @discord.ui.button(label="+1000 🔨", style=discord.ButtonStyle.danger, custom_id="bid_1000")
    async def bid_1000(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.update_bid(interaction, 1000)


@tasks.loop(seconds=300)
async def dynamic_updater():
    with sqlite3.connect(AUCTION_DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, item_name, start_bid, highest_bid, highest_bidder, highest_bidder_id,
                   end_time, channel_id, message_id, description, image_url
            FROM active_auctions
            """
        )
        auctions = cursor.fetchall()

        for row in auctions:
            auction_id, item_name, start_bid, highest_bid, highest_bidder, highest_bidder_id, end_time, channel_id, message_id, description, image_url = row
            remaining_mins = int((end_time - time.time()) / 60)

            if remaining_mins <= 0:
                try:
                    if highest_bidder_id:
                        eos_id = lookup_eos_id(int(highest_bidder_id))
                        if eos_id:
                            deduct_points(eos_id, int(highest_bid or 0))
                        else:
                            log.error("Auction %s ended, but no eos_id was found for winner Discord ID %s", auction_id, highest_bidder_id)

                    channel = await bot.fetch_channel(int(channel_id))
                    msg = await channel.fetch_message(int(message_id))
                    expired_channel = bot.get_channel(EXPIRED_CHANNEL_ID) if EXPIRED_CHANNEL_ID else None
                    if expired_channel:
                        embed = discord.Embed(
                            title=f"🚫 AUCTION CLOSED: {item_name.upper()} 🚫",
                            color=discord.Color.light_gray(),
                        )
                        embed.add_field(name="Final Bid", value=f"✅ **{highest_bid} Points**", inline=True)
                        embed.add_field(name="Winner", value=highest_bidder or "No bids", inline=True)
                        if image_url:
                            embed.set_image(url=image_url)
                        await expired_channel.send(embed=embed)
                    await msg.delete()
                except Exception:
                    log.exception("Error while closing auction id=%s", auction_id)
                finally:
                    cursor.execute("DELETE FROM active_auctions WHERE id = ?", (auction_id,))
            else:
                try:
                    channel = await bot.fetch_channel(int(channel_id))
                    msg = await channel.fetch_message(int(message_id))
                    embed = msg.embeds[0]
                    embed.set_field_at(4, name="⏰ Time Remaining", value=f"**{format_time(remaining_mins)}**", inline=False)
                    await msg.edit(embed=embed, view=AuctionView(str(message_id)))
                except Exception:
                    log.exception("Error while updating auction id=%s", auction_id)

        conn.commit()


@bot.tree.command(name="launch_auction", description="Launch a new auction")
@app_commands.choices(
    duration=[
        app_commands.Choice(name="1 Minute Test", value=1),
        app_commands.Choice(name="24 Hours", value=1440),
        app_commands.Choice(name="48 Hours", value=2880),
        app_commands.Choice(name="5 Days", value=7200),
    ]
)
async def launch_auction(
    interaction: discord.Interaction,
    item: str,
    starting_bid: int,
    duration: int,
    description: str = None,
    image: discord.Attachment = None,
):
    user_role_ids = {role.id for role in getattr(interaction.user, "roles", [])}
    if ALLOWED_ROLE_IDS and not (ALLOWED_ROLE_IDS & user_role_ids):
        return await interaction.response.send_message("❌ You are not authorized.", ephemeral=True)

    if AUCTION_CHANNEL_ID and interaction.channel_id != AUCTION_CHANNEL_ID:
        return await interaction.response.send_message("❌ Please use the correct auction channel.", ephemeral=True)

    with sqlite3.connect(AUCTION_DB_PATH) as conn:
        cursor = conn.cursor()
        day_ago = time.time() - 86400
        cursor.execute(
            "SELECT COUNT(*) FROM active_auctions WHERE creator_id = ? AND created_at > ?",
            (str(interaction.user.id), day_ago),
        )
        if cursor.fetchone()[0] >= 2:
            return await interaction.response.send_message("❌ You have reached your limit of 2 auctions per 24 hours.", ephemeral=True)

        end_time = time.time() + (duration * 60)
        created_at = time.time()
        image_url = image.url if image else None

        embed = discord.Embed(
            title=f"🔹 {item.upper()} AUCTION 🔹",
            description=description or "No details provided.",
            color=discord.Color.blue(),
        )
        if LOGO_URL:
            embed.set_thumbnail(url=LOGO_URL)
        embed.add_field(name="\u200b", value="\u200b", inline=False)
        embed.add_field(name="💰 Current Bid", value=f"✅ **{starting_bid} Points**", inline=True)
        embed.add_field(name="👤 Highest Bidder", value="**None yet**", inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=False)
        embed.add_field(name="⏰ Time Remaining", value=f"**{format_time(duration)}**", inline=False)
        if image_url:
            embed.set_image(url=image_url)

        msg = await interaction.channel.send(embed=embed)
        await msg.edit(view=AuctionView(str(msg.id)))

        cursor.execute(
            """
            INSERT INTO active_auctions
                (item_name, start_bid, highest_bid, highest_bidder, highest_bidder_id,
                 end_time, channel_id, message_id, description, image_url,
                 creator_name, creator_id, created_at)
            VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item,
                starting_bid,
                starting_bid,
                end_time,
                str(interaction.channel_id),
                str(msg.id),
                description,
                image_url,
                interaction.user.display_name,
                str(interaction.user.id),
                created_at,
            ),
        )
        conn.commit()

    await interaction.response.send_message("Auction live!", ephemeral=True)


@bot.event
async def on_ready():
    init_auction_db()
    for auction in sqlite3.connect(AUCTION_DB_PATH).execute("SELECT message_id FROM active_auctions").fetchall():
        bot.add_view(AuctionView(str(auction[0])))
    await bot.tree.sync()
    if not dynamic_updater.is_running():
        dynamic_updater.start()
    log.info("Auction bot is online as %s", bot.user)


if __name__ == "__main__":
    bot.run(TOKEN)
