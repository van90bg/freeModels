"""
Lấy catalog model từ models.dev (https://models.dev/api.json), đọc danh sách provider +
apiKey từ Google Sheet (cùng cấu trúc sheet "providers"/".env" mà bản GAS gốc dùng),
lọc model free theo cost trong catalog, verify thật bằng POST /chat/completions với key thật.

Chỉ cần 1 GitHub Secret (GOOGLE_SERVICE_ACCOUNT_JSON) — thêm provider mới chỉ cần thêm
dòng trong Sheet, không cần đụng repo/secrets.

Setup 1 lần:
  1. Google Cloud Console -> tạo Service Account -> bật Google Sheets API
  2. Tạo key JSON cho service account, copy toàn bộ nội dung JSON
  3. Share Google Sheet (Spreadsheet ID bên dưới) cho email service account
     (dạng xxx@xxx.iam.gserviceaccount.com), quyền Viewer là đủ để đọc,
     Editor nếu muốn script tự ghi ngược kết quả vào tab "Model"
  4. Paste JSON đó làm 1 GitHub Secret tên GOOGLE_SERVICE_ACCOUNT_JSON

Chạy: python fetch_free_models.py
Output: data/verified_models.json + .csv (ghi đè, để commit lại vào repo cho GH Pages đọc)
"""

import concurrent.futures
import csv
import json
import os
import sys
import time
from pathlib import Path

import gspread
import requests
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = "1X6YSk4Mcfjbiwj0Ekr4jNxRjYHjtXp3PZoW__0nnfm8"
SHEET_PROVIDERS = "providers"
SHEET_ENV = ".env"
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

MODELS_DEV_API_URL = "https://models.dev/api.json"
OUTPUT_DIR = Path(__file__).parent / "data"
OUTPUT_JSON = OUTPUT_DIR / "verified_models.json"
OUTPUT_CSV = OUTPUT_DIR / "verified_models.csv"

PROBE_MSG = "Reply with exactly three words."
PROBE_MAX_TOKENS = 8
REQUEST_TIMEOUT_S = 15
MAX_WORKERS = 20
RATE_LIMIT_MAX_RETRIES = 2
RATE_LIMIT_BACKOFF_S = 2.0

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

FIELDNAMES = [
    "provider", "provider_id", "model", "model_id", "family",
    "tool_call", "reasoning", "attachment", "structured_output", "temperature",
    "input_cost_1m", "output_cost_1m",
    "context_limit", "input_limit", "output_limit",
    "modalities_in", "modalities_out",
    "open_weights", "knowledge_cutoff", "release_date", "last_updated", "status",
    "probe", "last_synced",
]


def get_sheet_client():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not raw:
        raise RuntimeError("Thiếu biến môi trường GOOGLE_SERVICE_ACCOUNT_JSON")
    creds_dict = json.loads(raw)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def _rows_to_dicts(values):
    """values[0] là header. Trả list dict, key = header lowercase-trimmed."""
    if len(values) < 2:
        return []
    header = [str(h).strip().lower() for h in values[0]]
    out = []
    for row in values[1:]:
        row = row + [""] * (len(header) - len(row))  # pad nếu thiếu cột cuối
        out.append(dict(zip(header, row)))
    return out


def load_env_map(client):
    try:
        ws = client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_ENV)
    except gspread.WorksheetNotFound:
        return {}
    rows = _rows_to_dicts(ws.get_all_values())
    env_map = {}
    for r in rows:
        name = str(r.get("name", "")).strip()
        key = str(r.get("apikey", "")).strip()
        if not name or not key:
            continue
        env_map[name] = key
        env_map[name.lower()] = key
    return env_map


def load_providers(client):
    ws = client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_PROVIDERS)
    rows = _rows_to_dicts(ws.get_all_values())
    env_map = load_env_map(client)

    out = []
    for r in rows:
        pid = str(r.get("id", "")).strip()
        if not pid:
            continue
        raw_key = str(r.get("apikey", "")).strip()
        if raw_key.startswith("{env:") and raw_key.endswith("}"):
            env_name = raw_key[5:-1].strip()
            raw_key = env_map.get(env_name) or env_map.get(env_name.lower()) or ""
        out.append({
            "id": pid,
            "name": str(r.get("name", "")).strip() or pid,
            "baseUrl": str(r.get("baseurl", "")).strip().rstrip("/"),
            "apiKey": raw_key,
            "exclude": str(r.get("exclude", "")).strip(),
            "freeModels": str(r.get("freemodels", "")).strip(),
            "liveModels": str(r.get("livemodels", "")).strip() == "1",
        })
    return out


