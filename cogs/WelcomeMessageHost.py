# This is only test code that was pasted from google #

import os
import json

import discord

from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv

WELCOME_CHANNEL = int(os.getenv("WELCOME_CHANNEL_ID"))

RULES_CHANNEL_ID = int(os.getenv("RulesChannelID"))
HELP_DESK_CHANNEL_ID = int(os.getenv("HelpDeskChannelID"))


WELCOME_EMBED_TEMPLATE = discord.Embed(
    color=discord.Color.light_grey(),
    title="Welcome to Larp Nation!",
)


class Greetings(commands.Cog):
    def __init__(self, bot : commands.Bot):
        self.bot = bot
        self._last_member = None
    
    @commands.Cog.listener()
    async def on_member_join(self, member : discord.Member):
        try:
            channel = self.bot.get_channel(WELCOME_CHANNEL)
            rules_channel = self.bot.get_channel(RULES_CHANNEL_ID)
            help_channel = self.bot.get_channel(HELP_DESK_CHANNEL_ID)
            embed_description_template = f"Thank you for joining our server!! \n \n Before you start having fun, you should check out our {rules_channel.mention} channel to know how our server works! Once you've read the rules and accepted, you should also check out our {help_channel.mention} to learn server commands!"

            if channel is not None:
                WELCOME_EMBED_TEMPLATE.description = embed_description_template
                WELCOME_EMBED_TEMPLATE.set_thumbnail(url="https://media.discordapp.net/ephemeral-attachments/1534592191404576940/1534704784068575232/b0230cd0540ca427bc815d7df3d10823.gif?ex=6a786475&is=6a7712f5&hm=8e2c4e280f4d70dbc933c4d203d987b443b3ded4eb9e664ab8f3dca46c321be2&=")
                await channel.send(f'Welcome {member.mention} to Larp Nation!', embed=WELCOME_EMBED_TEMPLATE)
            else:
                print("None")
        except (Exception, discord.HTTPException) as e:
            print(e)
    @commands.Cog.listener()
    async def on_member_remove(self, member : discord.Member):
       LEAVE_EMBED_TEMPLATE = discord.Embed(
           color=discord.Color.light_grey(),
           title="We lost a Larp!",
           description="We hope you return soon!"
       )
       try:
        channel = self.bot.get_channel(WELCOME_CHANNEL)
        if channel is not None:            
            LEAVE_EMBED_TEMPLATE.set_image(url="https://c.tenor.com/twsA33xeoPUAAAAd/tenor.gif")
            await channel.send(f"{member.name} has left Larp Nation!", embed=LEAVE_EMBED_TEMPLATE)
        pass
       except (Exception, discord.HTTPException) as e:
           print(e)
           pass
           



async def setup(bot: commands.Bot):
    await bot.add_cog(Greetings(bot))