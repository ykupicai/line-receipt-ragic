"""
階段 3:LINE 收圖 → Claude 辨識單據 → 回傳欄位 → 使用者回「確認」→ 寫入 Ragic(含照片附件)
跑法:uvicorn server:app --port 8000
Ragic 設定未齊(.env 的 RAGIC_* 或 ragic_fields.py)時,自動退回階段 2 行為(只辨識不寫入)。
"""
import os
import re
import base64
import json
import threading
import traceback
import time

from fastapi import FastAPI, Request, HTTPException
from dotenv import load_dotenv

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, MessagingApiBlob,
    ReplyMessageRequest, TextMessage, QuickReply, QuickReplyItem, MessageAction,
)
from linebot.v3.webhooks import MessageEvent, ImageMessageContent, TextMessageContent

import anthropic

load_dotenv()

import ragic  # noqa: E402  須在 load_dotenv() 之後 import 才讀得到 RAGIC_* 環境變數
import ragic_fields as RF  # noqa: E402
import users  # noqa: E402  使用者身分(首次登記)

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
# anthropic 會自動讀環境變數 ANTHROPIC_API_KEY,不需手動帶

config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
claude = anthropic.Anthropic()
app = FastAPI()

# 用便宜的模型先試;想更省可換 "claude-haiku-4-5-20251001"
MODEL = "claude-sonnet-4-6"

EXTRACT_PROMPT = """你是出差單據辨識助手。請看這張圖片(可能是高鐵票、捷運票、計程車收據、機票、住宿發票、雜費收據等),
只回傳一個 JSON 物件,不要任何其他文字、不要 markdown 標記。欄位如下:
{
  "日期": "YYYY/MM/DD,看不出就 null",
  "類別": "必須是這幾個之一:高鐵 / 捷運 / 計程車 / 飛機 / 住宿費 / 雜費 / 其他。判斷不出來就填 其他",
  "支出項目": "單據上的品名,例如 高鐵車票、計程車車資、住宿費(供摘要用)",
  "起點": "交通類才填,否則 null。只寫站名/地點,不要加「高鐵」「台鐵」「捷運」等交通工具前綴(例:台南車站,不是 高鐵台南車站)",
  "終點": "交通類才填,否則 null。同上,不要加交通工具前綴",
  "數量": 數字(預設 1),
  "單價": 數字,
  "小計": 數字,
  "發票號碼": "有就填,否則 null",
  "是否電子發票": "只有單據上明確出現『電子發票』字樣才填 true,否則一律 false",
  "幣別": "預設 TWD"
}
類別判斷:高鐵票→高鐵、捷運/台鐵/公車→捷運、計程車/小黃→計程車、機票/登機證→飛機、
旅館住宿→住宿費、餐費或其他雜支→雜費,其餘→其他。
※ 報帳金額一律取「含稅總額」(總計/合計)。若發票把未稅與含稅分開列(如三聯式統一發票有
  「銷售額」「營業稅」「總計」),取「總計(含稅)」、不要取未稅銷售額;並把「單價」與「小計」
  都填總計值(例:銷售額6190+營業稅310=總計6500 → 單價、小計都填 6500)。金額常為手寫,逐位小心辨識。
金額一律用純數字、不要逗號或貨幣符號。看不清楚或單據上沒有的欄位填 null。"""

# 代墊款用:不分交通類別,只抓品名/金額(科目由使用者選)
EXTRACT_PROMPT_DM = """你是代墊款單據辨識助手。請看這張圖片(可能是加油發票、餐費收據、郵寄收據、停車費、購物發票等),
只回傳一個 JSON 物件,不要任何其他文字、不要 markdown 標記。欄位如下:
{
  "日期": "YYYY/MM/DD,看不出就 null",
  "摘要": "簡短品名或用途,2~8 字,例如 油資、餐費、停車費、郵寄、辦公用品",
  "數量": 數字(預設 1),
  "單價": 數字,
  "小計": 數字,
  "發票號碼": "有就填,否則 null",
  "是否電子發票": "只有單據上明確出現『電子發票』字樣才填 true,否則一律 false"
}
※ 報帳金額一律取「含稅總額」(總計/合計)。若發票把未稅與含稅分開列(如三聯式統一發票有
  「銷售額」「營業稅」「總計」),取「總計(含稅)」、不要取未稅銷售額;並把「單價」與「小計」
  都填總計值(例:銷售額6190+營業稅310=總計6500 → 單價、小計都填 6500)。金額常為手寫,逐位小心辨識。
金額一律用純數字、不要逗號或貨幣符號。看不清楚或單據上沒有的欄位填 null。"""


# Claude 暫時性錯誤(過載 529 / 限流 429 / 5xx / 連線中斷)→ 自動退避重試,
# 避免 Anthropic 一時過載就讓使用者看到「辨識失敗」。
_RETRYABLE = (
    anthropic.OverloadedError,        # 529 overloaded
    anthropic.RateLimitError,         # 429
    anthropic.InternalServerError,    # 500/5xx
    anthropic.APIConnectionError,     # 連線/逾時
)
_MAX_TRIES = 5          # 共試 5 次
_BACKOFF = [2, 5, 10, 20]   # 各次重試前等待秒數(指數退避)


