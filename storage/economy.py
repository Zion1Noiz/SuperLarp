import asyncio
import json
import os
import random
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from mods import Logging
# Falls back if mods/Messaging.py is an older copy without the constant, so a
# partially-updated deploy can't stop this cog loading.
try:
    from mods.Messaging import ERROR_DELETE_AFTER
except ImportError:
    ERROR_DELETE_AFTER = 5

INFO_FOLDER = "./info"
ECONOMY_FILE = os.path.join(INFO_FOLDER, "economy.json")
SHOP_FILE = os.path.join(INFO_FOLDER, "shop.json")

CURRENCY_EMOJI = "🪙"
CURRENCY_NAME = "coins"

# Chance the gambler WINS. A fair coin would be 0.50; this is tilted so losing
# is the more likely outcome. Because a win pays double the stake (net +stake)
# and a loss costs the stake, a 0.45 win chance works out to a 10% house edge:
#   EV = (0.45 * +bet) + (0.55 * -bet) = -0.10 * bet
# Change this one number to retune the odds.
WIN_CHANCE = 0.45

MIN_BET = 100
MAX_BET = 2_500

# Balance handed to a member the first time the economy sees them.
STARTING_BALANCE = 1_000

# Seconds between gamble uses, per user.
GAMBLE_COOLDOWN = 5

# Not seedable and not predictable from previous rolls, unlike the shared
# random module state.
_rng = random.SystemRandom()

DEFAULT_SHOP_ITEMS = [
    {
        "id": "vip",
        "name": "VIP Role",
        "description": "A shiny colour and bragging rights.",
        "emoji": "⭐",
        "price": 25000,
        "role_id": None,
    },
    {
        "id": "custom_vc",
        "name": "Private VC Token",
        "description": "Redeem with staff for your own private voice channel.",
        "emoji": "🔊",
        "price": 15000,
        "role_id": None,
    },
    {
        "id": "qotd_pick",
        "name": "QOTD Pick",
        "description": "Your submitted question gets picked next.",
        "emoji": "💬",
        "price": 7500,
        "role_id": None,
    },
    {
        "id": "colour_change",
        "name": "Name Colour Change",
        "description": "Pick a custom name colour for a week.",
        "emoji": "🎨",
        "price": 5000,
        "role_id": None,
    },
]


def format_coins(amount: int) -> str:
    """1234567 -> '🪙 **1,234,567** coins'"""
    return f"{CURRENCY_EMOJI} **{amount:,}** {CURRENCY_NAME}"


