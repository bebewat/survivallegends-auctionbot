import logging
import os
import mysql.connector
from mysql.connector import Error as MySQLError
import time
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

AUCTION_DB_HOST = os.getenv("AUCTION_DB_HOST", "127.0.0.1").strip()
AUCTION_DB_PORT = int(os.getenv("AUCTION_DB_PORT", "3306") or 3306)
AUCTION_DB_NAME = os.getenv("AUCTION_DB_NAME", "auctionbot").strip()
AUCTION_DB_USER = os.getenv("AUCTION_DB_USER", "").strip()
AUCTION_DB_PASSWORD = os.getenv("AUCTION_DB_PASSWORD", "").strip()

POINTS_DB_HOST = os.getenv("POINTS_DB_HOST", "127.0.0.1").strip()
POINTS_DB_PORT = int(os.getenv("POINTS_DB_PORT", "3306") or 3306)
POINTS_DB_NAME = os.getenv("POINTS_DB_NAME", "").strip()
POINTS_DB_USER = os.getenv("POINTS_DB_USER", "").strip()
POINTS_DB_PASSWORD = os.getenv("POINTS_DB_PASSWORD", "").strip()

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)


def get_auction_db_connection():
    return mysql.connector.connect(
        host=AUCTION_DB_HOST,
        port=AUCTION_DB_PORT,
        database=AUCTION_DB_NAME,
        user=AUCTION_DB_USER,
        password=AUCTION_DB_PASSWORD,
        autocommit=True,
    )


def get_points_db_connection():
    return mysql.connector.connect(
        host=POINTS_DB_HOST,
        port=POINTS_DB_PORT,
        database=POINTS_DB_NAME,
        user=POINTS_DB_USER,
        password=POINTS_DB_PASSWORD,
        autocommit=True,
    )


def init_auction_db() -> None:
    with get_auction_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS active_auctions (
                id INT AUTO_INCREMENT PRIMARY KEY,
                item_name VARCHAR(255) NOT NULL,
                start_bid INT NOT NULL,
                highest_bid INT NOT NULL,
                highest_bidder VARCHAR(255),
                highest_bidder_id VARCHAR(32),
                end_time DOUBLE NOT NULL,
                channel_id VARCHAR(32) NOT NULL,
                message_id VARCHAR(32) NOT NULL UNIQUE,
                description TEXT,
                image_url TEXT,
                creator_name VARCHAR(255) NOT NULL,
                creator_id VARCHAR(32) NOT NULL,
                created_at DOUBLE NOT NULL
            )
            """
        )
        cursor.close()

def format_time(mins: int) -> str:
    days = mins // 1440
    hours = (mins % 1440) // 60
    minutes = mins % 60
    return f"📅 {days}d | 🕒 {hours}h | ⏳ {minutes}m"


def _table_exists(conn, table_name: str) -> bool:
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.tables
        WHERE table_schema = DATABASE()
          AND table_name = %s
        """,
        (table_name,),
    )
    exists = cursor.fetchone()[0] > 0
    cursor.close()
    return exists


