"""
階段 3:把辨識結果寫入 Ragic「差旅費申請單」(YKU / invoice-form/1)。

Ragic API 要點(已查證,見 CLAUDE.md):
- 端點: POST https://{server}/{帳號}/{頁籤}/{表單編號}?api&v=3
- 認證: Authorization: Basic {RAGIC_API_KEY}
- 欄位鍵 = 數字欄位編號(名稱無效);日期格式 yyyy/MM/dd。
- 子表格(JSON):{"_subtable_<群組編號>": {"-1": {<欄位編號>: 值, ...}}}(-1=新列暫時列號)。
- 有附件時改走 multipart/form-data,子表格欄位鍵用 "<欄位編號>_-1" 的平面寫法。
- 必填欄位若缺,Ragic 會回 status=INVALID;機構脈絡欄位(申請人/職稱/上班地點/團隊)
  由登記者的 profile(users.py)帶入,專案用問答或 DEFAULT_PROJECT。
"""
import difflib
import os
from datetime import date

import requests

import ragic_fields as F

# 專案來源:Ragic 專案表(每位使用者用「自己的金鑰」即時讀 → 只拿到本人/本團隊看得到的專案)。
# 不再用共用 Excel;這樣天然分權、永遠即時,新專案簽約後同仁打「更新專案」就抓得到。
PROJECT_SHEET = "data-inquiry/3"
PROJECT_EXCLUDE_STATUSES = ("已結案",)


def _fuzzy(query: str, options: list[str], cutoff: float = 0.4) -> tuple[list[str], str | None]:
    """通用模糊比對。回傳 (候選清單, 唯一命中或 None)。
    先做去空白的子字串比對,只命中一個就採用;否則用相似度找相近的。"""
    q = query.replace(" ", "").strip()
    if not q:
        return [], None
    subs = [p for p in options if q in p.replace(" ", "")]
    if len(subs) == 1:
        return subs, subs[0]
    if len(subs) > 1:
        return subs, None
    close = difflib.get_close_matches(query, options, n=5, cutoff=cutoff)
    if len(close) == 1:
        return close, close[0]
    return close, None


# 每把金鑰各自的專案快取:cache_key(金鑰或"")→ (專案名稱清單, {專案名稱: 接案團隊})
# 用金鑰當快取鍵 → 不同使用者(不同權限)各自一份,不會互相汙染。
_proj_cache: dict[str, tuple[list[str], dict[str, str]]] = {}


def _load_projects_for(user_key: str) -> tuple[list[str], dict[str, str]]:
    """用指定金鑰即時讀 Ragic 專案表,建 (清單, 專案→接案團隊)。排除已結案。讀不到回 ([], {})。"""
    key = user_key or RAGIC_API_KEY
    names: set[str] = set()
    teams: dict[str, str] = {}
    try:
        resp = requests.get(f"https://{RAGIC_SERVER}/{RAGIC_ACCOUNT}/{PROJECT_SHEET}",
                            headers={"Authorization": "Basic " + key},
                            params={"api": "", "v": "3", "limit": 1000}, timeout=90)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return [], {}
    if isinstance(data, dict):
        for rec in data.values():
            if not isinstance(rec, dict):
                continue
            name = str(rec.get("專案名稱", "") or "").strip()
            if not name:
                continue
            if str(rec.get("訂單現況", "") or "").strip() in PROJECT_EXCLUDE_STATUSES:
                continue
            names.add(name)
            team = rec.get("接案團隊")
            if isinstance(team, list):
                team = ",".join(str(x).strip() for x in team if x)   # 多團隊以逗號串
            team = str(team or "").strip()
            if team:
                teams[name] = team
    return sorted(names), teams


def get_projects(user_key: str = "") -> list[str]:
    """該金鑰能看到的專案清單(第一次用才讀 Ragic,之後快取;讀不到保留舊快取)。"""
    ck = user_key or ""
    if ck not in _proj_cache or not _proj_cache[ck][0]:
        loaded = _load_projects_for(user_key)
        if loaded[0]:
            _proj_cache[ck] = loaded
    return _proj_cache.get(ck, ([], {}))[0]


def team_for(project: str, user_key: str = "") -> str:
    """查某專案的接案團隊(該金鑰的視角;沒有則回空字串)。"""
    ck = user_key or ""
    if ck not in _proj_cache:
        get_projects(user_key)
    return _proj_cache.get(ck, ([], {}))[1].get((project or "").strip(), "")


