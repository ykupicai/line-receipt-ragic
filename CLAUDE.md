# LINE 出差單據 → Ragic 自動填單機器人

## 專案目的 (WHY)
讓使用者(衍古開發顧問,城市規劃顧問業)出差時用手機拍車票/收據傳到 LINE,
經 Claude 影像辨識後,自動填入公司 Ragic 的「差旅費申請單」並附上單據照片。
先供本人試用,穩定後再推廣到全公司(約 20 人)。

## 系統流程 (WHAT)
拍照上傳(LINE) → Webhook 伺服器 → 抓原圖 → Claude 辨識(結構化 JSON)
→ 累積多張明細 → 回「確認」一次寫入 Ragic 同一張差旅費申請單(2026/09 改版後每列附上單據照片,見下方「表單改版對應」)。
目前選用 LINE 收圖(非技術同事手機都有);Discord 也評估過,中段與結尾的程式一樣。

## 目錄結構與檔案位置(2026-10-14 補記)
- 專案是**雙層巢狀**:本檔所在的 `報帳工具\` 只有本檔、已停用的 `專案訂單查詢.xlsx`、子資料夾 `line-receipt-ragic\`;
  **真正的程式在 `報帳工具\line-receipt-ragic\line-receipt-ragic\`**(下稱 `<code>`,`.venv` 也在這裡)。
- `<code>\CLAUDE.md` 是本檔的舊副本(2026-09-04 14:29,少了最後的「結案回饋檢查」一節)。**以本檔為準**,改內容時不要只改那份。
- 主要檔案:`server.py`(LINE webhook、Claude 辨識、問答狀態機)、`ragic.py`(專案清單、建單、追加、查單)、
  `ragic_fields.py`(欄位編號與科目清單)、`users.py`＋`users.json`(使用者身分)、`start_bot.ps1`/`restart_bot.ps1`、
  `references\台灣發票報帳對照.md`。
- 健康檢查端點:`GET http://localhost:8000/` 回 `{"status":"ok"}`;`POST /callback` 無簽章應回 400。
- log:`<code>\uvicorn.out.log`、`uvicorn.err.log`、`ngrok.out.log`、`ngrok.err.log`(每次重啟會被覆寫)。
- 版控:**目前沒有 git**(2026-10-14 確認)。要納入版控前必須先處理 `start_bot.ps1` 的明文 ngrok token 與 `users.json` 的個人金鑰,見「慣例與安全」。
- 自動化測試:無。驗證方式見本檔各節的「驗證」與「部署」。

### LINE 互動方式(問答式,目前)
狀態機 step:user_id → project/purpose/route/ready(server.py)。
1. 傳第一張單據 → 辨識後**自動開始問答**,依序問:① 專案名稱 → ② 出差事由 → ③ 起訖地點。
2. 使用者逐題回答(每題可回「跳過」用預設/單據帶入):
   - 專案:對 PROJECT_OPTIONS 模糊比對(子字串/相似度);多個相近會請更精確、查無提示換關鍵字。
   - 事由、起訖:直接當文字(可加關鍵字前綴,會自動去除)。
3. 三題答完 → 顯示核對畫面,使用者回「確認」寫入、「取消」清除。
4. 問答期間或 ready 後**再補傳照片** → 併入同一張申請單,並重述目前該回答的問題。
5. ready 後想改某欄 → 直接重打(「專案 …」「地點 …」「事由 …」),用 _apply_text 路由。
- 表頭值來源:出差日期取最早單據;起訖/事由/專案優先用問答輸入、否則退預設或單據帶入;摘要只放起訖。
- 快速回覆按鈕(情境式,server.py `_buttons_for(user_id)`):
  閒置時顯示「🚆出差 / 🧾代墊款」(選模式);選模式後進入填寫(active_entry),改顯示「✅確認 / ✖️取消」;
  送出成功或取消(_clear 會移除 active_entry)→ 回到選模式按鈕;登記中不顯示。
  狀態判斷 `_in_entry` = active_entry ∪ 有 step ∪ 有 pending。reply() 需帶 user_id 才會附按鈕。
