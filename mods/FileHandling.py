import json
import os


def ReadFromJSONFile(path):
    with open(path, "r") as f:
        return json.load(f)

def ReadFromTextFile(path, whitespace : bool | None = False):
    if whitespace:
        with open(path, "r") as f:
            return f.read()
    else:
        with open(path, "r") as f:
            return f.read().strip()

def WriteToTextFile(path, content):
    with open(path, "w", encoding="utf-8") as file:
        file.write(str(content)) # convert the content into a string, no matter if it's a string or not.


def ReadFromJSONFile(path: str):
    """Reads JSON from a file and returns a list."""
    if not os.path.exists(path):
        return []

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return []


def WriteToJSON(path: str, data):
    """Writes data to a JSON file."""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        return "Success"

    except Exception as e:
        return f"Function execution failed. Error: {e}"