def fetch_catalog():
    resp = requests.get(MODELS_DEV_API_URL, timeout=30, headers={"User-Agent": "free-model-tool/1.0"})
    resp.raise_for_status()
    return resp.json()


def is_free(cost):
    if not cost:
        return False
    return (cost.get("input") or 0) == 0 and (cost.get("output") or 0) == 0


def is_chat_capable(model):
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


def is_excluded(model_id, exclude_str):
    if not exclude_str:
        return False
    tokens = [t.strip() for t in exclude_str.replace(";", ",").split(",") if t.strip()]
    return any(t in model_id for t in tokens)


def fetch_live_models(base_url, api_key):
    try:
        resp = requests.get(
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": "free-model-tool/1.0"},
            timeout=REQUEST_TIMEOUT_S,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
        return ids or None
    except requests.RequestException:
        return None


def build_candidates(prov, meta):
    """Trả list (model_id, model_meta_dict). model_meta_dict rỗng nếu không có catalog (live/manual)."""
    if prov["liveModels"]:
        live_ids = fetch_live_models(prov["baseUrl"], prov["apiKey"]) if prov["baseUrl"] else None
        if live_ids:
            print(f"[{prov['id']}] {len(live_ids)} live models từ /models")
            candidates = [(mid, {}) for mid in live_ids]
        else:
            candidates = []
    elif meta.get("models"):
        candidates = []
        for model_id, model in meta["models"].items():
            if not is_free(model.get("cost")):
                continue
            if not is_chat_capable(model):
                continue
            candidates.append((model_id, model))
    else:
        ids = [s.strip() for s in prov["freeModels"].replace(";", ",").split(",") if s.strip()]
        candidates = [(mid, {}) for mid in ids]

    # exclude áp dụng đồng nhất cho MỌI nguồn
    return [(mid, m) for mid, m in candidates if not is_excluded(mid, prov["exclude"])]


def probe_one(base_url, model_id, api_key):
    url = f"{base_url}/chat/completions"
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": PROBE_MSG}],
        "max_tokens": PROBE_MAX_TOKENS,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {api_key}", "User-Agent": "free-model-tool/1.0"}

    for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT_S)
        except requests.Timeout:
            return "timeout"
        except requests.RequestException:
            return "error"

        code = resp.status_code
        if code == 200:
            return "ok"
        if code == 429:
            if attempt < RATE_LIMIT_MAX_RETRIES:
                time.sleep(RATE_LIMIT_BACKOFF_S * (attempt + 1))
                continue
            return "rate-limited"
        if code in (401, 403):
            return "auth"
        if code == 402:
            return "paid"
        if code == 404:
            return "notfound"
        if code in (400, 422):
            body = resp.text[:300].lower()
            if "model" in body and any(k in body for k in ("not found", "not exist", "unknown", "no such", "does not")):
                return "notfound"
            return "error"
        if code in (405, 501, 502, 503):
            return "notsupported"
        return "error"
    return "error"


def bool_text(v):
    if v is True:
        return "Yes"
    if v is False:
        return "No"
    return ""