def refresh_projects(user_key: str = "") -> int:
    """強制重讀某金鑰的專案清單(LINE 打「更新專案」用)。回傳可比對專案數。"""
    loaded = _load_projects_for(user_key)
    if loaded[0] or (user_key or "") not in _proj_cache:
        _proj_cache[user_key or ""] = loaded
    return len(_proj_cache.get(user_key or "", ([], {}))[0])


def match_project(query: str, user_key: str = "") -> tuple[list[str], str | None]:
    """專案名稱模糊比對(用該使用者金鑰能看到的專案清單)。"""
    return _fuzzy(query, get_projects(user_key))


def match_subject(query: str) -> tuple[list[str], str | None]:
    """代墊款科目(支出項目)模糊比對。先看別名關鍵字,再比科目字串。"""
    q = query.replace(" ", "").strip()
    if not q:
        return [], None
    if q in F.DM_SUBJECT_ALIASES:                 # 別名完全命中
        opt = F.DM_SUBJECT_ALIASES[q]
        return [opt], opt
    hits = set()
    for kw, opt in F.DM_SUBJECT_ALIASES.items():  # 別名雙向子字串
        if q in kw or kw in q:
            hits.add(opt)
    for opt in F.DM_SUBJECT_OPTIONS:              # 科目字串子字串
        if q in opt.replace(" ", ""):
            hits.add(opt)
    hits = sorted(hits)
    if len(hits) == 1:
        return hits, hits[0]
    if len(hits) > 1:
        return hits, None
    close = difflib.get_close_matches(query, F.DM_SUBJECT_OPTIONS, n=5, cutoff=0.3)
    if len(close) == 1:
        return close, close[0]
    return close, None

RAGIC_API_KEY = os.environ.get("RAGIC_API_KEY", "")
RAGIC_SERVER = os.environ.get("RAGIC_SERVER", "")
RAGIC_ACCOUNT = os.environ.get("RAGIC_ACCOUNT", "")
RAGIC_SHEET = os.environ.get("RAGIC_SHEET", "")


def is_ready() -> tuple[bool, str]:
    """檢查 Ragic 設定是否齊全;不齊時回傳缺什麼。"""
    missing = []
    if not RAGIC_API_KEY:
        missing.append(".env 的 RAGIC_API_KEY")
    if not RAGIC_SERVER or not RAGIC_ACCOUNT or not RAGIC_SHEET \
            or "頁籤" in RAGIC_SHEET or "帳號" in RAGIC_ACCOUNT:
        missing.append(".env 的 RAGIC_SERVER / RAGIC_ACCOUNT / RAGIC_SHEET")
    if not F.is_configured():
        missing.append("ragic_fields.py 的欄位編號")
    if missing:
        return False, "Ragic 設定未完成,缺:" + "、".join(missing)
    return True, ""


_PLACE_PREFIXES = ("高鐵", "台鐵", "臺鐵", "捷運")
# 這些交通票券一律視為「非電子發票」(實體票券);住宿/雜費/其他才看辨識結果。
_NON_EINVOICE_CATS = ("高鐵", "捷運", "計程車", "飛機")


def _clean_place(name: str | None, category: str | None = None) -> str:
    """清掉站名開頭的交通工具前綴;高鐵類站名若沒帶「站」就補「車站」。"""
    s = (name or "").strip()
    for p in _PLACE_PREFIXES:
        if s.startswith(p):
            s = s[len(p):].strip()
            break
    if category == "高鐵" and s and "站" not in s:
        s += "車站"
    return s


def _route(data: dict) -> str:
    """起訖地點字串:『起點 → 終點』(站名去前綴、高鐵補車站)。沒有起訖回空字串。"""
    if data.get("起點") or data.get("終點"):
        cat = data.get("類別")
        a = _clean_place(data.get("起點"), cat)
        b = _clean_place(data.get("終點"), cat)
        return f"{a} → {b}".strip(" →")
    return ""


def _einvoice(data: dict) -> str:
    """是否為電子發票:交通票券一律『否』,其餘看辨識結果。"""
    if data.get("類別") in _NON_EINVOICE_CATS:
        return "否"
    return "是" if data.get("是否電子發票") else "否"


