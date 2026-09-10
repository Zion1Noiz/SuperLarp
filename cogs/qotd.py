import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from mods import FileHandling, Logging, Messaging

# The daily time the QOTD should post at, in US Pacific time. ZoneInfo
# automatically handles the PST/PDT switch across daylight saving time,
# unlike a fixed UTC offset which would drift an hour twice a year.
StartTime = datetime.strptime("15:00", "%H:%M").time().replace(tzinfo=ZoneInfo("America/Los_Angeles")) # 5:00 pm Pacific

load_dotenv()

INFO_FOLDER = "./info"

QOTD_CHANNEL_FILE = os.path.join(INFO_FOLDER, "qotd-channel.txt")
QOTD_INFO_FILE = os.path.join(INFO_FOLDER, "qotd-channel.json")

QOTD_DIR = os.path.join(INFO_FOLDER, "qotd")
QOTD_SUBMISSIONS_FILE = os.path.join(QOTD_DIR, "submissions.json")
QOTD_FORGOTTEN_FILE = os.path.join(QOTD_DIR, "forgotten.json")
QOTD_QUEUE_FILE = os.path.join(QOTD_DIR, "qotd-queue.json")
QOTD_COOLDOWN_FILE = os.path.join(QOTD_DIR, "cooldowns.json")

SUBMISSION_COOLDOWN_SECONDS = 3600  # 1 hour

QOTD_PING_ROLE_ID = os.getenv("QOTD_PING_ROLE_ID")

QOTD_SUBMISSIONS_LOG_CHANNEL_ID = 1542309694729617438
QOTD_SUBMISSIONS_CHANNEL_ID = 1542315550393245696


def _read_json(path: str, default):
    """Reads a JSON file, returning `default` (and its type) if missing/empty/invalid."""
    data = FileHandling.ReadFromJSONFile(path)
    return data if isinstance(data, type(default)) else default


def _write_json(path: str, data) -> None:
    """Writes data to a JSON file."""
    FileHandling.WriteToJSON(path, data)


def _get_remaining_cooldown(user_id: int) -> float:
    """Returns seconds remaining before the given user can submit again, or 0 if they're free to."""
    cooldowns = _read_json(QOTD_COOLDOWN_FILE, {})
    last_submitted = cooldowns.get(str(user_id))

    if last_submitted is None:
        return 0

    remaining = SUBMISSION_COOLDOWN_SECONDS - (time.time() - last_submitted)
    return max(0, remaining)


def _set_cooldown(user_id: int) -> None:
    """Records that the given user just submitted, starting their cooldown."""
    cooldowns = _read_json(QOTD_COOLDOWN_FILE, {})
    cooldowns[str(user_id)] = time.time()
    _write_json(QOTD_COOLDOWN_FILE, cooldowns)


def _format_cooldown(remaining_seconds: float) -> str:
    """Formats a remaining-cooldown duration as e.g. '42m 10s'."""
    remaining_seconds = int(remaining_seconds)
    minutes, seconds = divmod(remaining_seconds, 60)
    return f"{minutes}m {seconds}s"


def _next_available_date(queue: dict) -> str:
    """Finds the earliest date (starting today) not already present in the queue.
    Keys are 'M/D/YYYY' strings, e.g. '8/27/2026', matching the queue's format."""
    day = datetime.now()
    for _ in range(366):
        key = f"{day.month}/{day.day}/{day.year}"
        if key not in queue:
            return key
        day += timedelta(days=1)
    # Fallback, should never realistically be hit
    return f"{day.month}/{day.day}/{day.year}"


def _parse_schedule_date(raw: str) -> str:
    """Parses a moderator-entered date into a queue key ('M/D/YYYY').

    Accepts 'M/D' (year is inferred: this year, or next year if that date
    has already passed) or an explicit 'M/D/YYYY'. Raises ValueError if the
    input doesn't match either format.
    """
    raw = raw.strip()

    try:
        parsed = datetime.strptime(raw, "%m/%d/%Y").date()
    except ValueError:
        parsed_no_year = datetime.strptime(raw, "%m/%d").date()
        today = datetime.now().date()
        year = today.year
        candidate = parsed_no_year.replace(year=year)
        if candidate < today:
            year += 1
        parsed = parsed_no_year.replace(year=year)

    return f"{parsed.month}/{parsed.day}/{parsed.year}"


