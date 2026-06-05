# cloud_bake/monitor.ps1
#
# Vandir 50k render live monitor (PowerShell window).
# Polls each render box via SSH, tracks per-box MCA tile counts, shows a live
# ETA, beeps on milestones (25/50/75%), beeps + red on a 40-min stall, and
# plays a fanfare at completion (9,409 tiles).
#
# Usage (PowerShell, project root):
#   .\cloud_bake\monitor.ps1 -Ips 1.2.3.4,5.6.7.8,...      (the 8 render IPs)
# If execution is blocked:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#
# Matches render_50k.sh: round-robin z-rows (box b gets rows b, b+N, ...),
# MCAs written to /root/minecraft-worldgen/output/, done flag /root/r50_done.
#
# Keys: R = refresh now, Q = quit (renders keep running on the boxes).

param(
    [Parameter(Mandatory=$true)][string[]]$Ips,
    [int]$Grid = 97,
    [int]$PollMinutes = 5,
    [int]$StallMinutes = 40,
    [string]$RemoteOut = "/root/minecraft-worldgen/output"
)

# Robust IP parsing: Start-Process/-File can deliver "-Ips a,b,c" as a single
# comma-joined string instead of a [string[]]. Split + trim defensively.
if ($Ips.Count -eq 1 -and $Ips[0] -match ',') { $Ips = $Ips[0] -split ',' }
$Ips = @($Ips | ForEach-Object { $_.Trim() } | Where-Object { $_ })

$nb = $Ips.Count
# Round-robin row quota: box b renders rows {b, b+nb, b+2nb, ...} (each row = Grid tiles)
$tilesPerBox = @()
for ($b = 0; $b -lt $nb; $b++) {
    $rows = 0; for ($z = $b; $z -lt $Grid; $z += $nb) { $rows++ }
    $tilesPerBox += ($rows * $Grid)
}
$totalTiles = ($tilesPerBox | Measure-Object -Sum).Sum

$prevCounts   = @{}
$stallTracker = @{}
$startTime    = Get-Date
$milestonesHit = @{}   # 25/50/75 one-time pings

function Poll-AllBoxes {
    $result = @{ TotalNow = 0; PerBox = @{}; Done = @{}; Stalled = @(); Errors = @() }
    for ($i = 0; $i -lt $script:Ips.Count; $i++) {
        $ip = $script:Ips[$i]; $boxId = $i + 1; $quota = $script:tilesPerBox[$i]
        try {
            $raw = ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "root@$ip" `
                "ls $script:RemoteOut/r.*.mca 2>/dev/null | wc -l; test -f /root/r50_done && echo DONE" 2>$null
            $lines = @($raw -split "`n")
            $n = [int](($lines[0]) -replace '\D','')
            $isDone = ($raw -match 'DONE')
        } catch { $n = -1; $isDone = $false }

        if ($n -lt 0) { $result.Errors += $boxId } else { $result.TotalNow += $n }
        $result.PerBox[$boxId] = $n
        $result.Done[$boxId]   = $isDone

        if ($prevCounts.ContainsKey($boxId) -and $n -ge 0 -and -not $isDone) {
            if ($n -eq $prevCounts[$boxId] -and $n -lt $quota) {
                if (-not $stallTracker.ContainsKey($boxId)) { $stallTracker[$boxId] = 0 }
                $stallTracker[$boxId] += $script:PollMinutes
                if ($stallTracker[$boxId] -ge $script:StallMinutes) { $result.Stalled += $boxId }
            } else { $stallTracker[$boxId] = 0 }
        }
        $prevCounts[$boxId] = $n
    }
    return $result
}

