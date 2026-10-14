import base64
import json
import re
import zlib

LEDGER_MARKER = "<!-- harness:ledger -->"
MAX_COMMENT_LENGTH = 65000
MAX_UNCOMPRESSED_LENGTH = 50000


class LedgerTooLarge(Exception):
    pass


class LedgerCorrupt(Exception):
    pass


def render_ledger(snapshot: dict, summary_md: str) -> str:
    json_str = json.dumps(snapshot, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    # Escape < and > to ensure `-->` and `<!--` cannot appear
    json_str = json_str.replace("<", "\\u003c").replace(">", "\\u003e")

    if len(json_str) > MAX_UNCOMPRESSED_LENGTH:
        compressed_bytes = zlib.compress(json_str.encode("utf-8"), 9)
        b64_str = base64.b64encode(compressed_bytes).decode("ascii")
        payload = f"z:{b64_str}"
    else:
        payload = json_str

    result = f"{LEDGER_MARKER}\n{summary_md.rstrip()}\n\n<!-- harness:state v1 {payload} -->"
    if len(result) > MAX_COMMENT_LENGTH:
        raise LedgerTooLarge("Ledger comment exceeds the maximum allowed length.")

    return result


def parse_ledger(comment_body: str) -> dict | None:
    if LEDGER_MARKER not in comment_body:
        return None

    match = re.search(r"<!-- harness:state v1 (.*) -->", comment_body, re.DOTALL)
    if not match:
        return None

    payload = match.group(1).strip()

    try:
        if payload.startswith("z:"):
            b64_str = payload[2:]
            compressed_bytes = base64.b64decode(b64_str, validate=True)
            json_bytes = zlib.decompress(compressed_bytes)
            json_str = json_bytes.decode("utf-8")
        else:
            json_str = payload

        return json.loads(json_str)
    except Exception as e:
        raise LedgerCorrupt(f"Failed to parse ledger payload: {e}") from e


def is_ledger(body: str) -> bool:
    return body is not None and LEDGER_MARKER in body


def find_ledger(comments: list[dict]) -> dict | None:
    ledgers = [c for c in comments if "body" in c and is_ledger(c["body"])]
    if not ledgers:
        return None

    def sort_key(c):
        created = c.get("created_at", "")
        id_ = c.get("id", 0)
        return (created, id_)

    ledgers.sort(key=sort_key)
    return ledgers[0]


def render_event(date: str, issue: int, event: str, bullets: list[str]) -> str:
    lines = [f"**{date} · #{issue} {event}.**"]
    for bullet in bullets:
        lines.append(f"- {bullet}")
    return "\n".join(lines)
