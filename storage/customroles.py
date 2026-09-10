import asyncio
import io
import os
import time
import uuid
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

# Pillow lets the bot convert and shrink whatever image someone links into
# something Discord will accept. It's optional: without it the bot still works,
# it just requires people to supply an already-valid icon.
try:
    from PIL import Image

    PILLOW_AVAILABLE = True
except ImportError:
    PILLOW_AVAILABLE = False

from mods import FileHandling, Logging
# Falls back if mods/Messaging.py is an older copy without the constant, so a
# partially-updated deploy can't stop this cog loading.
try:
    from mods.Messaging import ERROR_DELETE_AFTER
except ImportError:
    ERROR_DELETE_AFTER = 5

INFO_FOLDER = "./info"

# Approved roles, keyed by user ID. This is what makes a role stick "forever" —
# it survives restarts, and the member gets their role back if they leave and
# rejoin (see on_member_join).
CUSTOM_ROLES_FILE = os.path.join(INFO_FOLDER, "custom-roles.json")

# Requests waiting on a moderator.
CUSTOM_ROLE_REQUESTS_FILE = os.path.join(INFO_FOLDER, "custom-role-requests.json")

# Channel the review messages get posted to. Falls back to the normal logs
# channel when this hasn't been set with /customrole-channel.
CUSTOM_ROLE_CHANNEL_FILE = os.path.join(INFO_FOLDER, "custom-role-channel.txt")
LOGS_CHANNEL_FILE = os.path.join(INFO_FOLDER, "logs-channel.txt")

# The shop item that unlocks this. Must match an "id" in info/shop.json.
UNLOCK_ITEM_ID = "custom_role_icon"

MAX_ROLE_NAME_LENGTH = 100

# How long the "Design my role" button hangs around when the command was typed
# rather than run as a slash command. Discord only allows ephemeral ("Only you
# can see this") replies to interactions, and a typed message isn't one — so
# the next best thing is a message that clears itself away quickly.
BUTTON_VISIBLE_SECONDS = 60

# Discord's ceiling for a role icon.
MAX_ICON_BYTES = 256 * 1024

# How much we're willing to download before converting. Anything within this is
# shrunk to fit MAX_ICON_BYTES; beyond it we don't bother trying.
MAX_SOURCE_BYTES = 8 * 1024 * 1024

# What Discord itself accepts, used when Pillow isn't installed to convert.
ALLOWED_ICON_TYPES = ("image/png", "image/jpeg", "image/webp")
ALLOWED_ICON_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")

# Extra formats accepted when Pillow is available, since they get converted.
CONVERTIBLE_EXTENSIONS = ALLOWED_ICON_EXTENSIONS + (
    ".gif", ".bmp", ".tiff", ".tif", ".ico", ".apng", ".jfif", ".avif",
)

# Sizes tried in order when squeezing an image under the byte limit. A role icon
# renders tiny next to a name, so dropping resolution is invisible in practice.
ICON_SIZE_LADDER = (256, 192, 128, 96, 64, 48)

# Role icons are a Boost Level 2 perk; without it Discord rejects display_icon.
ROLE_ICON_GUILD_FEATURE = "ROLE_ICONS"

# Colour words accepted in the modal alongside hex codes, so nobody has to go
# looking up "what number is purple".
NAMED_COLOURS = {
    "red": 0xE74C3C,
    "crimson": 0xDC143C,
    "pink": 0xE91E63,
    "orange": 0xE67E22,
    "yellow": 0xF1C40F,
    "gold": 0xFFD700,
    "green": 0x2ECC71,
    "lime": 0x32CD32,
    "teal": 0x1ABC9C,
    "cyan": 0x00FFFF,
    "blue": 0x3498DB,
    "navy": 0x000080,
    "purple": 0x9B59B6,
    "magenta": 0xFF00FF,
    "white": 0xFFFFFF,
    "black": 0x010101,
    "grey": 0x95A5A6,
    "gray": 0x95A5A6,
}


def _read_json(path: str, default):
    """Reads a JSON file, returning `default` (and its type) if the file is
    missing, empty, or invalid."""
    data = FileHandling.ReadFromJSONFile(path)
    return data if isinstance(data, type(default)) else default