def extract_receipt(image_bytes: bytes, prompt: str = EXTRACT_PROMPT) -> dict:
    b64 = base64.standard_b64encode(image_bytes).decode()
    last_err = None
    for attempt in range(_MAX_TRIES):
        try:
            msg = claude.messages.create(
                model=MODEL,
                max_tokens=600,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image",
                         "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                        {"type": "text", "text": prompt},
                    ],
                }],
            )
            text = "".join(b.text for b in msg.content if b.type == "text").strip()
            text = text.replace("```json", "").replace("```", "").strip()
            return json.loads(text)
        except _RETRYABLE as e:
            last_err = e
            if attempt < _MAX_TRIES - 1:
                wait = _BACKOFF[attempt]
                print(f"[辨識重試] 第 {attempt + 1} 次遇到 {type(e).__name__},{wait}s 後重試…", flush=True)
                time.sleep(wait)
            else:
                print(f"[辨識重試] 已重試 {_MAX_TRIES} 次仍失敗:{type(e).__name__}", flush=True)
    # 重試用盡仍失敗 → 拋出讓上層回友善訊息
    raise last_err


def _mode_buttons() -> QuickReply:
    """閒置時:選出差 / 代墊款 / 追加既有單號。"""
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="🚆 出差", text="出差")),
        QuickReplyItem(action=MessageAction(label="🧾 代墊款", text="代墊款")),
        QuickReplyItem(action=MessageAction(label="➕ 追加", text="追加")),
    ])


def _confirm_buttons() -> QuickReply:
    """填寫中:確認或取消。"""
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="✅ 確認", text="確認")),
        QuickReplyItem(action=MessageAction(label="✖️ 取消", text="取消")),
    ])


def _cancel_button() -> QuickReply:
    """等待輸入單號時:只給取消。"""
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="✖️ 取消", text="取消")),
    ])


def _buttons_for(user_id: str | None):
    """依使用者狀態決定要顯示哪組按鈕。"""
    if not user_id or user_id in reg:
        return None                    # 系統訊息/登記中:不附按鈕
    if user_id in awaiting_no:
        return _cancel_button()        # 等待輸入追加單號:只給取消
    if _in_entry(user_id):
        return _confirm_buttons()      # 已選模式、填寫中:確認/取消
    return _mode_buttons()             # 閒置:選 出差/代墊款/追加


def reply(reply_token: str, text: str, user_id: str | None = None, quick=False):
    """回覆訊息並附快速回覆按鈕。quick=False→依狀態自動;quick=None→不附;quick=QuickReply→用指定的。"""
    try:
        qr = _buttons_for(user_id) if quick is False else quick
        msg = TextMessage(text=text, quick_reply=qr)
        with ApiClient(config) as c:
            MessagingApi(c).reply_message(
                ReplyMessageRequest(reply_token=reply_token, messages=[msg])
            )
    except Exception:
        print("[reply 失敗]\n" + traceback.format_exc(), flush=True)


# 每位使用者「本趟出差」累積中的多張單據:user_id → [辨識結果 dict, ...]
# 一次可傳多張(高鐵票、計程車票…),回「確認」時一起寫成同一張申請單的多列明細。
pending: dict[str, list] = {}
# 使用者用打字設定的表頭欄位(user_id → str)
purpose: dict[str, str] = {}   # 出差事由
route: dict[str, str] = {}     # 起訖地點
project: dict[str, str] = {}   # 專案名稱(已模糊比對命中)
# 問答進度:user_id → "project"/"purpose"/"route"/"ready"(出差)
#                     或 "dm_project"/"dm_subject"/"ready"(代墊款)
step: dict[str, str] = {}
# 報帳模式:user_id → "travel"(出差/差旅費,預設)/ "advance"(代墊款)
mode: dict[str, str] = {}
# 正在填寫一張單(已選模式、尚未送出);決定按鈕要顯示「確認/取消」還是「出差/代墊款」
active_entry: set = set()
# 追加模式:user_id → {sheet, gid, attach, ragic_id, expense_no, mode, applicant, project}
# 設定後,之後拍的單據於「確認」時追加到該既有記錄(不新建、不動表頭)。
append_target: dict[str, dict] = {}
# 已按「➕追加」鈕、正在等使用者輸入費用單號的使用者
awaiting_no: set = set()

# 費用單號樣式:BT=差旅費、BS=代墊款,後接日期+流水號(至少 6 位數字)
_NO_RE = re.compile(r"(B[TS])[-\s]?(\d{6,})", re.IGNORECASE)


def _parse_no(text: str) -> str | None:
    """從文字抓費用單號,正規化成大寫『BT-…』/『BS-…』;抓不到回 None。"""
    m = _NO_RE.search((text or "").replace(" ", ""))
    return f"{m.group(1)}-{m.group(2)}".upper() if m else None


def _key(user_id: str) -> str:
    """該使用者的 Ragic 金鑰(沒填則空字串→用共用);決定他能比對到哪些專案。"""
    return (users.get(user_id) or {}).get("ragic_key", "")


