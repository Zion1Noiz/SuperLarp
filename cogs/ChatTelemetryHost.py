import os
import json
from datetime import datetime, timezone

import discord
from discord.ext import commands

from mods import FileHandling

TEMP_FOLDER = "./temp"
LOG_JSON_FILE = os.path.join(TEMP_FOLDER, "chat-logs.json")

# Fallback bucket used when migrating old entries that don't carry
# enough info to know which "date" bucket they originally belonged to.
MIGRATED_BUCKET = "migrated-unknown-date"

# Fields expected on every message-log entry, with a default to fill in
# if the field is missing (used by the repair pass below).
ENTRY_DEFAULTS = {
    "id": None,
    "content": "",
    "attachments": [],
    "created_timestamp": None,
    "edited_timestamp": None,
    "author": {"id": None, "name": "unknown", "display_name": "unknown"},
    "mentions": [],
    "mention_everyone": False,
    "tts": False,
    "embeds": [],
    "channel": {"id": None, "name": "unknown"},
    "category": None,
    "message_type": "unknown",
}


class ChatTelemetry(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._ensure_file_exists()

    def _ensure_file_exists(self):
        os.makedirs(TEMP_FOLDER, exist_ok=True)

        if not os.path.exists(LOG_JSON_FILE) or os.path.getsize(LOG_JSON_FILE) == 0:
            with open(LOG_JSON_FILE, "w", encoding="utf-8") as file:
                json.dump({}, file, indent=4)
            return

        try:
            with open(LOG_JSON_FILE, "r", encoding="utf-8") as file:
                existing = json.load(file)
        except (json.JSONDecodeError, OSError) as e:
           
            print(f"Warning: could not parse {LOG_JSON_FILE} ({e}). "
                  f"Leaving original in place and creating a fresh log file.")
            backup_path = LOG_JSON_FILE + ".unparseable.bak"
            if not os.path.exists(backup_path):
                os.replace(LOG_JSON_FILE, backup_path)
            with open(LOG_JSON_FILE, "w", encoding="utf-8") as file:
                json.dump({}, file, indent=4)
            return

        if isinstance(existing, dict):
      
            repaired = self._validate_and_repair(existing)
            if repaired != existing:
                backup_path = LOG_JSON_FILE + ".pre-repair.bak"
                if not os.path.exists(backup_path):
                    with open(backup_path, "w", encoding="utf-8") as file:
                        json.dump(existing, file, indent=4)
                with open(LOG_JSON_FILE, "w", encoding="utf-8") as file:
                    json.dump(repaired, file, indent=4)
                print(f"Repaired {LOG_JSON_FILE}. Original preserved at {backup_path}.")
            return

        if isinstance(existing, list):
          
            print(f"Found {len(existing)} entries in old list format. Migrating...")
            migrated = {}

            for entry in existing:
                bucket = MIGRATED_BUCKET
                if isinstance(entry, dict):
                    ts = entry.get("created_timestamp")
                    if ts:
                        try:
                            dt = datetime.fromisoformat(ts)
                            bucket = dt.astimezone().strftime("%b%d%Y")
                        except ValueError:
                            pass
                migrated.setdefault(bucket, []).append(entry)

          
            backup_path = LOG_JSON_FILE + ".pre-migration.bak"
            if not os.path.exists(backup_path):
                with open(backup_path, "w", encoding="utf-8") as file:
                    json.dump(existing, file, indent=4)

            migrated = self._validate_and_repair(migrated)

            with open(LOG_JSON_FILE, "w", encoding="utf-8") as file:
                json.dump(migrated, file, indent=4)

            print(f"Migration complete. Original preserved at {backup_path}.")
            return

     
        print(f"Warning: {LOG_JSON_FILE} contained unexpected type "
              f"{type(existing).__name__}. Leaving file as-is; not touching it.")

    @staticmethod
    def _repair_entry(entry):
        """
        Try to fix a single message-log entry.
        Returns the (possibly fixed) entry, or None if it's unfixable and
        should be dropped.
        """
        if not isinstance(entry, dict):
           
            return None

        fixed = dict(entry)
        changed = False

        for field, default in ENTRY_DEFAULTS.items():
            if field not in fixed:
                fixed[field] = default
                changed = True
                continue

            value = fixed[field]

            if field in ("attachments", "mentions", "embeds") and not isinstance(value, list):
                fixed[field] = [] if value in (None, "") else [value]
                changed = True
            elif field in ("author", "channel") and not isinstance(value, dict):
                fixed[field] = dict(default)
                changed = True
            elif field == "mention_everyone" and not isinstance(value, bool):
                fixed[field] = bool(value)
                changed = True
            elif field == "tts" and not isinstance(value, bool):
                fixed[field] = bool(value)
                changed = True

        if fixed.get("id") is None and not fixed.get("content") and not fixed.get("attachments"):
            return None

        if changed:
            print(f"Repaired malformed entry (id={fixed.get('id')}).")

        return fixed

    @classmethod
    def _validate_and_repair(cls, data: dict) -> dict:
        """
        Walk a {date_key: [entries]} dict, fixing what can be fixed and
        dropping what can't. A date key whose value isn't a usable list
        (and can't be coerced into one) is deleted entirely.
        """
        repaired = {}

        for key, value in data.items():
            if isinstance(value, dict):
               
                value = [value]
            elif not isinstance(value, list):
                print(f"Dropping key '{key}': value is {type(value).__name__}, "
                      f"not a list of entries and not fixable.")
                continue

            fixed_entries = []
            dropped = 0
            for entry in value:
                fixed_entry = cls._repair_entry(entry)
                if fixed_entry is None:
                    dropped += 1
                else:
                    fixed_entries.append(fixed_entry)

            if dropped:
                print(f"Key '{key}': dropped {dropped} unfixable entr"
                      f"{'y' if dropped == 1 else 'ies'} out of {len(value)}.")

            if fixed_entries:
                repaired[key] = fixed_entries
            else:
                print(f"Dropping key '{key}': no valid entries remained after repair.")

        return repaired

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        message_log_json = {
            "id": message.id,
            "content": message.content,
            "attachments": [a.url for a in message.attachments],
            "created_timestamp": message.created_at.isoformat(),
            "edited_timestamp": message.edited_at.isoformat() if message.edited_at else None,
            "author": {
                "id": message.author.id,
                "name": str(message.author),
                "display_name": message.author.display_name,
            },
            "mentions": [m.id for m in message.mentions],
            "mention_everyone": message.mention_everyone,
            "tts": message.tts,
            "embeds": [embed.to_dict() for embed in message.embeds],
            "channel": {
                "id": message.channel.id,
                "name": message.channel.name,
            },
            "category": message.channel.category.name if message.channel.category else None,
            "message_type": str(message.type),
        }

        try:
            read_json = FileHandling.ReadFromJSONFile(LOG_JSON_FILE)

            if not isinstance(read_json, dict):
                
                print(f"Error: {LOG_JSON_FILE} is not a dict "
                      f"(got {type(read_json).__name__}). Skipping write to avoid data loss.")
                return

            
            read_json = self._validate_and_repair(read_json)

            formatted_date = datetime.now(timezone.utc).astimezone().strftime("%b%d%Y")

            read_json.setdefault(formatted_date, [])
            read_json[formatted_date].append(message_log_json)

            FileHandling.WriteToJSON(LOG_JSON_FILE, read_json)

            print(f"Saved message under {formatted_date}")

        except Exception as e:
            print(f"Error saving message: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(ChatTelemetry(bot))