def _header_fields(receipts: list[dict], profile: dict, purpose: str = "",
                   route: str = "", project: str = "") -> dict:
    """表頭欄位(以欄位編號為鍵):登記者身分 + 申請日期 + 使用者輸入/單據帶入。"""
    proj = project.strip() or F.DEFAULT_PROJECT
    fields = {
        str(F.FIELD_APPLICANT): profile["name"],
        str(F.FIELD_TITLE):     profile["title"],
        str(F.FIELD_PAYEE):     profile["name"],   # 付款對象 = 申請人
        str(F.FIELD_OFFICE):    profile["office"],
        str(F.FIELD_PROJECT):   proj,
        str(F.FIELD_APPLY_DATE): date.today().strftime("%Y/%m/%d"),
    }
    # 接案團隊:依專案從該使用者的專案清單對照帶入(API 不會觸發 UI 的自動連動,要自己填)
    team = team_for(proj, profile.get("ragic_key", ""))
    if team:
        fields[str(F.FIELD_TEAM)] = team

    # 出差日期:取最早的單據日期
    dates = sorted(r["日期"] for r in receipts if r.get("日期"))
    if dates:
        fields[str(F.HEADER_FIELDS["出差日期"])] = dates[0]

    # 起訖地點:優先用使用者打字輸入;沒給才退回「第一筆有起訖的單據」
    route = route.strip()
    if not route:
        for r in receipts:
            r2 = _route(r)
            if r2:
                route = r2
                break
    if route:
        fields[str(F.HEADER_FIELDS["起訖地點"])] = route

    # 出差事由:使用者用 LINE 打字輸入;沒給就預設「出差」
    fields[str(F.HEADER_FIELDS["出差事由"])] = purpose.strip() or "出差"
    # 起訖地點為改版後的必填欄位:使用者「跳過」且單據也無起訖時,退回用出差事由防呆(避免建單被擋)
    fields.setdefault(str(F.HEADER_FIELDS["起訖地點"]),
                      purpose.strip() or "出差")
    return fields


def _expense_item(category: str | None) -> str:
    """OCR 類別 → Ragic 支出項目字串(6203_差旅費-{類別})。未知歸『其他』。"""
    cat = category if category in F.EXPENSE_CATEGORIES else "其他"
    return F.EXPENSE_ITEM_PREFIX + cat


def _img_name(data: dict, idx: int) -> str:
    """附件檔名(ASCII 安全):日期(數字)+ 序號,避免非 ASCII 檔名在 multipart 出狀況。"""
    d = (data.get("日期") or "").replace("/", "").replace("-", "")
    d = "".join(ch for ch in d if ch.isascii() and ch.isdigit())
    return f"{d or 'receipt'}_{idx + 1}.jpg"


def _attach_image(row: dict, src: dict, idx: int) -> None:
    """把單據照片位元組掛到子表格列(私有鍵,交給 _flatten_multipart 打包成 file part)。"""
    img = src.get("_image")
    if img:
        row["_image"] = img
        row["_image_name"] = src.get("_image_name") or _img_name(src, idx)


def _flatten_multipart(header: dict, rows: list[dict], attach_field: int) -> tuple[dict, dict]:
    """把 JSON 版的表頭 dict + 子表格列 dict 清單,攤平成 multipart 的 (data, files)。
    子表格一般欄位鍵用平面 "<欄位編號>_-<列號>";附件用同格式的 file part。"""
    data: dict = {}
    for k, v in header.items():
        if v is not None:
            data[str(k)] = str(v)
    files: dict = {}
    for i, row in enumerate(rows):
        rn = f"_-{i + 1}"
        img = row.pop("_image", None)
        name = row.pop("_image_name", None)
        for fid, val in row.items():
            if val is not None:
                data[f"{fid}{rn}"] = str(val)
        if img:
            files[f"{attach_field}{rn}"] = (name or f"receipt_{i + 1}.jpg", img, "image/jpeg")
    return data, files