def _in_entry(user_id: str) -> bool:
    """是否正在填寫中(已選模式、追加中、或已累積單據/問答進行中)。"""
    return (user_id in active_entry or user_id in step
            or user_id in append_target or bool(pending.get(user_id)))

# 首次登記進度:user_id → {"step": name/title/office/team, 已答欄位...}
reg: dict[str, dict] = {}
REG_ORDER = ["name", "title", "office", "ragic_key"]
REG_QUESTIONS = {
    "name": "首次使用,先登記你的基本資料(只需一次,之後自動帶入)。\n①【姓名】?(會填到「申請人」)",
    "title": "②【職稱】?(例:經理)",
    "office": "③【上班地點】?(例:台南辦公室)",
    "ragic_key": ("④ 最後,貼上你的【Ragic API 金鑰】(這樣建單會用你本人的權限)。\n"
                  "取得:登入 Ragic → 右上角你的名字 → 個人工具/Personal Settings → "
                  "複製「API / HTTP API」的金鑰。\n沒有或不確定,回「跳過」用共用金鑰。"),
}
REG_LABELS = {"name": "姓名", "title": "職稱", "office": "上班地點", "ragic_key": "Ragic金鑰"}


def _start_registration(user_id: str) -> str:
    reg[user_id] = {"step": "name"}
    return REG_QUESTIONS["name"]


def _handle_registration(user_id: str, text: str) -> str:
    data = reg[user_id]
    st = data["step"]
    val = text.strip()
    if st == "ragic_key":
        if val in SKIP_WORDS:
            val = ""                      # 跳過 → 用共用金鑰
    elif not val:
        return "這格不能空白。\n" + REG_QUESTIONS[st]
    data[st] = val
    # 金鑰不回顯(只說已設定/略過),其餘回顯填的值
    shown = (("已設定" if val else "略過(用共用金鑰)") if st == "ragic_key" else val)
    idx = REG_ORDER.index(st)
    if idx + 1 < len(REG_ORDER):
        nxt = REG_ORDER[idx + 1]
        data["step"] = nxt
        return f"{REG_LABELS[st]}:{shown}\n\n{REG_QUESTIONS[nxt]}"
    profile = {k: data[k] for k in REG_ORDER}
    users.save_user(user_id, profile)
    reg.pop(user_id, None)
    key_line = ("Ragic金鑰:已設定(用你本人權限)" if profile.get("ragic_key")
                else "Ragic金鑰:未設定(用共用金鑰)")
    return ("登記完成 ✅\n"
            f"姓名:{profile['name']}\n職稱:{profile['title']}\n"
            f"上班地點:{profile['office']}\n{key_line}\n\n"
            "之後直接拍單據就會用這份資料(接案團隊會依專案自動帶入)。"
            "要修改打「重新登記」、查詢打「我的資料」。")


def _my_profile(user_id: str) -> str:
    p = users.get(user_id)
    if not p:
        return _start_registration(user_id)
    key_line = ("Ragic金鑰:已設定(本人)" if p.get("ragic_key")
                else "Ragic金鑰:未設定(用共用)")
    return (f"你目前的登記資料:\n姓名:{p['name']}\n職稱:{p['title']}\n"
            f"上班地點:{p['office']}\n{key_line}\n\n要修改打「重新登記」。")

QUESTIONS = {
    "project": "①【專案名稱】是哪個案子?\n打關鍵字就好(例:標案準備),我會幫你比對;不確定可回「跳過」用預設。",
    "purpose": "②【出差事由】是什麼?\n例:拜訪台北客戶。可回「跳過」。",
    "route": "③【起訖地點】?\n例:台南車站 → 台北客戶處。可回「跳過」(改用單據自動帶入)。",
}
SKIP_WORDS = {"跳過", "略過", "免", "無", "不用", "沒有", "skip", "Skip", "SKIP"}


def _summary_line(data: dict) -> str:
    """單張單據的一行摘要,給累積清單顯示用。"""
    r = ""
    if data.get("起點") or data.get("終點"):
        r = f"{data.get('起點') or ''}→{data.get('終點') or ''}".strip("→")
    return f"{data.get('類別') or '?'} {r} ${data.get('小計') or data.get('單價') or '?'}".strip()


def _clear(user_id: str):
    """清掉某使用者的全部暫存狀態(回到閒置 → 顯示選模式按鈕)。"""
    for d in (pending, purpose, route, project, step, append_target):
        d.pop(user_id, None)
    active_entry.discard(user_id)
    awaiting_no.discard(user_id)


def _status(user_id: str) -> str:
    """目前累積狀態(明細筆數 + 已設定的表頭),附在回覆末尾。"""
    n = len(pending.get(user_id, []))
    parts = [f"目前累積 {n} 筆明細"]
    if purpose.get(user_id):
        parts.append(f"出差事由:{purpose[user_id]}")
    if route.get(user_id):
        parts.append(f"起訖地點:{route[user_id]}")
    if project.get(user_id):
        parts.append(f"專案:{project[user_id]}")
    parts.append("全部設定好後回「確認」送出、或「取消」清除。")
    return "\n".join(parts)