def _key_to_date(key: str):
    """Parses a queue key ('M/D/YYYY') back into a date object, or None if malformed."""
    try:
        month, day, year = (int(part) for part in key.split("/"))
        return datetime(year, month, day).date()
    except (ValueError, TypeError):
        return None


def _build_queue_pages(queue: dict, per_page: int = 10) -> list[discord.Embed]:
    """Builds a chronologically-sorted, paginated set of embeds for the QOTD queue."""
    entries = []
    for key, question in queue.items():
        entries.append((_key_to_date(key), key, question))

    # Chronological order; anything with an unparsable key is pushed to the end.
    entries.sort(key=lambda entry: (entry[0] is None, entry[0]))

    if not entries:
        return [
            discord.Embed(
                title="📅 QOTD Queue",
                description="The queue is empty. Use `/schedule-qotd` to add some!",
                color=discord.Color.blurple(),
            )
        ]

    total_pages = (len(entries) - 1) // per_page + 1
    pages = []

    for page_num in range(total_pages):
        chunk = entries[page_num * per_page : (page_num + 1) * per_page]
        embed = discord.Embed(title="📅 QOTD Queue", color=discord.Color.blurple())

        for date_obj, key, question in chunk:
            label = (
                f"{date_obj.strftime('%A, %B')} {date_obj.day}, {date_obj.year}"
                if date_obj else key
            )
            embed.add_field(name=f"🗓️ {label}", value=question[:1024], inline=False)

        embed.set_footer(
            text=f"Page {page_num + 1} of {total_pages} • {len(entries)} scheduled"
        )
        pages.append(embed)

    return pages


class QOTDQueueView(discord.ui.View):
    """Pagination controls for browsing the QOTD queue."""

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


class QOTDReviewView(discord.ui.View):
    """Attached to a submission's log message. Lets moderators approve or deny it."""

    def __init__(self, bot: commands.Bot, submission_id: str):
        super().__init__(timeout=None)
        self.bot = bot
        self.submission_id = submission_id

        approve_button = discord.ui.Button(
            label="Approve",
            style=discord.ButtonStyle.success,
            custom_id=f"qotd_approve:{submission_id}",
        )
        approve_button.callback = self.approve_callback
        self.add_item(approve_button)

        deny_button = discord.ui.Button(
            label="Deny",
            style=discord.ButtonStyle.danger,
            custom_id=f"qotd_deny:{submission_id}",
        )
        deny_button.callback = self.deny_callback
        self.add_item(deny_button)

    @staticmethod
    def _is_moderator(interaction: discord.Interaction) -> bool:
        perms = interaction.user.guild_permissions if interaction.guild else None
        return bool(perms and (perms.administrator or perms.manage_guild))

    async def _resolve(self, interaction: discord.Interaction, approved: bool):
        if not self._is_moderator(interaction):
            await interaction.response.send_message(
                "You don't have permission to review QOTD submissions.",
                ephemeral=True,
            )
            return

        submissions = _read_json(QOTD_SUBMISSIONS_FILE, [])
        submission = next(
            (s for s in submissions if s.get("id") == self.submission_id), None
        )

        if submission is None:
            for item in self.children:
                item.disabled = True
            self.stop()
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(
                "This submission has already been reviewed or no longer exists.",
                ephemeral=True,
            )
            return

        # Remove from the pending submissions list
        submissions = [s for s in submissions if s.get("id") != self.submission_id]
        _write_json(QOTD_SUBMISSIONS_FILE, submissions)

        submission["status"] = "approved" if approved else "denied"
        submission["reviewed_by"] = f"{interaction.user} ({interaction.user.id})"
        submission["reviewed_at"] = time.time()

        date_key = None

        if approved:
            queue = _read_json(QOTD_QUEUE_FILE, {})
            date_key = _next_available_date(queue)
            queue[date_key] = submission["question"]
            _write_json(QOTD_QUEUE_FILE, queue)
        else:
            forgotten = _read_json(QOTD_FORGOTTEN_FILE, [])
            forgotten.append(submission)
            _write_json(QOTD_FORGOTTEN_FILE, forgotten)

        # Ack the interaction FIRST. DMs are a separate, slower API call and
        # can fail (closed DMs, uncached user, etc). Doing this beforehand
        # was causing Discord to time out waiting for a response whenever
        # the DM step blew up.
        for item in self.children:
            item.disabled = True
        self.stop()

        embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
        embed.color = discord.Color.green() if approved else discord.Color.red()

        if approved:
            embed.set_footer(
                text=f"✅ Approved by {interaction.user.display_name} — scheduled for {date_key}"
            )
        else:
            embed.set_footer(text=f"❌ Denied by {interaction.user.display_name}")

        await interaction.response.edit_message(embed=embed, view=self)

        # Best-effort DM to the submitter. Never let this fail the review.
        try:
            user_object = self.bot.get_user(submission["user_id"])
            if user_object is None:
                user_object = await self.bot.fetch_user(submission["user_id"])

            if approved:
                await user_object.send(
                    f'Your QOTD question: "{submission["question"]}" has been approved! '
                    f"You should see your question on {date_key}!!!"
                )
            else:
                await user_object.send(
                    f'Your QOTD question: "{submission["question"]}" has been rejected. '
                    f"For more info, make a ticket."
                )
        except discord.HTTPException:
            pass  # e.g. user has DMs closed — not worth failing the review over
        except Exception as e:
            await Logging.log_message("⚠️ QOTD Review DM Error", f"```{e}```")

    async def approve_callback(self, interaction: discord.Interaction):
        await self._resolve(interaction, approved=True)

    async def deny_callback(self, interaction: discord.Interaction):
        await self._resolve(interaction, approved=False)

