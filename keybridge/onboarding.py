"""One-time connection selection in a human's private terminal."""
from .config import PROVIDERS, Policy, write_settings
from .errors import BridgeError
from .vault import field_choices


def safe_label(value):
    return "".join(c if c.isprintable() else "?" for c in str(value))[:100]


def choose(prompt, choices, label=str):
    for index, choice in enumerate(choices, 1):
        print(f"  {index}. {safe_label(label(choice))}")
    try:
        index = int(input(prompt + " ")) - 1
        if 0 <= index < len(choices):
            return choices[index]
    except ValueError:
        pass
    raise BridgeError("Choose a number from the list. Run start --configure to try again.")


def configure(directory, vault):
    print("Connect a service. Key values stay out of this display.")
    provider = choose("Service number:", PROVIDERS)
    items = vault.items()
    if not items:
        raise BridgeError("No usable vault items were found. Save the API key in Bitwarden first.")
    item = choose("Vault item number:", items, lambda i: i.get("name", "Unnamed item"))
    fields = field_choices(item)
    field = fields[0] if len(fields) == 1 else choose("API-key field number:", fields)
    # Only the reference survives setup; do not store a copy of the vault entry.
    item_id = item["id"]
    del item, items
    models = tuple(x.strip() for x in input("Allowed model IDs (comma separated): ").split(","))
    policy = Policy(provider, models)
    print("Allow up to 10 attempts per session, 1 hour, and 1024 output tokens per call.")
    print("Text will go to this provider. Its usual charges and data policies apply.")
    print("These are request limits, not a dollar spending cap. All local agents share them.")
    if input("Save these permissions? Type yes: ").strip().lower() != "yes":
        raise BridgeError("Connection was not saved. Run start again when you are ready.")
    write_settings(directory, policy, item_id, field)
    return policy, item_id, field