def _confirm_summary(user_id: str) -> str:
    """三題都問完後的核對畫面。"""
    items = pending.get(user_id, [])
    cart = "\n".join(f"  {i + 1}. {_summary_line(d)}" for i, d in enumerate(items))
    proj = project.get(user_id) or RF.DEFAULT_PROJECT + "(預設)"
    purp = purpose.get(user_id) or "出差(預設)"
    rt = route.get(user_id) or "(用單據自動帶入)"
    return (f"請核對:\n明細({len(items)} 筆):\n{cart}\n"
            f"專案:{proj}\n事由:{purp}\n起訖:{rt}\n\n"
            "沒問題回【確認】送出;要改可重打(專案/事由/地點),或回【取消】重來。")


def _ask_current(user_id: str) -> str:
    """目前該問的問題;ready 階段回核對畫面。"""
    st = step.get(user_id)
    if st in QUESTIONS:
        return QUESTIONS[st]
    if st == "ready":
        return _confirm_summary(user_id)
    return _status(user_id)


def _travel_add_photo(user_id: str, data: dict) -> str:
    """出差模式:累積一張單據,回覆文字。"""
    items = pending.setdefault(user_id, [])
    items.append(data)
    n = len(items)
    if user_id not in step:
        step[user_id] = "project"
        return (f"已辨識第 1 筆:{_summary_line(data)}\n"
                "(本趟還有單據可隨時再拍給我,會併入同一張)\n\n" + QUESTIONS["project"])
    return f"已加入第 {n} 筆:{_summary_line(data)}\n\n" + _ask_current(user_id)


# ===================== 代墊款模式 =====================
DM_SUBJECT_HINT = "油資 / 郵寄 / 內部餐 / 餐會 / 福利旅遊 / 健檢 / 公務車 / 特支"


def _dm_amt(d: dict) -> int:
    v = d.get("小計") or d.get("單價") or 0
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


MEMO_SAME = {"同上", "沿用", "同品名", "一樣", "同"}


def _dm_summary(item: dict) -> str:
    s = f"{item.get('品名') or '?'} ${item.get('小計') or item.get('單價') or '?'}"
    if item.get("科目"):
        s += f" / {item['科目']}"
    if item.get("摘要"):
        s += f" / {item['摘要']}"
    return s


def _next_no_subject(user_id: str):
    for i, it in enumerate(pending.get(user_id, [])):
        if not it.get("科目"):
            return i
    return None


def _next_no_memo(user_id: str):
    for i, it in enumerate(pending.get(user_id, [])):
        if it.get("科目") and not it.get("摘要"):
            return i
    return None


def _dm_subject_question(user_id: str, idx: int) -> str:
    it = pending[user_id][idx]
    return (f"第 {idx + 1} 筆({it.get('品名') or '?'} "
            f"${it.get('小計') or it.get('單價') or '?'})的【科目】?\n"
            f"打關鍵字即可,常用:{DM_SUBJECT_HINT}\n"
            "(其他科目也能比對,如 文具/電話/印刷/清潔/會費/廣告…;不確定可回「跳過」歸特支費)")


def _dm_memo_question(user_id: str, idx: int) -> str:
    it = pending[user_id][idx]
    pin = it.get("品名") or ""
    hint = f"\n(單據看到的是「{pin}」,可直接回「同上」沿用)" if pin else ""
    return f"第 {idx + 1} 筆的【摘要說明】?(這筆買什麼/用途){hint}"


def _dm_project_question() -> str:
    return "【專案名稱/用途】是哪個?\n打關鍵字(例:嘉義農再);可回「跳過」用預設「其他」。"


def _dm_confirm(user_id: str) -> str:
    items = pending.get(user_id, [])
    cart = "\n".join(f"  {i + 1}. {_dm_summary(d)}" for i, d in enumerate(items))
    total = sum(_dm_amt(d) for d in items)
    tgt = append_target.get(user_id)
    if tgt:                                # 追加模式:顯示要追加到哪張單
        return (f"請核對(代墊款,追加到 {tgt['expense_no']}):\n明細({len(items)} 筆):\n{cart}\n"
                f"合計:約 ${total}\n\n沒問題回【確認】追加,或回【取消】放棄。")
    proj = project.get(user_id) or RF.DM_DEFAULT_PROJECT + "(預設)"
    return (f"請核對(代墊款):\n明細({len(items)} 筆):\n{cart}\n"
            f"專案/用途:{proj}\n合計:約 ${total}\n\n"
            "沒問題回【確認】送出;要改專案可打「專案 …」,或回【取消】重來。")


def _dm_advance_to_next(user_id: str) -> str:
    """決定下一個該問的(先補當前筆的摘要,再換下一筆科目,都完成則核對)。"""
    mi = _next_no_memo(user_id)       # 有科目但缺摘要 → 先補摘要(完成這一筆)
    if mi is not None:
        step[user_id] = "dm_memo"
        return _dm_memo_question(user_id, mi)
    si = _next_no_subject(user_id)    # 還有沒科目的 → 問科目
    if si is not None:
        step[user_id] = "dm_subject"
        return _dm_subject_question(user_id, si)
    step[user_id] = "ready"
    return _dm_confirm(user_id)