class SubmitAQOTDMessageView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
            super().__init__(timeout=None)
            self.bot = bot
    
            submit_button = discord.ui.Button(
                label="Submit your own QOTD",
                style=discord.ButtonStyle.success,
                custom_id="submit_qotd_question_btn"
            )
            submit_button.callback = self.submit_button_callback
            self.add_item(submit_button)

    async def submit_button_callback(self, interaction):
        await interaction.response.send_modal(QuestionSubmissionModal(self.bot))
    
    

class QuestionSubmissionModal(discord.ui.Modal, title="Submit a QOTD question!"):
    def __init__(self, bot : commands.Bot):
            super().__init__(timeout=300)
            self.bot = bot
            self.SubmissionMessageObject = discord.Embed(
                color=discord.Color.light_grey(),
            )

    body = discord.ui.TextInput(
            label="Enter QOTD submission.",
            style=discord.TextStyle.long,
            placeholder="Enter your QOTD submission here.",
            required=True,
    )


    async def on_submit(self, interaction : discord.Interaction):
        try:
            #remaining = _get_remaining_cooldown(interaction.user.id)

            #if remaining > 0:
            #    await interaction.response.send_message(
            #        f"⏳ You're on cooldown. You can submit another question in **{_format_cooldown(remaining)}**.",
            #        ephemeral=True,
            #    )
            #    return

            submission = {
                "id": uuid.uuid4().hex[:8],
                "question": self.body.value,
                "user_id": interaction.user.id,
                "user_name": str(interaction.user),
                "submitted_at": time.time(),
                "status": "pending",
            }

            submissions = _read_json(QOTD_SUBMISSIONS_FILE, [])
            submissions.append(submission)
            _write_json(QOTD_SUBMISSIONS_FILE, submissions)

            #_set_cooldown(interaction.user.id)

            SubmissionsLogChannelObject = self.bot.get_channel(QOTD_SUBMISSIONS_LOG_CHANNEL_ID)

            embed = discord.Embed(
                title="📝 New QOTD Submission",
                description=f'"{submission["question"]}"',
                color=discord.Color.light_grey(),
            )
            embed.add_field(
                name="Submitted by",
                value=f"{interaction.user.mention} [@{interaction.user.name}] (`{interaction.user.id}`)",
                inline=False,
            )
            embed.add_field(name="Submission ID", value=f"`{submission['id']}`", inline=False)
            embed.set_thumbnail(url=interaction.user.display_avatar.url)
            embed.set_footer(text="Awaiting review")

            view = QOTDReviewView(self.bot, submission["id"])

            if SubmissionsLogChannelObject is not None:
                await SubmissionsLogChannelObject.send(embed=embed, view=view)
            else:
                await Logging.log_message(
                    "⚠️ QOTD Submission Error",
                    "Could not find the QOTD submissions log channel."
                )

            await interaction.response.send_message(
                "✅ Thanks! Your question has been submitted for review.",
                ephemeral=True,
            )

        except (Exception, discord.HTTPException) as e:
            await Logging.log_message("⚠️ QOTD Submission Error", f"```{e}```")
            try:
                await interaction.response.send_message(
                    "Something went wrong submitting your question. Please try again later.",
                    ephemeral=True,
                )
            except discord.InteractionResponded:
                pass