def _subtable_row(data: dict, idx: int = 0) -> dict:
    """子表格一列(以欄位編號為鍵)。一張單據 = 一列明細。"""
    # 摘要說明:只放起訖地點;非交通類(無起訖)則退回支出項目/類別
    摘要 = _route(data) or data.get("支出項目") or data.get("類別") or ""
    raw = {
        "支出憑證分類": F.DEFAULT_VOUCHER_CLASS,
        "支出項目": _expense_item(data.get("類別")),
        "支出方式": F.DEFAULT_PAY_METHOD,
        "摘要說明": 摘要,
        "數量": data.get("數量") or 1,
        "單價": data.get("單價"),
        "小計": data.get("小計"),
        "是否為電子發票": _einvoice(data),
    }
    row = {str(F.SUBTABLE_FIELDS[name]): v
           for name, v in raw.items() if v is not None}
    row[str(F.FIELD_PAPER)] = F.PAPER_VALUE          # 有無紙本一律「有」
    _attach_image(row, data, idx)                     # 憑證上傳(照片)
    return row


class RagicWritePermissionError(Exception):
    """使用者自己的 Ragic 金鑰沒有 API 寫入權(方案 A:不退共用金鑰,請他找管理者開通)。"""


def _post_create(url: str, payload: dict, user_key: str, files: dict | None = None) -> dict:
    """建單 POST,用使用者自己的金鑰(沒填才用共用)。回傳 Ragic 的 JSON 結果。
    有附件(files)時走 multipart/form-data(payload 為攤平後的 form 欄位);否則走 JSON。
    方案 A:若使用者填了自己的金鑰但被權限擋(guest / access right),
    不改用共用金鑰(避免別人的單跑進共用帳號),改丟 RagicWritePermissionError。"""
    key = user_key or RAGIC_API_KEY
    headers = {"Authorization": "Basic " + key}  # multipart 的 Content-Type 交給 requests 設
    if files is not None:
        resp = requests.post(url, headers=headers, data=payload, files=files, timeout=120)
    else:
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    result = resp.json()
    blocked = "access right" in (result.get("msg") or "") or \
              "guest account" in (result.get("msg") or "")
    if result.get("status") != "SUCCESS" and blocked \
            and user_key and user_key != RAGIC_API_KEY:
        print("[建單] 使用者金鑰無 API 寫入權,依方案 A 不退共用金鑰", flush=True)
        raise RagicWritePermissionError(
            "你的 Ragic 帳號尚未開通 API 建單權限,請聯絡管理者開通"
            "(差旅費/代墊款表單的 API 寫入)。開通後再回「確認」即可送出。")
    return result


def create_expense(receipts: list[dict], profile: dict, purpose: str = "",
                   route: str = "", project: str = "") -> tuple[str, str]:
    """建立一筆差旅費申請單(多張單據 = 多列明細)。
    profile = 登記者身分。回傳 (資料網址, 費用單號)。失敗丟例外。"""
    # doFormula=true:建立時重算公式欄位(總計、合併摘要…),讓統計金額自動呈現。
    url = f"https://{RAGIC_SERVER}/{RAGIC_ACCOUNT}/{RAGIC_SHEET}?api&v=3&doFormula=true"
    header = _header_fields(receipts, profile, purpose, route, project)
    rows = [_subtable_row(r, i) for i, r in enumerate(receipts)]
    for r in rows:                                   # 實際使用者(必填)= 申請人本人
        r[str(F.FIELD_ACTUAL_USER)] = profile["name"]
    if any("_image" in r for r in rows):
        # 有單據照片 → multipart 上傳(含 憑證上傳 附件)
        data, files = _flatten_multipart(header, rows, F.FIELD_ATTACH)
        result = _post_create(url, data, profile.get("ragic_key"), files=files)
    else:
        payload = dict(header)
        payload[f"_subtable_{F.SUBTABLE_ID}"] = {f"-{i + 1}": r for i, r in enumerate(rows)}
        result = _post_create(url, payload, profile.get("ragic_key"))
    if result.get("status") != "SUCCESS":
        raise RuntimeError(f"Ragic 回應非 SUCCESS:{result.get('msg') or result}")
    record_id = result.get("ragicId", "")
    # 費用單號由 Ragic 自動產生,從回應(以欄位編號為鍵)讀回
    expense_no = (result.get("data") or {}).get(str(F.FIELD_EXPENSE_NO), "")
    record_url = f"https://{RAGIC_SERVER}/{RAGIC_ACCOUNT}/{RAGIC_SHEET}/{record_id}"
    return record_url, expense_no