def _dm_ask_current(user_id: str) -> str:
    st = step.get(user_id)
    if st == "dm_project":
        return _dm_project_question()
    if st == "dm_subject":
        idx = _next_no_subject(user_id)
        if idx is not None:
            return _dm_subject_question(user_id, idx)
    if st == "dm_memo":
        idx = _next_no_memo(user_id)
        if idx is not None:
            return _dm_memo_question(user_id, idx)
    return _dm_confirm(user_id)


def _advance_add_photo(user_id: str, data: dict) -> str:
    """代墊款模式:累積一張單據(科目、摘要待逐筆問),回覆文字。"""
    data["品名"] = data.get("摘要")   # OCR 品名留作建議
    data["摘要"] = None               # 摘要改由問答填
    data["科目"] = None
    items = pending.setdefault(user_id, [])
    items.append(data)
    n = len(items)
    st = step.get(user_id)
    head = f"{'已辨識' if st is None else '已加入'}第 {n} 筆:{_dm_summary(data)}\n\n"
    if st is None:
        step[user_id] = "dm_project"           # 第一張 → 先問專案
        return "代墊款模式。" + head + _dm_project_question()
    if st in ("dm_project", "dm_subject", "dm_memo"):
        return head + _dm_ask_current(user_id)
    return head + _dm_advance_to_next(user_id)  # ready 後又加單 → 問該筆科目


# ===================== 追加到既有單號 =====================
def _offer_recent(reply_token: str, user_id: str, profile: dict | None):
    """列出使用者近期單號做成快速回覆鈕讓他點選(撈單要連網,故在背景執行)。"""
    try:
        recs = ragic.recent_records((profile or {}).get("ragic_key", ""), limit=10,
                                    applicant=(profile or {}).get("name", ""))
    except Exception:
        recs = []
        print("[追加列近期單失敗]\n" + traceback.format_exc(), flush=True)
    if not recs:
        reply(reply_token,
              "查不到你近期的單號。請直接貼上要追加的費用單號(例:BS-20260903004),或回【取消】。",
              user_id)                     # awaiting_no 仍在 → 自動附「取消」鈕
        return
    lines, items = [], []
    for i, r in enumerate(recs, 1):
        no = r["expense_no"]
        kind = "差旅" if r["mode"] == "travel" else "代墊"
        lines.append(f"{i}. {no}({kind}) {r.get('project') or '—'}")
        items.append(QuickReplyItem(action=MessageAction(label=no[:20], text=no)))
    items.append(QuickReplyItem(action=MessageAction(label="✖️ 取消", text="取消")))
    body = "選一張要追加的單(或直接貼單號):\n" + "\n".join(lines)
    reply(reply_token, body, user_id, quick=QuickReply(items=items))


def _start_append(reply_token: str, user_id: str, expense_no: str, profile: dict | None):
    """依費用單號查既有記錄,設定追加狀態(查單要連網,故在背景執行)。"""
    try:
        rec = ragic.find_record_by_no(expense_no, (profile or {}).get("ragic_key", ""))
    except Exception:
        rec = None
        print("[追加查單失敗]\n" + traceback.format_exc(), flush=True)
    if not rec:
        reply(reply_token,
              f"查無單號 {expense_no}。\n請確認號碼正確(BT 開頭=差旅費、BS=代墊款),"
              "且是你的金鑰看得到的單。", user_id)
        return
    _clear(user_id)                      # 清掉舊暫存後再設定追加目標
    append_target[user_id] = rec
    mode[user_id] = rec["mode"]
    active_entry.add(user_id)
    kind = "差旅費" if rec["mode"] == "travel" else "代墊款"
    extra = "每筆我會問你科目與摘要。" if rec["mode"] == "advance" else ""
    reply(reply_token,
          f"找到單號 {rec['expense_no']}({kind};申請人 {rec.get('applicant') or '—'}、"
          f"專案 {rec.get('project') or '—'})。\n"
          f"現在起拍的單據會【追加】到這張(不新建、不動表頭)。{extra}\n"
          "傳單據吧,傳完回【確認】,或【取消】放棄。", user_id)


def _append_add_photo(user_id: str, data: dict) -> str:
    """追加模式:累積一張單據;代墊款仍逐筆問科目/摘要,差旅費直接累積。"""
    tgt = append_target[user_id]
    if tgt["mode"] == "advance":
        data["品名"] = data.get("摘要")
        data["摘要"] = None
        data["科目"] = None
        items = pending.setdefault(user_id, [])
        items.append(data)
        head = f"已加入第 {len(items)} 筆:{_dm_summary(data)}\n\n"
        if step.get(user_id) in ("dm_subject", "dm_memo"):
            return head + _dm_ask_current(user_id)
        return head + _dm_advance_to_next(user_id)   # 直接問科目(不問專案,表頭已存在)
    items = pending.setdefault(user_id, [])
    items.append(data)
    return (f"已加入第 {len(items)} 筆:{_summary_line(data)}\n"
            f"(追加到 {tgt['expense_no']};可再拍,傳完回【確認】)")


