import os
import json
import discord
from discord import app_commands
from discord.ext import commands
from time import time

import dotenv
from mods import Logging, FileHandling

dotenv.load_dotenv()

ban_list_path = "./info/confessions/banlist.json"

_default_protected_roles = "1526828189001846815,1526822799337984122,1515171114647818272"
PROTECTED_ROLE_IDS = [
    int(rid) for rid in os.getenv("ProtectedRoleIDs", _default_protected_roles).split(",")
    if rid.strip()
]

def _is_protected(member: discord.Member) -> bool:
        return any(member.get_role(role_id) for role_id in PROTECTED_ROLE_IDS)

def is_owner():
    async def predicate(interaction: discord.Interaction):
        return await interaction.client.is_owner(interaction.user)
    return app_commands.check(predicate)


class SendConfessionButtons(discord.ui.View):
    def __init__(self, cog: "Confessions"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        style=discord.ButtonStyle.success,
        label="Send Confession",
        custom_id="send_confession",
    )
    async def SendConfessionButton(self, interaction : discord.Interaction, button):
        try:
            await interaction.response.send_modal(SendConfessionModal(self.cog.bot, upload=None))
        except Exception as e:
            await interaction.response.send_message(f"Something went wrong with your request. Please try again later. Error: {e}", ephemeral=True)
            return

    # Reply button

    #@discord.ui.Button(
    #    style=discord.ButtonStyle.grey,
   #     label="Reply to Confession",
    #    custom_id="reply_confession"
    #)

class SendConfessionModal(discord.ui.Modal, title="Send a Confession!"):
    def __init__(self, bot : commands.Bot, upload : str | None = None):
        super().__init__(timeout=300)
        self.bot = bot
        self.attachment = upload
        self.ConfessionMessageObject = discord.Embed(
            color=discord.Color.light_grey(),
        )


    body = discord.ui.TextInput(
        label="Enter confession",
        style=discord.TextStyle.long,
        placeholder="Enter your confession here.",
        required=True,
    )

    async def on_submit(self, interaction : discord.Interaction):
        try:
            ConfessionChannelID = int(os.getenv("CONFESSION_CHANNEL_ID"))
            ConfessionLogsChannelID = 1531438097248813267
            ConfessionChannelObject = self.bot.get_channel(ConfessionChannelID)
            self.ConfessionMessageObject.description = self.body.value

            

            LastConfessionNumber = int(FileHandling.ReadFromTextFile("./info/confessions/LastConfessionNumber.txt"))
            LastConfessionNumber += 1
            FileHandling.WriteToTextFile("./info/confessions/LastConfessionNumber.txt", str(LastConfessionNumber))
            self.ConfessionMessageObject.title = f"Anonymous Confession (#{LastConfessionNumber})"

            sendarray = []
            attachment_url = None

            if self.attachment is not None:
                attachment_url = self.attachment.url
                self.ConfessionMessageObject.set_image(url=self.attachment.url)
                sendarray = [self.attachment.url]

            await Logging.log_message(f"Anonymous Confession (#{LastConfessionNumber})", f' "{self.body.value}" \n \n User: || {interaction.user.display_name} [@{interaction.user.name}] || \n \n User ID: || [{interaction.user.id}] || ', thumb=interaction.user.avatar, channel_id=ConfessionLogsChannelID, images=sendarray)

            ConfessionMessage = await ConfessionChannelObject.send(
                embed=self.ConfessionMessageObject,
                view=SendConfessionButtons(self.bot.get_cog("Confessions"))
            )

            send_meta_temp = {
                "confession_number": LastConfessionNumber,
                "sender": {
                    "display_name": interaction.user.display_name,
                    "username": interaction.user.name,
                    "user-id": interaction.user.id,
                },
                "message_id": ConfessionMessage.id,
                "content": self.body.value,
                "attachment": attachment_url,
                }
            confession_logs_json = FileHandling.ReadFromJSONFile("./info/confessions/confession-logs.json")

            confession_logs_json[str(LastConfessionNumber)] = send_meta_temp
            
            FileHandling.WriteToJSON("./info/confessions/confession-logs.json", confession_logs_json)
            
            await interaction.response.send_message("Successfully sent confession! For moderation purposes, we've sent a copy of your confession to moderators.", ephemeral=True)

        except Exception as e:
            await interaction.response.send_message(f"Something went wrong with your request. Error: {e}", ephemeral=True)


def IsUserConfessionBanned(user : discord.Member):
    ban_list_json = FileHandling.ReadFromJSONFile(ban_list_path)

    target_user_id = str(user.id)

    if target_user_id in ban_list_json:
        return True
    else:
        return False

