# LINE 出差單據 → Ragic 自動填單機器人

出差時用手機拍車票/收據傳到 LINE,Claude 辨識後自動填入 Ragic 差旅費申請單。

完整脈絡與進度見 `CLAUDE.md`(Claude Code 會自動讀取)。

## 快速開始（Windows / PowerShell）

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      # 然後編輯 .env 填入金鑰
uvicorn server:app --port 8000
```

另開一個視窗建立對外通道(給 LINE webhook 用):

```powershell
cloudflared tunnel --url http://localhost:8000
```

把印出的 `https://xxx.trycloudflare.com/callback` 填到
LINE Developers Console → 你的 channel → Messaging API → Webhook URL,按 Verify、啟用 Use webhook。
然後用手機對官方帳號拍一張單據傳過去測試。

> trycloudflare 網址每次重啟通道會變,試用期重啟後需回 LINE console 重貼。

## 現況
- 階段 2 完成:收圖 → Claude 辨識 → LINE 回傳欄位(尚未寫入 Ragic)。
- 階段 3 待辦:接 Ragic 寫入 + 上傳照片。需先補齊 Ragic 欄位編號,詳見 `CLAUDE.md`。

## 安全
金鑰只放 `.env`(已被 `.gitignore` 排除)。曾外流過的金鑰請先 revoke/重發。