def _dm_handle_answer(user_id: str, text: str) -> str:
    st = step.get(user_id)
    if st is None:
        return "代墊款模式:先拍一張單據傳給我。"
    skip = text in SKIP_WORDS

    if st == "dm_project":
        if not skip:
            q = _strip_kw(text, ("專案名稱", "專案", "用途", "案件")) or text
            cands, chosen = ragic.match_project(q, _key(user_id))
            if chosen:
                project[user_id] = chosen
            elif cands:
                return "找到多個相近專案,請更精確:\n" + "\n".join(f"  ・{c}" for c in cands)
            else:
                return "查無相近專案,請換關鍵字(或回「跳過」用預設「其他」)。"
        head = f"專案/用途:{project.get(user_id) or '其他(預設)'}\n\n"
        return head + _dm_advance_to_next(user_id)

    if st == "dm_subject":
        idx = _next_no_subject(user_id)
        if idx is None:
            return _dm_advance_to_next(user_id)
        if skip:
            chosen = RF.DM_DEFAULT_SUBJECT
            pending[user_id][idx]["科目"] = chosen
        else:
            cands, chosen = ragic.match_subject(text)
            if chosen:
                pending[user_id][idx]["科目"] = chosen
            elif cands:
                return "找到多個相近科目,請更精確:\n" + "\n".join(f"  ・{c}" for c in cands)
            else:
                return f"查無相近科目,請換關鍵字({DM_SUBJECT_HINT}),或回「跳過」歸特支費。"
        return f"第 {idx + 1} 筆科目:{chosen}\n\n" + _dm_advance_to_next(user_id)

    if st == "dm_memo":
        idx = _next_no_memo(user_id)
        if idx is None:
            return _dm_advance_to_next(user_id)
        it = pending[user_id][idx]
        memo = it.get("品名") if (skip or text in MEMO_SAME) else text.strip()
        it["摘要"] = memo or it.get("品名") or "代墊款"
        return f"第 {idx + 1} 筆摘要:{it['摘要']}\n\n" + _dm_advance_to_next(user_id)

    # st == "ready":可改專案,否則提示確認
    q = _strip_kw(text, ("專案名稱", "專案", "用途"))
    if q is not None:
        cands, chosen = ragic.match_project(q, _key(user_id))
        if chosen:
            project[user_id] = chosen
            return f"專案/用途改為:{chosen}\n\n" + _dm_confirm(user_id)
        if cands:
            return "找到多個相近:\n" + "\n".join(f"  ・{c}" for c in cands)
        return "查無相近專案。"
    return "已就緒。回【確認】送出、或【取消】重來。"


def process_image(message_id: str, reply_token: str, user_id: str):
    print(f"[收到圖片] message_id={message_id} user_id={user_id}", flush=True)
    # 未登記身分 → 先請他登記(在下載/辨識之前擋下,不浪費 API)
    if not users.is_registered(user_id):
        if user_id in reg:
            reply(reply_token, "請先完成登記:\n" + REG_QUESTIONS[reg[user_id]["step"]], user_id)
        else:
            reply(reply_token, _start_registration(user_id), user_id)
        return
    # 按了「➕追加」但還沒貼單號就先拍照 → 先請他貼單號(避免用到殘留模式)
    if user_id in awaiting_no:
        reply(reply_token, "請先貼上要追加的費用單號(例:BS-20260903004),再拍單據喔。", user_id)
        return
    try:
        # 下載原圖
        with ApiClient(config) as c:
            image_bytes = MessagingApiBlob(c).get_message_content(message_id=message_id)
        print(f"[下載完成] {len(image_bytes)} bytes,送 Claude 辨識…", flush=True)
        is_advance = mode.get(user_id) == "advance"
        # 丟給 Claude 辨識(代墊款用不同 prompt)
        data = extract_receipt(image_bytes, EXTRACT_PROMPT_DM if is_advance else EXTRACT_PROMPT)
        print(f"[辨識完成]{'(代墊款)' if is_advance else ''} {data}", flush=True)
        ready, why = ragic.is_ready()
        if not ready:
            lines = "\n".join(f"{k}：{v}" for k, v in data.items())
            text = "辨識結果(僅辨識,未寫入):\n" + lines + f"\n\n{why}"
        else:
            # 保留原始照片位元組,確認時一併上傳到 Ragic 的「憑證上傳」附件欄
            data["_image"] = image_bytes
            if user_id in append_target:
                text = _append_add_photo(user_id, data)
            elif is_advance:
                text = _advance_add_photo(user_id, data)
            else:
                text = _travel_add_photo(user_id, data)
    except _RETRYABLE as e:
        # Anthropic 暫時性錯誤(已重試用盡):非使用者問題,請稍後再傳同一張
        print(f"[process_image 過載] {type(e).__name__}: {e}", flush=True)
        text = ("辨識服務目前忙碌(伺服器暫時過載),這不是你的單據有問題。\n"
                "請稍等十幾秒後，把同一張照片再傳一次即可，先前選的模式/問答都還在。")
    except Exception:
        tb = traceback.format_exc()
        print("[process_image 失敗]\n" + tb, flush=True)
        text = "辨識失敗:" + tb.strip().splitlines()[-1]
    reply(reply_token, text, user_id)