- 專案清單(2026/09 改版:**每人用自己的金鑰即時讀 Ragic,回歸個人可見範圍**):
  來源改為 Ragic 專案表 `ragic.PROJECT_SHEET = data-inquiry/3`,**用該使用者的 ragic_key** 讀
  (沒填→用共用金鑰),排除訂單現況=已結案。→ 每人只比對得到「自己/自己團隊看得到的專案」,天然分權、永遠即時。
  - 快取:`ragic._proj_cache` 以「金鑰」為鍵,各人一份(讀不到保留舊快取);LINE 打「更新專案」= `refresh_projects(本人金鑰)`。
  - `match_project(q, user_key)`、`team_for(專案, user_key)`、`get_projects(user_key)` 都吃使用者金鑰;
    server.py 用 `_key(user_id)` 取金鑰後傳入(4 個比對點都改了)。差旅費/代墊款共用同一份(依金鑰)。
  - 實測:吳依珊金鑰看到 123 個、林哲安(共用)看到 31 個;「台南農再」兩者都比對得到並帶「台南團隊」。
  - ⚠️ 不再用 Excel「專案訂單查詢.XLSX」(該檔會過期、且是全公司單一份,無法分權);ragic_fields 的 PROJECT_XLSX_* 常數已停用備查。
  - ⚠️ 專案名稱是 Ragic「連結欄位」,只吃完全正確值;即時讀回的名稱本就是正確值 → match_project 命中即可安全送。
    DEFAULT_PROJECT/DM_DEFAULT_PROJECT 仍是硬字串,需為 Ragic 專案主檔的真實值。
- 接案團隊:UI 的「選專案自動帶團隊」是 link-and-load,**API 建單不會觸發**;
  故由 `ragic.team_for(專案, 本人金鑰)` 從該筆專案的「接案團隊」欄查出後**主動送 FIELD_TEAM/DM_FIELD_TEAM**。
  多團隊(Ragic 回 list)以逗號串接後送。

## 目前進度
- [x] 階段 1:帳號與金鑰、本機環境
- [x] 階段 2:LINE 收圖 → Claude 辨識 → 回傳欄位（見 server.py）
- [x] 階段 3:接 Ragic 寫入 —— 程式完成且欄位編號已全部到位(YKU/invoice-form/1)。
      (原「表單無附件欄位、不傳照片」已被 2026/09 表單改版推翻,現在照片進每一列。)
      欄位編號取得法見 ragic_fields.py 開頭(用「空更新回應以編號為鍵」這招,不需設計權限)。
      已實測建單(2026/09/03,見「表單改版對應」「追加明細」兩節的驗證)。
- [x] 常駐部署:固定 webhook 網址 + 開機自動啟動(見下方「部署」)。
- [x] 多人身分:首次登記(users.py / users.json),每人一份申請人/職稱/上班地點/團隊。
- [x] 出差/代墊款分流:指令「出差」/「代墊款」切換,代墊款寫入 invoice-form/2(見下方)。
- [x] 確認按鈕:已改 LINE 快速回覆鈕(見「LINE 互動方式」的 `_buttons_for`)。
- [ ] 階段 4:推廣到全公司(目前 users.json 登記 3 人)。

## 部署(常駐)
- 服務 = uvicorn(:8000)+ ngrok(固定網域)兩個程序,跑在使用者這台 Windows。
- 固定 webhook URL:`https://wireless-default-shelf.ngrok-free.dev/callback`(ngrok 免費固定網域,不會變)。
- 開機自動啟動:`start_bot.ps1`(會在沒在跑時才啟動 uvicorn/ngrok),
  由開機資料夾的 `LineReceiptBot.vbs`(隱藏視窗)在「登入時」呼叫。
  → 開機資料夾:%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\LineReceiptBot.vbs
- 限制:這台電腦要保持**開機且已登入**才會服務(登入觸發)。要關機/免登入也跑 → 改 Windows 服務或小雲端主機。
- 編碼雷:start_bot.ps1 必須存成 **UTF-8 帶 BOM**、VBS 存成 **UTF-16(Unicode)**,
  否則 PS 5.1 / wscript 會用 Big5 誤讀中文路徑「報帳工具」導致啟動失敗。
- 手動重啟:跑 `restart_bot.ps1`(停 :8000 → start_bot.ps1 → 健康檢查,一鍵完成);
  日誌 uvicorn.out.log / ngrok.out.log。
