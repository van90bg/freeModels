"""
So sánh data/verified_models.json vừa tạo với bản trong commit trước (git HEAD).
Có thay đổi (model free mới / hết free) -> gửi thông báo qua SeaTalk webhook.
Không thay đổi -> không gửi. Lỗi gửi chỉ warning, không làm fail workflow.

Cần: secret SEATALK_WEBHOOK_URL (env khi chạy trong GitHub Actions).

Chạy: python notify_changes.py
"""

import json
import os
import subprocess
import sys
import urllib.request

DATA_FILE = "data/verified_models.json"
WEBHOOK_ENV = "SEATALK_WEBHOOK_URL"


def load_old_rows():
    """Đọc bản verified_models.json ở commit trước; None nếu không đọc được."""
    r = subprocess.run(["git", "show", f"HEAD:{DATA_FILE}"], capture_output=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: không parse được dữ liệu cũ: {e}")
        return None


def as_pairs(rows):
    return {(r["provider_id"], r["model_id"]) for r in rows}


def name_of(rows, pid, mid):
    for r in rows:
        if r["provider_id"] == pid and r["model_id"] == mid:
            return r.get("model") or mid
    return mid


def build_message(new_rows, old_rows, added, removed):
    lines = [f"Cập nhật free models — hiện có {len(new_rows)} model free"]
    if added:
        lines.append(f"\n[MỚI] {len(added)} model free mới:")
        lines += [f"+ {pid}: {name_of(new_rows, pid, mid)}" for pid, mid in sorted(added)]
    if removed:
        lines.append(f"\n[HẾT FREE] {len(removed)} model:")
        lines += [f"- {pid}: {name_of(old_rows, pid, mid)}" for pid, mid in sorted(removed)]
    return "\n".join(lines)


def send_seatalk(webhook, text):
    payload = json.dumps({"tag": "text", "text": {"content": text}}).encode()
    req = urllib.request.Request(
        webhook, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        body = r.read().decode(errors="replace")
        print(f"Đã gửi thông báo SeaTalk (HTTP {r.status}): {body[:120]}")


def main():
    new_rows = json.load(open(DATA_FILE, encoding="utf-8"))
    old_rows = load_old_rows()
    if old_rows is None:
        print("Không có dữ liệu cũ để so sánh, bỏ qua thông báo.")
        return

    old, new = as_pairs(old_rows), as_pairs(new_rows)
    added, removed = new - old, old - new
    if not added and not removed:
        print("Không có thay đổi so với lần chạy trước, không gửi thông báo.")
        return
    print(f"Phát hiện thay đổi: +{len(added)} mới, -{len(removed)} hết free")

    webhook = os.environ.get(WEBHOOK_ENV, "")
    if not webhook:
        print(f"WARNING: thiếu {WEBHOOK_ENV}, không gửi thông báo.")
        return
    try:
        send_seatalk(webhook, build_message(old_rows, new_rows, added, removed))
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: gửi SeaTalk lỗi (workflow vẫn tiếp tục): {e}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: notify_changes lỗi: {e}", file=sys.stderr)