def write_to_ragic(reply_token: str, user_id: str):
    items = pending.pop(user_id, None)
    if not items:
        reply(reply_token, "目前沒有待確認的單據,請先傳照片。", user_id)
        return
    profile = users.get(user_id)
    if not profile:
        reply(reply_token, "你還沒登記身分,請先打「重新登記」完成登記再送出。", user_id)
        return
    is_advance = mode.get(user_id) == "advance"
    tgt = append_target.get(user_id)
    try:
        if tgt:                                     # 追加到既有單號(不新建)
            if tgt["mode"] == "advance":
                url = ragic.append_advance(items, profile, tgt["ragic_id"])
            else:
                url = ragic.append_expense(items, profile, tgt["ragic_id"])
            expense_no = tgt["expense_no"]
            kind = "代墊款" if tgt["mode"] == "advance" else "差旅費"
            _clear(user_id)  # 成功才清掉(含追加目標)
            print(f"[追加成功] {kind} 單號={expense_no} +{len(items)} 筆 {url}", flush=True)
            reply(reply_token,
                  f"已追加 {len(items)} 筆到 {expense_no} ✅({kind})\n{url}", user_id)
            return
        if is_advance:
            url, expense_no = ragic.create_advance(
                items, profile, project.get(user_id, ""))
        else:
            url, expense_no = ragic.create_expense(
                items, profile, purpose.get(user_id, ""), route.get(user_id, ""),
                project.get(user_id, ""))
        _clear(user_id)  # 成功才清掉全部暫存
        kind = "代墊款" if is_advance else "差旅費"
        print(f"[寫入成功] {kind} 單號={expense_no} {url}", flush=True)
        no_line = f"費用單號:{expense_no}\n" if expense_no else ""
        reply(reply_token,
              f"已寫入 Ragic ✅({kind},共 {len(items)} 筆明細)\n{no_line}{url}", user_id)
    except ragic.RagicWritePermissionError as e:
        pending[user_id] = items  # 保留批次,開通後可直接再「確認」
        reply(reply_token, str(e), user_id)
    except Exception as e:
        pending[user_id] = items  # 失敗放回去,可再「確認」重試
        print("[寫入失敗]\n" + traceback.format_exc(), flush=True)
        reply(reply_token, f"寫入 Ragic 失敗:{e}\n可回覆「確認」重試,或「取消」放棄。", user_id)


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event):
    # 辨識較慢,丟到背景執行緒,讓 webhook 立刻回 200
    threading.Thread(
        target=process_image,
        args=(event.message.id, event.reply_token, event.source.user_id),
        daemon=True,
    ).start()


@handler.add(MessageEvent, message=TextMessageContent)
def handle_text(event):
    text = event.message.text.strip()
    user_id = event.source.user_id

    # ---- 身分登記優先 ----
    if user_id in reg:                       # 登記進行中 → 當作登記答案
        reply(event.reply_token, _handle_registration(user_id, text), user_id)
        return
    if text in ("重新登記", "重新登錄", "修改資料", "修改身分"):
        users.delete(user_id)
        _clear(user_id)
        reply(event.reply_token, _start_registration(user_id), user_id)
        return
    if text in ("我的資料", "我的身分", "查詢資料"):
        reply(event.reply_token, _my_profile(user_id), user_id)
        return
    if text in ("更新專案", "刷新專案", "重新整理專案"):
        n = ragic.refresh_projects(_key(user_id))
        reply(event.reply_token,
              f"已重新整理你能看到的專案清單,共 {n} 個可比對。", user_id)
        return
    if not users.is_registered(user_id):     # 還沒登記 → 先登記
        reply(event.reply_token, _start_registration(user_id), user_id)
        return

    # ---- 按了「➕追加」鈕、正在等單號 → 這則文字當單號處理 ----
    if user_id in awaiting_no:
        if text in ("取消", "返回", "上一步"):
            awaiting_no.discard(user_id)
            reply(event.reply_token, "已取消追加。", user_id)
            return
        no = _parse_no(text)
        if no:
            awaiting_no.discard(user_id)
            threading.Thread(
                target=_start_append,
                args=(event.reply_token, user_id, no, users.get(user_id)),
                daemon=True,
            ).start()
        else:
            reply(event.reply_token,
                  "看不出單號。請貼完整費用單號(例:BS-20260903004=代墊款、BT-…=差旅費),或回【取消】。",
                  user_id)
        return

    # ---- 模式切換:只在「閒置」(沒有進行中的問答/批次)時才當指令 ----
    # 否則「出差/代墊款」視為當前問題的答案(例如出差事由就想打「出差」兩字)。
    # 要在填寫中換模式,先回「取消」回到閒置即可,與按鈕邏輯一致。
    idle = step.get(user_id) is None and not pending.get(user_id)
    if idle and text in ("出差", "差旅", "差旅費"):
        mode[user_id] = "travel"
        _clear(user_id)
        active_entry.add(user_id)
        reply(event.reply_token, "已切換到【出差/差旅費】模式。拍單據開始,我會問專案/事由/起訖。", user_id)
        return
    if idle and text in ("代墊款", "代墊", "代墊金"):
        mode[user_id] = "advance"
        _clear(user_id)
        active_entry.add(user_id)
        reply(event.reply_token, "已切換到【代墊款】模式。拍單據開始,每筆我會問你科目、最後問專案/用途。", user_id)
        return

    # ---- 追加:按「➕追加」鈕(或打「追加」)→ 列出近期單號讓他點選(也可直接貼單號)----
    if idle and text in ("追加", "繼續", "接續", "追加明細", "加到既有單"):
        awaiting_no.add(user_id)
        threading.Thread(
            target=_offer_recent,
            args=(event.reply_token, user_id, users.get(user_id)),
            daemon=True,
        ).start()
        return

    # ---- 追加到既有單號:閒置時直接輸入費用單號(BT.../BS...)也可 ----
    if idle and _parse_no(text):
        threading.Thread(
            target=_start_append,
            args=(event.reply_token, user_id, _parse_no(text), users.get(user_id)),
            daemon=True,
        ).start()
        return

    # ---- 已登記 → 報帳流程 ----
    if text in ("確認", "确认", "OK", "ok", "Ok"):
        if pending.get(user_id):
            threading.Thread(
                target=write_to_ragic,
                args=(event.reply_token, user_id),
                daemon=True,
            ).start()
        else:
            reply(event.reply_token, "目前沒有待確認的單據。請先選【出差】或【代墊款】再拍單據。", user_id)
    elif text == "取消":
        had = pending.get(user_id)
        _clear(user_id)
        reply(event.reply_token,
              f"已清除全部待確認資料(共 {len(had)} 筆明細)。" if had else "目前沒有待確認的資料。", user_id)
    elif mode.get(user_id) == "advance":
        reply(event.reply_token, _dm_handle_answer(user_id, text), user_id)
    else:
        reply(event.reply_token, _handle_answer(user_id, text), user_id)