def _dm_row(item: dict, payee: str, idx: int = 0) -> dict:
    """代墊款子表格一列。item 需含 科目(完整字串)、摘要、單價、小計、數量、是否電子發票。"""
    raw = {
        "支出憑證分類": F.DEFAULT_VOUCHER_CLASS,
        "支出項目": item.get("科目"),
        "支出方式": F.DEFAULT_PAY_METHOD,
        "摘要說明": item.get("摘要") or item.get("品名") or "代墊款",
        "數量": item.get("數量") or 1,
        "單價": item.get("單價"),
        "小計": item.get("小計"),
        "是否為電子發票": "是" if item.get("是否電子發票") else "否",
        "付款對象": payee,
    }
    row = {str(F.DM_SUBTABLE_FIELDS[name]): v
           for name, v in raw.items() if v is not None}
    row[str(F.DM_FIELD_PAPER)] = F.PAPER_VALUE       # 有無紙本一律「有」
    _attach_image(row, item, idx)                     # 憑證上傳(照片)
    return row


def create_advance(rows: list[dict], profile: dict, project: str = "") -> tuple[str, str]:
    """建立一筆代墊款申請單(invoice-form/2,多筆 = 多列)。回傳 (資料網址, 費用單號)。"""
    url = f"https://{RAGIC_SERVER}/{RAGIC_ACCOUNT}/{F.DM_SHEET}?api&v=3&doFormula=true"

    proj = project.strip() or F.DM_DEFAULT_PROJECT
    payload = {
        str(F.DM_FIELD_APPLICANT): profile["name"],
        str(F.DM_FIELD_PAYEE):     profile["name"],
        str(F.DM_FIELD_PROJECT):   proj,
        str(F.DM_FIELD_APPLY_DATE): date.today().strftime("%Y/%m/%d"),
    }
    # 接案團隊:依專案從該使用者的專案清單對照帶入
    team = team_for(proj, profile.get("ragic_key", ""))
    if team:
        payload[str(F.DM_FIELD_TEAM)] = team
    sub_rows = [_dm_row(r, profile["name"], i) for i, r in enumerate(rows)]
    if any("_image" in r for r in sub_rows):
        data, files = _flatten_multipart(payload, sub_rows, F.DM_FIELD_ATTACH)
        result = _post_create(url, data, profile.get("ragic_key"), files=files)
    else:
        payload[f"_subtable_{F.DM_SUBTABLE_ID}"] = {
            f"-{i + 1}": r for i, r in enumerate(sub_rows)}
        result = _post_create(url, payload, profile.get("ragic_key"))
    if result.get("status") != "SUCCESS":
        raise RuntimeError(f"Ragic 回應非 SUCCESS:{result.get('msg') or result}")
    record_id = result.get("ragicId", "")
    expense_no = (result.get("data") or {}).get(str(F.DM_FIELD_EXPENSE_NO), "")
    record_url = f"https://{RAGIC_SERVER}/{RAGIC_ACCOUNT}/{F.DM_SHEET}/{record_id}"
    return record_url, expense_no


# ======================= 追加明細到既有單號 =======================
# 依費用單號前綴判斷是哪張表單:BT=差旅費(invoice-form/1)、BS=代墊款(invoice-form/2)。
def _sheet_of_no(expense_no: str):
    """回傳 (sheet, 子表格群組, 單號欄位編號, 附件欄位編號, mode) 或 None(前綴不符)。"""
    no = (expense_no or "").strip().upper()
    if no.startswith("BT"):
        return RAGIC_SHEET, F.SUBTABLE_ID, F.FIELD_EXPENSE_NO, F.FIELD_ATTACH, "travel"
    if no.startswith("BS"):
        return F.DM_SHEET, F.DM_SUBTABLE_ID, F.DM_FIELD_EXPENSE_NO, F.DM_FIELD_ATTACH, "advance"
    return None