def write_model_sheet(client, rows):
    """Ghi đè sheet Model — tương đương writeSheet_() trong bản GAS gốc.
    Không fail toàn bộ script nếu thiếu quyền Editor, chỉ log warning."""
    try:
        ss = client.open_by_key(SPREADSHEET_ID)
        try:
            ws = ss.worksheet(SHEET_MODEL)
        except gspread.WorksheetNotFound:
            ws = ss.add_worksheet(title=SHEET_MODEL, rows=max(len(rows) + 10, 100), cols=len(MODEL_SHEET_HEADERS))

        data = [MODEL_SHEET_HEADERS] + [
            [
                r["provider"], r["provider_id"], r["model"], r["model_id"], r["family"],
                r["tool_call"], r["reasoning"], r["attachment"], r["structured_output"], r["temperature"],
                r["input_cost_1m"], r["output_cost_1m"],
                r["context_limit"], r["input_limit"], r["output_limit"],
                r["modalities_in"], r["modalities_out"],
                r["open_weights"], r["knowledge_cutoff"], r["release_date"], r["last_updated"], r["status"],
                r["probe"], r["last_synced"],
            ]
            for r in rows
        ]

        ws.clear()
        ws.update(data, value_input_option="RAW")
        ws.format(f"A1:{gspread.utils.rowcol_to_a1(1, len(MODEL_SHEET_HEADERS))}", {
            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
            "backgroundColor": {"red": 0.18, "green": 0.33, "blue": 0.59},
            "horizontalAlignment": "CENTER",
        })
        ws.freeze(rows=1)
        print(f"Đã ghi {len(rows)} dòng vào sheet '{SHEET_MODEL}'")
    except gspread.exceptions.APIError as e:
        print(f"WARNING: không ghi được sheet '{SHEET_MODEL}' (kiểm tra quyền Editor của service account): {e}")


def main():
    client = get_sheet_client()
    providers = load_providers(client)
    print(f"Providers: {len(providers)}")
    catalog = fetch_catalog()

    rows = []
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    for prov in providers:
        if not prov["apiKey"]:
            print(f"[{prov['id']}] không có apiKey (kiểm tra cột apiKey / tab .env), bỏ qua")
            continue

        meta = catalog.get(prov["id"], {})
        base_url = prov["baseUrl"] or meta.get("api", "")
        if not base_url:
            print(f"[{prov['id']}] không có baseUrl (sheet lẫn models.dev), bỏ qua")
            continue
        prov["baseUrl"] = base_url.rstrip("/")

        candidates = build_candidates(prov, meta)
        if not candidates:
            continue
        print(f"[{prov['id']}] {len(candidates)} candidates, probing (concurrent, timeout={REQUEST_TIMEOUT_S}s)...")

        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(probe_one, prov["baseUrl"], mid, prov["apiKey"]): (mid, m)
                for mid, m in candidates
            }
            for fut in concurrent.futures.as_completed(futures):
                model_id, model_meta = futures[fut]
                status = fut.result()
                if status != "ok":
                    if status == "rate-limited":
                        print(f"  [rate-limited] {prov['id']} {model_id}")
                    continue

                cost = model_meta.get("cost", {}) or {}
                limit = model_meta.get("limit", {}) or {}
                modalities = model_meta.get("modalities", {}) or {}
                rows.append({
                    "provider": meta.get("name") or prov["name"],
                    "provider_id": prov["id"],
                    "model": model_meta.get("name", model_id),
                    "model_id": model_id,
                    "family": model_meta.get("family", ""),
                    "tool_call": bool_text(model_meta.get("tool_call")),
                    "reasoning": bool_text(model_meta.get("reasoning")),
                    "attachment": bool_text(model_meta.get("attachment")),
                    "structured_output": bool_text(model_meta.get("structured_output")),
                    "temperature": bool_text(model_meta.get("temperature")),
                    "input_cost_1m": cost.get("input", 0),
                    "output_cost_1m": cost.get("output", 0),
                    "context_limit": limit.get("context", ""),
                    "input_limit": limit.get("input", ""),
                    "output_limit": limit.get("output", ""),
                    "modalities_in": ", ".join(modalities.get("input", []) or []),
                    "modalities_out": ", ".join(modalities.get("output", []) or []),
                    "open_weights": bool_text(model_meta.get("open_weights")),
                    "knowledge_cutoff": model_meta.get("knowledge", ""),
                    "release_date": model_meta.get("release_date", ""),
                    "last_updated": model_meta.get("last_updated", ""),
                    "status": model_meta.get("status", ""),
                    "probe": status,
                    "last_synced": now,
                })

    rows.sort(key=lambda r: (r["provider"], r["model_id"]))
    print(f"Verified: {len(rows)} model free.")

    OUTPUT_DIR.mkdir(exist_ok=True)
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    with open(OUTPUT_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Đã ghi {OUTPUT_JSON} và {OUTPUT_CSV}")

    write_model_sheet(client, rows)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)
