# cloud_bake/monitor.ps1
#
# Vandir render auto-monitor.
# Polls each render box every 15 min via SSH, tracks MCA tile counts per
# box, detects stalls (no progress for 45+ min), and alerts on stalls /
# completion via audible beeps + colored console output.
#
# Usage (on your laptop, PowerShell):
#   cd C:\Users\nicho\minecraft-worldgen
#   .\cloud_bake\monitor.ps1
#
# If PowerShell blocks script execution:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\cloud_bake\monitor.ps1
#
# Behavior:
#   - Per-box display with color (green=done, yellow=slow, red=stalled/error)
#   - Stall alert (audible beep + red text) when a box's tile count
#     doesn't increase for 3 consecutive 15-min polls (= 45 min stalled)
#   - Completion alert (5-beep fanfare) when total reaches 9,409
#   - SSH error alert (single beep) when ssh can't reach a box
#   - Refreshes every 15 min, indefinitely (Ctrl+C to stop)
#   - Renders keep running even if you stop the monitor

# Edit these IPs to match your worker droplets (in z-stripe order):
$ips = @(
    '142.93.250.15','137.184.53.235','137.184.69.99','192.241.153.34',
    '162.243.162.91','104.248.113.140','64.227.11.255','198.199.83.151',
    '159.223.179.166','104.248.239.34'
)
$totalTiles = 9409
$tilesPerBox = 940
$pollMinutes = 15
$stallThresholdMinutes = 45

$prevCounts = @{}
$stallMinutes = @{}
$startTime = Get-Date

while ($true) {
    $now = Get-Date
    $totalNow = 0
    $perBox = @{}
    $stalledBoxes = @()
    $errorBoxes = @()

    for ($i = 0; $i -lt $ips.Count; $i++) {
        $ip = $ips[$i]
        $boxId = $i + 1
        try {
            $raw = ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "root@$ip" "ls /root/minecraft-worldgen/output_s83v17_world/*.mca 2>/dev/null | wc -l" 2>$null
            $n = [int]($raw -replace '\D','')
        } catch { $n = -1 }

        if ($n -lt 0) { $errorBoxes += $boxId } else { $totalNow += $n }
        $perBox[$boxId] = $n

        if ($prevCounts.ContainsKey($boxId) -and $n -ge 0) {
            if ($n -eq $prevCounts[$boxId] -and $n -lt $tilesPerBox) {
                if (-not $stallMinutes.ContainsKey($boxId)) { $stallMinutes[$boxId] = 0 }
                $stallMinutes[$boxId] += $pollMinutes
                if ($stallMinutes[$boxId] -ge $stallThresholdMinutes) {
                    $stalledBoxes += $boxId
                }
            } else {
                $stallMinutes[$boxId] = 0
            }
        }
        $prevCounts[$boxId] = $n
    }

    Clear-Host
    $elapsedHr = [Math]::Round(($now - $startTime).TotalHours, 1)
    Write-Host "=== Vandir Render Monitor - $($now.ToString('HH:mm:ss'))  (elapsed: $elapsedHr hr) ===" -ForegroundColor Cyan
    Write-Host ""
    $pct = [Math]::Round($totalNow * 100.0 / $totalTiles, 1)
    Write-Host "TOTAL: $totalNow / $totalTiles  ($pct%)" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Per box:"
    for ($i = 0; $i -lt $ips.Count; $i++) {
        $boxId = $i + 1
        $n = $perBox[$boxId]
        $stall = if ($stallMinutes.ContainsKey($boxId)) { $stallMinutes[$boxId] } else { 0 }
        $color = "White"
        $tag = ""
        if ($n -lt 0) { $color = "Red"; $tag = "SSH ERROR" }
        elseif ($n -ge $tilesPerBox) { $color = "Green"; $tag = "DONE" }
        elseif ($stall -ge $stallThresholdMinutes) { $color = "Red"; $tag = "STALLED ($stall min)" }
        elseif ($stall -gt 0) { $color = "Yellow"; $tag = "no progress $stall min" }
        else { $tag = "running" }
        Write-Host ("  Box {0,2}: {1,4} / {2,4}  [{3}]  ({4})" -f $boxId, $n, $tilesPerBox, $tag, $ips[$i]) -ForegroundColor $color
    }
    Write-Host ""

    if ($errorBoxes.Count -gt 0) {
        Write-Host "SSH ERRORS on box(es): $($errorBoxes -join ', ')" -ForegroundColor Red
        [console]::Beep(800, 400)
    }
    if ($stalledBoxes.Count -gt 0) {
        Write-Host "STALL ALERT on box(es): $($stalledBoxes -join ', ')" -ForegroundColor Red
        Write-Host "  SSH in and check: ssh root@<IP> 'tmux attach -t render-box<N>'" -ForegroundColor Yellow
        [console]::Beep(2000, 500)
        Start-Sleep -Milliseconds 200
        [console]::Beep(2000, 500)
    }
    if ($totalNow -ge $totalTiles) {
        Write-Host ""
        Write-Host "RENDER COMPLETE! Total: $totalNow / $totalTiles" -ForegroundColor Green
        Write-Host "Next: run collect_outputs.sh to download MCAs" -ForegroundColor Green
        1..5 | ForEach-Object { [console]::Beep(1500, 300); Start-Sleep -Milliseconds 100; [console]::Beep(2500, 300); Start-Sleep -Milliseconds 100 }
        break
    }

    Write-Host ""
    Write-Host "Next check in $pollMinutes min. Ctrl+C to stop monitor (renders keep running)." -ForegroundColor DarkGray
    Start-Sleep -Seconds ($pollMinutes * 60)
}
