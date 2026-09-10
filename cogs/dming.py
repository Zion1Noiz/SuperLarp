import discord
from discord import app_commands
from discord.ext import commands


class DMTool(commands.Cog):
    """Fallback tool for admins to DM a specific member directly from the bot."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="dm",
        description="Tool used to DM a specific member. This should be used as a fallback."
    )
    @app_commands.describe(
        user="The member to DM.",
        title="Optional embed title.",
        description="Optional embed description.",
        image="Optional image to attach to the embed.",
    )
    @app_commands.default_permissions(administrator=True)
    async def dm_user(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        title: str | None = None,
        description: str | None = None,
        image: discord.Attachment | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        # An embed with nothing set will be rejected by Discord, so make sure
        # at least one field was actually provided.
        if not title and not description and image is None:
            await interaction.followup.send(
                "You must provide at least a title, description, or image.",
                ephemeral=True,
            )
            return

        send_embed = discord.Embed(
            title=title,
            description=description,
        )

        if image is not None:
            send_embed.set_image(url=image.url)

        try:
            await user.send(embed=send_embed)
            await interaction.followup.send(
                "Command completed successfully!",
                ephemeral=True,
            )

        except discord.Forbidden:
            await interaction.followup.send(
                f"I couldn't DM {user.mention} — they may have DMs disabled or have blocked the bot.",
                ephemeral=True,
            )

        except discord.HTTPException as e:
            await interaction.followup.send(
                f"An HTTP error occurred with your request. Error: {e}",
                ephemeral=True,
            )

        except Exception as e:
            await interaction.followup.send(
                f"Something went wrong with your request. {e}",
                ephemeral=True,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(DMTool(bot))