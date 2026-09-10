import json
import os
import time
import uuid
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from mods import Logging

INFO_FOLDER = "./info"
USER_INFO_FILE = os.path.join(INFO_FOLDER, "user-info.json")

# Roles that are exempt from moderation commands (e.g. staff roles).
# Set ProtectedRoleIDs in your .env as a comma-separated list of role IDs.
# Falls back to the IDs that were hardcoded in the original commands.
_default_protected_roles = "1526828189001846815,1526822799337984122,1515171114647818272"
PROTECTED_ROLE_IDS = [
    int(rid) for rid in os.getenv("ProtectedRoleIDs", _default_protected_roles).split(",")
    if rid.strip()
]


class Warnings(commands.Cog):
    """Handles issuing, removing, and viewing member warnings."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._ensure_file_exists()

    @staticmethod
    def _ensure_file_exists():
        os.makedirs(INFO_FOLDER, exist_ok=True)

        if not os.path.exists(USER_INFO_FILE) or os.path.getsize(USER_INFO_FILE) == 0:
            with open(USER_INFO_FILE, "w", encoding="utf-8") as file:
                json.dump({}, file, indent=4)

    @staticmethod
    def _read_warnings() -> dict:
        with open(USER_INFO_FILE, "r", encoding="utf-8") as file:
            return json.load(file)

    @staticmethod
    def _write_warnings(data: dict):
        with open(USER_INFO_FILE, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=4)

    @staticmethod
    def _is_protected(member: discord.Member) -> bool:
        return any(member.get_role(role_id) for role_id in PROTECTED_ROLE_IDS)

    # -------------------------
    # /warn
    # -------------------------

    @app_commands.command(
        name="warn",
        description="Issues a warning to the target user."
    )
    @app_commands.describe(
        target_user="The member to warn.",
        send_dm="Whether to DM the user about this warning.",
        reason="The reason for the warning.",
    )
    @app_commands.choices(
        send_dm=[
            app_commands.Choice(name="yes", value="yes"),
            app_commands.Choice(name="no", value="no"),
        ]
    )
    @app_commands.default_permissions(ban_members=True)
    async def warn_user(
        self,
        interaction: discord.Interaction,
        target_user: discord.Member,
        send_dm: app_commands.Choice[str],
        reason: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        if self._is_protected(target_user):
            await interaction.followup.send(
                "You can't use this command on that user!",
                ephemeral=True
            )
            return

        if not reason:
            reason = "unspecified reason"

        author = interaction.user
        warning_json = self._read_warnings()
        user_id = str(target_user.id)
        warn_id = str(uuid.uuid4())

        # Create the user if they don't already exist
        if user_id not in warning_json:
            warning_json[user_id] = {
                "strikes": {},
                "d_name": target_user.display_name,
                "username": target_user.name,
                "warnings": []
            }

        # Add the warning
        warning_json[user_id]["warnings"].append({
            "reason": reason,
            "timestamp": time.time(),
            "responsible_moderator": author.id,
            "warning-id": warn_id
        })

        self._write_warnings(warning_json)

        # Sends the user a DM for their warning, if requested
        dm_failed = False
        if send_dm.value == "yes":
            try:
                await target_user.send(
                    f"You have been warned in Larp Nation for {reason}. "
                    "If you believe this is a mistake, please file a ticket."
                )
            except discord.Forbidden:
                dm_failed = True

        confirmation = f"Successfully sent warning to {target_user.name}!"
        if dm_failed:
            confirmation += " (Could not DM them — they may have DMs disabled.)"

        await interaction.followup.send(confirmation, ephemeral=True)

        await Logging.log_message(
            f"⚠️ {author.name} has issued a warning against {target_user.name}.",
            f"Warning ID is {warn_id}. To view user's warnings, do "
            f"'/show-warnings <user>'. Reason: {reason}"
        )

    # -------------------------
    # /remove-warning
    # -------------------------

    @app_commands.command(
        name="remove-warning",
        description="Removes a warning from the target user."
    )
    @app_commands.describe(
        warning_id="The ID of the warning to remove.",
        user="The member the warning belongs to.",
        reason="The reason for rescinding the warning.",
    )
    @app_commands.default_permissions(ban_members=True)
    async def remove_warning(
        self,
        interaction: discord.Interaction,
        warning_id: str,
        user: discord.Member,
        reason: str | None = "unspecified reason"
    ):
        await interaction.response.defer(ephemeral=True)

        if self._is_protected(user):
            await interaction.followup.send(
                "You can't use this command on that user!",
                ephemeral=True
            )
            return

        warning_json = self._read_warnings()
        user_id = str(user.id)

        if user_id not in warning_json:
            await interaction.followup.send(
                "That user couldn't be found in the warnings database.",
                ephemeral=True
            )
            return

        warnings = warning_json[user_id]["warnings"]

        for warning in warnings:
            if warning["warning-id"] == warning_id:
                warnings.remove(warning)
                self._write_warnings(warning_json)

                await interaction.followup.send(
                    f"Successfully removed warning `{warning_id}` from {user.mention}.",
                    ephemeral=True
                )

                try:
                    await user.send(embed=discord.Embed(
                        title="Update on your warning",
                        description=(
                            "After careful consideration, we've decided to rescind our "
                            "decision on one of your warnings. We're sorry for giving this "
                            "warning out in the first place and we will do better moving "
                            f"forward.\n\nWarning ID: {warning_id}\n\nReason: {reason}"
                        )
                    ))

                    await Logging.log_message(f"⚠️ Removed a warning from {user.name}'s record!", f"Reason: {reason} \n \n Warning ID: {warning_id}")
                except discord.Forbidden:
                    pass

                return

        await interaction.followup.send(
            "That warning ID wasn't found for that user.",
            ephemeral=True
        )

    # -------------------------
    # /show-warnings
    # -------------------------

    @app_commands.command(
        name="show-warnings",
        description="Shows the warnings that a user has."
    )
    @app_commands.describe(user="The member to look up.")
    @app_commands.default_permissions(ban_members=True)
    async def show_warnings(
        self,
        interaction: discord.Interaction,
        user: discord.Member
    ):
        await interaction.response.defer(ephemeral=True)

        warning_json = self._read_warnings()
        user_id = str(user.id)

        if user_id not in warning_json:
            await interaction.followup.send(
                "That user couldn't be found in the warnings database.",
                ephemeral=True
            )
            return

        warnings = warning_json[user_id]["warnings"]

        if len(warnings) == 0:
            await interaction.followup.send(
                "That user doesn't have any active warnings!",
                ephemeral=True
            )
            return

        warning_string_list = ""

        for index, warning in enumerate(warnings, start=1):
            try:
                responsible_mod = await self.bot.fetch_user(warning["responsible_moderator"])
                mod_name = responsible_mod.name
            except discord.NotFound:
                mod_name = "Unknown moderator"

            utc_dt = datetime.fromtimestamp(warning["timestamp"], tz=timezone.utc)
            local_dt = utc_dt.astimezone()
            timestamp_str = local_dt.strftime("%Y-%m-%d %H:%M:%S")

            warning_string_list += (
                f"⚠️ **Warning** {index}\n\n"
                f"**Reason**: {warning['reason']}\n"
                f"**Timestamp**: {timestamp_str}\n"
                f"**warning-id**: {warning['warning-id']}\n"
                f"**Responsible Moderator**: {mod_name}\n\n"
            )

        response_embed = discord.Embed(
            color=discord.Color.light_grey(),
            title=f"Warnings for **{user.name}**",
            description=warning_string_list,
        )

        await interaction.followup.send(
            f"Here are the warnings for {user.name}\n",
            embed=response_embed,
            ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Warnings(bot))