class Economy(commands.Cog):
    """Currency, gambling, and the shop."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Every read-modify-write of economy.json goes through this so two
        # near-simultaneous commands (or a double-clicked Confirm button)
        # can't both read the same balance and each spend it.
        self._lock = asyncio.Lock()
        # Separate from _lock so shop edits and balance changes don't block
        # each other; they touch different files.
        self._shop_lock = asyncio.Lock()
        self._ensure_files_exist()

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    @staticmethod
    def _ensure_files_exist():
        os.makedirs(INFO_FOLDER, exist_ok=True)

        if not os.path.exists(ECONOMY_FILE) or os.path.getsize(ECONOMY_FILE) == 0:
            with open(ECONOMY_FILE, "w", encoding="utf-8") as file:
                json.dump({}, file, indent=4)

        if not os.path.exists(SHOP_FILE) or os.path.getsize(SHOP_FILE) == 0:
            with open(SHOP_FILE, "w", encoding="utf-8") as file:
                json.dump({"items": DEFAULT_SHOP_ITEMS}, file, indent=4)

    @staticmethod
    def _read_economy() -> dict:
        try:
            with open(ECONOMY_FILE, "r", encoding="utf-8") as file:
                return json.load(file)
        except (json.JSONDecodeError, FileNotFoundError):
            return {}

    @staticmethod
    def _write_economy(data: dict):
        with open(ECONOMY_FILE, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=4)

    @staticmethod
    def _read_shop_items() -> list[dict]:
        try:
            with open(SHOP_FILE, "r", encoding="utf-8") as file:
                data = json.load(file)
        except (json.JSONDecodeError, FileNotFoundError):
            return list(DEFAULT_SHOP_ITEMS)

        # Tolerate both {"items": [...]} and a bare [...] at the top level.
        if isinstance(data, dict):
            return data.get("items", [])
        if isinstance(data, list):
            return data
        return []

    @staticmethod
    def _write_shop_items(items: list[dict]):
        """Always writes the {"items": [...]} shape, normalising a file that
        happened to be a bare list."""
        with open(SHOP_FILE, "w", encoding="utf-8") as file:
            json.dump({"items": items}, file, indent=4, ensure_ascii=False)

    @staticmethod
    def _slugify(name: str) -> str:
        """'Name Colour Change!' -> 'name_colour_change'. Used to mint an id for
        a new shop item so admins never have to think about ids."""
        slug = "".join(char if char.isalnum() else "_" for char in name.lower())

        while "__" in slug:
            slug = slug.replace("__", "_")

        return slug.strip("_")[:50] or "item"

    @classmethod
    def _unique_item_id(cls, name: str, items: list[dict]) -> str:
        """Slugify `name`, then suffix a number if that id is already taken."""
        base = cls._slugify(name)
        taken = {str(item.get("id")) for item in items}

        if base not in taken:
            return base

        counter = 2
        while f"{base}_{counter}" in taken:
            counter += 1

        return f"{base}_{counter}"

    @staticmethod
    def _parse_price(raw: str) -> int | None:
        """Parse a price out of admin input. Accepts 25000, 25,000, and 25k.
        Returns None for anything that isn't a positive whole number."""
        text = raw.strip().lower().replace(",", "").replace("_", "")

        multiplier = 1
        if text.endswith("k"):
            multiplier, text = 1_000, text[:-1]
        elif text.endswith("m"):
            multiplier, text = 1_000_000, text[:-1]

        try:
            value = float(text) * multiplier
        except (ValueError, OverflowError):
            return None

        if value != value or value in (float("inf"), float("-inf")):
            return None
        if value <= 0:
            return None

        return int(value)

    @classmethod
    def _get_account(cls, data: dict, user_id: int) -> dict:
        """Fetch (or create in-memory) a member's account record. The caller is
        responsible for writing `data` back out."""
        key = str(user_id)
        account = data.get(key)

        if account is None:
            account = {"balance": STARTING_BALANCE}
            data[key] = account

        # Older records may predate some of these fields.
        account.setdefault("balance", STARTING_BALANCE)
        account.setdefault("total_wagered", 0)
        account.setdefault("total_won", 0)
        account.setdefault("total_lost", 0)
        account.setdefault("wins", 0)
        account.setdefault("losses", 0)
        account.setdefault("inventory", [])

        return account

    def owns_item(self, user_id: int, item_id: str) -> bool:
        """Whether a member has bought a given shop item. Other cogs use this to
        gate perks behind a purchase — see cogs/customroles.py."""
        account = self._read_economy().get(str(user_id))

        if not account:
            return False

        return any(
            entry.get("id") == item_id for entry in account.get("inventory", [])
        )

    async def get_balance(self, user_id: int) -> int:
        async with self._lock:
            data = self._read_economy()
            balance = self._get_account(data, user_id)["balance"]
            self._write_economy(data)
            return balance

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_amount(raw: str, balance: int) -> int | None:
        """Turn user input into a coin amount. Accepts plain numbers, comma
        separators, k/m suffixes, and the keywords all/max/half. Returns None
        if it can't be parsed."""
        text = raw.strip().lower().replace(",", "").replace("_", "")

        # The keywords get clamped to the table limit rather than rejected —
        # someone sitting on 50k who types "all" means "the most I'm allowed".
        # An explicit over-limit number still errors, so nobody is silently
        # charged less than they typed.
        if text in ("all", "max"):
            return min(balance, MAX_BET)
        if text == "half":
            return min(balance // 2, MAX_BET)

        multiplier = 1
        if text.endswith("k"):
            multiplier, text = 1_000, text[:-1]
        elif text.endswith("m"):
            multiplier, text = 1_000_000, text[:-1]

        try:
            value = float(text) * multiplier
        except (ValueError, OverflowError):
            return None

        # Rejects nan and inf, which float() happily accepts.
        if value != value or value in (float("inf"), float("-inf")):
            return None

        return int(value)

    async def _resolve_bet(self, ctx: commands.Context, raw_amount: str) -> int | None:
        """Validate a bet against the invoker's balance. Sends the error message
        itself and returns None if the bet isn't usable."""
        balance = await self.get_balance(ctx.author.id)
        amount = self._parse_amount(raw_amount, balance)

        if amount is None:
            await ctx.send(
                f"❌ `{raw_amount}` isn't a valid amount. Try `2500`, `2.5k`, `half`, or `all`.",
                ephemeral=True,
                delete_after=ERROR_DELETE_AFTER,
            )
            return None

        if amount <= 0:
            await ctx.send("❌ You have to bet a positive amount.", ephemeral=True, delete_after=ERROR_DELETE_AFTER)
            return None

        if amount < MIN_BET:
            await ctx.send(f"❌ The minimum bet is {format_coins(MIN_BET)}.", ephemeral=True, delete_after=ERROR_DELETE_AFTER)
            return None

        if amount > MAX_BET:
            await ctx.send(f"❌ The maximum bet is {format_coins(MAX_BET)}.", ephemeral=True, delete_after=ERROR_DELETE_AFTER)
            return None

        if amount > balance:
            await ctx.send(
                f"❌ You can't bet {format_coins(amount)} — you only have "
                f"{format_coins(balance)}.",
                ephemeral=True,
                delete_after=ERROR_DELETE_AFTER,
            )
            return None

        return amount

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="gamble",
        description="Bet your coins. Win and you get double back, lose and it's gone.",
    )
    @app_commands.describe(amount="How much to bet. Accepts 2500, 2.5k, half, or all.")
    @commands.cooldown(1, GAMBLE_COOLDOWN, commands.BucketType.user)
    async def gamble(self, ctx: commands.Context, amount: str):
        bet = await self._resolve_bet(ctx, amount)

        if bet is None:
            ctx.command.reset_cooldown(ctx)
            return

        won = _rng.random() < WIN_CHANCE

        async with self._lock:
            data = self._read_economy()
            account = self._get_account(data, ctx.author.id)

            # Re-check under the lock; the balance could have moved between the
            # validation read and now.
            if bet > account["balance"]:
                self._write_economy(data)
                await ctx.send(
                    f"❌ Your balance changed — you only have "
                    f"{format_coins(account['balance'])} now.",
                    ephemeral=True,
                    delete_after=ERROR_DELETE_AFTER,
                )
                return

            account["total_wagered"] += bet

            if won:
                # The stake was never actually taken, and they're paid an equal
                # amount on top, so a 2,500 bet returns 5,000: net +2,500.
                account["balance"] += bet
                account["total_won"] += bet
                account["wins"] += 1
            else:
                account["balance"] -= bet
                account["total_lost"] += bet
                account["losses"] += 1

            new_balance = account["balance"]
            self._write_economy(data)

        if won:
            embed = discord.Embed(
                title="🎉 You won!",
                description=(
                    f"You bet {format_coins(bet)} and got {format_coins(bet * 2)} back.\n"
                    f"**Profit:** +{bet:,}"
                ),
                color=discord.Color.green(),
            )
        else:
            embed = discord.Embed(
                title="💀 You lost.",
                description=(
                    f"You bet {format_coins(bet)} and lost all of it.\n"
                    f"**Profit:** -{bet:,}"
                ),
                color=discord.Color.red(),
            )

        embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
        embed.set_footer(text=f"New balance: {new_balance:,} {CURRENCY_NAME}")

        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="balance",
        aliases=["bal", "coins"],
        description="Check how many coins you (or someone else) have.",
    )
    @app_commands.describe(member="Whose balance to check. Defaults to you.")
    async def balance(self, ctx: commands.Context, member: discord.Member = None):
        member = member or ctx.author

        async with self._lock:
            data = self._read_economy()
            account = self._get_account(data, member.id)
            self._write_economy(data)

        played = account["wins"] + account["losses"]

        embed = discord.Embed(
            title=f"{CURRENCY_EMOJI} Balance",
            description=format_coins(account["balance"]),
            color=discord.Color.gold(),
        )
        embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)

        if played:
            win_rate = account["wins"] / played * 100
            embed.add_field(
                name="Gambling record",
                value=(
                    f"{account['wins']}W / {account['losses']}L ({win_rate:.1f}%)\n"
                    f"Wagered: {account['total_wagered']:,}\n"
                    f"Net: {account['total_won'] - account['total_lost']:+,}"
                ),
                inline=False,
            )

        if account["inventory"]:
            owned = ", ".join(
                entry.get("name", entry.get("id", "?")) for entry in account["inventory"]
            )
            embed.add_field(name="Owned", value=owned[:1024], inline=False)

        await ctx.send(embed=embed)

    @commands.hybrid_command(name="shop", description="Browse the shop and spend your coins.")
    async def shop(self, ctx: commands.Context):
        items = self._read_shop_items()

        if not items:
            await ctx.send("The shop is empty right now.", ephemeral=True)
            return

        # Discord caps a select menu at 25 options.
        items = items[:25]
        balance = await self.get_balance(ctx.author.id)

        embed = discord.Embed(
            title="🛒 Shop",
            description=(
                f"Your balance: {format_coins(balance)}\n\n"
                "Pick something from the dropdown below."
            ),
            color=discord.Color.blurple(),
        )

        for item in items:
            embed.add_field(
                name=f"{item.get('emoji') or ''} {item.get('name', item.get('id', '?'))}".strip(),
                value=(
                    f"{item.get('description', 'No description.')}\n"
                    f"Price: {format_coins(int(item.get('price', 0)))}"
                ),
                inline=False,
            )

        view = ShopView(self, ctx.author, items)
        view.message = await ctx.send(embed=embed, view=view)

    @commands.hybrid_command(
        name="shopadmin",
        description="Add, edit, reprice, or remove shop items.",
    )
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def shopadmin(self, ctx: commands.Context):
        view = ShopAdminView(self, ctx.author)
        view.message = await ctx.send(
            embed=build_shop_admin_embed(self._read_shop_items()),
            view=view,
            ephemeral=True,
        )

    @commands.hybrid_command(name="addcoins", description="Give coins to a member.")
    @app_commands.describe(
        member="Who to give coins to.",
        amount="How many coins to add. Use a negative number to take coins away.",
    )
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def addcoins(self, ctx: commands.Context, member: discord.Member, amount: int):
        async with self._lock:
            data = self._read_economy()
            account = self._get_account(data, member.id)
            account["balance"] = max(0, account["balance"] + amount)
            new_balance = account["balance"]
            self._write_economy(data)

        await ctx.send(
            f"✅ {'Gave' if amount >= 0 else 'Took'} {format_coins(abs(amount))} "
            f"{'to' if amount >= 0 else 'from'} {member.mention}. "
            f"They now have {format_coins(new_balance)}."
        )

        await Logging.log_message(
            f"{CURRENCY_EMOJI} Balance adjusted",
            f"{ctx.author} adjusted {member}'s balance by {amount:+,}.\n"
            f"New balance: {new_balance:,}",
        )

    @commands.hybrid_command(
        name="setcoins",
        description="Set a member's coin balance to an exact number.",
    )
    @app_commands.describe(
        member="Whose balance to set.",
        amount="The exact balance they should end up with.",
    )
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def setcoins(self, ctx: commands.Context, member: discord.Member, amount: int):
        if amount < 0:
            await ctx.send("❌ A balance can't be negative.", ephemeral=True, delete_after=ERROR_DELETE_AFTER)
            return

        async with self._lock:
            data = self._read_economy()
            account = self._get_account(data, member.id)
            old_balance = account["balance"]
            account["balance"] = amount
            self._write_economy(data)

        await ctx.send(f"✅ Set {member.mention}'s balance to {format_coins(amount)}.")

        await Logging.log_message(
            f"{CURRENCY_EMOJI} Balance set",
            f"{ctx.author} set {member}'s balance to {amount:,} (was {old_balance:,}).",
        )

    @commands.hybrid_command(
        name="leaderboard",
        aliases=["lb", "rich"],
        description="See who has the most coins.",
    )
    async def leaderboard(self, ctx: commands.Context):
        data = self._read_economy()

        ranked = sorted(
            data.items(),
            key=lambda entry: entry[1].get("balance", 0),
            reverse=True,
        )[:10]

        if not ranked:
            await ctx.send("Nobody has any coins yet.", ephemeral=True)
            return

        medals = ["🥇", "🥈", "🥉"]
        lines = []

        for index, (user_id, account) in enumerate(ranked):
            member = ctx.guild.get_member(int(user_id)) if ctx.guild else None
            name = member.display_name if member else f"User {user_id}"
            rank = medals[index] if index < len(medals) else f"**{index + 1}.**"
            lines.append(f"{rank} {name} — {account.get('balance', 0):,}")

        embed = discord.Embed(
            title=f"{CURRENCY_EMOJI} Richest members",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )

        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # Errors
    # ------------------------------------------------------------------

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        error = getattr(error, "original", error)

        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(
                f"⏳ Slow down — try again in {error.retry_after:.1f}s.",
                ephemeral=True,
                delete_after=ERROR_DELETE_AFTER,
            )
            return

        if isinstance(error, commands.MissingPermissions):
            await ctx.send(
                "❌ You don't have permission to use that.",
                ephemeral=True,
                delete_after=ERROR_DELETE_AFTER,
            )
            return

        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            await ctx.send(
                f"❌ {error}", ephemeral=True, delete_after=ERROR_DELETE_AFTER
            )
            return

        print(f"[economy] Unhandled error in {ctx.command}: {error!r}")
        await ctx.send(
            "❌ Something went wrong running that command.",
            ephemeral=True,
            delete_after=ERROR_DELETE_AFTER,
        )


