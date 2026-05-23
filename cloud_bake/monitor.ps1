# cloud_bake/monitor.ps1
#
# Vandir render auto-monitor.
# Polls each render box every 15 min via SSH (or on-demand via R key),
# tracks MCA tile counts per box, detects stalls (no progress for 45+
# min), and alerts on stalls / completion via audible beeps + colored
# console output.
#
# Usage (on your laptop, PowerShell):
#   cd C:\Users\nicho\minecraft-worldgen
#   .\cloud_bake\monitor.ps1
#
# If PowerShell blocks script execution:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\cloud_bake\monitor.ps1
#
# Interactive keys while monitor is running:
#   R or r   - refresh immediately (skip the 15 min wait)
#   Q or q   - quit the monitor (renders keep running)
#   Ctrl+C   - also works to quit
#
# Behavior:
#   - Per-box display with color (green=done, yellow=slow, red=stalled/error)
#   - Stall alert (audible beep + red text) when a box's tile count
#     doesn't increase for 3 consecutive 15-min polls (= 45 min stalled)
#   - Completion alert (5-beep fanfare) when total reaches 9,409
#   - SSH error alert (single beep) when ssh can't reach a box
#   - Auto-refreshes every 15 min, indefinitely
#   - Renders keep running even if you stop the monitor

# Worker boxes (Hetzner CCX63 in Ashburn):
$ips = @(
    '5.78.221.156','5.78.208.65','5.78.74.149','5.78.216.24',
    '5.78.211.133','5.78.197.171','5.78.223.37','5.78.215.210'
)
# Tiles per box (z-stripe partition of 97 rows across 8 boxes):
# Box 1: 13 rows x 97 cols = 1261. Boxes 2-8: 12 rows x 97 cols = 1164.
$tilesPerBoxArr = @(1261, 1164, 1164, 1164, 1164, 1164, 1164, 1164)
$totalTiles = 9409
$pollMinutes = 15
$stallThresholdMinutes = 45

$prevCounts = @{}
$stallMinutes = @{}
$startTime = Get-Date

function Poll-AllBoxes {
    param([hashtable]$prev, [hashtable]$stall)
    $result = @{
        TotalNow = 0
        PerBox = @{}
        Stalled = @()
        Errors = @()
    }
    for ($i = 0; $i -lt $script:ips.Count; $i++) {
        $ip = $script:ips[$i]
        $boxId = $i + 1
        $perBoxQuota = $script:tilesPerBoxArr[$i]
        try {
            $raw = ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "root@$ip" "ls /root/minecraft-worldgen/output_s83v17_world/*.mca 2>/dev/null | wc -l" 2>$null
            $n = [int]($raw -replace '\D','')
        } catch { $n = -1 }

        if ($n -lt 0) { $result.Errors += $boxId } else { $result.TotalNow += $n }
        $result.PerBox[$boxId] = $n

        if ($prev.ContainsKey($boxId) -and $n -ge 0) {
            if ($n -eq $prev[$boxId] -and $n -lt $perBoxQuota) {
                if (-not $stall.ContainsKey($boxId)) { $stall[$boxId] = 0 }
                $stall[$boxId] += $script:pollMinutes
                if ($stall[$boxId] -ge $script:stallThresholdMinutes) {
                    $result.Stalled += $boxId
                }
            } else {
                $stall[$boxId] = 0
            }
        }
        $prev[$boxId] = $n
    }
    return $result
}

function Render-Display {
    param($poll, [datetime]$now, [datetime]$start, [hashtable]$stall)
    Clear-Host
    $elapsedHr = [Math]::Round(($now - $start).TotalHours, 2)
    Write-Host "=== Vandir Render Monitor - $($now.ToString('HH:mm:ss'))  (elapsed: $elapsedHr hr) ===" -ForegroundColor Cyan
    Write-Host ""
    $pct = [Math]::Round($poll.TotalNow * 100.0 / $script:totalTiles, 2)
    Write-Host "TOTAL: $($poll.TotalNow) / $script:totalTiles  ($pct%)" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Per box:"
    for ($i = 0; $i -lt $script:ips.Count; $i++) {
        $boxId = $i + 1
        $n = $poll.PerBox[$boxId]
        $quota = $script:tilesPerBoxArr[$i]
        $sm = if ($stall.ContainsKey($boxId)) { $stall[$boxId] } else { 0 }
        $color = "White"
        $tag = ""
        if ($n -lt 0) { $color = "Red"; $tag = "SSH ERROR" }
        elseif ($n -ge $quota) { $color = "Green"; $tag = "DONE" }
        elseif ($sm -ge $script:stallThresholdMinutes) { $color = "Red"; $tag = "STALLED ($sm min)" }
        elseif ($sm -gt 0) { $color = "Yellow"; $tag = "no progress $sm min" }
        else { $tag = "running" }
        Write-Host ("  Box {0,2}: {1,4} / {2,4}  [{3}]  ({4})" -f $boxId, $n, $quota, $tag, $script:ips[$i]) -ForegroundColor $color
    }
    Write-Host ""

    if ($poll.Errors.Count -gt 0) {
        Write-Host "SSH ERRORS on box(es): $($poll.Errors -join ', ')" -ForegroundColor Red
        [console]::Beep(800, 400)
    }
    if ($poll.Stalled.Count -gt 0) {
        Write-Host "STALL ALERT on box(es): $($poll.Stalled -join ', ')" -ForegroundColor Red
        Write-Host "  SSH in and check: ssh root@<IP> 'tmux attach -t render-box<N>'" -ForegroundColor Yellow
        [console]::Beep(2000, 500)
        Start-Sleep -Milliseconds 200
        [console]::Beep(2000, 500)
    }
    if ($poll.TotalNow -ge $script:totalTiles) {
        Write-Host ""
        Write-Host "RENDER COMPLETE! Total: $($poll.TotalNow) / $script:totalTiles" -ForegroundColor Green
        Write-Host "Next: run collect_outputs.sh to download MCAs" -ForegroundColor Green
        1..5 | ForEach-Object { [console]::Beep(1500, 300); Start-Sleep -Milliseconds 100; [console]::Beep(2500, 300); Start-Sleep -Milliseconds 100 }
        return $true  # signal completion
    }
    Write-Host ""
    Write-Host "[R] refresh now   [Q] quit   |   auto-refresh in $script:pollMinutes min" -ForegroundColor DarkGray
    return $false
}

function Wait-WithKeyHandler {
    param([int]$Seconds)
    $end = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $end) {
        if ([Console]::KeyAvailable) {
            $key = [Console]::ReadKey($true)
            switch ($key.Key) {
                'R' {
                    return 'refresh'
                }
                'Q' {
                    return 'quit'
                }
            }
        }
        Start-Sleep -Milliseconds 200
    }
    return 'timeout'
}

# Main loop
while ($true) {
    $now = Get-Date
    $poll = Poll-AllBoxes -prev $prevCounts -stall $stallMinutes
    $done = Render-Display -poll $poll -now $now -start $startTime -stall $stallMinutes
    if ($done) { break }

    $action = Wait-WithKeyHandler -Seconds ($pollMinutes * 60)
    if ($action -eq 'quit') {
        Write-Host ""
        Write-Host "Monitor stopped. Renders keep running on cloud boxes." -ForegroundColor Yellow
        Write-Host "Re-run .\cloud_bake\monitor.ps1 anytime to resume monitoring." -ForegroundColor DarkGray
        break
    }
    # If 'refresh' or 'timeout' → loop runs Poll + Render again
}