def _write_json(path: str, data) -> None:
    FileHandling.WriteToJSON(path, data)


def parse_colour(raw: str) -> discord.Colour | None:
    """Accepts '#ff0000', 'ff0000', 'f00', or a name like 'purple'.
    Returns None if it can't be read as a colour."""
    text = raw.strip().lower().lstrip("#")

    if text in NAMED_COLOURS:
        return discord.Colour(NAMED_COLOURS[text])

    # Shorthand hex: 'f00' means 'ff0000'.
    if len(text) == 3 and all(char in "0123456789abcdef" for char in text):
        text = "".join(char * 2 for char in text)

    if len(text) == 6 and all(char in "0123456789abcdef" for char in text):
        value = int(text, 16)

        # Discord renders pure black as "no colour", so nudge it.
        return discord.Colour(value or 0x010101)

    return None


def looks_like_image_url(raw: str) -> bool:
    """Cheap sanity check before a request is filed. The real validation
    (size, content type) happens at download time on approval."""
    try:
        parsed = urlparse(raw.strip())
    except ValueError:
        return False

    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False

    # With Pillow present anything it can open is fair game, since it gets
    # converted; without it, only what Discord takes directly.
    accepted = CONVERTIBLE_EXTENSIONS if PILLOW_AVAILABLE else ALLOWED_ICON_EXTENSIONS

    return parsed.path.lower().endswith(accepted)


def convert_icon(data: bytes) -> tuple[bytes | None, str | None]:
    """Turn any image Pillow can read into a PNG that fits Discord's role icon
    limits. Returns (bytes, None) or (None, reason).

    This is CPU-bound and blocking — call it through asyncio.to_thread so it
    doesn't stall the bot."""
    try:
        image = Image.open(io.BytesIO(data))

        # An animated GIF/APNG has many frames; role icons are static, so take
        # the first one.
        if getattr(image, "is_animated", False):
            image.seek(0)

        # RGBA keeps transparency, which most role icons rely on.
        image = image.convert("RGBA")
    except Exception as e:
        return None, f"I couldn't read that image ({type(e).__name__})"

    for size in ICON_SIZE_LADDER:
        resized = image.copy()
        # thumbnail preserves aspect ratio and never scales up.
        resized.thumbnail((size, size), Image.LANCZOS)

        buffer = io.BytesIO()

        try:
            resized.save(buffer, format="PNG", optimize=True)
        except Exception as e:
            return None, f"I couldn't convert that image ({type(e).__name__})"

        if buffer.tell() <= MAX_ICON_BYTES:
            return buffer.getvalue(), None

    # Even at the smallest size PNG was too big — WebP compresses far harder.
    try:
        smallest = image.copy()
        smallest.thumbnail((ICON_SIZE_LADDER[-1], ICON_SIZE_LADDER[-1]), Image.LANCZOS)
        buffer = io.BytesIO()
        smallest.save(buffer, format="WEBP", quality=80, method=6)

        if buffer.tell() <= MAX_ICON_BYTES:
            return buffer.getvalue(), None
    except Exception:
        pass

    return None, "that image can't be squeezed under Discord's size limit"


async def download_icon(url: str) -> tuple[bytes | None, str | None]:
    """Fetches a role icon. Returns (bytes, None) on success or
    (None, reason) on failure, so the reason can be shown to the moderator."""
    # Without Pillow the image has to already be something Discord takes.
    cap = MAX_SOURCE_BYTES if PILLOW_AVAILABLE else MAX_ICON_BYTES

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status != 200:
                    return None, f"the image URL returned HTTP {response.status}"

                content_type = (response.content_type or "").lower()

                if not PILLOW_AVAILABLE and content_type not in ALLOWED_ICON_TYPES:
                    return None, f"that isn't a PNG, JPEG, or WebP (got `{content_type}`)"

                if PILLOW_AVAILABLE and not content_type.startswith("image/"):
                    return None, f"that link isn't an image (it's `{content_type}`)"

                # Read one byte past the cap so an oversized file is detectable
                # even when the server lies about Content-Length.
                data = await response.content.read(cap + 1)

                if len(data) > cap:
                    return None, (
                        f"the image is over {cap // (1024 * 1024)}MB, which is too big "
                        "to work with"
                        if PILLOW_AVAILABLE
                        else f"the image is over Discord's {MAX_ICON_BYTES // 1024}KB limit"
                    )

                if not data:
                    return None, "the image was empty"

    except aiohttp.ClientError as e:
        return None, f"the image couldn't be downloaded ({e})"
    except Exception as e:
        return None, f"something went wrong fetching the image ({e})"

    if not PILLOW_AVAILABLE:
        return data, None

    # Conversion is CPU-bound, so it runs off the event loop.
    return await asyncio.to_thread(convert_icon, data)