- **看門狗(自動復活)**:Windows 排程工作 `LineBotWatchdog`,每 3 分鐘以 wscript 跑開機資料夾的
  `LineReceiptBot.vbs`(→ 隱藏視窗呼叫冪等的 `start_bot.ps1`)。uvicorn 中途死掉時約數秒內自動拉回,
  ngrok 還活著就不動。只在使用者登入時跑(Interactive,符合「需開機且登入」前提),不需密碼。
  實測:殺掉 uvicorn → 觸發排程 → ~4s 恢復、LastTaskResult=0。
  管理:`Get-ScheduledTaskInfo -TaskName LineBotWatchdog`(看下次/上次結果)、
  `Start-ScheduledTask`(手動觸發)、`Unregister-ScheduledTask -TaskName LineBotWatchdog -Confirm:$false`(移除)。
  重建要點(原對話已不在):動作 `wscript.exe "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\LineReceiptBot.vbs"`,
  觸發 Once ＋ RepetitionInterval 3 分鐘,Principal 為登入使用者、Interactive。
  ⚠️ 看門狗無法處理「沒登入/關機」——那情況整台機器沒在服務,需改 Windows 服務或雲端主機。
- ⚠️ **多 bot 共存 / ngrok 判斷**(2026/07 踩過的雷):這台機器上另有一個**不同**的 LINE bot 專案在跑
  (`python -m line_bot.main`,port 8001,ngrok 網域 `sanctity-guide-upwind`,**不同 authtoken/帳號**)。
  舊版 start_bot.ps1 的 ngrok 守門用「有沒有『任何』ngrok 程序」判斷 → 一看到那個別專案的 ngrok
  就以為「通道有了」,**永遠不啟動我們自己的 `wireless-default-shelf` 通道** → 我方固定網域離線、
  LINE 打進來 404、bot 完全沒反應(uvicorn 明明活著也一樣)。
  **修法**:start_bot.ps1 改用 `Test-OurNgrok`——掃 ngrok.exe 命令列、只認含**我方網域**字串的那個;
  沒有才啟動。已實測:別專案 ngrok 在、殺掉我方 ngrok → 跑 start_bot.ps1 會把我方通道補回、/callback 復 400。
  兩個 ngrok agent(不同帳號)可在同機並存,免費方案各自 1 session 不衝突。
  診斷指令:`(Get-CimInstance Win32_Process -Filter "Name='ngrok.exe'").CommandLine`(看每個 ngrok 通到哪)、
  `Invoke-RestMethod http://localhost:4040/api/tunnels`(某個 agent 的通道對應埠)。
- Ragic API 通用知識 + 本公司欄位編號速查,已存成跨專案 skill:
  `~/.claude/skills/ragic-api/`(SKILL.md + references/yku-ragic.md)。改欄位編號時兩邊同步。
- ngrok 需 ≥3.20(本機已 update 到 3.39)。%LOCALAPPDATA%\ngrok\ngrok.yml 也存有 authtoken,但**啟動腳本實際不讀它**(見下一點)。
- ⚠️ 開機自動啟動時 ngrok 讀不到認證(實測:無 --config 時 ERR_NGROK_4018;
  指定 --config 時又「cannot find path」——登入初期 %LOCALAPPDATA% 設定檔讀不到)。
  最終解法:start_bot.ps1 的 ngrok 直接帶 `--authtoken <token>`(不依賴任何設定檔),
  並用 while 迴圈重試最多 5 次(每次間隔 8s),防登入初期環境未就緒。
  ⚠️ 代價:**token 以明文寫在 `start_bot.ps1`**。這支腳本不可外傳、不可提交版控;看 ngrok 程序命令列時 token 也會出現,貼出前要遮蔽。
- 驗證開機啟動:停掉 uvicorn+ngrok,跑開機資料夾的 LineReceiptBot.vbs,確認**兩個**程序都起來、
  且固定網址 POST /callback 回 400(直達程式)。注意:此模擬從已登入 session 跑,
  無法重現「真實重開機登入初期環境未就緒」,真正的驗證只能靠使用者實際重開機。
  若重開機後仍無反應:登出再登入、或點兩下開機資料夾的 LineReceiptBot.vbs(start_bot 具重試與冪等性)。

