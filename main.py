import asyncio
import io
import os
import sys
from urllib.parse import urlparse

import aiohttp
import discord
from discord.ext import commands
from discord import app_commands
import dotenv

from cogs import qotd

import json

from mods import Logging, FileHandling, Messaging, privileges

dotenv.load_dotenv()

# Initialize bot with intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix=commands.when_mentioned_or("/", "."), intents=intents)

Logging.set_bot(bot)

DEV_GUILD_ID = os.getenv("DEV_GUILD_ID")

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
AUDIO_EXTENSIONS = (".ogg", ".mp3", ".wav", ".m4a")


def get_cog_extensions() -> set[str]:
    """Return the extension name (e.g. 'cogs.foo') for every valid cog
    file currently sitting in ./cogs. Used both at startup and by
    /reloadall so newly-added files get picked up without a restart."""
    return {
        f"cogs.{filename[:-3]}"
        for filename in os.listdir("./cogs")
        if filename.endswith(".py") and not filename.startswith("_")
    }


def get_filename_from_url(url: str) -> str:
    """Strips Discord's ?ex=...&is=...&hm=... query params off a CDN URL and
    returns the bare filename, e.g. 'image.png'."""
    return os.path.basename(urlparse(url).path)


async def download_attachment_as_file(url: str) -> discord.File | None:
    """Downloads an attachment from a stored CDN URL and wraps it as a
    discord.File. Note: Discord CDN URLs are signed and expire (the
    ex/is/hm query params), so this can fail if the URL is old by the time
    this runs."""
    filename = get_filename_from_url(url)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    print(f"Failed to download attachment from {url}: HTTP {resp.status}")
                    return None
                data = await resp.read()
        return discord.File(io.BytesIO(data), filename=filename)
    except Exception as e:
        print(f"Error downloading attachment: {e}")
        return None


@bot.command()
@app_commands.default_permissions(ban_members=True)
async def reload(ctx, extension: str):
    """Reload a specific cog."""
    if privileges.IsMemberAnOwner(ctx.author) or privileges.CheckIfUserHasRole(ctx.author, 1540944861362913330):
        try:
            await bot.reload_extension(f"cogs.{extension}")
            # Re-sync slash commands after reloading
            await sync_commands()
            await ctx.send(f"✅ Successfully reloaded `{extension}` and synced commands.")
        except Exception as e:
            await ctx.send(f"❌ Failed to reload `{extension}`\n```py\n{e}\n```")
    else:
        print("User is not allowed to use the reload command.")


@bot.command()
@app_commands.default_permissions(manage_guild=True)
async def reloadall(ctx, mode: str = None):
    """Reload every currently loaded cog and load any brand-new cog files
    found on disk.

    Pass `hard` (e.g. `/reloadall hard`) to additionally restart the whole
    process afterwards, which re-reads this file (main.py) itself from
    disk. Without it, only the cogs folder is touched — main.py's own
    code won't change until you do a hard reload or restart manually
    """

    if privileges.IsMemberAnOwner(ctx.author) or privileges.CheckIfUserHasRole(ctx.author, 1540944861362913330):

    
        hard = mode is not None and mode.lower() == "hard"

        reloaded = []
        newly_loaded = []
        failed = []

        already_loaded = set(bot.extensions.keys())
        on_disk = get_cog_extensions()

        # Reload existing cogs
        for extension in list(already_loaded):
            try:
                await bot.reload_extension(extension)
                reloaded.append(extension)
            except Exception as e:
                failed.append((extension, e))

        # Load any new cogs that appeared on disk
        for extension in on_disk - already_loaded:
            try:
                await bot.load_extension(extension)
                newly_loaded.append(extension)
            except Exception as e:
                failed.append((extension, e))

        # Re-sync slash commands after all changes
        sync_status = "🔄 Slash commands synchronized."
        try:
            await sync_commands()
        except Exception as e:
            sync_status = f"❌ Failed to sync commands:\n```py\n{e}\n```"

        lines = [sync_status]

        if reloaded:
            lines.append(
                "✅ Reloaded:\n" +
                "\n".join(f"- `{ext}`" for ext in reloaded)
            )

        if newly_loaded:
            lines.append(
                "🆕 Newly loaded:\n" +
                "\n".join(f"- `{ext}`" for ext in newly_loaded)
            )

        if failed:
            lines.append(
                "❌ Failed:\n" +
                "\n".join(
                    f"- `{ext}`\n```py\n{e}\n```"
                    for ext, e in failed
                )
            )

        if len(lines) == 1:
            lines.append("No cogs are currently loaded.")

        await ctx.send("\n".join(lines))

        if not hard:
            return

        await ctx.send(
            "🔄 Hard reload requested — restarting process to reload `main.py` itself..."
        )
        os.execv(sys.executable, [sys.executable] + sys.argv)
    else:
        print("User is not allowed to use the reload command.")
async def load_extensions():
    """Automatically load every cog in the cogs folder."""
    for extension in get_cog_extensions():
        try:
            await bot.load_extension(extension)
            print(f"Loaded {extension}")
        except Exception as e:
            print(f"Failed to load {extension}: {e}")

