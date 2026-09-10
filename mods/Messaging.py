
import discord

# How long an error or permission-denied message stays up before the bot tidies
# it away. Shared so every cog expires them at the same rate.
ERROR_DELETE_AFTER = 5


async def SendTemporaryError(ctx, message: str, ephemeral: bool = True):
    """Send an error that removes itself after ERROR_DELETE_AFTER seconds.

    Falls back to a plain send if the message can't be scheduled for deletion,
    since an error the user can see is better than no error at all."""
    try:
        await ctx.send(message, ephemeral=ephemeral, delete_after=ERROR_DELETE_AFTER)
    except TypeError:
        # A context that doesn't accept ephemeral (e.g. a plain TextChannel).
        try:
            await ctx.send(message, delete_after=ERROR_DELETE_AFTER)
        except discord.HTTPException as e:
            print(f"Couldn't send error message: {e}")
    except discord.HTTPException as e:
        print(f"Couldn't send error message: {e}")


async def SendTemporaryErrorInteraction(interaction: discord.Interaction, message: str):
    """The interaction equivalent of SendTemporaryError."""
    try:
        await interaction.response.send_message(
            message, ephemeral=True, delete_after=ERROR_DELETE_AFTER
        )
    except discord.HTTPException as e:
        print(f"Couldn't send error message: {e}")


async def SendPublicMessage(channel : discord.TextChannel, attachments : list[discord.Attachment | discord.File], client_only : bool | None = False, message : str | None = "", embed : discord.Embed | None = None, interaction : discord.Interaction | None = None):
    attachments_to_send = []
    if len(attachments) > 0:
        for attachment in attachments:
            if embed != None:
                attachments_to_send.append(attachment.url)
            else:
                attachments_to_send.append(attachment)

    try: 
        if embed == None:
            try:
                await channel.send(message, files=attachments_to_send)
            except discord.HTTPException and Exception as e:
                print(f"There was an error sending your message. Error: {e}")
        else:
            try:
                for attachment in attachments_to_send:
                    embed.set_image(attachment.url)
                await channel.send(message, embed=embed)
            except Exception and discord.HTTPException as e:
                print(f"There was an error sending your message. Error: {e}")

    except Exception and discord.HTTPException as e:
        print(f"Something went wrong. Error: {e}")