function Render-Display {
    param($poll, [datetime]$now)
    Clear-Host
    $elapsed = $now - $script:startTime
    $elapsedHr = [Math]::Round($elapsed.TotalHours, 2)
    $pct = [Math]::Round($poll.TotalNow * 100.0 / $script:totalTiles, 1)
    # ETA from overall rate
    $eta = "--"
    if ($poll.TotalNow -gt 0 -and $elapsed.TotalMinutes -gt 1) {
        $rate = $poll.TotalNow / $elapsed.TotalMinutes   # tiles/min
        $remain = $script:totalTiles - $poll.TotalNow
        if ($rate -gt 0) {
            $etaMin = [Math]::Round($remain / $rate, 0)
            $finish = $now.AddMinutes($etaMin).ToString('HH:mm')
            $eta = "{0} min  (~{1})  @ {2} tiles/min" -f $etaMin, $finish, [Math]::Round($rate,1)
        }
    }
    Write-Host "=== Vandir 50k Monitor - $($now.ToString('HH:mm:ss'))  (elapsed $elapsedHr hr) ===" -ForegroundColor Cyan
    Write-Host ""
    Write-Host ("TOTAL: {0} / {1}  ({2}%)" -f $poll.TotalNow, $script:totalTiles, $pct) -ForegroundColor Yellow
    Write-Host ("ETA:   {0}" -f $eta) -ForegroundColor Yellow
    Write-Host ""
    for ($i = 0; $i -lt $script:Ips.Count; $i++) {
        $boxId = $i + 1; $n = $poll.PerBox[$boxId]; $quota = $script:tilesPerBox[$i]
        $sm = if ($stallTracker.ContainsKey($boxId)) { $stallTracker[$boxId] } else { 0 }
        $color = "White"; $tag = "running"
        if ($n -lt 0) { $color = "Red"; $tag = "SSH ERROR" }
        elseif ($poll.Done[$boxId]) { $color = "Green"; $tag = "DONE" }
        elseif ($sm -ge $script:StallMinutes) { $color = "Red"; $tag = "STALLED ($sm min)" }
        elseif ($sm -gt 0) { $color = "Yellow"; $tag = "no progress $sm min" }
        Write-Host ("  Box {0,2}: {1,5} / {2,5}  [{3}]  ({4})" -f $boxId, $n, $quota, $tag, $script:Ips[$i]) -ForegroundColor $color
    }
    Write-Host ""

    if ($poll.Errors.Count -gt 0) { Write-Host "SSH ERROR box(es): $($poll.Errors -join ', ')" -ForegroundColor Red; [console]::Beep(800,400) }
    if ($poll.Stalled.Count -gt 0) {
        Write-Host "STALL ALERT box(es): $($poll.Stalled -join ', ')  -> ssh root@<IP> 'tmux attach -t r50'" -ForegroundColor Red
        [console]::Beep(2000,500); Start-Sleep -Milliseconds 150; [console]::Beep(2000,500)
    }
    # Milestone pings (one-time)
    foreach ($m in 25,50,75) {
        if ($pct -ge $m -and -not $script:milestonesHit.ContainsKey($m)) {
            $script:milestonesHit[$m] = $true
            Write-Host ">>> $m% milestone reached" -ForegroundColor Green
            [console]::Beep(1500,250); Start-Sleep -Milliseconds 80; [console]::Beep(1800,250)
        }
    }
    $allDone = $true; foreach ($k in $poll.Done.Keys) { if (-not $poll.Done[$k]) { $allDone = $false } }
    if ($allDone -or $poll.TotalNow -ge $script:totalTiles) {
        Write-Host ""; Write-Host "RENDER COMPLETE! $($poll.TotalNow) / $script:totalTiles" -ForegroundColor Green
        1..5 | ForEach-Object { [console]::Beep(1500,300); Start-Sleep -Milliseconds 100; [console]::Beep(2500,300); Start-Sleep -Milliseconds 100 }
        return $true
    }
    Write-Host "[R] refresh now   [Q] quit   |   auto-refresh in $script:PollMinutes min" -ForegroundColor DarkGray
    return $false
}

function Wait-WithKeyHandler { param([int]$Seconds)
    $end = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $end) {
        if ([Console]::KeyAvailable) {
            $k = [Console]::ReadKey($true)
            if ($k.Key -eq 'R') { return 'refresh' }
            if ($k.Key -eq 'Q') { return 'quit' }
        }
        Start-Sleep -Milliseconds 200
    }
    return 'timeout'
}

Write-Host "Monitoring $nb box(es), $totalTiles tiles. Poll $PollMinutes min, stall alert $StallMinutes min." -ForegroundColor Cyan
while ($true) {
    $poll = Poll-AllBoxes
    if (Render-Display -poll $poll -now (Get-Date)) { break }
    if ((Wait-WithKeyHandler -Seconds ($PollMinutes * 60)) -eq 'quit') {
        Write-Host "Monitor stopped. Renders keep running on the boxes." -ForegroundColor Yellow; break
    }
}
