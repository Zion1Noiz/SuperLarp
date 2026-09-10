import discord
import os
import dotenv
import Logging



async def ChangeChannelPermissionsForUsers(
    user_list: list[discord.Member],
    channel: discord.TextChannel,
    overwrite: discord.PermissionOverwrite,
):
    for user in user_list:
        current = channel.overwrites_for(user)

        for name, value in overwrite:
            if value is not None:
                setattr(current, name, value)

        await channel.set_permissions(user, overwrite=current)