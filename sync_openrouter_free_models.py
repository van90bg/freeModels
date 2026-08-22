"""
Chạy riêng (không nằm trong fetch_free_models.py). Lấy danh sách model FREE thật sự của
OpenRouter (pricing.prompt == 0 && pricing.completion == 0) từ endpoint public
https://openrouter.ai/api/v1/models (không cần key), ghi đè vào cột freeModels của dòng
có id chứa "openrouter" trong sheet "providers" — tương đương syncOpenRouterFreeModels_
trong bản GAS.

Cần GOOGLE_SERVICE_ACCOUNT_JSON có quyền Editor trên Sheet (không chỉ Viewer) để ghi được.

Chạy: python sync_openrouter_free_models.py
"""

import os
import sys

import gspread
import requests
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = "1X6YSk4Mcfjbiwj0Ekr4jNxRjYHjtXp3PZoW__0nnfm8"
SHEET_PROVIDERS = "providers"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def get_sheet_client():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not raw:
        raise RuntimeError("Thiếu biến môi trường GOOGLE_SERVICE_ACCOUNT_JSON")
    import json
    creds = Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
    return gspread.authorize(creds)


def main():
    resp = requests.get(
        "https://openrouter.ai/api/v1/models",
        headers={"User-Agent": "free-model-tool/1.0"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    free_ids = [
        m["id"] for m in data.get("data", [])
        if float((m.get("pricing") or {}).get("prompt", 1) or 0) == 0
        and float((m.get("pricing") or {}).get("completion", 1) or 0) == 0
    ]
    print(f"OpenRouter: {len(free_ids)} model free tìm thấy.")

    client = get_sheet_client()
    ws = client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_PROVIDERS)
    values = ws.get_all_values()
    header = [h.strip().lower() for h in values[0]]

    try:
        id_col = header.index("id")
        free_models_col = header.index("freemodels")
    except ValueError:
        print("Sheet providers thiếu cột id hoặc freeModels", file=sys.stderr)
        sys.exit(1)

    target_row = None
    for i, row in enumerate(values[1:], start=2):  # 1-indexed, row 1 = header
        cell = row[id_col] if id_col < len(row) else ""
        if "openrouter" in cell.lower():
            target_row = i
            break

    if target_row is None:
        print("Không tìm thấy dòng có id chứa 'openrouter' trong sheet providers", file=sys.stderr)
        sys.exit(1)

    ws.update_cell(target_row, free_models_col + 1, ", ".join(free_ids))
    print(f"Đã ghi {len(free_ids)} model vào sheet providers, dòng {target_row}")


if __name__ == "__main__":
    main()