class ScheduleQOTDModal(discord.ui.Modal, title="Schedule QOTDs"):
    """Lets a moderator bulk-add questions to the queue, spreadsheet-style.

    Discord modals cap out at 5 fields and have no real table/grid widget,
    so this is the closest approximation: five stacked rows, each one
    'Date, Question' pair — like filling in cells row by row.
    """

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=300)
        self.bot = bot

    row_1 = discord.ui.TextInput(
        label="Row 1 — Date, Question",
        placeholder="8/27, What's your favorite season?",
        required=True,
        max_length=300,
    )

    row_2 = discord.ui.TextInput(
        label="Row 2 — Date, Question",
        placeholder="8/28, What's your dream job?",
        required=False,
        max_length=300,
    )

    row_3 = discord.ui.TextInput(
        label="Row 3 — Date, Question",
        placeholder="8/29, What's your favorite movie?",
        required=False,
        max_length=300,
    )

    row_4 = discord.ui.TextInput(
        label="Row 4 — Date, Question",
        placeholder="8/30, What's a skill you'd like to learn?",
        required=False,
        max_length=300,
    )

    row_5 = discord.ui.TextInput(
        label="Row 5 — Date, Question",
        placeholder="8/31, What's your go-to comfort food?",
        required=False,
        max_length=300,
    )

    @staticmethod
    def _parse_row(raw: str) -> tuple[str, str]:
        """Splits a 'Date, Question' row into (date_key, question).
        Raises ValueError if the row is malformed."""
        date_part, sep, question_part = raw.partition(",")
        date_part = date_part.strip()
        question_part = question_part.strip()

        if not sep or not date_part or not question_part:
            raise ValueError("expected `Date, Question` format")

        date_key = _parse_schedule_date(date_part)
        return date_key, question_part

    async def on_submit(self, interaction: discord.Interaction):
        rows = [self.row_1.value, self.row_2.value, self.row_3.value, self.row_4.value, self.row_5.value]

        queue = _read_json(QOTD_QUEUE_FILE, {})
        scheduled: list[str] = []
        overwritten: list[str] = []
        errors: list[str] = []

        for i, raw in enumerate(rows, start=1):
            if not raw or not raw.strip():
                continue

            try:
                date_key, question = self._parse_row(raw)
            except ValueError:
                errors.append(f"Row {i}: `{raw}` — expected `Date, Question`, e.g. `8/27, What's your favorite season?`")
                continue

            if date_key in queue:
                overwritten.append(date_key)

            queue[date_key] = question
            scheduled.append(date_key)

        if scheduled:
            _write_json(QOTD_QUEUE_FILE, queue)

        lines = []

        if scheduled:
            lines.append("✅ Scheduled: " + ", ".join(f"**{d}**" for d in scheduled))
        if overwritten:
            lines.append("⚠️ Replaced an existing entry for: " + ", ".join(f"**{d}**" for d in overwritten))
        if errors:
            lines.append("❌ Couldn't process:\n" + "\n".join(errors))
        if not lines:
            lines.append("Nothing to schedule — every row was empty.")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

        if scheduled:
            summary = "\n".join(f"**{d}:** {queue[d]}" for d in scheduled)
            await Logging.log_message(
                "📅 QOTD Scheduled (bulk)",
                f"**Scheduled by:** {interaction.user} ({interaction.user.id})\n\n{summary}",
            )