## 代墊款(invoice-form/2)
- 模式:預設「出差」(差旅費 invoice-form/1);打「代墊款」切到代墊款、「出差」切回(mode 狀態,切換會清批次)。
- 與差旅費差異:無出差日期/起訖/事由/上班地點/職稱;子表格群組 1000130;科目多種(非單一 6203)。
- 欄位編號見 ragic_fields.py 的 DM_* 區;子表格每列另有「付款對象」(=申請人)。
- 科目(支出項目):**完整會計科目清單 48 種**(DM_SUBJECT_OPTIONS,使用者提供),
  使用者**每筆**用關鍵字模糊比對選;此欄接受自由文字,match 命中後照原字串寫入。
  對話只顯示常用幾個(DM_SUBJECT_HINT),其他科目打關鍵字也比得到(子字串/別名/相似度)。
  別名表 DM_SUBJECT_ALIASES 補口語(內部餐/餐會/停車/文具/電話/印刷/健保/差旅/保險…)。
  ⚠️ 少數科目分隔符是「-」非「_」(6210-健保費、6226-政府稅捐-印花稅、6240-人事費-其他、
     6250-古宜霖-帳務往來),照使用者清單原樣;若寫入後標籤對不上再校正分隔符。
- LINE 流程:第一張照片→問「專案/用途」→再「逐筆」問【科目】再問【摘要說明】→核對→確認。
  (狀態 dm_project→dm_subject→dm_memo→…→ready;每筆先科目後摘要才換下一筆。)
- 摘要說明由問答輸入(OCR 品名當建議,回「同上」可沿用);是否電子發票用 OCR。
- 寫入用 ragic.create_advance(),一樣帶 doFormula=true。

## 2026/09 表單改版對應(附件上傳 + 新必填欄位)
兩張表單改版新增欄位,程式已跟進(欄位編號用「空更新回應反查」取得,見 ragic_fields.py DM_*/FIELD_* 區):
- **憑證上傳(附件)**:差旅費子表格 `1002909`、代墊款 `1002908`。LINE 傳的單據照片位元組在 server.py
  `process_image` 存進該筆 `data["_image"]`,確認時由 ragic.py `_flatten_multipart` 打包成 **multipart** 上傳
  (子表格檔案平面鍵 `<欄位編號>_-<列號>`,檔名走 ASCII `_img_name`)。有照片才走 multipart,否則沿用 JSON。
  → 這翻轉了舊的「表單沒有附件欄位、不傳照片」決定;現在照片直接進每一列。
- **有無紙本**:差旅費 `1002911`、代墊款 `1002910`,一律填「有」(使用者指定,`F.PAPER_VALUE`)。
- **實際使用者(可複選)**:差旅費子表格 `1002667`,**改版後為必填**;不送會 INVALID。
  create_expense 每列帶入申請人本人。(代墊款無此欄。)
- **起訖地點**(表頭 `1000089`)改版後也必填 → `_header_fields` 已加防呆:使用者「跳過」且單據無起訖時退回用出差事由。
- 其餘新欄位(店家/憑證金額/發票日期/發票號碼)編號已反查存於 ragic_fields.py 註解,目前未寫入(使用者未要求)。
- 驗證:2026/09/03 用測試 JPEG 各建一筆(差旅費 BT-…001、代墊款 BS-…003)→ 憑證上傳有值、有無紙本=有 → 刪除,全通過。

## 追加明細到既有單號(2026/09 新功能)
閒置時輸入既有費用單號(如 `BS-20260903004`)→ 之後拍的單據於「確認」時**追加**到那張記錄的子表格,不新建、不動表頭。
- 觸發(兩種):① 閒置時**按「➕追加」快速鈕**(送「追加」)→ 進入 `awaiting_no` → 背景 `_offer_recent`
  **列出使用者近期 10 張單號做成快速回覆鈕**讓他點(點了送出該單號);② 閒置時**直接貼單號**也可
  (`handle_text` 用 `_parse_no` regex `(B[TS])[-\s]?\d{6,}` 認出)。貼出/點出單號 → `_start_append`。
  前綴決定表單/模式:**BT=差旅費(travel)、BS=代墊款(advance)**。
- 近期單清單:`ragic.recent_records(本人金鑰, limit=10, applicant=本人姓名)` 合併撈差旅費+代墊款(各 GET 50 筆),
  取單號數字部分由大到小(=新到舊),回 {expense_no,sheet,mode,project,date}。
  **用 `applicant` 依「申請人」欄位過濾 → 只列該人自己送的單**(即使共用/管理者金鑰看得到全部,也只顯示本人的)。
  `_offer_recent` 把它做成 QuickReply(每顆 label/text=單號,+ 取消;≤13 顆);查不到則請他直接貼單號。
  `reply(...)` 新增 `quick` 參數:False=依狀態自動、None=不附、傳入 QuickReply=用指定的。
