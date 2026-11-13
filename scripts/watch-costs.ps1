# SPDX-License-Identifier: Apache-2.0
param(
    [string]$BaseUrl = "http://localhost:8000",
    [int]$IntervalSeconds = 5
)

$prev = @{}
$sessionStart = $null

function Get-Actors {
    try {
        $r = Invoke-RestMethod -Uri "$BaseUrl/api/actors" -TimeoutSec 5
        return $r
    } catch {
        Write-Host "  [error] Could not reach $BaseUrl/api/actors - is wactorz running?" -ForegroundColor Red
        return $null
    }
}

function Format-Cost([double]$usd) {
    if ($usd -eq 0) { return '$0.000000' }
    return ('$' + [math]::Round($usd, 6).ToString('0.000000'))
}

Clear-Host
Write-Host "Wactorz cost watcher  --  polling every ${IntervalSeconds}s  (Ctrl+C to stop)" -ForegroundColor Cyan
Write-Host "Base URL: $BaseUrl" -ForegroundColor DarkGray
Write-Host ""

while ($true) {
    $actors = Get-Actors
    if ($null -eq $actors) {
        Start-Sleep -Seconds $IntervalSeconds
        continue
    }

    $now = Get-Date
    $total = 0.0
    $rows  = @()

    foreach ($a in $actors) {
        $cost = if ($null -ne $a.costUsd) { [double]$a.costUsd } else { 0.0 }
        $total += $cost

        $delta = 0.0
        if ($prev.ContainsKey($a.name)) {
            $delta = $cost - $prev[$a.name]
        }
        $prev[$a.name] = $cost

        if ($cost -gt 0 -or $delta -ne 0) {
            $rows += [PSCustomObject]@{
                Agent  = $a.name
                Cost   = $cost
                Delta  = $delta
                State  = $a.state
            }
        }
    }

    if ($null -eq $sessionStart) {
        $sessionStart = $total
    }
    $sessionDelta = $total - $sessionStart

    $timestamp = $now.ToString("HH:mm:ss")
    Write-Host ""
    Write-Host "[$timestamp]" -ForegroundColor DarkGray -NoNewline
    Write-Host "  Total: " -NoNewline
    Write-Host (Format-Cost $total) -ForegroundColor Yellow -NoNewline
    Write-Host "   Session delta: " -NoNewline
    $deltaColor = if ($sessionDelta -gt 0) { "Green" } else { "DarkGray" }
    Write-Host ('+' + (Format-Cost $sessionDelta)) -ForegroundColor $deltaColor

    if ($rows.Count -eq 0) {
        Write-Host "  (no agents with cost yet)" -ForegroundColor DarkGray
    } else {
        $header = "  {0,-35} {1,12}  {2,12}  {3}" -f "Agent", "Cost", "+This poll", "State"
        Write-Host $header -ForegroundColor DarkGray
        Write-Host ("  " + ("-" * 70)) -ForegroundColor DarkGray

        foreach ($row in ($rows | Sort-Object Cost -Descending)) {
            if ($row.Delta -gt 0) {
                $deltaStr   = '+' + (Format-Cost $row.Delta)
                $deltaColor = "Green"
            } else {
                $deltaStr   = "-"
                $deltaColor = "DarkGray"
            }
            $stateColor = switch ($row.State) {
                "running" { "Cyan" }
                "idle"    { "DarkGray" }
                default   { "White" }
            }

            Write-Host ("  {0,-35} {1,12}  " -f $row.Agent, (Format-Cost $row.Cost)) -NoNewline
            Write-Host ("{0,12}  " -f $deltaStr) -ForegroundColor $deltaColor -NoNewline
            Write-Host $row.State -ForegroundColor $stateColor
        }
    }

    Start-Sleep -Seconds $IntervalSeconds
}
