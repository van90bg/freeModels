"""
Quet toan bo catalog models.dev, lay danh sach cac model free
(cost.input == 0 va cost.output == 0, chat-capable), ghi vao Google Sheet
(tab "Model") va gui thong bao qua Telegram khi co model free MOI xuat hien.

Khong verify qua API key. Chi doc catalog cong khai + ghi ket qua ra sheet.

Trang thai model free da biet duoc lay TU SHEET (cot Provider ID + Model ID
cua tab "Model"): lan chay sau chi bao nhung model CHUA co trong sheet.

Can secret (env khi chay trong GitHub Actions):
  GOOGLE_SERVICE_ACCOUNT_JSON  JSON service account co quyen Editor tren sheet
  TELEGRAM_BOT_TOKEN           token cua bot @BotFather
  TELEGRAM_CHAT_ID             id cuoc tro chuyen nhan thong bao

Chay: python check_free_models.py
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

CATALOG_URL = "https://models.dev/api.json"

SPREADSHEET_ID = "1X6YSk4Mcfjbiwj0Ekr4jNxRjYHjtXp3PZoW__0nnfm8"
SHEET_MODEL = "Model"

MODEL_SHEET_HEADERS = [
    "Provider", "Provider ID", "Model", "Model ID", "Family",
    "Tool Call", "Reasoning", "Attachment", "Structured Output", "Temperature",
    "Input Cost/1M", "Output Cost/1M",
    "Context Limit", "Input Limit", "Output Limit",
    "Modalities In", "Modalities Out",
    "Open Weights", "Knowledge Cutoff", "Release Date", "Last Updated", "Status",
    "Probe", "Last Synced",
]

HEADER_TO_FIELD = {
    "Provider": "provider",
    "Provider ID": "provider_id",
    "Model": "model",
    "Model ID": "model_id",
    "Family": "family",
    "Tool Call": "tool_call",
    "Reasoning": "reasoning",
    "Attachment": "attachment",
    "Structured Output": "structured_output",
    "Temperature": "temperature",
    "Input Cost/1M": "input_cost_1m",
    "Output Cost/1M": "output_cost_1m",
    "Context Limit": "context_limit",
    "Input Limit": "input_limit",
    "Output Limit": "output_limit",
    "Modalities In": "modalities_in",
    "Modalities Out": "modalities_out",
    "Open Weights": "open_weights",
    "Knowledge Cutoff": "knowledge_cutoff",
    "Release Date": "release_date",
    "Last Updated": "last_updated",
    "Status": "status",
    "Probe": "probe",
    "Last Synced": "last_synced",
}

MAX_MESSAGE_CHARS = 4000  # dung luong an toan duoi gioi han 4096 cua Telegram


def fetch_catalog() -> dict:
    req = urllib.request.Request(CATALOG_URL, headers={"User-Agent": "free-model-tool/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def is_free(cost: dict | None) -> bool:
    if not cost:
        return False
    return (cost.get("input") or 0) == 0 and (cost.get("output") or 0) == 0


def is_chat_capable(model: dict) -> bool:
    mod = model.get("modalities") or {}
    ins = mod.get("input") or []
    outs = mod.get("output") or []
    if outs and "text" not in outs:
        return False
    if ins and "text" not in ins:
        return False
    if model.get("tool_call") is False:
        return False
    return True


def bool_text(v) -> str:
    if v is True:
        return "Yes"
    if v is False:
        return "No"
    return ""


def collect_free(catalog: dict) -> dict:
    """Tra ve {provider_id: {model_id: metadata}} cho moi model free."""
    out: dict[str, dict[str, dict]] = {}
    for pid, prov in catalog.items():
        for mid, meta in (prov.get("models") or {}).items():
            if not is_free(meta.get("cost")):
                continue
            if not is_chat_capable(meta):
                continue
            out.setdefault(pid, {})[mid] = meta
    return out


def build_rows(free: dict, catalog: dict) -> list[dict]:
    rows: list[dict] = []
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for pid, mods in free.items():
        prov_name = str((catalog.get(pid) or {}).get("name") or "").strip() or pid
        for mid, meta in mods.items():
            cost = meta.get("cost", {}) or {}
            limit = meta.get("limit", {}) or {}
            mod = meta.get("modalities", {}) or {}
            rows.append({
                "provider": prov_name,
                "provider_id": pid,
                "model": str(meta.get("name") or "").strip() or mid,
                "model_id": mid,
                "family": meta.get("family", ""),
                "tool_call": bool_text(meta.get("tool_call")),
                "reasoning": bool_text(meta.get("reasoning")),
                "attachment": bool_text(meta.get("attachment")),
                "structured_output": bool_text(meta.get("structured_output")),
                "temperature": bool_text(meta.get("temperature")),
                "input_cost_1m": cost.get("input", 0),
                "output_cost_1m": cost.get("output", 0),
                "context_limit": limit.get("context", ""),
                "input_limit": limit.get("input", ""),
                "output_limit": limit.get("output", ""),
                "modalities_in": ", ".join(mod.get("input", []) or []),
                "modalities_out": ", ".join(mod.get("output", []) or []),
                "open_weights": bool_text(meta.get("open_weights")),
                "knowledge_cutoff": meta.get("knowledge", ""),
                "release_date": meta.get("release_date", ""),
                "last_updated": meta.get("last_updated", ""),
                "status": "free",
                "probe": "",
                "last_synced": now,
            })
    rows.sort(key=lambda r: (r["provider_id"], r["model_id"]))
    return rows


def get_sheet_client():
    import gspread
    from google.oauth2.service_account import Credentials
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not raw:
        return None
    creds = Credentials.from_service_account_info(
        json.loads(raw), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds)


def write_sheet(rows: list[dict]) -> None:
    client = get_sheet_client()
    if client is None:
        print("WARNING: thieu GOOGLE_SERVICE_ACCOUNT_JSON, bo qua ghi sheet.")
        return
    try:
        ss = client.open_by_key(SPREADSHEET_ID)
        try:
            ws = ss.worksheet(SHEET_MODEL)
        except gspread.WorksheetNotFound:
            ws = ss.add_worksheet(title=SHEET_MODEL, rows=max(len(rows) + 10, 100),
                                  cols=len(MODEL_SHEET_HEADERS))
        data = [MODEL_SHEET_HEADERS] + [
            [r.get(HEADER_TO_FIELD[h], "") for h in MODEL_SHEET_HEADERS] for r in rows
        ]
        ws.clear()
        ws.update(data, value_input_option="RAW")
        print(f"Da ghi {len(rows)} dong vao sheet '{SHEET_MODEL}'")
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: ghi sheet loi: {e}")


def load_existing_pairs() -> set[tuple[str, str]] | None:
    """Doc (provider_id, model_id) da co trong sheet. None neu khong doc duoc."""
    client = get_sheet_client()
    if client is None:
        return None
    try:
        ws = client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_MODEL)
        vals = ws.get_all_values()
        if len(vals) < 2:
            return set()
        header = [str(h).strip().lower() for h in vals[0]]
        pid_i = header.index("provider id") if "provider id" in header else -1
        mid_i = header.index("model id") if "model id" in header else -1
        if mid_i < 0:
            return set()
        out = set()
        for row in vals[1:]:
            pid = row[pid_i] if 0 <= pid_i < len(row) else ""
            mid = row[mid_i] if 0 <= mid_i < len(row) else ""
            if mid:
                out.add((pid, mid))
        return out
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: khong doc duoc sheet de doi chieu: {e}")
        return None


def as_pairs(state: dict) -> set[tuple[str, str]]:
    return {(pid, mid) for pid, mods in state.items() for mid in mods}


def names_of(state: dict, pid: str, mid: str) -> str:
    return state.get(pid, {}).get(mid, mid)


def build_messages(added: set[tuple[str, str]], new_state: dict) -> list[str]:
    header = f"\U0001f195 {len(added)} model free moi tren models.dev:"
    lines: list[str] = []
    for pid, mid in sorted(added):
        name = names_of(new_state, pid, mid)
        lines.append(f"\u2022 `{pid}/{mid}` \u2014 {name}")
    blocks = [header]
    msgs: list[str] = []
    for line in lines:
        if len("\n".join(blocks)) + len(line) + 1 > MAX_MESSAGE_CHARS:
            msgs.append("\n".join(blocks))
            blocks = [line]
        else:
            blocks.append(line)
    msgs.append("\n".join(blocks))
    return msgs


def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = (
        f"https://api.telegram.org/bot{token}/sendMessage"
        f"?chat_id={urllib.parse.quote(chat_id)}"
        f"&parse_mode=Markdown"
        f"&text={urllib.parse.quote(text)}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "free-model-tool/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        print(f"Da gui Telegram (HTTP {r.status})")


def main() -> None:
    catalog = fetch_catalog()
    free = collect_free(catalog)
    new_state = {
        pid: {mid: (str(m.get("name") or "").strip() or mid) for mid, m in mods.items()}
        for pid, mods in free.items()
    }
    current = as_pairs(new_state)

    existing = load_existing_pairs()

    rows = build_rows(free, catalog)
    write_sheet(rows)

    if existing is None:
        print("Khong the doi chieu voi sheet, bo qua gui thong bao.")
        return

    added = current - existing
    if not added:
        print("Khong co model free moi so voi sheet hien tai.")
        return

    print(f"Phat hien {len(added)} model free moi.")
    token = (os.environ.get("TELEGRAM_BOT_TOKEN", "") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID", "") or "").strip()
    if not token or not chat_id:
        print("WARNING: thieu TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID, bo qua gui thong bao.")
        return
    for msg in build_messages(added, new_state):
        try:
            send_telegram(token, chat_id, msg)
        except Exception as e:  # noqa: BLE001
            print(f"WARNING: gui Telegram loi: {e}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)