- 按鈕:`_mode_buttons` 三顆(🚆出差/🧾代墊款/➕追加);`awaiting_no` 時 `_buttons_for` 回 `_cancel_button`(只有取消)。
  `awaiting_no` 狀態:按追加鈕加入、貼出單號或取消時移除(`_clear` 也會清);拍照若還沒貼單號會先請他貼單號。
- 查單:ragic.py `find_record_by_no(單號, 本人金鑰)` 用 `?where=<單號欄位編號>,eq,<單號>` 查,並以「費用單號」逐筆比對確認;
  回 {sheet,gid,attach,ragic_id,expense_no,mode,applicant,project}。用本人金鑰 → 只找得到本人看得到的單。
- 狀態:`append_target[user_id]`;設定時 `_clear` 後再存(_clear 也會清它)。`_in_entry` 納入它(顯示確認/取消鈕)。
- 收單:`_append_add_photo` —— 代墊款仍逐筆問科目/摘要(但**不問專案**,表頭已存在);差旅費直接累積。
- 寫入:`append_expense`/`append_advance` → `_append_rows` POST 到 `/{ragic_id}`,body 只帶新子表格列(負數暫時列號)
  → Ragic 只新增這些列。有照片走 multipart。差旅費新列一樣補「實際使用者=申請人」。回應資料網址;單號沿用既有。
- ⚠ Ragic 表單有**伺服器端「防重複申報」驗證**(同人/同日/同類別/同專案;連 doFormula 都擋得住,刪除後仍記得),
  正常使用不受影響,但自動化測試要用每次唯一的資料避開。
- 驗證(2026/09/03):建拋棄式記錄→追加一列→列數 1→2→刪除,差旅費/代墊款兩路徑 + server 全流程(查單→問答→確認)皆通過。

## 多人身分(首次登記)
- users.py + users.json:user_id → {name, title, office, team},永久保存(原子寫入)。
- 未登記者傳照片/文字 → 先依序問 姓名→職稱→上班地點→Ragic金鑰(server.py 的 reg 狀態機),
  存檔後才進報帳流程(未登記前不下載/不辨識,省 API)。接案團隊不問(依專案連動)。
- 指令:「重新登記」更新身分、「我的資料」查詢。
- 建單時表頭的 申請人/職稱/付款對象(=申請人)/上班地點/接案團隊 由該人 profile 帶入;
  專案名稱用問答模糊比對、否則 DEFAULT_PROJECT。
- users.json 目前登記 3 位使用者(2026-10-14 確認;林哲安 user_id U76f6b…be56 為最早預先寫入者)。
- 每人自己的 Ragic 權限:登記第 ④ 題填本人「Ragic API 金鑰」(可「跳過」用共用)。
  建單 ragic._post_create() 用本人金鑰(沒填才用共用)。
  **方案 A(現行):本人填了自己的金鑰但沒有 API 寫入權(Ragic 回 access right/guest)→ 不退共用金鑰,
  改丟 RagicWritePermissionError**,server 回「你的 Ragic 帳號尚未開通 API 建單權限,請聯絡管理者」
  並保留批次(開通後再按確認即可)。→ 保護共用帳號:絕不會有別人的單掛到共用金鑰名下。
  ※ 實測:一般使用者(如吳依珊)的金鑰可「讀」但不可「API 建單」,需管理者開通該表單 API 寫入權。
    跳過金鑰(用共用)或共用/admin 金鑰才寫得進去。金鑰存 users.json(明碼,內部用)。

## 技術堆疊
Python 3.10+ / FastAPI + uvicorn / line-bot-sdk v3 / anthropic SDK / requests / python-dotenv。
實測可用版本:fastapi 0.136、uvicorn 0.49、line-bot-sdk 3.23、anthropic 0.109、python-dotenv 1.2。
執行環境:使用者在 Windows(PowerShell)。

## 執行方式(新機器從零建置;日常啟動見「部署」)
1. 在 `<code>` 內:`python -m venv .venv ; .venv\Scripts\activate`
2. `pip install -r requirements.txt`(其中 `openpyxl` 已無程式使用,可不裝)
3. 複製 `.env.example` 成 `.env`,填入金鑰
4. 啟動:跑 `start_bot.ps1`(uvicorn :8000 ＋ ngrok 固定網域),或 `restart_bot.ps1`
5. LINE Developers Console 的 Webhook URL = `https://wireless-default-shelf.ngrok-free.dev/callback`
(舊版寫的 cloudflared／trycloudflare 臨時網址已不用;`<code>\README.md` 仍是舊說法,以本檔為準。)

