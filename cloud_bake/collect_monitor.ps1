# cloud_bake/collect_monitor.ps1
#
# Live monitor for the 50k COLLECT phase (tar-over-ssh -> local SSD).
# Samples the destination folder every N seconds: file count, GB, MB/s, ETA.
# Beeps + green when all tiles have landed.
#
# Usage (PowerShell):
#   .\cloud_bake\collect_monitor.ps1 -Dest D:\Vandir50k\region
# If blocked:  Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

param(
    [string]$Dest = "D:\Vandir50k\region",
    [int]$TotalTiles = 9409,
    [double]$TotalMB = 62875,
    [int]$RefreshSec = 20
)

$prevMB = $null; $prevTime = $null
Write-Host "Collect monitor: $Dest  (target $TotalTiles tiles / $TotalMB MB)" -ForegroundColor Cyan
while ($true) {
    $files = @(Get-ChildItem -Path $Dest -Filter "r.*.mca" -ErrorAction SilentlyContinue)
    $count = $files.Count
    $sum = ($files | Measure-Object -Property Length -Sum).Sum
    $mb = if ($sum) { [Math]::Round($sum / 1MB, 0) } else { 0 }
    $now = Get-Date
    $rate = 0.0
    if ($null -ne $prevMB) {
        $dt = ($now - $prevTime).TotalSeconds
        if ($dt -gt 0) { $rate = ($mb - $prevMB) / $dt }
    }
    $pct = [Math]::Round($count * 100.0 / $TotalTiles, 1)
    $eta = "--"
    if ($rate -gt 0.5) {
        $etaMin = [Math]::Round(($TotalMB - $mb) / $rate / 60, 0)
        $finish = $now.AddMinutes($etaMin).ToString('HH:mm')
        $eta = "$etaMin min  (~$finish)"
    }
    Clear-Host
    Write-Host "=== Vandir 50k COLLECT - $($now.ToString('HH:mm:ss')) ===" -ForegroundColor Cyan
    Write-Host ""
    Write-Host ("Files: {0,5} / {1}   ({2}%)" -f $count, $TotalTiles, $pct) -ForegroundColor Yellow
    Write-Host ("Data:  {0,5} / {1} MB" -f $mb, $TotalMB) -ForegroundColor Yellow
    Write-Host ("Rate:  {0} MB/s" -f [Math]::Round($rate, 1)) -ForegroundColor Yellow
    Write-Host ("ETA:   {0}" -f $eta) -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Dest: $Dest" -ForegroundColor DarkGray
    if ($count -ge $TotalTiles) {
        Write-Host "`nCOLLECT COMPLETE! $count / $TotalTiles" -ForegroundColor Green
        1..5 | ForEach-Object { [console]::Beep(1500,300); Start-Sleep -Milliseconds 100; [console]::Beep(2500,300); Start-Sleep -Milliseconds 100 }
        break
    }
    $prevMB = $mb; $prevTime = $now
    Start-Sleep -Seconds $RefreshSec
}