class ShopView(discord.ui.View):
    """The dropdown half of the shop flow."""

    def __init__(self, cog: Economy, author: discord.abc.User, items: list[dict]):
        super().__init__(timeout=120)
        self.cog = cog
        self.author = author
        self.message: discord.Message | None = None
        self.add_item(ShopSelect(cog, items))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "This isn't your shop menu — run the command yourself.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class ShopSelect(discord.ui.Select):
    def __init__(self, cog: Economy, items: list[dict]):
        self.cog = cog
        self.items_by_id = {str(item.get("id", item.get("name"))): item for item in items}

        options = [
            discord.SelectOption(
                label=str(item.get("name", item.get("id", "?")))[:100],
                value=str(item.get("id", item.get("name")))[:100],
                description=f"{int(item.get('price', 0)):,} {CURRENCY_NAME}"[:100],
                emoji=item.get("emoji") or None,
            )
            for item in items
        ]

        super().__init__(
            placeholder="Choose an item to buy...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        item = self.items_by_id.get(self.values[0])

        if item is None:
            await interaction.response.send_message(
                "That item no longer exists.", ephemeral=True
            )
            return

        price = int(item.get("price", 0))
        balance = await self.cog.get_balance(interaction.user.id)

        embed = discord.Embed(
            title="Are you sure?",
            description=(
                f"You're about to buy **{item.get('name', item.get('id'))}** for "
                f"{format_coins(price)}.\n\n"
                f"Balance now: {format_coins(balance)}\n"
                f"Balance after: {format_coins(max(0, balance - price))}"
            ),
            color=discord.Color.orange(),
        )

        if balance < price:
            embed.color = discord.Color.red()
            embed.description += f"\n\n❌ You're {format_coins(price - balance)} short."
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        confirm_view = ConfirmPurchaseView(self.cog, interaction.user, item)
        await interaction.response.send_message(embed=embed, view=confirm_view, ephemeral=True)
        confirm_view.message = await interaction.original_response()


class ConfirmPurchaseView(discord.ui.View):
    """The 'are you sure?' half of the shop flow."""

    def __init__(self, cog: Economy, author: discord.abc.User, item: dict):
        super().__init__(timeout=60)
        self.cog = cog
        self.author = author
        self.item = item
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author.id

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

        if self.message is not None:
            try:
                await self.message.edit(content="⌛ Purchase timed out.", view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        price = int(self.item.get("price", 0))
        item_id = str(self.item.get("id", self.item.get("name")))
        item_name = str(self.item.get("name", item_id))

        async with self.cog._lock:
            data = self.cog._read_economy()
            account = self.cog._get_account(data, interaction.user.id)

            # Both are re-checked here rather than trusting the numbers on the
            # confirmation embed, which may be stale by the time they click.
            # Ownership is tested first: someone who just bought this item has
            # already paid for it, so "you can't afford it" would be a confusing
            # way to describe a double-click.
            if any(entry.get("id") == item_id for entry in account["inventory"]):
                self.cog._write_economy(data)
                await interaction.response.edit_message(
                    content=f"❌ You already own **{item_name}**.",
                    embed=None,
                    view=None,
                    delete_after=ERROR_DELETE_AFTER,
                )
                return

            if account["balance"] < price:
                self.cog._write_economy(data)
                await interaction.response.edit_message(
                    content=(
                        f"❌ You can't afford that any more — you have "
                        f"{format_coins(account['balance'])}."
                    ),
                    embed=None,
                    view=None,
                    delete_after=ERROR_DELETE_AFTER,
                )
                return

            account["balance"] -= price
            account["inventory"].append({
                "id": item_id,
                "name": item_name,
                "price": price,
                "purchased_at": datetime.now(timezone.utc).isoformat(),
            })
            new_balance = account["balance"]
            self.cog._write_economy(data)

        # Hand out the attached role, if the item has one configured.
        role_note = ""
        role_id = self.item.get("role_id")

        if role_id and isinstance(interaction.user, discord.Member):
            role = interaction.guild.get_role(int(role_id))

            if role is None:
                role_note = "\n⚠️ The role for this item no longer exists — ping staff."
            else:
                try:
                    await interaction.user.add_roles(role, reason=f"Bought {item_name}")
                    role_note = f"\n✅ You've been given {role.mention}."
                except discord.Forbidden:
                    # Refund rather than charge for something they never received.
                    async with self.cog._lock:
                        data = self.cog._read_economy()
                        account = self.cog._get_account(data, interaction.user.id)
                        account["balance"] += price
                        account["inventory"] = [
                            entry for entry in account["inventory"]
                            if entry.get("id") != item_id
                        ]
                        self.cog._write_economy(data)

                    await interaction.response.edit_message(
                        content=(
                            "❌ I don't have permission to give you that role, so "
                            "nothing was charged. Please tell staff."
                        ),
                        embed=None,
                        view=None,
                        delete_after=ERROR_DELETE_AFTER,
                    )
                    return

        embed = discord.Embed(
            title="✅ Purchased",
            description=(
                f"You bought **{item_name}** for {format_coins(price)}.\n"
                f"Balance: {format_coins(new_balance)}{role_note}"
            ),
            color=discord.Color.green(),
        )

        # Some items are the gateway to a follow-up step. Rather than making the
        # buyer go and run another command, open it right here on the back of
        # the same click.
        custom_roles = self.cog.bot.get_cog("CustomRoles")

        if custom_roles is not None and custom_roles.unlocks_designer(item_id):
            modal, problem = custom_roles.modal_for(interaction.user, interaction.guild)

            if modal is not None:
                # A modal has to BE the response to the interaction, so the
                # purchase receipt goes onto the original message separately.
                await interaction.response.send_modal(modal)

                if self.message is not None:
                    try:
                        await self.message.edit(embed=embed, view=None)
                    except discord.HTTPException:
                        pass

                await self._log_purchase(interaction, item_name, price, new_balance)
                return

            # Couldn't open it — say why, but the purchase itself still stands.
            embed.description += f"\n\n⚠️ {problem}"

        await interaction.response.edit_message(embed=embed, view=None)

        await self._log_purchase(interaction, item_name, price, new_balance)

    @staticmethod
    async def _log_purchase(
        interaction: discord.Interaction, item_name: str, price: int, new_balance: int
    ):
        await Logging.log_message(
            f"{CURRENCY_EMOJI} Shop purchase",
            f"{interaction.user} bought **{item_name}** for {price:,} {CURRENCY_NAME}.\n"
            f"Remaining balance: {new_balance:,}",
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="Purchase cancelled.", embed=None, view=None
        )


def build_shop_admin_embed(items: list[dict]) -> discord.Embed:
    """The item list shown on the admin panel."""
    embed = discord.Embed(
        title="🛠️ Shop Admin",
        description=(
            "Use the buttons below to manage the shop.\n"
            "Changes save to `info/shop.json` straight away."
        ),
        color=discord.Color.dark_teal(),
    )

    if not items:
        embed.add_field(
            name="No items",
            value="The shop is empty. Press **Add Item** to create one.",
            inline=False,
        )
        return embed

    for item in items[:25]:
        role_id = item.get("role_id")
        role_line = f"\nGrants role: <@&{role_id}>" if role_id else "\nGrants role: none"

        embed.add_field(
            name=f"{item.get('emoji') or ''} {item.get('name', '?')}".strip(),
            value=(
                f"{item.get('description', 'No description.')}\n"
                f"Price: {int(item.get('price', 0)):,} {CURRENCY_NAME}"
                f"{role_line}\n"
                f"`id: {item.get('id', '?')}`"
            ),
            inline=False,
        )

    if len(items) > 25:
        embed.set_footer(
            text=f"Showing 25 of {len(items)} items — the shop only displays the first 25."
        )

    return embed


class ShopAdminView(discord.ui.View):
    """Add / Edit / Remove panel for the shop."""

    def __init__(self, cog: Economy, author: discord.abc.User):
        super().__init__(timeout=300)
        self.cog = cog
        self.author = author
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "This isn't your panel — run the command yourself.", ephemeral=True
            )
            return False
        return True

    async def refresh(self, interaction: discord.Interaction):
        """Redraw the panel with whatever is currently on disk. Used after any
        change so the admin sees the result immediately."""
        if self.message is None:
            return

        try:
            await self.message.edit(
                embed=build_shop_admin_embed(self.cog._read_shop_items()),
                view=self,
            )
        except discord.HTTPException:
            pass

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Add Item", emoji="➕", style=discord.ButtonStyle.success)
    async def add_item_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ShopItemModal(self.cog, self))

    @discord.ui.button(label="Edit Item", emoji="✏️", style=discord.ButtonStyle.primary)
    async def edit_item_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        items = self.cog._read_shop_items()

        if not items:
            await interaction.response.send_message(
                "There's nothing to edit yet.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            "Pick the item you want to edit:",
            view=ItemPickerView(self.cog, self, items[:25], mode="edit"),
            ephemeral=True,
        )

    @discord.ui.button(label="Remove Item", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def remove_item_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        items = self.cog._read_shop_items()

        if not items:
            await interaction.response.send_message(
                "There's nothing to remove.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            "Pick the item you want to remove:",
            view=ItemPickerView(self.cog, self, items[:25], mode="remove"),
            ephemeral=True,
        )


class ShopItemModal(discord.ui.Modal):
    """Create a new item, or edit an existing one when `existing` is passed."""

    def __init__(self, cog: Economy, panel: ShopAdminView, existing: dict | None = None):
        super().__init__(
            title="Edit Shop Item" if existing else "Add Shop Item",
            timeout=600,
        )
        self.cog = cog
        self.panel = panel
        self.existing = existing

        self.name = discord.ui.TextInput(
            label="Name",
            placeholder="VIP Role",
            default=existing.get("name") if existing else None,
            max_length=100,
            required=True,
        )
        self.description = discord.ui.TextInput(
            label="Description",
            style=discord.TextStyle.paragraph,
            placeholder="What the buyer actually gets.",
            default=existing.get("description") if existing else None,
            max_length=300,
            required=False,
        )
        self.price = discord.ui.TextInput(
            label="Price",
            placeholder="25000 (or 25k)",
            default=str(existing.get("price")) if existing else None,
            max_length=20,
            required=True,
        )
        self.emoji = discord.ui.TextInput(
            label="Emoji (optional)",
            placeholder="⭐",
            default=existing.get("emoji") if existing else None,
            max_length=32,
            required=False,
        )
        self.role_id = discord.ui.TextInput(
            label="Role ID to grant (optional)",
            placeholder="Leave blank for no role. Paste the role ID here.",
            default=str(existing.get("role_id")) if existing and existing.get("role_id") else None,
            max_length=25,
            required=False,
        )

        for field in (self.name, self.description, self.price, self.emoji, self.role_id):
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction):
        price = self.cog._parse_price(self.price.value)

        if price is None:
            await interaction.response.send_message(
                f"❌ `{self.price.value}` isn't a valid price. Use a positive whole "
                "number like `25000` or `25k`.",
                ephemeral=True,
                delete_after=ERROR_DELETE_AFTER,
            )
            return

        # An empty optional field comes back as "", which should clear the role
        # rather than be stored as a falsy string.
        role_id = None
        warnings = []
        raw_role = self.role_id.value.strip()

        if raw_role:
            # Tolerate someone pasting a <@&123> mention instead of a bare ID.
            digits = "".join(char for char in raw_role if char.isdigit())

            if not digits:
                await interaction.response.send_message(
                    f"❌ `{raw_role}` isn't a valid role ID. Paste the numeric ID, "
                    "or leave the field blank.",
                    ephemeral=True,
                    delete_after=ERROR_DELETE_AFTER,
                )
                return

            role_id = int(digits)
            role = interaction.guild.get_role(role_id) if interaction.guild else None

            if role is None:
                warnings.append(
                    "⚠️ No role with that ID exists in this server — buyers will get "
                    "a warning instead of the role."
                )
            else:
                me = interaction.guild.me

                if me is not None and not me.guild_permissions.manage_roles:
                    warnings.append(
                        "⚠️ I don't have **Manage Roles**, so I can't hand this out. "
                        "Purchases will be refunded."
                    )
                elif me is not None and role >= me.top_role:
                    warnings.append(
                        f"⚠️ {role.mention} sits above my highest role, so I can't "
                        "assign it. Move my role above it in Server Settings."
                    )

        async with self.cog._shop_lock:
            items = self.cog._read_shop_items()

            if self.existing is None:
                item_id = self.cog._unique_item_id(self.name.value, items)
                items.append({
                    "id": item_id,
                    "name": self.name.value.strip(),
                    "description": self.description.value.strip() or "No description.",
                    "emoji": self.emoji.value.strip() or None,
                    "price": price,
                    "role_id": role_id,
                })
                action = "Added"
            else:
                item_id = str(self.existing.get("id"))
                target = next(
                    (item for item in items if str(item.get("id")) == item_id), None
                )

                if target is None:
                    await interaction.response.send_message(
                        "❌ That item was removed while you had the form open.",
                        ephemeral=True,
                        delete_after=ERROR_DELETE_AFTER,
                    )
                    return

                # The id deliberately stays put even when the name changes, so
                # existing inventory entries keep matching.
                target["name"] = self.name.value.strip()
                target["description"] = self.description.value.strip() or "No description."
                target["emoji"] = self.emoji.value.strip() or None
                target["price"] = price
                target["role_id"] = role_id
                action = "Updated"

            self.cog._write_shop_items(items)

        lines = [
            f"✅ {action} **{self.name.value.strip()}** — {format_coins(price)}",
            f"`id: {item_id}`",
        ]
        lines.extend(warnings)

        await interaction.response.send_message("\n".join(lines), ephemeral=True)
        await self.panel.refresh(interaction)

        await Logging.log_message(
            f"{CURRENCY_EMOJI} Shop item {action.lower()}",
            f"{interaction.user} {action.lower()} **{self.name.value.strip()}** "
            f"(`{item_id}`) at {price:,} {CURRENCY_NAME}.",
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"[economy] Shop item modal error: {error!r}")

        if not interaction.response.is_done():
            await interaction.response.send_message(
                "❌ Something went wrong saving that item.", ephemeral=True, delete_after=ERROR_DELETE_AFTER
            )