class CustomRoles(commands.Cog):
    """Lets members who bought the custom role item design their own role.
    Every request goes to moderators for approval before the role is created."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._ensure_files_exist()

    @staticmethod
    def _ensure_files_exist():
        os.makedirs(INFO_FOLDER, exist_ok=True)

        if not os.path.exists(CUSTOM_ROLES_FILE) or os.path.getsize(CUSTOM_ROLES_FILE) == 0:
            _write_json(CUSTOM_ROLES_FILE, {})

        if (not os.path.exists(CUSTOM_ROLE_REQUESTS_FILE)
                or os.path.getsize(CUSTOM_ROLE_REQUESTS_FILE) == 0):
            _write_json(CUSTOM_ROLE_REQUESTS_FILE, [])

    async def cog_load(self):
        """Re-register the Approve/Deny buttons for anything still pending, so
        they keep working across a restart."""
        for request in _read_json(CUSTOM_ROLE_REQUESTS_FILE, []):
            self.bot.add_view(CustomRoleReviewView(self, request["id"]))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _review_channel_id() -> int | None:
        for path in (CUSTOM_ROLE_CHANNEL_FILE, LOGS_CHANNEL_FILE):
            if not os.path.isfile(path) or os.path.getsize(path) == 0:
                continue

            raw = FileHandling.ReadFromTextFile(path)

            if raw and raw.isdigit():
                return int(raw)

        return None

    def _has_access(self, user_id: int) -> bool:
        """Whether this member has bought the shop item that unlocks the
        designer."""
        economy = self.bot.get_cog("Economy")

        if economy is None:
            # Without the economy cog there's nothing to check a purchase
            # against, so fail closed rather than hand out free roles.
            return False

        return economy.owns_item(user_id, UNLOCK_ITEM_ID)

    @staticmethod
    def _existing_role(user_id: int) -> dict | None:
        return _read_json(CUSTOM_ROLES_FILE, {}).get(str(user_id))

    @staticmethod
    def _pending_request(user_id: int) -> dict | None:
        return next(
            (
                request
                for request in _read_json(CUSTOM_ROLE_REQUESTS_FILE, [])
                if request.get("user_id") == user_id
            ),
            None,
        )

    @staticmethod
    def unlocks_designer(item_id: str) -> bool:
        """Whether buying this shop item should open the role designer. The
        shop calls this straight after a purchase."""
        return item_id == UNLOCK_ITEM_ID

    def modal_for(
        self, user: discord.abc.User, guild: discord.Guild
    ) -> tuple["CustomRoleModal | None", str | None]:
        """Returns (modal, reason_it_cant_open). Everything that has to be true
        before someone designs a role is checked in one place, so the command
        and the shop can't drift apart."""
        if not self._has_access(user.id):
            return None, (
                "You need to buy **Custom Role** from the shop first. "
                "Run `/shop` to have a look."
            )

        problem = self._permission_problem(guild)

        if problem is not None:
            return None, f"{problem} Please tell staff."

        if self._review_channel_id() is None:
            return None, (
                "No review channel is set up, so I can't send this to the mods. "
                "An admin needs to run `/customrole-channel` first."
            )

        if self._pending_request(user.id) is not None:
            return None, (
                "You already have a request waiting on a moderator. "
                "Hang tight until it's reviewed."
            )

        # Only offer the icon field where Discord will actually accept one,
        # rather than letting someone fill it in and get rejected on submit.
        allow_icon = ROLE_ICON_GUILD_FEATURE in guild.features

        modal = CustomRoleModal(
            self, self._existing_role(user.id), allow_icon=allow_icon
        )

        return modal, None

    def _permission_problem(self, guild: discord.Guild) -> str | None:
        """Explains why the bot couldn't create or assign a role, or None if
        it can. Checked before taking a request so nobody waits on approval
        for something that was never going to work."""
        me = guild.me

        if me is None:
            return "I can't see my own member object in this server."

        if not me.guild_permissions.manage_roles:
            return "I don't have the **Manage Roles** permission."

        # A bot can only manage roles below its own highest one, and new roles
        # are created at the bottom, so this only fails if the bot's top role
        # is itself at the bottom.
        if me.top_role.position <= 1:
            return (
                "My highest role is at the very bottom of the list, so I can't "
                "create roles underneath it. Move my role up in Server Settings."
            )

        return None

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="customrole",
        description="Design your own role. Requires the shop item and mod approval.",
    )
    @commands.guild_only()
    async def customrole(self, ctx: commands.Context):
        modal, problem = self.modal_for(ctx.author, ctx.guild)

        if problem is not None:
            await ctx.send(f"❌ {problem}", ephemeral=True, delete_after=ERROR_DELETE_AFTER)
            return

        if ctx.interaction is not None:
            await ctx.interaction.response.send_modal(modal)
            return

        # A prefix invocation has no interaction to attach a modal to, so offer
        # a button that does. It's locked to the person who ran the command and
        # removes itself as soon as it's used.
        view = OpenModalView(self, ctx.author)
        view.message = await ctx.send(
            f"{ctx.author.mention} — press the button to design your role. "
            "Only you can use it, and it'll disappear shortly.",
            view=view,
            delete_after=BUTTON_VISIBLE_SECONDS,
        )

    @commands.hybrid_command(
        name="customrole-channel",
        description="Set the channel custom role requests get sent to for review.",
    )
    @app_commands.describe(channel="Where moderators should receive the requests.")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def customrole_channel(
        self, ctx: commands.Context, channel: discord.TextChannel
    ):
        FileHandling.WriteToTextFile(CUSTOM_ROLE_CHANNEL_FILE, channel.id)
        await ctx.send(f"✅ Custom role requests will now go to {channel.mention}.")

    @commands.hybrid_command(
        name="customrole-remove",
        description="Delete a member's custom role.",
    )
    @app_commands.describe(member="Whose custom role to remove.")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    @commands.guild_only()
    async def customrole_remove(self, ctx: commands.Context, member: discord.Member):
        roles = _read_json(CUSTOM_ROLES_FILE, {})
        record = roles.pop(str(member.id), None)

        if record is None:
            await ctx.send(f"❌ {member.mention} doesn't have a custom role.", ephemeral=True, delete_after=ERROR_DELETE_AFTER)
            return

        _write_json(CUSTOM_ROLES_FILE, roles)

        role = ctx.guild.get_role(int(record["role_id"]))

        if role is not None:
            try:
                await role.delete(reason=f"Custom role removed by {ctx.author}")
            except discord.HTTPException as e:
                await ctx.send(
                    f"⚠️ Removed the record, but couldn't delete the role itself: {e}"
                )
                return

        await ctx.send(f"✅ Removed {member.mention}'s custom role.")

        await Logging.log_message(
            "🎨 Custom role removed",
            f"{ctx.author} deleted {member}'s custom role **{record.get('name')}**.",
        )

    # ------------------------------------------------------------------
    # Listeners
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Give a returning member their custom role back — this is the
        "saves forever" part."""
        record = self._existing_role(member.id)

        if record is None:
            return

        role = member.guild.get_role(int(record["role_id"]))

        if role is None:
            return

        try:
            await member.add_roles(role, reason="Restoring custom role on rejoin")
        except discord.HTTPException as e:
            print(f"[customroles] Couldn't restore {member}'s role: {e}")


class OpenModalView(discord.ui.View):
    """Bridges a prefix invocation to the modal, which needs an interaction."""

    def __init__(self, cog: CustomRoles, author: discord.abc.User):
        super().__init__(timeout=BUTTON_VISIBLE_SECONDS)
        self.cog = cog
        self.author = author
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "That isn't your button — run the command yourself.",
                ephemeral=True,
                delete_after=ERROR_DELETE_AFTER,
            )
            return False
        return True

    async def _clear(self):
        """Take the button message away once it's served its purpose."""
        self.stop()

        if self.message is None:
            return

        try:
            await self.message.delete()
        except discord.HTTPException:
            # Already gone (delete_after beat us to it), or no permission.
            pass

    async def on_timeout(self):
        await self._clear()

    @discord.ui.button(label="Design my role", emoji="🎨", style=discord.ButtonStyle.primary)
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Re-checked at click time rather than reused from the command, in case
        # something changed while the button sat there.
        modal, problem = self.cog.modal_for(interaction.user, interaction.guild)

        if problem is not None:
            await interaction.response.send_message(f"❌ {problem}", ephemeral=True, delete_after=ERROR_DELETE_AFTER)
            await self._clear()
            return

        await interaction.response.send_modal(modal)
        await self._clear()


