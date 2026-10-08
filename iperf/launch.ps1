param(
    [Parameter(Mandatory=$true)][ValidateSet('adaptive','fixed')][string]$Mode,
    [Parameter(Mandatory=$true)][ValidateSet('send','receive')][string]$Role,
    [string]$PythonPath,
    [string]$IperfPath,
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$projectRoot = Split-Path -Parent $PSScriptRoot
try {
    Set-Location -LiteralPath $projectRoot
    # 复用已经修正过的Python定位流程；CheckPython不会启动旧文件程序或修改网络。
    if (-not $PythonPath) {
        $pythonOutput = & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $projectRoot 'launch_experiment.ps1') -Role receive -CheckPython
        if ($LASTEXITCODE -ne 0) { throw '需要可运行的Python 3.10或更新版本。' }
        $PythonPath = ([string]($pythonOutput | Select-Object -Last 1)).Trim()
    }
    if (-not $IperfPath) {
        & $PythonPath -m iperf.common.runtime
        if ($LASTEXITCODE -ne 0) { throw 'iperf3安装或校验失败，请检查下载网络。' }
        $IperfPath = Join-Path $PSScriptRoot 'tools\runtime\iperf3.exe'
    }
    if ($CheckOnly) { Write-Output "Python=$PythonPath; iperf=$IperfPath"; exit 0 }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        # 此窗口需要交互选择网卡/IP；在管理员窗口中启动同一个入口。
        $arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -Mode {1} -Role {2} -PythonPath "{3}" -IperfPath "{4}"' -f $PSCommandPath,$Mode,$Role,$PythonPath,$IperfPath
        Start-Process powershell.exe -Verb RunAs -ArgumentList $arguments -Wait
        exit 0
    }
    $config = Get-Content -LiteralPath (Join-Path $PSScriptRoot "$Mode\config.json") -Raw | ConvertFrom-Json
    foreach ($protocol in @('TCP','UDP')) {
        $rule = "1008CPE-file-iperf-$Mode-$Role-$protocol"
        if (Get-NetFirewallRule -Name $rule -ErrorAction SilentlyContinue) {
            Remove-NetFirewallRule -Name $rule
        }
        $ports = if ($Role -eq 'send') { @($config.lan.port, $config.cpe.port) } else { 'Any' }
        # iperf反向模式也需要TCP控制。接收UDP端口由iperf分配，因此按程序限定。
        New-NetFirewallRule -Name $rule -DisplayName $rule -Direction Inbound -Action Allow `
            -Protocol $protocol -LocalPort $ports -Program $IperfPath -Profile Any | Out-Null
    }
    Write-Host "模式=$Mode；角色=$Role；Python=$PythonPath"
    & $PythonPath (Join-Path $PSScriptRoot "$Mode\$Role\main.py") --iperf $IperfPath
    if ($LASTEXITCODE -ne 0) { throw "实验未完成，退出码=$LASTEXITCODE，请查看窗口提示和结果。" }
} catch {
    Write-Host "启动失败：$($_.Exception.Message)" -ForegroundColor Red
    if ($CheckOnly) { exit 1 }
}
Read-Host '按回车关闭窗口'
