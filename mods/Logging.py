import os

import discord

INFO_FOLDER = "./info"
logs_channel_file = os.path.join(INFO_FOLDER, "logs-channel.txt")

# Holds a reference to the *actual* running bot, set once from main.py via
# set_bot(bot) right after the bot is created. Previously this module created
# its own separate commands.Bot() instance that was never started/logged in,
# which is why fetch_channel() blew up — that bot's internal ready-tracking
# Event was never initialized.
_bot: discord.Client | None = None


def set_bot(bot_instance: discord.Client):
    """Give this module a reference to the live bot instance. Call this once
    from main.py, right after `bot = commands.Bot(...)` is created."""
    global _bot
    _bot = bot_instance


with open(logs_channel_file, "r", encoding="utf-8") as file:
        default_channel_id_str = file.read().strip()

async def log_message(
    message_title,
    description,
    images: list | None = None,
    channel_id: str | None = default_channel_id_str,
    img_type: str | None = "img",
    thumb: str | None = None,
    image_file: discord.File | None = None,
    image_files: list[discord.File] | None = None,
):
    if _bot is None:
        print(
            "Logging.log_message() was called before Logging.set_bot() was "
            "set up — skipping this log message."
        )
        return

    if not os.path.isfile(logs_channel_file):
        return

    if os.path.getsize(logs_channel_file) == 0:
        return

    channel_id_str = channel_id

    if not channel_id_str:
        return

    try:
        channel_id = int(channel_id_str)
    except ValueError:
        print(f"logs-channel.txt doesn't contain a valid channel ID: {channel_id_str!r}")
        return

    await _bot.wait_until_ready()

    try:
        logs_channel = await _bot.fetch_channel(channel_id)

        send_embed = discord.Embed(
            title=message_title,
            description=description,
            color=discord.Color.light_grey(),
        )

        # Fold the single-file and multi-file parameters into one list.
        # image_file is the older form and is kept so existing callers keep
        # working unchanged.
        files = list(image_files) if image_files else []

        if image_file is not None:
            files.insert(0, image_file)

        # Discord rejects a message carrying more than 10 attachments.
        if len(files) > 10:
            print(
                f"log_message: {len(files)} attachments is over Discord's limit "
                "of 10 — only the first 10 will be sent."
            )
            files = files[:10]

        # Re-uploaded files take priority over URL-based images, since the URL
        # is likely dead (e.g. a deleted message's CDN link). An embed holds
        # only one image, so the first file goes inline and the rest ride along
        # as attachments, which Discord previews underneath.
        if files:
            send_embed.set_image(url=f"attachment://{files[0].filename}")
        elif images:
            # `images` is a list of URL strings. Only one can be shown, so the
            # last entry wins — pre-existing behaviour, left as it was.
            for image in images:
                send_embed.set_image(url=image)

        if thumb is not None:
            send_embed.set_thumbnail(url=thumb)

        if files:
            await logs_channel.send(embed=send_embed, files=files)
        else:
            await logs_channel.send(embed=send_embed)

    except discord.HTTPException as e:
        print(f"Something went wrong sending the log message. {e}")

    except Exception as e:
        print(f"Something went wrong. {e}")