class CustomRoleModal(discord.ui.Modal):
    """Collects the name and colour, then files the request for review."""

    def __init__(self, cog: CustomRoles, existing: dict | None = None,
                 allow_icon: bool = False):
        super().__init__(
            title="Edit your role" if existing else "Design your role",
            timeout=600,
        )
        self.cog = cog
        self.existing = existing
        self.allow_icon = allow_icon

        self.role_name = discord.ui.TextInput(
            label="Role name",
            placeholder="What should it be called?",
            default=existing.get("name") if existing else None,
            max_length=MAX_ROLE_NAME_LENGTH,
            required=True,
        )
        self.colour = discord.ui.TextInput(
            label="Colour",
            placeholder="#9B59B6, or a name like purple",
            default=existing.get("colour") if existing else None,
            max_length=32,
            required=True,
        )

        self.add_item(self.role_name)
        self.add_item(self.colour)

        # Only the icon tier gets this field; everyone else never sees it.
        self.icon_url = None

        if allow_icon:
            self.icon_url = discord.ui.TextInput(
                label="Role icon URL (optional)",
                placeholder="Direct link to an image - I will resize it to fit",
                default=existing.get("icon_url") if existing else None,
                max_length=500,
                required=False,
            )
            self.add_item(self.icon_url)

    async def on_submit(self, interaction: discord.Interaction):
        name = self.role_name.value.strip()
        colour = parse_colour(self.colour.value)

        if colour is None:
            await interaction.response.send_message(
                f"❌ `{self.colour.value}` isn't a colour I recognise. Use a hex code "
                f"like `#9B59B6`, or a name like `purple`.",
                ephemeral=True,
                delete_after=ERROR_DELETE_AFTER,
            )
            return

        if not name:
            await interaction.response.send_message(
                "❌ The role needs a name.", ephemeral=True, delete_after=ERROR_DELETE_AFTER
            )
            return

        # Discord silently mangles these, and they'd be confusing in a role list.
        if "@" in name or "`" in name:
            await interaction.response.send_message(
                "❌ Role names can't contain `@` or backticks.", ephemeral=True, delete_after=ERROR_DELETE_AFTER
            )
            return

        icon_url = self.icon_url.value.strip() if self.icon_url is not None else ""

        if icon_url and not looks_like_image_url(icon_url):
            await interaction.response.send_message(
                "❌ That icon link doesn't look like a direct image URL. It needs to "
                "start with `http`/`https` and end in `.png`, `.jpg`, or `.webp` — "
                "right-click the image and pick **Copy Image Address**.",
                ephemeral=True,
                delete_after=ERROR_DELETE_AFTER,
            )
            return

        if icon_url and ROLE_ICON_GUILD_FEATURE not in interaction.guild.features:
            await interaction.response.send_message(
                "❌ This server needs **Boost Level 2** before roles can have icons. "
                "You can still request the role without one.",
                ephemeral=True,
                delete_after=ERROR_DELETE_AFTER,
            )
            return

        channel_id = self.cog._review_channel_id()
        channel = self.cog.bot.get_channel(channel_id) if channel_id else None

        if channel is None:
            await interaction.response.send_message(
                "❌ I couldn't find the review channel. Please tell an admin.",
                ephemeral=True,
                delete_after=ERROR_DELETE_AFTER,
            )
            return

        request = {
            "id": uuid.uuid4().hex[:12],
            "user_id": interaction.user.id,
            "user_display": str(interaction.user),
            "guild_id": interaction.guild.id,
            "name": name,
            "colour": f"#{colour.value:06X}",
            "icon_url": icon_url or None,
            "is_edit": self.existing is not None,
            "requested_at": time.time(),
        }

        requests = _read_json(CUSTOM_ROLE_REQUESTS_FILE, [])
        requests.append(request)
        _write_json(CUSTOM_ROLE_REQUESTS_FILE, requests)

        embed = discord.Embed(
            title="🎨 Custom role request" + (" (edit)" if request["is_edit"] else ""),
            description=(
                f"**Requested by:** {interaction.user.mention} "
                f"(`{interaction.user.id}`)\n"
                f"**Role name:** {name}\n"
                f"**Colour:** {request['colour']}"
            ),
            color=colour,
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)

        # Show moderators the proposed icon so they can judge it before approving.
        if icon_url:
            embed.add_field(name="Icon", value=icon_url, inline=False)
            embed.set_image(url=icon_url)

        if self.existing is not None:
            embed.add_field(
                name="Currently",
                value=f"{self.existing.get('name')} — {self.existing.get('colour')}",
                inline=False,
            )

        await channel.send(embed=embed, view=CustomRoleReviewView(self.cog, request["id"]))

        await interaction.response.send_message(
            "✅ Sent to the moderators for approval. You'll get a DM when it's "
            "been reviewed.",
            ephemeral=True,
        )


