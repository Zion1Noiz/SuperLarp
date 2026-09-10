import os

import discord
from discord import app_commands
from discord.ext import commands

from datetime import date, time, datetime
from mods import FileHandling, Logging

INFO_FOLDER = "./info"
FAST_FLAGS_FILE = os.path.join(INFO_FOLDER, "fast-flags.json")
VENT_WARNING_FILE = os.path.join(INFO_FOLDER, "VentingWarningMessage.txt")
ACCEPTED_VENT_USERS = os.path.join(INFO_FOLDER, "UsersThatHaveAccessToVentChannel.json")

# Role granted to members who opt into the venting channel.
VENT_ACCESS_ROLE_ID = os.getenv("VentChannelPermissionRoleID")
VENT_ACCESS_ROLE_ID = int(VENT_ACCESS_ROLE_ID) if VENT_ACCESS_ROLE_ID else None

class VentOptInView(discord.ui.View):

    def __init__(self, member: discord.Member, role_id: int):
        super().__init__(timeout=300)
        self.member = member
        self.role_id = role_id

    @discord.ui.button(
        label="I understand.",
        style=discord.ButtonStyle.success,
        custom_id="vent_opt_in_confirm",
    )
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        role = interaction.guild.get_role(self.role_id)

        if role is None:
            await interaction.response.send_message(
                "The vent-access role could not be found. Please contact staff.",
                ephemeral=True,
            )
            return

        try:
            await self.member.add_roles(role, reason="Opted into the venting channel.")
            await interaction.response.send_message(
                "✅ You now have access to the venting channel.",
                ephemeral=True,
            )
            await Logging.log_message(f"⚠️ User signed up for venting channel ", f"Username: {interaction.user.name} \n \n UserID: {interaction.user.id}")

        except discord.Forbidden:
            await interaction.response.send_message(
                "⛔ An exception occured. SUPER LARP does not have the correct permissions to assign you a role. Please contact staff.",
                ephemeral=True,
            )

        for child in self.children:
            child.disabled = True
        self.stop()