async def sync_commands():
    """Syncs the slash-command tree with Discord."""
    try:
        if DEV_GUILD_ID:
            guild = discord.Object(id=int(DEV_GUILD_ID))
            synced = await bot.tree.sync(guild=guild)
            print(f"Synced {len(synced)} command(s) to guild {DEV_GUILD_ID}.")
        else:
            synced = await bot.tree.sync()
            print(f"Synced {len(synced)} command(s) globally.")
    except Exception as e:
        print(f"Failed to sync commands: {e}")


@bot.event
async def on_ready():
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Game("Managing Larp Nation")
    )

    await sync_commands()

    print(f"Logged in as {bot.user}")
    print(f"Text command prefix: {bot.command_prefix!r}")
    print(f"Message content intent: {bot.intents.message_content}")


async def send_temporary_error(ctx: commands.Context, message: str):
    """Reply with an error that deletes itself after a few seconds.

    Prefers the shared helper in mods/Messaging.py, but works without it so a
    partially-updated deploy still reports errors instead of crashing."""
    helper = getattr(Messaging, "SendTemporaryError", None)

    if helper is not None:
        await helper(ctx, message)
        return

    delay = getattr(Messaging, "ERROR_DELETE_AFTER", 1)

    try:
        await ctx.send(message, delete_after=delay)
    except discord.HTTPException as e:
        print(f"Couldn't send error message: {e}")


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    """Catch-all for text commands. Replies with a short reason that deletes
    itself after a few seconds, so failed commands don't leave clutter behind."""
    # Cogs with their own handler (e.g. Economy) have already replied.
    if hasattr(ctx.command, "on_error"):
        return

    if ctx.cog is not None and ctx.cog.has_error_handler():
        return

    error = getattr(error, "original", error)

    # With "." as the prefix, any message starting with a full stop lands here.
    # Replying to all of them would be noise, so unknown commands stay silent.
    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, commands.MissingPermissions):
        message = "❌ You don't have permission to use that."
    elif isinstance(error, commands.BotMissingPermissions):
        message = f"❌ I'm missing a permission I need: {error}"
    elif isinstance(error, commands.CommandOnCooldown):
        message = f"⏳ Slow down — try again in {error.retry_after:.1f}s."
    elif isinstance(error, commands.NoPrivateMessage):
        message = "❌ That command only works in a server."
    elif isinstance(error, commands.CheckFailure):
        message = "❌ You can't use that command here."
    elif isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument,
                            commands.TooManyArguments)):
        message = f"❌ {error}"
    else:
        message = "❌ Something went wrong running that command."
        print(f"Unhandled error in {ctx.command}: {error!r}")

    await send_temporary_error(ctx, message)


@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot:
        return

    author = message.author
    content = message.content
    channel = message.channel
    attachments = message.attachments

    image_files = []

    if len(attachments) > 0:
        if content == "":
            content = "Attachment"

        for attachment in attachments:
            content_type = attachment.content_type or ""
            if "audio" in content_type or attachment.filename.endswith(".ogg"):
                content = f"Audio URL: {attachment.url}"
                continue

            # Every image is kept, not just the first — a deleted message with
            # four screenshots should log all four.
            if content_type.startswith("image/"):
                try:
                    image_files.append(await attachment.to_file())
                except discord.HTTPException as e:
                    print(f"Failed to re-download attachment {attachment.filename}: {e}")

    await Logging.log_message(
        f"⚠️ Message by {author} was deleted! ⚠️",
        f"Message content: {content}\nAssociated Channel: {channel}\n",
        image_files=image_files
    )


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):

    if payload.cached_message is not None:
        return

    channel = bot.get_channel(payload.channel_id)
    channel_display = channel.mention if channel else f"channel ID {payload.channel_id}"

    def searchForDeletedMessage(id):
        with open(os.path.join("./temp", "chat-logs.json"), "r", encoding="utf-8") as f:
            content = json.loads(f.read())

            for day in content:
                for msg in content.get(day):
                    if msg.get("id") == id:
                        return msg

            print("Could not find message with that id.")
            return None

    logged_message = searchForDeletedMessage(payload.message_id)

    if logged_message is None:
        await Logging.log_message(
            "⚠️ Message Deleted (uncached)",
            f"A message was deleted in {channel_display}, but it wasn't in the bot's "
            f"cache, so the author and content are unknown.\nMessage ID: {payload.message_id}"
        )
        return

    content = logged_message.get("content") or ""
    attachment_urls = logged_message.get("attachments") or []
    image_files = []

    if attachment_urls:
        if content == "":
            content = "Attachment"

        for url in attachment_urls:
            filename = get_filename_from_url(url)
            lower_filename = filename.lower()

            if lower_filename.endswith(AUDIO_EXTENSIONS):
                content = f"Audio URL: {url}"
                continue

            # As above, every image is collected. Any that fail to download
            # (expired CDN link) come back as None and are skipped.
            if lower_filename.endswith(IMAGE_EXTENSIONS):
                downloaded = await download_attachment_as_file(url)

                if downloaded is not None:
                    image_files.append(downloaded)

    author_name = logged_message.get("author", {}).get("display_name") \
        or logged_message.get("author", {}).get("name", "Unknown user")

    await Logging.log_message(
        f"⚠️ Message by {author_name} was deleted! ⚠️",
        f"Message content: {content}\nAssociated Channel: {channel_display}\n",
        image_files=image_files
    )


async def main():
    async with bot:
        await load_extensions()
        await bot.start(os.getenv("BOT_KEY"))


asyncio.run(main())