## 已查證的 API 事實（寫程式直接用,不必重查）
### LINE Messaging API（line-bot-sdk v3）
- 收圖:圖片事件 → `MessagingApiBlob(api_client).get_message_content(message_id=event.message.id)` 回傳 bytes。
- webhook 必須驗 `X-Line-Signature`（用 `WebhookHandler`）。
- reply token 時效短、只能用一次;辨識較慢 → 丟背景執行緒,webhook 先回 200。
### Ragic HTTP API
- 端點:`POST https://{server}/{帳號}/{頁籤}/{表單編號}?api&v=3`
- 認證:HTTP header `Authorization: Basic {RAGIC_API_KEY}`
- 一般欄位用 JSON;**檔案/圖片附件只能用 multipart/form-data 上傳**。
- 欄位以數字「欄位編號」對應;日期格式 `yyyy/MM/dd`。
- 目標表單含子表格「費用支出明細」(可多列),子表格欄位寫法與表頭不同,
  需參考 Ragic 文件「寫入 > 建立新資料」中的子表格範例。
### Claude API（辨識用）
- model:`claude-sonnet-4-6`(可改 `claude-haiku-4-5-20251001` 更省)。
- 圖片用 base64 image block 傳入,prompt 要求只回 JSON。
- **過載/暫時性錯誤自動重試**(server.py `extract_receipt`):遇到 `OverloadedError`(529)/
  `RateLimitError`(429)/`InternalServerError`(5xx)/`APIConnectionError` → 退避重試最多 5 次,
  間隔 2→5→10→20 秒(`_MAX_TRIES` / `_BACKOFF`)。多數瞬間過載幾秒內恢復,使用者通常無感。
  重試用盡仍失敗 → `process_image` 用 `_RETRYABLE` 攔截,回友善訊息「服務忙碌,稍後把同一張再傳一次」,
  **且狀態保留**(模式/問答/pending 都還在,只要重傳該張照片,不必重頭)。要更積極就加大 _BACKOFF。

## 目標 Ragic 表單:差旅費申請單
- 表頭(部分可由辨識帶入):出差日期、起訖地點、出差事由、請款金額…
- 子表格「費用支出明細」:支出憑證分類、支出項目、支出方式、摘要說明、數量、單價、小計、是否為電子發票、合併摘要。
- OCR 可填:子表格的支出項目/摘要/數量/單價/小計/是否電子發票 + 表頭的出差日期、起訖地點。
- 其餘(專案名稱、接案團隊、上班地點、職稱…)屬機構脈絡,由使用者選或設預設值。

## 表單事實(已查證,2026/06)
- 位置:ap13.ragic.com / YKU / invoice-form/1(已填入 .env)。
- 欄位編號:已全部填入 ragic_fields.py。
- (2026/06 時表單沒有附件欄位;2026/09 改版已新增「憑證上傳」子表格欄位,現在會傳照片,見「表單改版對應」。)
- 子表格選單欄位接受自由文字:支出項目格式 `6203_差旅費-{類別}`;支出方式固定「員工代墊」。
- 必填的機構脈絡欄位(申請人/職稱/付款對象/上班地點/專案名稱/接案團隊)由 HEADER_DEFAULTS 帶入。
- 建單請求帶 `doFormula=true` → Ragic 重算公式(總計、合併摘要…)。
- 摘要說明/起訖:站名去「高鐵/台鐵/捷運」前綴;高鐵類站名沒帶「站」會自動補「車站」。
- 是否為電子發票:交通票券(高鐵/捷運/計程車/飛機)一律「否」(_NON_EINVOICE_CATS);
  住宿/雜費/其他才用 OCR 結果(AI 看圖判斷電子發票不可靠,故用類別規則)。
- 發票金額通則:**報帳金額一律取「含稅總額」(總計/合計)**。陷阱在三聯式統一發票(有「銷售額/營業稅/總計」),
  要取含稅「總計」、非未稅銷售額;OCR prompt 已把單價與小計都填總計值(例:6190+310=總計6500 → 6500)。
  各類型(三聯式/二聯式/收銀機/電子發票證明聯/收據/車票…)的對照見
  references/台灣發票報帳對照.md。金額常是手寫(如「9」易誤判),提示已要求逐位小心。

