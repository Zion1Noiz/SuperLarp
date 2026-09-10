import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from mods import Logging

INFO_FOLDER = "./info"
USER_INFO_FILE = os.path.join(INFO_FOLDER, "user-info.json")

# Roles that are exempt from moderation commands (e.g. staff roles).
# Set ProtectedRoleIDs in your .env as a comma-separated list of role IDs.
_default_protected_roles = "1526828189001846815,1526822799337984122,1515171114647818272"
PROTECTED_ROLE_IDS = [
    int(rid) for rid in os.getenv("ProtectedRoleIDs", _default_protected_roles).split(",")
    if rid.strip()
]

FIRST_STRIKE_MSG = (
    "Since this is your first strike, consider this your first 'serious' warning. "
    "As a result of this, you've been timed out for 1 hour. If you get 2 more strikes, "
    "we will have to ban you from the server with a possible (or impossible, depends on "
    "the ban reason) chance of appeal."
)
SECOND_STRIKE_MSG = (
    "This is now your second strike. Because this is your second strike, you have been "
    "timed out for a day. If you continue these actions, you will receive your 3rd and "
    "final strike. Please ensure to follow the rules so everybody can have a fun time at "
    "Larp Nation."
)
THIRD_STRIKE_MSG = (
    "You have received your third and final strike. You have been banned from our server. "
    "If you believe this action was unfair or a mistake, please contact moderation / support."
)