class ItemPickerView(discord.ui.View):
    """Dropdown of existing items, feeding either the edit or remove flow."""

    def __init__(self, cog: Economy, panel: ShopAdminView, items: list[dict], mode: str):
        super().__init__(timeout=120)
        self.add_item(ItemPickerSelect(cog, panel, items, mode))


class ItemPickerSelect(discord.ui.Select):
    def __init__(self, cog: Economy, panel: ShopAdminView, items: list[dict], mode: str):
        self.cog = cog
        self.panel = panel
        self.mode = mode
        self.items_by_id = {str(item.get("id")): item for item in items}

        super().__init__(
            placeholder=f"Choose an item to {mode}...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=str(item.get("name", item.get("id", "?")))[:100],
                    value=str(item.get("id"))[:100],
                    description=f"{int(item.get('price', 0)):,} {CURRENCY_NAME}"[:100],
                    emoji=item.get("emoji") or None,
                )
                for item in items
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        item = self.items_by_id.get(self.values[0])

        if item is None:
            await interaction.response.send_message(
                "That item no longer exists.", ephemeral=True
            )
            return

        if self.mode == "edit":
            await interaction.response.send_modal(
                ShopItemModal(self.cog, self.panel, existing=item)
            )
            return

        await interaction.response.send_message(
            embed=discord.Embed(
                title="Remove this item?",
                description=(
                    f"**{item.get('name', item.get('id'))}** "
                    f"({int(item.get('price', 0)):,} {CURRENCY_NAME}) will be taken off "
                    "the shop.\n\nMembers who already bought it keep it in their "
                    "inventory."
                ),
                color=discord.Color.red(),
            ),
            view=ConfirmRemoveView(self.cog, self.panel, item),
            ephemeral=True,
        )


class ConfirmRemoveView(discord.ui.View):
    """The 'are you sure?' step before an item is deleted."""

    def __init__(self, cog: Economy, panel: ShopAdminView, item: dict):
        super().__init__(timeout=60)
        self.cog = cog
        self.panel = panel
        self.item = item

    @discord.ui.button(label="Remove", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        item_id = str(self.item.get("id"))
        item_name = str(self.item.get("name", item_id))

        async with self.cog._shop_lock:
            items = self.cog._read_shop_items()
            remaining = [item for item in items if str(item.get("id")) != item_id]

            if len(remaining) == len(items):
                await interaction.response.edit_message(
                    content="❌ That item was already removed.",
                    embed=None,
                    view=None,
                    delete_after=ERROR_DELETE_AFTER,
                )
                return

            self.cog._write_shop_items(remaining)

        await interaction.response.edit_message(
            content=f"✅ Removed **{item_name}** from the shop.", embed=None, view=None
        )
        await self.panel.refresh(interaction)

        await Logging.log_message(
            f"{CURRENCY_EMOJI} Shop item removed",
            f"{interaction.user} removed **{item_name}** (`{item_id}`) from the shop.",
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="Cancelled — nothing was removed.", embed=None, view=None
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Economy(bot))