class CustomRoleReviewView(discord.ui.View):
    """Attached to a request in the review channel. Lets moderators approve or
    deny it. Persistent, so the buttons survive a restart."""

    def __init__(self, cog: CustomRoles, request_id: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.request_id = request_id

        approve = discord.ui.Button(
            label="Approve",
            style=discord.ButtonStyle.success,
            custom_id=f"customrole_approve:{request_id}",
        )
        approve.callback = self.approve_callback
        self.add_item(approve)

        deny = discord.ui.Button(
            label="Deny",
            style=discord.ButtonStyle.danger,
            custom_id=f"customrole_deny:{request_id}",
        )
        deny.callback = self.deny_callback
        self.add_item(deny)

    @staticmethod
    def _is_moderator(interaction: discord.Interaction) -> bool:
        perms = interaction.user.guild_permissions if interaction.guild else None
        return bool(perms and (perms.administrator or perms.manage_guild))

    @staticmethod
    async def _notify(user: discord.User | None, message: str):
        if user is None:
            return

        try:
            await user.send(message)
        except discord.HTTPException:
            # DMs closed — not worth failing the approval over.
            pass

    async def _resolve(self, interaction: discord.Interaction, approved: bool):
        if not self._is_moderator(interaction):
            await interaction.response.send_message(
                "You don't have permission to review custom role requests.",
                ephemeral=True,
            )
            return

        requests = _read_json(CUSTOM_ROLE_REQUESTS_FILE, [])
        request = next((r for r in requests if r.get("id") == self.request_id), None)

        if request is None:
            for item in self.children:
                item.disabled = True
            self.stop()
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(
                "This request has already been reviewed.", ephemeral=True
            )
            return

        _write_json(
            CUSTOM_ROLE_REQUESTS_FILE,
            [r for r in requests if r.get("id") != self.request_id],
        )

        member = interaction.guild.get_member(request["user_id"])
        colour = parse_colour(request["colour"]) or discord.Colour.default()
        note = ""

        if approved:
            roles = _read_json(CUSTOM_ROLES_FILE, {})
            existing = roles.get(str(request["user_id"]))
            role = None

            if existing:
                role = interaction.guild.get_role(int(existing["role_id"]))

            # Fetch the icon before touching the role, so a bad image doesn't
            # leave a half-applied role behind. A failure here is reported but
            # doesn't block the rest of the approval.
            icon_bytes = None
            icon_note = ""
            requested_icon = request.get("icon_url")

            if requested_icon:
                if ROLE_ICON_GUILD_FEATURE not in interaction.guild.features:
                    icon_note = (
                        "\n⚠️ Icon skipped — the server isn't Boost Level 2."
                    )
                else:
                    icon_bytes, reason = await download_icon(requested_icon)

                    if icon_bytes is None:
                        icon_note = f"\n⚠️ Icon skipped — {reason}."

            try:
                if role is not None:
                    # An edit — rename and recolour the role they already have.
                    edits = {
                        "name": request["name"],
                        "colour": colour,
                        "reason": f"Custom role edit approved by {interaction.user}",
                    }

                    if icon_bytes is not None:
                        edits["display_icon"] = icon_bytes

                    await role.edit(**edits)
                else:
                    creation = {
                        "name": request["name"],
                        "colour": colour,
                        "reason": f"Custom role approved by {interaction.user}",
                    }

                    if icon_bytes is not None:
                        creation["display_icon"] = icon_bytes

                    role = await interaction.guild.create_role(**creation)

                if member is not None and role not in member.roles:
                    await member.add_roles(role, reason="Custom role approved")

            except discord.Forbidden:
                await interaction.response.send_message(
                    "❌ I don't have permission to create or assign that role. "
                    "The request has been put back — fix my permissions and try again.",
                    ephemeral=True,
                    delete_after=ERROR_DELETE_AFTER,
                )
                # `requests` is the list as it was read, still containing this
                # request — writing it back restores the queue exactly. Appending
                # here would file the request a second time.
                _write_json(CUSTOM_ROLE_REQUESTS_FILE, requests)
                return

            except discord.HTTPException as e:
                await interaction.response.send_message(
                    f"❌ Discord rejected that: {e}", ephemeral=True, delete_after=ERROR_DELETE_AFTER
                )
                # `requests` is the list as it was read, still containing this
                # request — writing it back restores the queue exactly. Appending
                # here would file the request a second time.
                _write_json(CUSTOM_ROLE_REQUESTS_FILE, requests)
                return

            roles[str(request["user_id"])] = {
                "role_id": role.id,
                "name": request["name"],
                "colour": request["colour"],
                # Only remember the icon if it actually applied, so an edit
                # doesn't keep re-offering a URL that never worked.
                "icon_url": requested_icon if icon_bytes is not None else None,
                "approved_by": f"{interaction.user} ({interaction.user.id})",
                "approved_at": time.time(),
            }
            _write_json(CUSTOM_ROLES_FILE, roles)

            note = f"created as {role.mention}{icon_note}"

            await self._notify(
                member,
                f"Your custom role **{request['name']}** was approved and is "
                f"now yours.{icon_note}",
            )
        else:
            await self._notify(
                member,
                f"Your custom role request (**{request['name']}**) was denied. "
                f"You still own the shop item, so you can submit a different "
                f"name or colour with `/customrole`.",
            )

        for item in self.children:
            item.disabled = True
        self.stop()

        embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
        embed.color = discord.Color.green() if approved else discord.Color.red()

        # If the icon couldn't be applied, say so on the review message itself so
        # the moderator sees it rather than only the requester.
        if approved and icon_note:
            embed.add_field(name="Note", value=icon_note.strip(), inline=False)

        embed.set_footer(
            text=(
                f"✅ Approved by {interaction.user.display_name}"
                if approved
                else f"❌ Denied by {interaction.user.display_name}"
            )
        )

        await interaction.response.edit_message(embed=embed, view=self)

        await Logging.log_message(
            "🎨 Custom role " + ("approved" if approved else "denied"),
            f"{interaction.user} {'approved' if approved else 'denied'} "
            f"**{request['name']}** ({request['colour']}) for "
            f"{request['user_display']}. {note}",
        )

    async def approve_callback(self, interaction: discord.Interaction):
        await self._resolve(interaction, approved=True)

    async def deny_callback(self, interaction: discord.Interaction):
        await self._resolve(interaction, approved=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(CustomRoles(bot))