class Strikes(commands.Cog):
    """Handles issuing, removing, and viewing member strikes."""

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
    def _read_user_info() -> dict:
        with open(USER_INFO_FILE, "r", encoding="utf-8") as file:
            return json.load(file)

    @staticmethod
    def _write_user_info(data: dict):
        with open(USER_INFO_FILE, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=4)

    @staticmethod
    def _is_protected(member: discord.Member) -> bool:
        return any(member.get_role(role_id) for role_id in PROTECTED_ROLE_IDS)

    @staticmethod
    def _coerce_to_list(value) -> list:
        """Some older/other code may have stored 'strikes' or 'warnings' as a
        dict (e.g. {strike_id: {...}}) instead of a list. Normalize either
        shape back into a plain list so .append()/.pop() always work."""
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            return list(value.values())
        return []

    def _get_or_create_user(self, user_info_json: dict, member: discord.Member) -> dict:
        """Fetch a user's record, creating it if needed, and normalizing its
        shape in case it was previously written in a different/legacy format."""
        user_id = str(member.id)

        if user_id not in user_info_json:
            user_info_json[user_id] = {
                "strikes": [],
                "d_name": member.display_name,
                "username": member.name,
                "warnings": []
            }
        else:
            record = user_info_json[user_id]
            record["strikes"] = self._coerce_to_list(record.get("strikes"))
            record["warnings"] = self._coerce_to_list(record.get("warnings"))
            # Keep display name / username fresh too.
            record["d_name"] = member.display_name
            record["username"] = member.name

        return user_info_json[user_id]

    async def _safe_followup(self, interaction: discord.Interaction, content: str):
        """Always resolve the deferred interaction, even if the original followup fails."""
        try:
            await interaction.followup.send(content, ephemeral=True)
        except discord.HTTPException:
            # Interaction webhook token may have expired (>15 min) — nothing more we can do,
            # but at least we tried instead of silently swallowing the error.
            pass

    # -------------------------
    # /strike
    # -------------------------

    @app_commands.command(
        name="strike",
        description="Issues a strike to the target user."
    )
    @app_commands.describe(
        target_user="The member to strike.",
        reason="The reason for the strike.",
    )
    @app_commands.default_permissions(ban_members=True)
    async def strike(
        self,
        interaction: discord.Interaction,
        target_user: discord.Member,
        reason: str
    ):
        await interaction.response.defer(ephemeral=True)

        try:
            if self._is_protected(target_user):
                await self._safe_followup(interaction, "You can't strike that user!")
                return

            if not reason:
                reason = "unspecified reason"

            author = interaction.user
            user_info_json = self._read_user_info()
            user_id = str(target_user.id)
            strike_id = str(uuid.uuid4())

            user_record = self._get_or_create_user(user_info_json, target_user)

            user_record["strikes"].append({
                "reason": reason,
                "timestamp": time.time(),
                "responsible_moderator": author.id,
                "strike-id": strike_id
            })

            self._write_user_info(user_info_json)

            amount_of_strikes = len(user_record["strikes"])

            dm_strike_msg = ""
            moderator_note = ""
            mod_action_failed = False

            # Punishment actions (timeout/ban) can fail due to missing bot permissions
            # or role-hierarchy issues — never let that leave the interaction hanging.
            try:
                match amount_of_strikes:
                    case 1:
                        dm_strike_msg = FIRST_STRIKE_MSG
                        await target_user.timeout(timedelta(hours=1))
                    case 2:
                        dm_strike_msg = SECOND_STRIKE_MSG
                        await target_user.timeout(timedelta(days=1))
                    case 3:
                        dm_strike_msg = THIRD_STRIKE_MSG
                        await target_user.ban(reason=f"Reached 3 strikes with strike id of {strike_id}")
                    case _:
                        # Should be rare — the user should already be banned by strike 3.
                        moderator_note = " (Note: this user already had 3+ strikes.)"
            except discord.Forbidden:
                mod_action_failed = True
                moderator_note += " (Warning: I couldn't timeout/ban this user — check my role position/permissions.)"
            except discord.HTTPException as e:
                mod_action_failed = True
                moderator_note += f" (Warning: Discord API error while applying punishment: {e})"

            dm_embed = discord.Embed(
                title="You have been issued a strike.",
                description=(
                    "You received this strike because you violated one of our rules! In order "
                    "to keep Larp Nation a safe place for everyone, members must follow all "
                    f"rules.\n\n{dm_strike_msg}\n\nYour associated strike-id is {strike_id}"
                ),
                color=discord.Color.light_grey()
            )

            dm_failed = False
            try:
                await target_user.send(embed=dm_embed)
            except discord.Forbidden:
                dm_failed = True
            except discord.HTTPException:
                dm_failed = True

            confirmation = f"Successfully sent strike to {target_user.name}!{moderator_note}"
            if dm_failed:
                confirmation += " (Could not DM them — they may have DMs disabled.)"

            await self._safe_followup(interaction, confirmation)

            try:
                await Logging.log_message(
                    f"⚠️ {author.name} has issued a **strike** against {target_user.name}.",
                    f"Strike ID is {strike_id}. To view this user's strikes, do '/show-strikes <user>'."
                )
            except Exception:
                # Logging failures shouldn't affect the command's success/failure.
                pass

        except Exception as e:
            await self._safe_followup(interaction, f"Something went wrong while issuing the strike: {e}")
            raise

    # -------------------------
    # /remove-strike
    # -------------------------

    @app_commands.command(
        name="remove-strike",
        description="Removes a strike from the target user."
    )
    @app_commands.describe(
        user="The member the strike belongs to.",
        strike_id="The ID of the strike to remove.",
        reason="The reason for rescinding the strike.",
    )
    @app_commands.default_permissions(ban_members=True)
    async def remove_strike(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        strike_id: str,
        reason: str | None = None
    ):
        await interaction.response.defer(ephemeral=True)

        try:
            if not reason:
                reason = "unspecified reason"

            user_info_json = self._read_user_info()
            user_id = str(user.id)

            if user_id not in user_info_json:
                await self._safe_followup(interaction, "That user couldn't be found in the user info database.")
                return

            user_record = self._get_or_create_user(user_info_json, user)
            strikes = user_record["strikes"]

            match_index = None
            for index, strike in enumerate(strikes):
                if strike["strike-id"] == strike_id:
                    match_index = index
                    break

            if match_index is None:
                await self._safe_followup(interaction, "That strike ID wasn't found for that user.")
                return

            strikes.pop(match_index)
            self._write_user_info(user_info_json)

            try:
                if user.is_timed_out():
                    await user.timeout(None)
            except discord.Forbidden:
                pass
            except discord.HTTPException:
                pass

            await self._safe_followup(
                interaction, f"Successfully removed strike `{strike_id}` from {user.mention}."
            )

            try:
                await Logging.log_message(
                    f"⚠️ A strike has been removed from {user.name}'s record!",
                    f"Strike ID: {strike_id}. Reason: {reason}"
                )
            except Exception:
                pass

            try:
                await user.send(embed=discord.Embed(
                    title="One of your strikes has been removed!",
                    description=(
                        "Hello! We noticed that one of your strikes was incorrectly "
                        f"issued and have removed it from your record.\n\n"
                        f"Strike ID: {strike_id}\n\nReason: {reason}"
                    )
                ))
            except discord.Forbidden:
                pass
            except discord.HTTPException:
                pass

        except Exception as e:
            await self._safe_followup(interaction, f"Something went wrong while removing the strike: {e}")
            raise

    # -------------------------
    # /show-strikes
    # -------------------------

    @app_commands.command(
        name="show-strikes",
        description="Shows strike info about a user."
    )
    @app_commands.describe(user="The member to look up.")
    @app_commands.default_permissions(ban_members=True)
    async def show_strikes(
        self,
        interaction: discord.Interaction,
        user: discord.Member
    ):
        await interaction.response.defer(ephemeral=True)

        try:
            user_info_json = self._read_user_info()
            user_id = str(user.id)

            if user_id not in user_info_json:
                await self._safe_followup(interaction, "That user couldn't be found in the user info database.")
                return

            user_record = self._get_or_create_user(user_info_json, user)
            strikes = user_record["strikes"]

            if len(strikes) == 0:
                await self._safe_followup(interaction, "That user doesn't have any active strikes!")
                return

            strike_string_list = ""

            for index, strike in enumerate(strikes, start=1):
                try:
                    responsible_mod = await self.bot.fetch_user(strike["responsible_moderator"])
                    mod_name = responsible_mod.name
                except discord.NotFound:
                    mod_name = "Unknown moderator"
                except discord.HTTPException:
                    mod_name = "Unknown moderator"

                utc_dt = datetime.fromtimestamp(strike["timestamp"], tz=timezone.utc)
                local_dt = utc_dt.astimezone()
                timestamp_str = local_dt.strftime("%Y-%m-%d %H:%M:%S")

                strike_string_list += (
                    f"⚠️ **Strike** {index}\n\n"
                    f"**Reason**: {strike['reason']}\n"
                    f"**Timestamp**: {timestamp_str}\n"
                    f"**strike-id**: {strike['strike-id']}\n"
                    f"**Responsible Moderator**: {mod_name}\n\n"
                )

            response_embed = discord.Embed(
                color=discord.Color.light_grey(),
                title=f"Strikes for **{user.name}**",
                description=strike_string_list,
            )

            try:
                await interaction.followup.send(
                    f"Here are the strikes for {user.name}\n",
                    embed=response_embed,
                    ephemeral=True
                )
            except discord.HTTPException:
                pass

        except Exception as e:
            await self._safe_followup(interaction, f"Something went wrong while fetching strikes: {e}")
            raise


async def setup(bot: commands.Bot):
    await bot.add_cog(Strikes(bot))