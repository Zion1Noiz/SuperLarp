import os
import json
import discord
from discord import app_commands
from discord.ext import commands, tasks

import dotenv
from mods import Logging, FileHandling
from time import time

dotenv.load_dotenv()


class UserSelect(discord.ui.UserSelect):
    def __init__(self, view):
        super().__init__(
            placeholder="Select users",
            min_values=1,
            max_values=10,
        )

        self.view_ref = view

    async def callback(self, interaction: discord.Interaction):
        self.view_ref.selected_users = self.values
        await self.view_ref.refresh(interaction)


class VCNameModal(discord.ui.Modal, title="Name your VC"):
    name = discord.ui.TextInput(
        label="Channel name",
        placeholder="Mr Meaty's VC!",
        max_length=25,
    )

    def __init__(self, view):
        super().__init__()
        self.view_ref = view

    async def on_submit(self, interaction: discord.Interaction):
        self.view_ref.vc_name = self.name.value
        await self.view_ref.refresh(interaction)


class NameInputButton(discord.ui.Button):
    def __init__(self, view):
        current = view.vc_name or "Set Name"
        super().__init__(label=current, style=discord.ButtonStyle.primary, row=0)
        self.view_ref = view

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(VCNameModal(self.view_ref))


class WizardView(discord.ui.View):
    """
    Paged setup wizard.

    Page 1 - pick users
    Page 2 - name the VC
    Page 3 - disclaimers / confirm
    """

    def __init__(self, cog, author_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.author_id = author_id

        self.page = 1
        self.selected_users: list[discord.Member] | None = None
        self.vc_name: str | None = None
        self.confirmed = False

        self.build_page()

    # ---------- component / page management ----------

    def build_page(self):
        self.clear_items()

        if self.page == 1:
            self.add_item(UserSelect(self))
        elif self.page == 2:
            self.add_item(NameInputButton(self))
        # page 3 -> disclaimers only, no extra components

        self.add_item(self.back_btn)
        self.add_item(self.continue_btn)

        self.back_btn.disabled = (self.page == 1)
        self.continue_btn.disabled = self._continue_disabled()
        self.continue_btn.label = "Finish" if self.page == 3 else "Continue"

    def _continue_disabled(self) -> bool:
        if self.page == 1:
            return not self.selected_users
        if self.page == 2:
            return not self.vc_name
        return False

    def current_embed(self) -> discord.Embed:
        strings = {
            1: self.cog.Page1String,
            2: self.cog.Page2String,
            3: self.cog.Page3String,
        }
        embed = discord.Embed(
            title="Create A Private Voice Chat",
            description=strings[self.page],
        )
        if self.selected_users:
            embed.add_field(
                name="Selected users",
                value=", ".join(u.mention for u in self.selected_users),
                inline=False,
            )
        if self.vc_name:
            embed.add_field(name="VC Name", value=self.vc_name, inline=False)
        embed.set_footer(text=f"Step {self.page} of 3")
        return embed

    async def refresh(self, interaction: discord.Interaction):
        self.build_page()
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=self.current_embed(), view=self)
        else:
            await interaction.response.edit_message(embed=self.current_embed(), view=self)

    # ---------- guard ----------

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This isn't your setup menu.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

    # ---------- nav buttons ----------

    @discord.ui.button(label="Back", style=discord.ButtonStyle.danger, row=1)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(1, self.page - 1)
        await self.refresh(interaction)

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.success, row=1)
    async def continue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page < 3:
            self.page += 1
            await self.refresh(interaction)
        else:
            self.confirmed = True
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(view=None, embed=None, content="This action has been completed. You may dismiss this message.")
            self.stop()