async def ConfessionBanUser(user : discord.Member, reason : str, interaction : discord.Interaction):
    current_ban_list = FileHandling.ReadFromJSONFile(ban_list_path)
    meta_data = {
        "username": user.name,
        "id": user.id,
        "reason": reason,
        "responsible_moderator": {
            "id": interaction.user.id,
            "name": interaction.user.name
        },
        "timestamp": time()
    }
    try:
        current_ban_list[str(user.id)] = meta_data
        FileHandling.WriteToJSON(ban_list_path, current_ban_list)
        await Logging.log_message(
            message_title="⚠️ A user has been banned from making confessions!",
            description=f"User: {user.name} \n \n UserID: {user.id} \n \n Reason: {reason} \n \n Responsible Moderator: {interaction.user}",
            thumb=user.display_avatar.url
            )
    except Exception as e:
        print(f"Something went wrong with banning user from confessions. Error: {e}")


async def ConfessionUnbanUser(user : discord.Member, reason : str, interaction : discord.Interaction):
    current_ban_list = FileHandling.ReadFromJSONFile(ban_list_path)
    try:
        if str(user.id) in current_ban_list:
            current_ban_list.pop(str(user.id))
            FileHandling.WriteToJSON(ban_list_path,current_ban_list)
            await Logging.log_message(
                message_title="⚠️ A confessions ban has been removed!",
                description=f"User: {user.name} \n \n UserID: {user.id} \n \n Reason: {reason} \n \n Responsible Moderator: {interaction.user}",
                thumb=user.display_avatar.url
            )
    except Exception as e:
        print(f"Something went wrong with your request. Error: {e}")

async def DeleteConfession(confession_number : int, interaction : discord.Interaction, bot : commands.Bot, reason : str | None = "unspecified reason"):
    confession_logs_json = FileHandling.ReadFromJSONFile("./info/confessions/confession-logs.json")
    if str(confession_number) in confession_logs_json:
        try:
            target_data = confession_logs_json[str(confession_number)]
            ConfessionChannel = bot.get_channel(int(os.getenv("CONFESSION_CHANNEL_ID")))
            message_object = await ConfessionChannel.fetch_message(target_data['message_id'])
            if message_object:
                await message_object.delete()
                confession_logs_json.pop(str(confession_number))
                FileHandling.WriteToJSON("./info/confessions/confession-logs.json", confession_logs_json)
                await Logging.log_message(
                    f"⚠️ Confession (#{confession_number}) by {target_data['sender']['username']} was deleted!",
                    f"Responsible Moderator: {interaction.user.display_name} @[{interaction.user.name}] \n \n Responsible Moderator ID: {interaction.user.id} \n \n Reason For Deletion: {reason}"
                    
                )
                return True
            else:
                print("not found")
                return False
        except Exception as e:
            print(f"FAILURE: {e}")
            return 'Failure'
    else:
        return False

class Confessions(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="confession",
        description="Submits a confession!"
    )
    async def submit_confession(self, interaction : discord.Interaction, upload : discord.Attachment | None = None):
        user = interaction.user
        isBanned = IsUserConfessionBanned(user)
        if isBanned == True:
            await interaction.response.send_message("You're banned from making confessions!", ephemeral=True)
            return
        else:
            try:
                await interaction.response.send_modal(SendConfessionModal(self.bot, upload))
                
            except Exception as e:
                await interaction.response.send_message(f"Something went wrong with your request. Error: {e}", ephemeral=True)


    @app_commands.command(
            name="delete-confession",
            description="Deletes a confession"
    )
    @app_commands.default_permissions(ban_members=True)
    async def delete_confession(self, interaction : discord.Interaction, confession_id : int, reason : str):
        response = await DeleteConfession(confession_id, interaction, self.bot, reason)
        if (response == True):
            await interaction.response.send_message("Successfully deleted user's confession", ephemeral=True)
        elif (response == False):
            await interaction.response.send_message("Could not find the confession by it's id. Did you enter the right confession id?", ephemeral=True)
        elif (response == "Failure"):
            await interaction.response.send_message("An error occured with your request.", ephemeral=True)



    @app_commands.command(
        name="confession-ban",
        description="Bans a user from making confessions"
    )
    @app_commands.default_permissions(ban_members=True)
    async def confession_ban_user(self, interaction : discord.Interaction, user : discord.Member, reason : str | None = "unspecified reason"):
        try:
            if _is_protected(user):
                await interaction.followup.send(
                    "You can't use this command on that user!",
                 ephemeral=True
             )
                return
            await ConfessionBanUser(user=user, reason=reason, interaction=interaction)
            await interaction.response.send_message("Successfully moderated this user from confessions!", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Something went wrong with your request. Error: {e}", ephemeral=True)

    @app_commands.command(
            name="confession-unban",
            description="Bans a user from making confessions"
        )
    @app_commands.default_permissions(ban_members=True)
    async def confession_unban_user(self, interaction : discord.Interaction, user : discord.Member, reason : str | None = "unspecified reason"):
        try:
            if _is_protected(user):
                await interaction.followup.send(
                "You can't use this command on that user!",
                ephemeral=True
                 )
                return
            await ConfessionUnbanUser(user=user, reason=reason, interaction=interaction)
            await interaction.response.send_message("Successfully removed this user's ban!", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Something went wrong with your request. Error: {e}", ephemeral=True)



async def setup(bot: commands.Bot):
    cog = Confessions(bot)
    await bot.add_cog(cog)
    bot.add_view(SendConfessionButtons(cog))