## 慣例與安全
- 所有金鑰只放 `.env`,絕不寫進程式或 commit;`.env` 已列入 `.gitignore`。
  **例外(現況,待改善)**:`start_bot.ps1` 寫死 ngrok authtoken、`users.json` 存每人 Ragic 金鑰明碼。
  未來要 git init 時,這兩個檔必須排除(或先把 token 改由環境變數讀取),log 與 `專案訂單查詢.xlsx` 也要排除。
- 先前曾在聊天中貼過一批金鑰,視為已外流,務必到各平台 revoke/重發,新金鑰只放 `.env`。
- Claude Code 自身登入(Claude 訂閱或 Console 額度)與 `.env` 的 `ANTHROPIC_API_KEY`(機器人辨識用)是兩回事,別混用。

## Ragic 金鑰輪替 SOP(2026/09 踩過:重產金鑰後忘了同步 .env,bot 讀寫全失效)
- ⚠️ **在 Ragic 重新產生 API 金鑰後,舊金鑰立刻失效,但不會回 401**——它變成 guest:
  **HTTP 200 卻讀到 0 筆、建單被擋**。症狀 = 專案全空、任何表都 0 筆、建單失敗,但沒有明顯錯誤碼。
- **辨識法**:用該金鑰讀 `invoice-form/2` 應回非 0 筆;若三張表(invoice-form/1、/2、data-inquiry/3)全回 0 筆 HTTP 200
  → 幾乎確定是「金鑰失效(guest)」,先查對應金鑰。
- **輪替步驟**:
  1. 在 Ragic(右上角名字 → 個人工具/Personal Settings)產生新的 API/HTTP API 金鑰。
  2. 共用金鑰 → 貼進 `.env` 的 `RAGIC_API_KEY=`(存 UTF-8);個人金鑰 → 寫進 `users.json` 該人的 `ragic_key`
     (或請本人在 LINE 打「重新登記」到第④題貼上,較不外流)。
  3. **跑 `restart_bot.ps1`**(uvicorn 只在啟動時載入 `.env`,不重啟不會生效)。
  4. 驗證:讀 `invoice-form/2` 記錄數 > 0、LINE 打「更新專案」可比對數 > 0。
- 別在對話/聊天貼金鑰(會留紀錄);一律由使用者自己填檔。

## 給接手者的第一個任務
先讀 `<code>\server.py`(收圖、辨識、問答、確認流程)與 `<code>\ragic.py`(Ragic 寫入,單張單據 = 一列明細)。
服務目前正式上線、端對端已驗證(2026/09/03)。接手先做健康檢查:
`Get-NetTCPConnection -LocalPort 8000 -State Listen`、`Invoke-RestMethod http://localhost:8000/`、
`Get-ScheduledTaskInfo -TaskName LineBotWatchdog`(LastTaskResult=0 為正常)。
改程式後跑 `restart_bot.ps1`;測建單要用唯一資料並事後刪除(防重複申報驗證會擋重複資料)。

---

## 結案／交付前：跑衍古知識庫的回饋檢查

交付前跑一次 `Z:\8. AI協作\知識庫\衍古\01_AI協作共用知識\20_Workflows\案件成果沉澱機制.md`
的**結案回饋檢查七問**：①有新用途？②檢核清單漏了條款／圖層？③案件層寫了可通用的一次性腳本？
④使用者改了版面／寫作方式？⑤查了新法規？⑥踩了新坑？⑦移植或新訂了判準（門檻值／比率／判定規則）？

命中就**當下回寫**到知識庫對應位置：
- 坑 → `01_AI協作共用知識\50_Lessons\` ＋ `_工程教訓總表` 補一行
- 法規 → `03_案件經驗知識層\71_核心法規全文\`（逐點掃完再寫）
- **門檻值／判定規則 → `01_AI協作共用知識\00_Index\_判準登記表.md`**（移植判準必須登記；判斷法：改了會不會違法——會→查 [[法令檢核規則庫]]，不會→登記表 B 表）
- 可通用的工具／流程 → `01_AI協作共用知識\20_Workflows\`

> 為什麼要寫成規定：有回饋檢查的專案，教訓當天就進知識庫；沒有的專案平均延遲一個月以上
> （fishpv 的「QGIS 內建 Python 不可 pip 裝 numpy」躺在 README 一個多月才入庫）。
