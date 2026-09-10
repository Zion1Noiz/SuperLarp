import discord
from discord import app_commands
from discord.ext import commands

# Falls back if mods/Messaging.py is an older copy without the constant, so a
# partially-updated deploy can't stop this cog loading.
try:
    from mods.Messaging import ERROR_DELETE_AFTER
except ImportError:
    ERROR_DELETE_AFTER = 5

# Fallback only. The prefix shown in examples is read from the running bot (see
# HelpMenu._prefix), so it always reflects what main.py actually configured
# rather than a copy that can drift out of step.
DISPLAY_PREFIX = "."

# How many categories to put on a single page before spilling into the next.
CATEGORIES_PER_PAGE = 5

# Discord rejects an embed whose total content passes 6000 characters, so pages
# are cut well before that.
MAX_PAGE_CHARACTERS = 5000

LOCKED_EMOJI = "🔒"

# Friendlier names for the cog class names that show up as category headings.
CATEGORY_NAMES = {
    "Economy": "🪙 Economy",
    "Strikes": "⚠️ Strikes",
    "Warnings": "📋 Warnings",
    "Confessions": "🤫 Confessions",
    "QOTD": "💬 QOTD",
    "DMTool": "✉️ Direct Messages",
    "General": "⚙️ General",
}


class HelpMenu(commands.Cog):
    """Lists the bot's commands. Reads both the prefix commands and the slash
    command tree, since most cogs register app commands that never appear in
    bot.commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _prefix(self) -> str:
        """The prefix the bot is actually running with, so the examples printed
        here can't disagree with reality. command_prefix may be a string, a
        list, or a callable, so all three shapes are handled."""
        prefix = self.bot.command_prefix

        if callable(prefix):
            # A callable prefix needs a message to resolve; fall back rather
            # than invent one.
            return DISPLAY_PREFIX

        if isinstance(prefix, str):
            return prefix

        # A list/tuple — take the first entry that isn't a bot mention.
        for entry in prefix:
            if isinstance(entry, str) and not entry.startswith("<@"):
                return entry

        return DISPLAY_PREFIX

    # ------------------------------------------------------------------
    # Collection
    # ------------------------------------------------------------------

    @staticmethod
    def _restriction(command) -> discord.Permissions | None:
        """Return the permissions a command is gated behind, or None if anyone
        can run it. Checks the app command first, then falls back to the
        attribute that @app_commands.default_permissions leaves on the
        callback, which is where it lands on a plain prefix command."""
        app_command = getattr(command, "app_command", None) or command
        permissions = getattr(app_command, "default_permissions", None)

        if permissions is not None:
            return permissions

        callback = getattr(command, "callback", None)
        return getattr(callback, "__discord_app_commands_default_permissions__", None)

    @staticmethod
    def _describe(command) -> str:
        """Pull the best available one-liner off either command type."""
        for attribute in ("description", "short_doc", "help"):
            text = getattr(command, attribute, None)
            if text:
                return text.strip().split("\n")[0]

        return "No description."

    @staticmethod
    def _usage(command) -> str:
        """Build a '<name> <args>' usage string for either command type."""
        signature = getattr(command, "signature", None)

        if signature is None:
            # An app command has parameters instead of a rendered signature.
            parameters = getattr(command, "parameters", [])
            signature = " ".join(
                f"<{parameter.name}>" if parameter.required else f"[{parameter.name}]"
                for parameter in parameters
            )

        return f"{command.qualified_name} {signature}".strip()

    def _collect_commands(self) -> dict[str, list[dict]]:
        """Gather every command the bot exposes, grouped by category.

        Hybrid commands appear in both bot.commands and the tree, so the prefix
        side is read first and tree entries with a name already seen are
        skipped — otherwise everything in economy.py would be listed twice."""
        collected: dict[str, dict] = {}

        for command in self.bot.commands:
            if command.hidden:
                continue

            collected[command.qualified_name] = {
                "name": command.qualified_name,
                "description": self._describe(command),
                "usage": self._usage(command),
                "aliases": list(command.aliases),
                "restriction": self._restriction(command),
                "category": command.cog_name or "General",
                "slash_only": False,
            }

        for command in self.bot.tree.walk_commands():
            # Groups are containers; their subcommands come through separately.
            if isinstance(command, app_commands.Group):
                continue

            if command.qualified_name in collected:
                continue

            binding = getattr(command, "binding", None)

            collected[command.qualified_name] = {
                "name": command.qualified_name,
                "description": self._describe(command),
                "usage": self._usage(command),
                "aliases": [],
                "restriction": self._restriction(command),
                "category": getattr(binding, "qualified_name", None) or "General",
                "slash_only": True,
            }

        grouped: dict[str, list[dict]] = {}

        for entry in collected.values():
            grouped.setdefault(entry["category"], []).append(entry)

        for entries in grouped.values():
            entries.sort(key=lambda entry: entry["name"])

        return dict(sorted(grouped.items()))

    @staticmethod
    def _can_see(entry: dict, user: discord.abc.User) -> bool:
        """Whether this person should be shown this command. Anything gated
        behind permissions is hidden from members who don't hold them, so a
        normal member's help is only the commands they can actually run."""
        restriction = entry["restriction"]

        if restriction is None:
            return True

        # Outside a guild there are no permissions to check, so staff commands
        # stay hidden.
        permissions = getattr(user, "guild_permissions", None)

        if permissions is None:
            return False

        if permissions.administrator:
            return True

        return all(
            getattr(permissions, name, False)
            for name, value in restriction
            if value
        )

    def _filter_for(
        self, grouped: dict[str, list[dict]], user: discord.abc.User
    ) -> dict[str, list[dict]]:
        """Strip out everything `user` isn't allowed to run, dropping any
        category that ends up empty."""
        visible: dict[str, list[dict]] = {}

        for category, entries in grouped.items():
            allowed = [entry for entry in entries if self._can_see(entry, user)]

            if allowed:
                visible[category] = allowed

        return visible

    @staticmethod
    def _category_label(category: str) -> str:
        return CATEGORY_NAMES.get(category, f"📁 {category}")

    def _format_line(self, entry: dict) -> str:
        lock = f" {LOCKED_EMOJI}" if entry["restriction"] is not None else ""
        slash = " *(slash only)*" if entry["slash_only"] else ""
        return f"`{self._prefix()}{entry['name']}`{lock}{slash} — {entry['description']}"

    # ------------------------------------------------------------------
    # Page building
    # ------------------------------------------------------------------

    def _build_all_pages(self, grouped: dict[str, list[dict]]) -> list[discord.Embed]:
        """One embed per chunk of categories, split so no page can breach
        Discord's embed size limit."""
        pages: list[discord.Embed] = []
        prefix = self._prefix()
        total = sum(len(entries) for entries in grouped.values())

        # The key is pointless for someone whose list contains nothing locked.
        has_locked = any(
            entry["restriction"] is not None
            for entries in grouped.values()
            for entry in entries
        )
        legend = f"{LOCKED_EMOJI} = staff only · " if has_locked else ""

        def new_embed() -> discord.Embed:
            return discord.Embed(
                title="📖 All Commands",
                description=(
                    f"{total} commands available.\n"
                    f"{legend}"
                    f"Run `{prefix}help <command>` for details."
                ),
                color=discord.Color.blurple(),
            )

        embed = new_embed()
        used = 0

        for category, entries in grouped.items():
            # A single category can outgrow a field, so its lines are chunked.
            chunks: list[list[str]] = [[]]
            length = 0

            for entry in entries:
                line = self._format_line(entry)

                if length + len(line) > 1000:
                    chunks.append([])
                    length = 0

                chunks[-1].append(line)
                length += len(line) + 1

            for index, chunk in enumerate(chunks):
                label = self._category_label(category)

                if index:
                    label = f"{label} (cont.)"

                block = "\n".join(chunk)

                if used >= CATEGORIES_PER_PAGE or len(embed) + len(block) > MAX_PAGE_CHARACTERS:
                    pages.append(embed)
                    embed = new_embed()
                    used = 0

                embed.add_field(name=label, value=block, inline=False)
                used += 1

        if embed.fields:
            pages.append(embed)

        for index, page in enumerate(pages, start=1):
            page.set_footer(text=f"Page {index}/{len(pages)}")

        return pages

    def _build_overview(self, grouped: dict[str, list[dict]]) -> discord.Embed:
        prefix = self._prefix()
        total = sum(len(entries) for entries in grouped.values())

        embed = discord.Embed(
            title="📖 Help",
            description=(
                f"I have **{total}** commands across **{len(grouped)}** categories.\n\n"
                f"`{prefix}help all` — list every command\n"
                f"`{prefix}help <command>` — details for one command\n\n"
                f"Text commands use `{prefix}` — slash commands work too."
            ),
            color=discord.Color.blurple(),
        )

        for category, entries in grouped.items():
            names = ", ".join(f"`{entry['name']}`" for entry in entries)
            embed.add_field(
                name=f"{self._category_label(category)} ({len(entries)})",
                value=names[:1024],
                inline=False,
            )

        return embed

    def _build_detail(self, entry: dict) -> discord.Embed:
        embed = discord.Embed(
            title=f"`{self._prefix()}{entry['name']}`",
            description=entry["description"],
            color=discord.Color.blurple(),
        )

        embed.add_field(
            name="Usage",
            value=f"`{self._prefix()}{entry['usage']}`",
            inline=False,
        )

        if entry["aliases"]:
            embed.add_field(
                name="Aliases",
                value=", ".join(f"`{self._prefix()}{alias}`" for alias in entry["aliases"]),
                inline=False,
            )

        if entry["restriction"] is not None:
            allowed = [
                name.replace("_", " ").title()
                for name, value in entry["restriction"] if value
            ]
            embed.add_field(
                name=f"{LOCKED_EMOJI} Requires",
                value=", ".join(allowed) or "Staff permissions",
                inline=False,
            )

        if entry["slash_only"]:
            embed.set_footer(text=f"This one is slash-only — it won't work with {self._prefix()}")

        return embed

    # ------------------------------------------------------------------
    # Command
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="help",
        description="Show the bot's commands. Use 'all' to list every one.",
    )
    @app_commands.describe(
        query="Type 'all' for the full list, or a command name for details."
    )
    async def help_command(self, ctx: commands.Context, *, query: str = None):
        # Filtering here covers all three paths at once: the overview, the full
        # list, and the per-command lookup below.
        grouped = self._filter_for(self._collect_commands(), ctx.author)

        if query is None:
            await ctx.send(embed=self._build_overview(grouped))
            return

        wanted = query.strip().lower()

        if wanted in ("all", "everything", "list", "*"):
            pages = self._build_all_pages(grouped)

            if not pages:
                await ctx.send("I don't have any commands registered.", ephemeral=True)
                return

            if len(pages) == 1:
                await ctx.send(embed=pages[0])
                return

            view = HelpPageView(pages, ctx.author.id)
            view.message = await ctx.send(embed=pages[0], view=view)
            return

        # Otherwise treat it as a command name, matching aliases too.
        for entries in grouped.values():
            for entry in entries:
                if wanted == entry["name"].lower() or wanted in [
                    alias.lower() for alias in entry["aliases"]
                ]:
                    await ctx.send(embed=self._build_detail(entry))
                    return

        await ctx.send(
            f"❌ I don't have a command called `{query}`. "
            f"Try `{self._prefix()}help all` to see what I do have.",
            ephemeral=True,
            delete_after=ERROR_DELETE_AFTER,
        )


class HelpPageView(discord.ui.View):
    """Pagination controls for the full command list."""

    def __init__(self, pages: list[discord.Embed], author_id: int):
        super().__init__(timeout=180)
        self.pages = pages
        self.index = 0
        self.author_id = author_id
        self.message: discord.Message | None = None
        self._update_buttons()

    def _update_buttons(self):
        self.previous_button.disabled = self.index == 0
        self.next_button.disabled = self.index >= len(self.pages) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran this command can page through it.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = max(0, self.index - 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = min(len(self.pages) - 1, self.index + 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    # discord.py registers its own plain-text "help" command at startup, which
    # would collide with this one on load.
    bot.help_command = None
    await bot.add_cog(HelpMenu(bot))