class EditWizardView(discord.ui.View):
    """
        Page Edit Wizard
    
        Page 1 - Add / Remove Users
        Page 2 - Rename VC
        Page 3 - Delete VC
        """
    
    def __init__(self, cog, author_id : int):
        super().__init__(timeout=300)
        self.cog = cog
        self.author_id = author_id

        self.page = 1
        self.confirmed = False
    
    
    def build_page(self):
        # clearing items #
        self.clear_items()

        if self.page == 1:
            self.add_item(UserSelect(self))
        elif self.page == 2:
            self.add_item(NameInputButton(self))

        self.add_item(self.back_button)
        self.add_item(self.cont_button)

        self.back_button.disabled = (self.page == 1)
        self._continue_disabled == self._continue_disabled()
        self.cont_button.label = "Save changes" if self.page == 3 else "Next"


    def _continue_disabled(self):
        if self.page == 1:
         return not self.selected_users
        if self.page == 2:
            return not self.vc_name
        return False

    def current_embed(self):
        title_strings = {
            1: self.cog.EditAllowedUsersString,
            2: self.cog.RenameVCString,
            3: self.cog.DeleteVCString
        }
        

        pass

    async def interaction_check(self, interaction : discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This isn't your setup menu",
                ephemeral=True
            )
            return False
        return True
    
    

    async def refresh(self, interaction : discord.Interaction):
        self.build_page()
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=self.current_embed(), view=self)
        else:
            await interaction.response.edit_message(embed=self.current_embed, view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


    @discord.ui.button(
        label="Next",
        style=discord.ButtonStyle.success,
        row=1
    )
    async def cont_button(self, interaction : discord.Interaction):
        if self.page < 3:
            self.page += 1
            await self.refresh(interaction)
        else:
            self.confirmed = True
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(view=None, embed=None, content="This action has been completed. You may dismiss this message.")
            self.stop()
        pass


    @discord.ui.button(
        label="Previous",
        style=discord.ButtonStyle.danger,
        row=1
    )
    async def back_button(self, interaction : discord.Interaction):
        self.page = max(1, self.page - 1)
        await self.refresh(interaction)



    


class PrivateVoiceChatHost(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # setup wizard strings #

    Page1String = (
        "## First, we need to know who's allowed in here. ##\n\n"
        "These are the people that will be able to join and leave your private VC. (Not including yourself)"
    )
    Page2String = (
        "## Now, we need a name for this private VC. ##\n \n"
        "Give it a wonderful name that'll stick out to you! "
    )
    Page3String = (
        "## Finally, the disclaimers.. ## \n \n"
        "- Server moderators will be able to enter your voice channel and take appropriate action if an individual is breaking rules \n \n"
        "- To delete your voice channel, type '/pvc-remove'. Once a voice channel is deleted, all messages will be **hidden¹** from view. \n \n"
        "- If you leave your Private Voice channel inactive (meaning no one is in there) for 2 minutes, it will be automatically deleted and another channel would have to be created \n \n"
        "- To edit your voice channel, type '/pvc-settings' and whenever you finish, press save. \n \n \n"
        "¹ - Messages that are deleted from the channel are archived and will be able to be seen by server owners and moderators."
    )


    # edit wizard strings #
    EditAllowedUsersString = (
        "# Edit allowed users # \n \n "
        "You can remove or add users here."
    )

    RenameVCString = (
        "# Edit VC Name # \n \n"
        "Click the button below to edit your Private VC's name."
    )

    DeleteVCString = (
        "# Delete VC # \n \n"
        "Click the button to delete your PVC. (Note: This will immediately remove individuals from that voice channel.)"
    )

    @app_commands.command(
        name="private-vc",
        description="Allows you to create a private voice chat."
    )
    @commands.is_owner()
    async def createPrivateVC(self, interaction: discord.Interaction):
        try:
            view = WizardView(self, interaction.user.id)
            embed = view.current_embed()

            get_users_info = FileHandling.ReadFromJSONFile("./info/user-info.json")

            if get_users_info[str(interaction.user.id)]['private_vc']:
                print("f")
                await interaction.response.send_message(
                    "You already have an active private vc!", ephemeral=True
                )
                return


            content_message = await interaction.response.send_message(
                embed=embed,
                view=view,
                ephemeral=True
            )

            timed_out = await view.wait()

            if timed_out:
                await interaction.followup.send(
                    "Command exited with code: INACTIVITY_TIMEOUT", ephemeral=True
                )
                return

            if not view.confirmed:
                await interaction.followup.send(
                    "Command exited with code: SETUP_CANCELLED", ephemeral=True
                )
                return

            userlist = view.selected_users
            if interaction.user not in userlist:
                userlist.append(interaction.user)
            vc_name = view.vc_name

            if not userlist:
                await interaction.followup.send(
                    "Command exited with code: NO_USERS_SELECTED", ephemeral=True
                )
                return

            if not vc_name:
                await interaction.followup.send(
                    "Command exited with code: NO_NAME_SET", ephemeral=True
                )
                return

            for user in userlist:
                print(f"{user.name} ({user.id})")

            print(f"VC Name: {vc_name}")

            ModeratorPermissions = discord.PermissionOverwrite(

            )
            AllowedUsersPermission = discord.PermissionOverwrite(
                view_channel=True,
                connect=True,

            )

            @tasks.loop(minutes=5)
            async def VoiceChannelMemberCheck(self, VC : discord.VoiceChannel):
                members = VC.members
                if len(members) == 0:
                    # delete the channel with the reason, "inactivity" #
                    await VC.delete("Channel was inactive for 5 minutes.")
                    u_content = FileHandling.ReadFromJSONFile("./info/user-info.json")
                    member_data = u_content[str(interaction.user.id)]
                    if member_data["private_vc"]:
                        member_data["private_vc"] = {}
                    


            # TODO: actually create the voice channel here using userlist + vc_name
            Guild = self.bot.get_guild(int(os.getenv("GUILD_ID"))) # or server
            PVCCategory = Guild.get_channel(1533883181890277437) # Gets the private voice chats category
            AlphaModRole = Guild.get_role(int(os.getenv("ALPHA_ROLE_ID")))
            BetaModRole = Guild.get_role(int(os.getenv("BETA_ROLE_ID")))

            try:
                voiceChatObject = await Guild.create_voice_channel(
                name=f"{vc_name}"
                )
                view.clear_items()
                await interaction.followup.send("Successfully created your voice chat. It's in the 'Private Voice Channels' category. Messages may now be dismissed.", ephemeral=True)
            except (Exception, discord.HTTPException) as e:
                await interaction.followup.send(
                    f"Command exited with code: CHANNEL_CREATION_FAILURE \n \n Error: {e}", ephemeral=True
                )
                return

            try:

                await voiceChatObject.set_permissions(AlphaModRole, overwrite=ModeratorPermissions)
                await voiceChatObject.set_permissions(BetaModRole, overwrite=ModeratorPermissions)

                for user in userlist:
                    await voiceChatObject.set_permissions(user, overwrite=AllowedUsersPermission)
            except (Exception, discord.HTTPException) as e:
                await interaction.followup.send(
                    f"Command exited with code: PERMISSION_SET_FAILURE \n \n Error: {e}", ephemeral=True
                    )
                await voiceChatObject.delete(reason="Super Larp encountered an error")
                return

            try:

                await voiceChatObject.edit(
                    category=PVCCategory
                )
            except (Exception, discord.HTTPException) as e:
                await interaction.followup.send(
                    f"Command exited with code: CHANNEL_CONFIGURATION_EDIT_FAILED \n \n Error: {e}", ephemeral=True
                )
                await voiceChatObject.delete(reason="Super Larp encountered an error")
                return

            jsonSend = {
                "vc_name" : vc_name,
                "channel_id" : voiceChatObject.id,
                "StartedTimestamp" : time()
            }

            user_data = FileHandling.ReadFromJSONFile("./info/user-info.json")
            

            if user_data is "Not found":
                await interaction.followup.send(
                    f"Command exited with code: USER_METADATA_NOT_FOUND \n \n Error: Could not find the requested user data.", ephemeral=True
                )
                await voiceChatObject.delete(reason="Super Larp encountered an error")
                return 

            try:
                user_data[str(interaction.user.id)]["private_vc"] = jsonSend
                FileHandling.WriteToJSON("./info/user-info.json", user_data)
                
            except Exception as e:
                await interaction.followup.send(
                    f"Command exited with code: USER_METADATA_POST_FAILED \n \n Error: {e}", ephemeral=True
                )
                await voiceChatObject.delete(reason="Super Larp encountered an error")
                return 
            
        except Exception as e:
            print(e)

    @app_commands.command(
        name="pvc-edit",
        description="Allows a user to configure their VC settings"
    )
    @commands.is_owner()
    async def editPrivateVC(self, interaction : discord.Interaction):
        member_id = interaction.user.id

        editview = EditWizardView(self, member_id)
        embed = editview.current_embed()


        user_info = FileHandling.ReadFromJSONFile("./info/user-info.json")
        if not member_id in user_info:
            await interaction.response.send_message("The command could not proceed. User data for the interaction author could not be found. Please contact owners.", ephemeral=True)
            return

        user_data = user_info[str(member_id)]
        if not user_data["private_vc"]:
            await interaction.response.send_message("You do not own a private vc!", ephemeral=True)


        message = await interaction.response.send_message(
            embed=embed,
            view=editview,
            ephemeral=True
        )

        timed_out = await editview.wait()

        if timed_out:
            await interaction.followup.send(
                "Command exited with code: INACTIVITY_TIMEOUT", ephemeral=True
            )
            return

        if not editview.confirmed:
            await interaction.followup.send(
                "Command edited with code: SETUP_CANCELED",
                ephemeral=True
            )

        

        pass



async def setup(bot: commands.Bot):
    await bot.add_cog(PrivateVoiceChatHost(bot))