def _table_columns(conn, table_name: str) -> set[str]:
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = %s
        """,
        (table_name,),
    )
    columns = {row[0] for row in cursor.fetchall()}
    cursor.close()
    return columns


def _safe_table_name(table_name: str) -> str:
    if not table_name.replace("_", "").isalnum():
        raise ValueError(f"Unsafe table name: {table_name}")
    return f"`{table_name}`"


def _safe_column_name(column_name: str) -> str:
    if not column_name.replace("_", "").isalnum():
        raise ValueError(f"Unsafe column name: {column_name}")
    return f"`{column_name}`"

def lookup_eos_id(discord_id: int) -> Optional[str]:
    """Pull the bidder/winner eos_id from the points database using their Discord ID."""
    discord_id_text = str(discord_id)

    try:
        with get_points_db_connection() as conn:
            # Normalized schema: discord_links(discord_id, player_id)
            # joined to player_game_ids(player_id, eos_id).
            if _table_exists(conn, "discord_links") and _table_exists(conn, "player_game_ids"):
                cursor = conn.cursor(dictionary=True)
                cursor.execute(
                    """
                    SELECT pgi.eos_id
                    FROM discord_links dl
                    JOIN player_game_ids pgi ON pgi.player_id = dl.player_id
                    WHERE dl.discord_id = %s
                      AND pgi.eos_id IS NOT NULL
                      AND pgi.eos_id != ''
                    ORDER BY pgi.last_synced DESC
                    LIMIT 1
                    """,
                    (discord_id_text,),
                )
                row = cursor.fetchone()
                cursor.close()
                if row:
                    return str(row["eos_id"])

            # Simple one-table schema fallback: players(discord_id, eos_id).
            if _table_exists(conn, "players"):
                columns = _table_columns(conn, "players")
                if {"discord_id", "eos_id"}.issubset(columns):
                    cursor = conn.cursor(dictionary=True)
                    cursor.execute(
                        """
                        SELECT eos_id
                        FROM players
                        WHERE discord_id = %s
                          AND eos_id IS NOT NULL
                          AND eos_id != ''
                        LIMIT 1
                        """,
                        (discord_id_text,),
                    )
                    row = cursor.fetchone()
                    cursor.close()
                    if row:
                        return str(row["eos_id"])
    except MySQLError:
        log.exception("MySQL error while looking up eos_id for Discord ID %s", discord_id)
        return None

    log.warning("No eos_id found in points DB for Discord ID %s", discord_id)
    return None


def check_points(eos_id: str, amount: int) -> bool:
    """Return True only when the linked EOS account has at least amount points.

    The points DB is expected to have an eos_id column and one of these balance
    columns: amount, points, balance, point_balance, or current_points.
    This keeps the auction bot from accepting bids the player cannot afford.
    """
    if not eos_id or amount <= 0:
        return False

    balance_columns = ("amount", "points", "balance", "point_balance", "current_points")
    direct_tables = ("players", "player_points", "points", "balances", "economy")

    try:
        with get_points_db_connection() as conn:
            for table_name in direct_tables:
                if not _table_exists(conn, table_name):
                    continue
                columns = _table_columns(conn, table_name)
                if "eos_id" not in columns:
                    continue

                balance_column = next((column for column in balance_columns if column in columns), None)
                if not balance_column:
                    continue

                cursor = conn.cursor(dictionary=True)
                cursor.execute(
                    f"""
                    SELECT {_safe_column_name(balance_column)} AS held_points
                    FROM {_safe_table_name(table_name)}
                    WHERE eos_id = %s
                    LIMIT 1
                    """,
                    (eos_id,),
                )
                row = cursor.fetchone()
                cursor.close()
                if row is not None:
                    held_points = int(row["held_points"] or 0)
                    return held_points >= amount

            # Generic fallback: scan any table in the current points DB that has
            # eos_id plus a known balance column.
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = DATABASE()
                  AND table_type = 'BASE TABLE'
                """
            )
            tables = cursor.fetchall()
            cursor.close()

            for table in tables:
                table_name = table["table_name"]
                columns = _table_columns(conn, table_name)
                if "eos_id" not in columns:
                    continue

                balance_column = next((column for column in balance_columns if column in columns), None)
                if not balance_column:
                    continue

                cursor = conn.cursor(dictionary=True)
                cursor.execute(
                    f"""
                    SELECT {_safe_column_name(balance_column)} AS held_points
                    FROM {_safe_table_name(table_name)}
                    WHERE eos_id = %s
                    LIMIT 1
                    """,
                    (eos_id,),
                )
                row = cursor.fetchone()
                cursor.close()
                if row is not None:
                    held_points = int(row["held_points"] or 0)
                    return held_points >= amount
    except MySQLError:
        log.exception("MySQL error while checking points for eos_id=%s", eos_id)
        return False

    log.warning("No points balance found in points DB for eos_id=%s", eos_id)
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
        with get_auction_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT highest_bid, highest_bidder_id, creator_id
                FROM active_auctions
                WHERE message_id = %s
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
                SET highest_bid = %s, highest_bidder = %s, highest_bidder_id = %s
                WHERE message_id = %s
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


@tasks.loop(seconds=60)
async def dynamic_updater():
    with get_auction_db_connection() as conn:
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
                    cursor.execute("DELETE FROM active_auctions WHERE id = %s", (auction_id,))
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

    with get_auction_db_connection() as conn:
        cursor = conn.cursor()
        day_ago = time.time() - 86400
        cursor.execute(
            "SELECT COUNT(*) FROM active_auctions WHERE creator_id = %s AND created_at > %s",
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
            VALUES (%s, %s, %s, NULL, NULL, %s, %s, %s, %s, %s, %s, %s, %s)
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
    with get_auction_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT message_id FROM active_auctions")
        for auction in cursor.fetchall():
            bot.add_view(AuctionView(str(auction[0])))
        cursor.close()
    await bot.tree.sync()
    if not dynamic_updater.is_running():
        dynamic_updater.start()
    log.info("Auction bot is online as %s", bot.user)


if __name__ == "__main__":
    bot.run(TOKEN)
