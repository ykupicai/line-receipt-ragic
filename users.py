"""
使用者身分資料(多人共用一個 LINE 官方帳號時,每人一份)。
以 LINE user_id 為鍵,存成 users.json(永久保存,重啟不丟)。
每筆 profile:{"name":姓名, "title":職稱, "office":上班地點,
              "ragic_key":本人的 Ragic API 金鑰(可空 → 用共用金鑰)}
(接案團隊由 Ragic 依專案名稱自動連動,不在此存。)
"""
import json
import os
import threading

# 容器部署時用 USERS_FILE 指到掛載的資料夾(須掛資料夾,單檔掛載會讓 os.replace 失敗);沒設則維持原位置
_PATH = os.environ.get("USERS_FILE") or os.path.join(os.path.dirname(__file__), "users.json")
_lock = threading.Lock()
_users: dict[str, dict] = {}

FIELDS = ("name", "title", "office", "ragic_key")


def _load():
    global _users
    if os.path.exists(_PATH):
        try:
            with open(_PATH, encoding="utf-8") as f:
                _users = json.load(f)
        except Exception:
            _users = {}


def _save():
    # 已在 _lock 內呼叫
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_users, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _PATH)   # 原子替換,避免寫一半壞檔


def get(user_id: str) -> dict | None:
    return _users.get(user_id)


def is_registered(user_id: str) -> bool:
    return user_id in _users


def save_user(user_id: str, profile: dict):
    with _lock:
        _users[user_id] = {k: profile.get(k, "") for k in FIELDS}
        _save()


def delete(user_id: str):
    with _lock:
        if _users.pop(user_id, None) is not None:
            _save()


_load()