def _handle_answer(user_id: str, text: str) -> str:
    """依目前問答進度,把使用者的回覆歸到 專案 / 事由 / 起訖。"""
    st = step.get(user_id)
    if st is None:
        return "請先拍一張單據傳給我,我會接著依序問你:專案名稱 → 出差事由 → 起訖地點。"

    skip = text in SKIP_WORDS

    if st == "project":
        if not skip:
            q = _strip_kw(text, ("專案名稱", "專案", "案件")) or text
            cands, chosen = ragic.match_project(q, _key(user_id))
            if chosen:
                project[user_id] = chosen
            elif cands:
                lst = "\n".join(f"  ・{c}" for c in cands)
                return f"找到多個相近專案,請打更精確的關鍵字:\n{lst}"
            else:
                return "查無相近專案,請換個關鍵字(或回「跳過」用預設、「其他」歸為其他)。"
        step[user_id] = "purpose"
        head = f"專案:{project.get(user_id) or '(用預設)'}\n\n"
        return head + QUESTIONS["purpose"]

    if st == "purpose":
        if not skip:
            purpose[user_id] = _strip_kw(text, ("出差事由", "事由", "目的")) or text
        step[user_id] = "route"
        head = f"事由:{purpose.get(user_id) or '(用預設)'}\n\n"
        return head + QUESTIONS["route"]

    if st == "route":
        if not skip:
            route[user_id] = _strip_kw(text, ("起訖地點", "起訖", "地點", "路線")) or text
        step[user_id] = "ready"
        return _confirm_summary(user_id)

    # st == "ready":三題已答完,允許再修改任一欄位
    return _apply_text(user_id, text)


def _strip_kw(text: str, keywords: tuple) -> str | None:
    """若 text 以某關鍵字開頭,回傳去掉關鍵字與分隔符後的內容;否則 None。"""
    for kw in keywords:
        if text.startswith(kw):
            return text[len(kw):].lstrip("：: 　").strip()
    return None


def _apply_text(user_id: str, text: str) -> str:
    """把使用者打字的文字歸到 專案 / 起訖地點 / 出差事由,並回覆設定結果。"""
    # 專案名稱(模糊比對)
    q = _strip_kw(text, ("專案名稱", "專案", "案件"))
    if q is not None:
        if not q:
            return "請在「專案」後面加關鍵字,例如:專案 標案準備"
        cands, chosen = ragic.match_project(q, _key(user_id))
        if chosen:
            project[user_id] = chosen
            return f"專案名稱已設為:{chosen}\n" + _status(user_id)
        if cands:
            lst = "\n".join(f"  ・{c}" for c in cands)
            return f"找到多個相近專案,請換更精確的關鍵字:\n{lst}"
        return "查無相近專案,請換關鍵字(或回「專案 其他」)。"

    # 起訖地點(關鍵字開頭,或內容含箭頭)
    v = _strip_kw(text, ("起訖地點", "起訖", "地點", "路線"))
    if v is None and any(s in text for s in ("→", "->", "—>", "⇒", "～")):
        v = text
    if v is not None:
        route[user_id] = v
        return f"起訖地點已設為:{v}\n" + _status(user_id)

    # 其餘一律當作出差事由(可加「事由」前綴,也可直接打)
    v = _strip_kw(text, ("出差事由", "事由", "目的"))
    purpose[user_id] = v if v else text
    return f"出差事由已設為:{purpose[user_id]}\n" + _status(user_id)


@app.post("/callback")
async def callback(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = (await request.body()).decode("utf-8")
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return "OK"


@app.get("/")
def health():
    return {"status": "ok"}
