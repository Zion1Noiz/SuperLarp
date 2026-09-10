import discord
from discord import app_commands
from discord.ext import commands
import dotenv
import os

dotenv.load_dotenv()

# RoleID Envs #

ALPHA_MOD_ROLE_ID = int(os.getenv("ALPHA_ROLE_ID"))
BETA_MOD_ROLE_ID = int(os.getenv("BETA_ROLE_ID"))
OWNERS_ROLE_ID = int(os.getenv("OWNER_ROLE_ID"))

#    CheckIfUserHasRole( user : discord.Member object, target_role : discord.Role)
#  ---------------------
# This function checks to see if the member has the "target_role"
# If they do have the role, it returns "True"; if they don't have the role, it returns false.

#    IsMemberAnAdmin(user : discord.Member object)
#  ---------------------
# This function checks the member's roles for the "owners" roles from "ALPHA_MOD_ROLE_ID", "BETA_MOD_ROLE_ID", and "OWNERS_ROLE_ID"
# If they do have the role, it returns "True"; if they don't have the role, it returns false.

#    IsMemberAnOwner(user : discord.Member object)
#  ---------------------
# This function checks the member's roles for the "owners" roles from "OWNERS_ROLE_ID"
# If they do have the role, it returns "True"; if they don't have the role, it returns false.



def CheckIfUserHasRole(user : discord.Member, target_role : discord.Role | int):
    # Member sanity check #
    if user:

        # Check if they have the role mention in the "target" #
        # If the role is found, return True, if it isn't found, return false. #
        target_role = target_role.id if type(target_role) == discord.Role else target_role

        if user.get_role(target_role):
            return True
        else:
            return False
        
    else:
        return "User not found."

def IsMemberAnAdmin(user : discord.Member):
    # User sanity check
    if user:
        # Check if they have any of the roles that have administrative privileges #
        # If the role is found, return True, if it isn't found, return false. #
        if user.get_role(ALPHA_MOD_ROLE_ID) or user.get_role(BETA_MOD_ROLE_ID) or user.get_role(OWNERS_ROLE_ID):
            return True
        else:
            return False
    else:
        # Couldn't find the user
        return "User not found"

def IsMemberAnOwner(user : discord.Member):
    # User sanity check
    # Check if they have any of owner roles.#
    # If the role is found, return True, if it isn't found, return false. #
    if user:
        # Check if they have any of the owner roles. #
        # If the role is found, return True, if it isn't found, return false. #
        if user.get_role(OWNERS_ROLE_ID):
            return True
        else:
            return False
    else:
        return "User not found"