def find_record_by_no(expense_no: str, user_key: str = "") -> dict | None:
    """用費用單號找既有記錄。回傳 {sheet,gid,attach,ragic_id,expense_no,mode,applicant,project}
    或 None(查無/前綴不符)。讀取用本人金鑰(沒填才用共用),故只找得到本人看得到的資料。"""
    info = _sheet_of_no(expense_no)
    if not info:
        return None
    sheet, gid, no_fid, attach, mode = info
    no = expense_no.strip().upper()
    key = user_key or RAGIC_API_KEY
    url = f"https://{RAGIC_SERVER}/{RAGIC_ACCOUNT}/{sheet}"
    # where 過濾(命中則只回該筆);即使被忽略,下面仍以費用單號逐筆比對確保正確。
    resp = requests.get(url, headers={"Authorization": "Basic " + key},
                        params={"api": "", "v": "3", "where": f"{no_fid},eq,{no}"}, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        return None
    for rid, rec in data.items():
        if not isinstance(rec, dict):
            continue
        if str(rec.get("費用單號", "")).strip().upper() == no:
            return {"sheet": sheet, "gid": gid, "attach": attach,
                    "ragic_id": rec.get("_ragicId") or rid, "expense_no": no, "mode": mode,
                    "applicant": rec.get("申請人", ""), "project": rec.get("專案名稱", "")}
    return None


def _append_rows(sheet: str, gid: int, attach: int, ragic_id: str,
                 sub_rows: list[dict], user_key: str) -> str:
    """把新子表格列(負數暫時列號)POST 到既有記錄 → Ragic 只新增這些列、不動既有列/表頭。
    有照片走 multipart。回傳資料網址。失敗丟例外。"""
    url = f"https://{RAGIC_SERVER}/{RAGIC_ACCOUNT}/{sheet}/{ragic_id}?api&v=3&doFormula=true"
    if any("_image" in r for r in sub_rows):
        data, files = _flatten_multipart({}, sub_rows, attach)
        result = _post_create(url, data, user_key, files=files)
    else:
        payload = {f"_subtable_{gid}": {f"-{i + 1}": r for i, r in enumerate(sub_rows)}}
        result = _post_create(url, payload, user_key)
    if result.get("status") != "SUCCESS":
        raise RuntimeError(f"Ragic 回應非 SUCCESS:{result.get('msg') or result}")
    return f"https://{RAGIC_SERVER}/{RAGIC_ACCOUNT}/{sheet}/{ragic_id}"


def append_expense(receipts: list[dict], profile: dict, ragic_id: str) -> str:
    """把新單據追加到既有差旅費申請單(不動表頭)。回傳資料網址。"""
    rows = [_subtable_row(r, i) for i, r in enumerate(receipts)]
    for r in rows:                                   # 實際使用者(必填)= 申請人本人
        r[str(F.FIELD_ACTUAL_USER)] = profile["name"]
    return _append_rows(RAGIC_SHEET, F.SUBTABLE_ID, F.FIELD_ATTACH, ragic_id,
                        rows, profile.get("ragic_key", ""))


def append_advance(rows: list[dict], profile: dict, ragic_id: str) -> str:
    """把新明細追加到既有代墊款申請單(不動表頭)。回傳資料網址。"""
    sub_rows = [_dm_row(r, profile["name"], i) for i, r in enumerate(rows)]
    return _append_rows(F.DM_SHEET, F.DM_SUBTABLE_ID, F.DM_FIELD_ATTACH, ragic_id,
                        sub_rows, profile.get("ragic_key", ""))


def recent_records(user_key: str = "", limit: int = 10, applicant: str = "") -> list[dict]:
    """撈使用者近期的單(差旅費+代墊款合併,新到舊)。用本人金鑰 → 只看得到本人的單;
    另用 applicant(申請人)過濾 → 即使是管理者金鑰也只列該人送的單。
    回傳 [{expense_no, sheet, mode, project, date}, ...]。查不到回空清單。"""
    key = user_key or RAGIC_API_KEY
    who = (applicant or "").strip()
    out = []
    for sheet, mode in ((RAGIC_SHEET, "travel"), (F.DM_SHEET, "advance")):
        try:
            resp = requests.get(f"https://{RAGIC_SERVER}/{RAGIC_ACCOUNT}/{sheet}",
                                headers={"Authorization": "Basic " + key},
                                params={"api": "", "v": "3", "limit": 50}, timeout=60)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        for rid, rec in data.items():
            if not isinstance(rec, dict):
                continue
            no = str(rec.get("費用單號", "")).strip()
            if not no:
                continue
            if who and str(rec.get("申請人", "")).strip() != who:
                continue                          # 只列指定申請人送的單
            out.append({
                "expense_no": no, "sheet": sheet, "mode": mode,
                "project": str(rec.get("專案名稱", "") or "").strip(),
                "date": str(rec.get("申請日期", "") or "").strip(),
                "_digits": "".join(ch for ch in no if ch.isdigit()),
            })
    # 單號含日期+流水號,取數字部分由大到小 = 由新到舊
    out.sort(key=lambda r: r["_digits"], reverse=True)
    return out[:limit]
