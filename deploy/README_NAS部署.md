# 搬到 Synology NAS（DS1618+，DSM 7.1.1 的 Docker 套件）

## 準備（一次性）
1. NAS 上建共用資料夾，例如 `docker\line-receipt-bot\`，把本 repo 整個複製進去（或用 `git clone`）。
2. 複製 `deploy\bot.env.example` → `deploy\bot.env`，填入各項金鑰。
   - `LINE_CHANNEL_SECRET`、`LINE_CHANNEL_ACCESS_TOKEN`：LINE Developers Console。
   - `ANTHROPIC_API_KEY`、`RAGIC_API_KEY`：與目前 Windows 使用者變數相同的值。
   - `NGROK_AUTHTOKEN`：目前存在 Windows 的 `ngrok_token.local`。
   - 此檔含金鑰：資料夾權限只給管理員，不進版控、不貼聊天。
3. 建立 `deploy\data\`，把現在的 `users.json` 複製進去（含每人 Ragic 金鑰，走內網複製即可）。

## 啟動
- DSM 7.1.1（Docker 套件）：沒有「專案」功能時，用 SSH 或工作排程執行：
  `cd /volume1/docker/line-receipt-bot/deploy && docker compose up -d --build`
  （SSH 目前未開；可在「控制台 → 終端機和 SNMP」暫時啟用，用完關閉。）
- 升級到 DSM 7.2 後有 Container Manager「專案」：選 `deploy\docker-compose.yml` 建立即可。

## 切換（避免兩邊同時服務）
ngrok 免費固定網域同一時間只能有一個 agent 使用。
1. 先停現在這台 Windows 的 bot：停 uvicorn(:8000) 與我方 ngrok，並停用看門狗排程 `LineBotWatchdog`、移除開機資料夾 `LineReceiptBot.vbs`。
2. 在 NAS 啟動容器。
3. 驗證：`https://wireless-default-shelf.ngrok-free.dev/callback` 用 POST 無簽章應回 400；LINE 傳一張單據走完整流程；打「更新專案」確認專案可搜尋。
LINE Webhook URL 不變，不用改。

## 日常維運
- 看 log：`docker logs -f line-receipt-bot`
- 重啟：`docker compose restart bot`
- 換金鑰：改 `deploy\bot.env` → `docker compose up -d`（容器只在啟動時讀環境變數）。
- 更新程式：`git pull` → `docker compose up -d --build`。
- 備份：`deploy\data\users.json` 納入 NAS 備份（它是唯一的使用者資料）。

## 注意
- 只能跑一個 bot 實例：問答狀態在記憶體，重啟會清掉進行中的問答，重傳單據即可。
- 時區已設 Asia/Taipei。
- bot 與 ngrok 都用 `restart: unless-stopped`，NAS 重開機後自動拉起。