class QOTD(commands.Cog):
    """Automatically posts the Question of the Day every 24 hours."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # Load API configuration from your .env
        self.api_url = os.getenv("QOTD_API_URL")
        self.api_key = os.getenv("QOTD_API_KEY")

        self.headers = {
            "Authorization": self.api_key
        }

        self._ensure_files_exist()

    async def cog_load(self):
        """Re-attach review views for any submissions still awaiting review,
        so the Approve/Deny buttons keep working after a bot restart."""
        submissions = _read_json(QOTD_SUBMISSIONS_FILE, [])
        for submission in submissions:
            submission_id = submission.get("id")
            if submission_id:
                self.bot.add_view(QOTDReviewView(self.bot, submission_id))
                
        self.bot.add_view(SubmitAQOTDMessageView(self.bot))

    def cog_unload(self):
        """Stops the loop if the cog is unloaded."""
        if self.qotd_loop.is_running():
            self.qotd_loop.cancel()

    @staticmethod
    def _ensure_files_exist():
        """Creates the QOTD info files with sane defaults if they don't exist yet."""
        os.makedirs(INFO_FOLDER, exist_ok=True)
        os.makedirs(QOTD_DIR, exist_ok=True)

        if not os.path.exists(QOTD_CHANNEL_FILE):
            with open(QOTD_CHANNEL_FILE, "w", encoding="utf-8"):
                pass

        if not os.path.exists(QOTD_INFO_FILE) or os.path.getsize(QOTD_INFO_FILE) == 0:
            FileHandling.WriteToJSON(
                QOTD_INFO_FILE,
                {"LastPosted": 0, "IsDebugMode": False},
            )

        for path in (QOTD_SUBMISSIONS_FILE, QOTD_FORGOTTEN_FILE):
            if not os.path.exists(path) or os.path.getsize(path) == 0:
                FileHandling.WriteToJSON(path, [])

        for path in (QOTD_QUEUE_FILE, QOTD_COOLDOWN_FILE):
            if not os.path.exists(path) or os.path.getsize(path) == 0:
                FileHandling.WriteToJSON(path, {})

    @staticmethod
    def _get_channel_id() -> int | None:
        """Returns the configured QOTD channel ID, or None if it hasn't been set."""
        if os.path.getsize(QOTD_CHANNEL_FILE) == 0:
            return None

        content = FileHandling.ReadFromTextFile(QOTD_CHANNEL_FILE)
        return int(content) if content else None

    @staticmethod
    def _read_info() -> dict:
        info = FileHandling.ReadFromJSONFile(QOTD_INFO_FILE)
        if not info:
            info = {"LastPosted": 0, "IsDebugMode": False}
        info.setdefault("LastPosted", 0)
        info.setdefault("IsDebugMode", False)
        return info

    @staticmethod
    def _today_key() -> str:
        """Today's date as an 'M/D/YYYY' key, matching the queue file's format."""
        now = datetime.now()
        return f"{now.month}/{now.day}/{now.year}"

    # -------------------------
    # QOTD API + loop
    # -------------------------

    async def get_question_of_the_day(self):
        """Fetches today's Question of the Day from the API."""

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.api_url,
                    headers=self.headers
                ) as response:
                    response.raise_for_status()
                    return await response.json()

        except Exception as e:
            await Logging.log_message(
                "⚠️ QOTD Error",
                f"Failed to fetch today's Question of the Day.\n\n```{e}```"
            )
            return None

    
    
    async def _post_qotd(self):
        """Posts today's Question of the Day.

        Checks the scheduled queue (qotd-queue.json, keyed by 'M/D') for
        today's date first; if nothing is scheduled, falls back to the
        QOTD API.
        """

        try:
            channel_id = self._get_channel_id()

            if channel_id is None:
                await Logging.log_message(
                    "⚠️ QOTD Error",
                    "No QOTD channel has been configured. Run /qotd-setup first."
                )
                return

            today_key = self._today_key()
            queue = _read_json(QOTD_QUEUE_FILE, {})

            if today_key in queue:
                question = queue.pop(today_key)
                _write_json(QOTD_QUEUE_FILE, queue)
                source_note = f"Source: scheduled queue (`{today_key}`)"
            else:
                qotd_response = await self.get_question_of_the_day()

                if qotd_response is None:
                    return

                question = qotd_response.get("question")

                if not question:
                    await Logging.log_message(
                        "⚠️ QOTD Error",
                        "API response did not contain a question."
                    )
                    return

                source_note = f"Source: API\n\nAPI Response:\n```json\n{qotd_response}\n```"

            channel = await self.bot.fetch_channel(channel_id)

            embed = discord.Embed(
                title="❓ Here's Today's Question of the Day! ❓",
                description=f"{question} 🤔",
                color=discord.Color.light_grey()
            )

            ping_content = f"<@&{QOTD_PING_ROLE_ID}>" if QOTD_PING_ROLE_ID else None

            await Messaging.SendPublicMessage(
                message=ping_content,
                embed=embed,
                attachments=[],
                channel=channel
            )

            qotd_info = self._read_info()
            qotd_info["LastPosted"] = time.time()
            FileHandling.WriteToJSON(QOTD_INFO_FILE, qotd_info)

            await Logging.log_message(
                "✅ QOTD Posted",
                f"Successfully posted today's Question of the Day.\n\n{source_note}"
            )

        except Exception as e:
            print(e)

            await Logging.log_message(
                "⚠️ QOTD Loop Failed",
                f"```py\n{e}\n```"
            )

    @tasks.loop(time=StartTime)
    async def qotd_loop(self):
        """Posts a Question of the Day once a day at StartTime."""
        await self._post_qotd()

    @qotd_loop.before_loop
    async def before_qotd_loop(self):
        """Wait until the bot is fully logged in before starting."""
        await self.bot.wait_until_ready()

    # -------------------------
    # Slash commands
    # -------------------------

    @app_commands.command(
        name="qotd-setup",
        description="Sets up the question of the day service."
    )
    @app_commands.default_permissions(administrator=True)
    async def qotd_setup(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel
    ):
        await interaction.response.defer(ephemeral=True)

        if self._get_channel_id() is not None:
            await interaction.followup.send(
                "There's already a set QOTD channel! Run /edit-qotd-channel to change it.",
                ephemeral=True
            )
            return

        try:
            with open(QOTD_CHANNEL_FILE, "w", encoding="utf-8") as file:
                file.write(str(channel.id))

        except Exception as e:
            await Logging.log_message(
                "⚠️ QOTD Setup Error",
                f"Failed to write the QOTD channel file.\n\n```{e}```"
            )
            await interaction.followup.send(
                "Something went wrong while saving the channel. Check the logs.",
                ephemeral=True
            )
            return

        await interaction.followup.send(
            f"✅ QOTD channel set to {channel.mention}!",
            ephemeral=True
        )

    @app_commands.command(
        name="edit-qotd-channel",
        description="Changes the channel used by the question of the day service."
    )
    @app_commands.default_permissions(administrator=True)
    async def edit_qotd_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel
    ):
        await interaction.response.defer(ephemeral=True)

        if self._get_channel_id() is None:
            await interaction.followup.send(
                "There is no QOTD channel set! Run /qotd-setup to set one!",
                ephemeral=True
            )
            return

        with open(QOTD_CHANNEL_FILE, "w", encoding="utf-8") as file:
            file.write(str(channel.id))

        await interaction.followup.send(
            f"✅ Successfully changed the QOTD service's channel to {channel.mention}!",
            ephemeral=True
        )

    @app_commands.command(
        name="start-qotd",
        description="Starts the Question of the Day service."
    )
    @app_commands.describe(
        forcepost="If yes, also posts a QOTD immediately, in addition to the daily schedule."
    )
    @app_commands.choices(
        forcepost=[
            app_commands.Choice(name="yes", value="yes"),
            app_commands.Choice(name="no", value="no"),
        ]
    )
    @app_commands.default_permissions(administrator=True)
    async def start_qotd(
        self,
        interaction: discord.Interaction,
        forcepost: app_commands.Choice[str] | None = None
    ):
        await interaction.response.defer(ephemeral=True)

        forcepost_value = forcepost.value if forcepost else "no"

        if self._get_channel_id() is None:
            await interaction.followup.send(
                "There is no QOTD channel set! Run /qotd-setup first.",
                ephemeral=True
            )
            return

        already_running = self.qotd_loop.is_running()

        if already_running and forcepost_value == "no":
            await interaction.followup.send(
                "The Question of the Day service is already running.",
                ephemeral=True
            )
            return

        if forcepost_value == "yes":
            await self._post_qotd()

        if not already_running:
            self.qotd_loop.start()

        scheduled_time = datetime.combine(datetime.now().date(), StartTime).strftime("%H:%M %Z")

        if already_running:
            message = f"✅ Posted a QOTD right now. The daily post at **{scheduled_time}** is still scheduled as normal."
        else:
            message = f"✅ Successfully started the Question of the Day service! It will post daily at **{scheduled_time}**."
            if forcepost_value == "yes":
                message += " I've also posted one just now."

        await interaction.followup.send(message, ephemeral=True)

    #@app_commands.command(
   #     name="submit-qotd-question",
   #     description="Submit a question of the day "
   # )
    #async def submit_question(self, interaction : discord.Interaction):
       # await interaction.response.send_message("Command is currently unavailable.", ephemeral=True)
        #remaining = _get_remaining_cooldown(interaction.user.id)

        #if remaining > 0:
        #    await interaction.response.send_message(
        #        f"⏳ You're on cooldown. You can submit another question in **{_format_cooldown(remaining)}**.",
        #        ephemeral=True,
       #     )
        #    return

      #  modal = QuestionSubmissionModal(self.bot)
      #  await interaction.response.send_modal(modal)

    @app_commands.command(
        name="schedule-qotd",
        description="Schedule a question of the day for a specific date."
    )
    @app_commands.default_permissions(manage_guild=True)
    async def schedule_qotd(self, interaction: discord.Interaction):
        modal = ScheduleQOTDModal(self.bot)
        await interaction.response.send_modal(modal)

    @app_commands.command(
        name="qotd-queue",
        description="View the upcoming Question of the Day schedule."
    )
    async def qotd_queue(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        queue = _read_json(QOTD_QUEUE_FILE, {})
        pages = _build_queue_pages(queue)

        if len(pages) > 1:
            view = QOTDQueueView(pages, interaction.user.id)
            message = await interaction.followup.send(embed=pages[0], view=view, ephemeral=True)
            view.message = message
        else:
            await interaction.followup.send(embed=pages[0], ephemeral=True)
    #@app_commands.command(
    #    name="setup-qotd-submissions",
    #    description="self explanatory"
   # )
    #@commands.is_owner()
    #async def post_submissions_message(self, interaction : discord.Interaction):
       # try:
          #  embed = discord.Embed(
      #          color=discord.Color.light_gray(),
          #      title="Submit your own question of the day!",
          #      description="By pressing the button below, you will be able to submit your own question of the day! \n \n Before your question is shown to the public, it has to be approved by a moderator first."
         #   )
         #   if interaction.channel.id == int(os.getenv("QOTD_SETUP_CHANNEL_ID")):
        #        await interaction.response.send_message(embed=embed, view=SubmitAQOTDMessageView(self.bot))
       # except (Exception, discord.HTTPException) as e:
        #    print(f"Error: {e}")

    


async def setup(bot: commands.Bot):
    await bot.add_cog(QOTD(bot))