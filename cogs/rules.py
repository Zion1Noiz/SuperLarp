import os
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from mods import FileHandling, Logging

INFO_FOLDER = "./info"
USERS_FILE = os.path.join(INFO_FOLDER, "UsersThatAccepted.json")
RULES_FILE = os.path.join(INFO_FOLDER, "Rules.txt")

MAX_MESSAGE_LENGTH = 2000


class RulesButtonViews(discord.ui.View):
    """Persistent view for the rules message. Must be re-registered on bot
    startup (see RulesCog.cog_load) or the buttons stop working after a
    restart since Discord doesn't know which handler to call anymore."""

    def __init__(self, cog: "RulesCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="I Agree",
        style=discord.ButtonStyle.success,
        custom_id="agree_rules",
    )
    async def agree_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        member_role_id = int(os.getenv("MEMBERROLEID"))
        if member_role_id is None:
            await interaction.response.send_message(
                "The member role has not been configured.",
                ephemeral=True,
            )
            return

        role = interaction.guild.get_role(member_role_id)

        # User already accepted
        if role is not None and interaction.user.get_role(member_role_id):
            await interaction.response.send_message(
                "You have already accepted the server rules.",
                ephemeral=True,
            )
            return

        if role is None:
            await interaction.response.send_message(
                "The member role could not be found.",
                ephemeral=True,
            )
            return

        try:
            await interaction.user.add_roles(
                role,
                reason="Accepted the server rules.",
            )

            await self.cog.add_accepted_user(interaction.user.id)

            await interaction.response.send_message(
                "✅ Thank you for accepting the server rules! You now have access to the server.",
                ephemeral=True,
            )

        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I don't have permission to assign the member role.",
                ephemeral=True,
            )

        except Exception as e:
            await interaction.response.send_message(
                f"An unexpected error occurred:\n```{e}```",
                ephemeral=True,
            )

    @discord.ui.button(
        label="I Disagree",
        style=discord.ButtonStyle.danger,
        custom_id="disagree_rules",
    )
    async def disagree_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.send_message(
            "You will now be removed from the server.",
            ephemeral=True,
        )

        try:
            await interaction.user.send(
                "You were removed from the server because you did not agree to the rules. "
                "If you believe this was a mistake, please contact the server staff and rejoin."
            )
        except discord.Forbidden:
            pass

        try:
            await interaction.user.kick(
                reason="User did not agree to the server rules."
            )

        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I don't have permission to kick this member.",
                ephemeral=True,
            )

        except Exception as e:
            await interaction.followup.send(
                f"An unexpected error occurred:\n```{e}```",
                ephemeral=True,
            )


class RulesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        member_role_env = os.getenv("MemberRoleID")
        self.member_role_id = int(member_role_env) if member_role_env else None

        rules_channel_env = os.getenv("RulesChannelID")
        self.rules_channel_id = int(rules_channel_env) if rules_channel_env else None

    async def cog_load(self):
        # Re-register the persistent view so buttons on old messages keep
        # working after a bot restart.
        self.bot.add_view(RulesButtonViews(self))

    # -------------------------
    # Accepted-users helpers
    # -------------------------

    async def has_accepted(self, user_id: int) -> bool:
        """Returns True if the user has already accepted."""
        data = FileHandling.ReadFromJSONFile(USERS_FILE)
        return any(user["user-id"] == user_id for user in data)

    async def add_accepted_user(self, user_id: int):
        """Adds a user to the accepted users JSON file."""

        if await self.has_accepted(user_id):
            return

        data = FileHandling.ReadFromJSONFile(USERS_FILE)

        try:
            # Try cache first
            user = self.bot.get_user(user_id)

            # If not cached, fetch from Discord
            if user is None:
                user = await self.bot.fetch_user(user_id)

            now = time.time()
            user_metadata = {
                "user-id": user.id,
                "username": user.name,
                "time-agreed-unix": now,
                "time-agreed-dt": datetime.fromtimestamp(
                    now, tz=timezone.utc
                ).astimezone().strftime("%Y-%m-%d"),
            }

            data.append(user_metadata)

            result = FileHandling.WriteToJSON(USERS_FILE, data)

            if result != "Success":
                await Logging.log_message("⚠️ Warning", result)

        except (discord.HTTPException, Exception) as e:
            print(f"Something went wrong while adding a user: {e}")

    async def remove_accepted_user(self, user_id: int):
        """Removes a user from the accepted users JSON file."""

        data = FileHandling.ReadFromJSONFile(USERS_FILE)

        for index, user in enumerate(data):
            if user["user-id"] == user_id:
                data.pop(index)

                result = FileHandling.WriteToJSON(USERS_FILE, data)

                if result != "Success":
                    await Logging.log_message("⚠️ Warning", result)

                return

        await Logging.log_message(
            "⚠️ Warning",
            f"Could not find user ID {user_id} in accepted-users list.",
        )

    # -------------------------
    # /createrule command
    # -------------------------

    @app_commands.command(
        name="createrule",
        description="Create the server rules message from Rules.txt.",
    )
    @app_commands.describe(
        rereadneeded="Whether existing members must re-accept the rules (yes/no).",
        image="Optional image to attach to the rules embed.",
    )
    @app_commands.choices(
        rereadneeded=[
            app_commands.Choice(name="yes", value="yes"),
            app_commands.Choice(name="no", value="no"),
        ]
    )
    @app_commands.default_permissions(administrator=True)
    async def createrule(
        self,
        interaction: discord.Interaction,
        rereadneeded: app_commands.Choice[str],
        image: discord.Attachment | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        # Make sure this command is only used in a server
        if interaction.guild is None:
            await interaction.followup.send(
                "This command can only be used inside a server.",
                ephemeral=True,
            )
            return

        # Only allow this command in the rules channel
        if self.rules_channel_id is None or interaction.channel_id != self.rules_channel_id:
            await interaction.followup.send(
                "This command can only be used in the rules channel.",
                ephemeral=True,
            )
            return

        # -------------------------
        # Load Rules.txt
        # -------------------------

        if not os.path.exists(RULES_FILE):
            await interaction.followup.send(
                "Rules.txt does not exist. Please create it inside the `info` folder.",
                ephemeral=True,
            )
            return

        try:
            with open(RULES_FILE, "r", encoding="utf-8") as file:
                rulemessage = file.read()

        except Exception as e:
            await interaction.followup.send(
                f"Failed to read Rules.txt:\n```{e}```",
                ephemeral=True,
            )
            return

        if not rulemessage.strip():
            await interaction.followup.send(
                "Rules.txt is empty. Please add your rules before creating the message.",
                ephemeral=True,
            )
            return

        reread = rereadneeded.value

        if reread == "yes":
            if self.member_role_id is None:
                await interaction.followup.send(
                    "The configured member role could not be found.",
                    ephemeral=True,
                )
                return

            member_role = interaction.guild.get_role(self.member_role_id)

            if member_role is None:
                await interaction.followup.send(
                    "The configured member role could not be found.",
                    ephemeral=True,
                )
                return

            # -------------------------
            # Reset member roles
            # -------------------------
            async for member in interaction.guild.fetch_members(limit=None):
                if member_role in member.roles:
                    try:
                        await member.remove_roles(
                            member_role,
                            reason="Rules were recreated.",
                        )

                    except discord.Forbidden:
                        print(f"Missing permissions to remove role from {member}")

                    except discord.HTTPException as e:
                        print(f"Failed removing role from {member}: {e}")

            # Clear accepted users list
            FileHandling.WriteToJSON(USERS_FILE, [])

        # -------------------------
        # Rules Embed
        # -------------------------

        currentdate_unix = time.time()
        utc_dt = datetime.fromtimestamp(currentdate_unix, tz=timezone.utc)
        local_dt = utc_dt.astimezone()
        formatted_date = local_dt.strftime("%Y-%m-%d")

        embed = discord.Embed(
            description=(
                "By clicking **I Agree**, you confirm that you have read "
                "and understood the server rules and accept the consequences "
                "for breaking them. \n \n"
                f"**Last Updated on {formatted_date}**"
            ),
            color=discord.Color.light_grey(),
        )

        if image:
            embed.set_image(url=image.url)

        # -------------------------
        # Send Rules
        # -------------------------

        try:
            # Delete old messages
            async for message in interaction.channel.history(limit=None):
                try:
                    await message.delete()

                except discord.Forbidden:
                    pass

                except discord.HTTPException:
                    pass

            # Split messages if they exceed Discord's 2000 character limit
            chunks = []
            remaining = rulemessage

            while len(remaining) > MAX_MESSAGE_LENGTH:
                split_index = remaining.rfind("\n", 0, MAX_MESSAGE_LENGTH)

                if split_index == -1:
                    split_index = remaining.rfind(" ", 0, MAX_MESSAGE_LENGTH)

                if split_index == -1:
                    split_index = MAX_MESSAGE_LENGTH

                chunks.append(remaining[:split_index])
                remaining = remaining[split_index:].lstrip()

            if remaining:
                chunks.append(remaining)

            # Send all messages except the last
            for chunk in chunks[:-1]:
                await interaction.channel.send(chunk)

            # Last message gets the buttons + embed
            await interaction.channel.send(
                content=chunks[-1] if chunks else None,
                embed=embed,
                view=RulesButtonViews(self),
            )

        except discord.Forbidden:
            await interaction.followup.send(
                "I don't have permission to manage this channel.",
                ephemeral=True,
            )
            return

        except discord.HTTPException as e:
            await interaction.followup.send(
                f"Discord returned an error:\n```{e}```",
                ephemeral=True,
            )
            return

        summary_lines = [
            "Rules created successfully.",
            "• Rules loaded from Rules.txt.",
            "• Previous rule messages were removed.",
        ]

        if reread == "yes":
            summary_lines.append("• Member roles were reset.")
            summary_lines.append("• Accepted users list was cleared.")

        await interaction.followup.send(
            "\n".join(summary_lines),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(RulesCog(bot))