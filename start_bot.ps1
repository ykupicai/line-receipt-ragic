$ErrorActionPreference = "SilentlyContinue"
$dir = "C:\Users\user\Documents\Claude\Projects\報帳工具\line-receipt-ragic\line-receipt-ragic"
Set-Location $dir
$uvicorn = "$dir\.venv\Scripts\uvicorn.exe"
$ngrok   = "C:\Users\user\AppData\Local\Microsoft\WinGet\Links\ngrok.exe"
$token   = (Get-Content -LiteralPath "$dir\ngrok_token.local" -TotalCount 1).Trim()   # token 不進版控，存在同資料夾的 ngrok_token.local
$domain  = "wireless-default-shelf.ngrok-free.dev"

if (-not (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue)) {
    Start-Process -WindowStyle Hidden -FilePath $uvicorn -ArgumentList "server:app","--port","8000" -RedirectStandardOutput "$dir\uvicorn.out.log" -RedirectStandardError "$dir\uvicorn.err.log"
}

# ngrok:只在「我們自己的固定網域」通道沒在跑時才啟動(別的專案的 ngrok 不算數,
# 否則會誤判成已就緒而永遠不補自己的通道)。--authtoken 直接帶(不依賴設定檔);
# 失敗自動重試最多 5 次。
function Test-OurNgrok {
    $ps = Get-CimInstance Win32_Process -Filter "Name='ngrok.exe'" -ErrorAction SilentlyContinue
    foreach ($p in $ps) { if ($p.CommandLine -and $p.CommandLine -like "*$domain*") { return $true } }
    return $false
}
$tries = 0
while ($tries -lt 5 -and -not (Test-OurNgrok)) {
    Start-Process -WindowStyle Hidden -FilePath $ngrok -ArgumentList "http","--authtoken",$token,"--url=https://$domain","8000" -RedirectStandardOutput "$dir\ngrok.out.log" -RedirectStandardError "$dir\ngrok.err.log"
    Start-Sleep -Seconds 8
    $tries++
}