class AnonymousVentMessageView(discord.ui.Modal, title="Send Anonymous Vent!"):
    body = discord.ui.TextInput(
        label="Enter your message!",
        style=discord.TextStyle.long,
        placeholder="Enter your message here.",
        required=True,
    )

    async def on_submit(self, interaction : discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        SendEmbed = discord.Embed(
            title="Anonymous Vent",
            description=f"|| {self.body.value} ||",
            color=discord.Color.dark_gray(),

        )

        if interaction.channel.id == int(os.getenv("VENT_CHANNEL_ID")):
            await interaction.channel.send(embed=SendEmbed)
            await Logging.log_message(
                f"⚠️ Anonymous Vent sent by {interaction.user.name}",
                f"User: {interaction.user.display_name} [@{interaction.user.name}] \n \n User ID: {interaction.user.id} \n \n Message: || {self.body.value} || \n \n"
                )
            await interaction.followup.send("Successfully sent your vent! Note: For moderation purposes, we've sent a copy of your vent to moderators.", ephemeral=True)
    async def on_error(self, interaction, error):
        await interaction.followup.send(f"Submission failed. {error}",ephemeral=True)
    async def on_timeout(self, interaction):
        await interaction.followup.send("Time out ",ephemeral=True)

class VentOptIn(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="vent-opt-in",
        description="Grants access to the venting channel after you acknowledge its guidelines."
    )
    @app_commands.checks.has_role("Member")
    async def opt_in_vent_channel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        member = interaction.user

        # -------------------------
        # Feature flag check
        # -------------------------
        try:
            fast_flags = FileHandling.ReadFromJSONFile(FAST_FLAGS_FILE) or {}
        except Exception as e:
            await Logging.log_message(
                "⛔ Error",
                "A critical exception occurred in 'opt_in_vent_channel' while reading "
                f"fast flags, and the command was ignored with the following error:\n\n```{e}```"
            )
            await interaction.followup.send(
                "Something went wrong while checking feature availability. Please try again later.",
                ephemeral=True,
            )
            return

        mode = fast_flags.get("VentChannelAccessMode", "Closed")

        if mode == "ClosedDebugging":
            is_admin = member.guild_permissions.administrator

            if not (is_admin):
                await interaction.followup.send("You're not allowed to access this feature yet. ", ephemeral=True)
                return
            else:
                print("Administrator")

        if mode == "InsiderPreview":
            is_admin = member.guild_permissions.administrator

            if not (member.get_role(int(os.getenv("InsiderRoleID")))) and (not is_admin):
                await interaction.followup.send("You're not allowed to access this feature yet. ", ephemeral=True)
                return
            else:
                print("Insider")

                
        
        # -------------------------
        # Role / already-opted-in checks
        # -------------------------
        if VENT_ACCESS_ROLE_ID is None:
            await interaction.followup.send(
                "An exception occured. The configurations are incorrect. Error: ROLE_MISMATCH",
                ephemeral=True,
            )
            return
        else:
            print("Role ID is valid.")

        if member.get_role(VENT_ACCESS_ROLE_ID):
            await interaction.followup.send(
                "You already have access to the venting channel!",
                ephemeral=True,
            )
            return

        # -------------------------
        # Show the warning + confirm button
        # -------------------------

        warning_text = "Fallback message"

        if os.path.exists(VENT_WARNING_FILE):
            try:
                with open(VENT_WARNING_FILE, "r", encoding="utf-8") as file:
                    file_text = file.read().strip()
                    if file_text:
                        warning_text = file_text
            except Exception as e:
                await Logging.log_message(
                    "⚠️ Warning",
                    f"Could not read VentingWarningMessage.txt, using default text.\n\n```{e}```"
                )

        embed = discord.Embed(
            title="⚠️ Before you opt in...",
            description=warning_text,
            color=discord.Color.light_grey(),
        )

        try: 
            await interaction.followup.send(
                embed=embed,
                view=VentOptInView(member, role_id=VENT_ACCESS_ROLE_ID),
                ephemeral=True,
            )
        except discord.HTTPException and Exception as e:
            await interaction.followup.send(f"Something went wrong. Error: {e}", ephemeral=True)

    @app_commands.command(
        name="vent-opt-out",
        description="Removes permission to view the venting channel."
    )
    @app_commands.checks.has_role("Member")
    async def opt_out_vent_channel(self, interaction:discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user = interaction.user
        vent_access_role = interaction.guild.get_role(VENT_ACCESS_ROLE_ID)
        if user.get_role(VENT_ACCESS_ROLE_ID):
            await user.remove_roles(vent_access_role, reason="User opted out of vents.")
            await interaction.followup.send("Successfully opted out. ", ephemeral=True)
        else:
            await interaction.followup.send("You aren't opted in the vents channel.")
            pass
        pass

    @app_commands.command(
        name="anonymous-vent",
        description="Sends a vent message without revealing your name"
    )
    @app_commands.checks.has_role("VC")
    async def anonymous_vent(self, interaction : discord.Interaction):
        #await interaction.response.defer(ephemeral=True)
        currentChannel = interaction.channel
        user = interaction.user
        if user.get_role(VENT_ACCESS_ROLE_ID) and currentChannel.id == int(os.getenv("VENT_CHANNEL_ID")):
            try:
                await interaction.response.send_modal(AnonymousVentMessageView())
            except Exception and discord.HTTPException as e:
                await interaction.followup.send(f"Your action failed. Please try again later. Error: {e}", ephemeral=True)
        else:
            try:
                await interaction.followup.send("You don't have access to that channel!", ephemeral=True)
            except discord.HTTPException and Exception as e:
                print(e)
            return

        

async def setup(bot: commands.Bot):
    await bot.add_cog(VentOptIn(bot))