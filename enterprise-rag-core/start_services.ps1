<#
.SYNOPSIS
    Enterprise RAG Core - One-shot launcher (Windows).
.DESCRIPTION
    Bootstraps the standalone enterprise-rag-core MCP service: venv self-heal,
    Docker/Redis Stack, Ollama embeddings check, reranker model download,
    knowledge-base prepopulation (idempotent), and the streamable-HTTP MCP
    server on port 8010, with an optional Cloudflare tunnel.

    macOS Apple Silicon note: this PowerShell launcher targets Windows/Ollama.
    On macOS set RAG_CORE_EMBED_BACKEND=mlx + RAG_CORE_MLX_BASE_URL and start
    the service with: .venv/bin/enterprise-rag-core serve --port 8010
.PARAMETER KbPath
    Markdown knowledge base to prepopulate (doc-id "meridian-kb", tenant
    "default", required marker "meridian university"). Empty = skip.
.PARAMETER Port
    MCP server port (default 8010).
.PARAMETER SkipPrepopulate
    Skip the prepopulate step even when -KbPath is given.
.PARAMETER WithTunnel
    Start a Cloudflare quick tunnel for the MCP port.
.PARAMETER NamedTunnel
    Use a named Cloudflare tunnel (permanent URL) instead of a quick tunnel.
.PARAMETER TunnelName
    Named tunnel name (default "erc-rag-tunnel").
.EXAMPLE
    .\start_services.ps1
    .\start_services.ps1 -KbPath "D:\project\universityDemo\content\meridian\meridian_knowledge_base.md"
    .\start_services.ps1 -KbPath <kb> -WithTunnel
#>

param(
    [string]$KbPath = "",
    [int]$Port = 8010,
    [switch]$SkipPrepopulate = $false,
    [switch]$WithTunnel = $false,
    [switch]$NamedTunnel = $false,
    [string]$TunnelName = "erc-rag-tunnel"
)

# ---- Config ---------------------------------------------------------------
$ProjectRoot = $PSScriptRoot
$TunnelFile  = Join-Path $ProjectRoot ".erc_tunnel"
$VenvPython  = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$MCPUrl      = "http://127.0.0.1:$Port/mcp"
$MCPLog      = Join-Path $env:TEMP "erc_mcp.log"
$MCPErrLog   = Join-Path $env:TEMP "erc_mcp_err.log"
Write-Host "  Python: $(if (Test-Path $VenvPython) { $VenvPython } else { 'python (venv will be created)' })"

$ESC  = [char]27
$GREEN = "$ESC[32m"; $YELLOW = "$ESC[33m"; $RED = "$ESC[31m"
$CYAN = "$ESC[36m"; $RESET = "$ESC[0m"; $BOLD = "$ESC[1m"

function Write-Step   { Write-Host ("{0}{1}{2}--- {3} ---{4}" -f "`n", $CYAN, $BOLD, ($args -join ' '), $RESET) }
function Write-OK     { Write-Host ("{0}  OK: {1}{2}" -f $GREEN, ($args -join ' '), $RESET) }
function Write-Warn   { Write-Host ("{0}  WARN: {1}{2}" -f $YELLOW, ($args -join ' '), $RESET) }
function Write-Err    { Write-Host ("{0}  ERROR: {1}{2}" -f $RED, ($args -join ' '), $RESET) }

# ---- Tool discovery (user-scope winget installs are invisible to old shells) ----
function Find-DockerCli {
    $cmd = Get-Command "docker" -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($p in @(
        (Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\resources\bin\docker.exe"),
        "C:\Program Files\Docker\Docker\resources\bin\docker.exe"
    )) {
        if (Test-Path $p) { return $p }
    }
    return $null
}

function Find-DockerDesktopExe {
    foreach ($p in @(
        (Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\Docker Desktop.exe"),
        "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    )) {
        if (Test-Path $p) { return $p }
    }
    return $null
}

function Find-CloudflaredExe {
    $cmd = Get-Command "cloudflared" -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $p = Join-Path $env:LOCALAPPDATA "Programs\cloudflared\cloudflared.exe"
    if (Test-Path $p) { return $p }
    return $null
}

function Start-QuickTunnel {
    # Cloudflare quick tunnel for the MCP port; reuses a live cached URL and
    # never double-starts (same conventions as universityDemo's launchers).
    param(
        [string]$Exe,
        [int]$Port,
        [string]$CacheFile,
        [string]$LogBase
    )

    if (Test-Path $CacheFile) {
        $cached = (Get-Content $CacheFile -Raw).Trim()
        if ($cached) {
            $cachedCode = curl.exe -s -o NUL -w "%{http_code}" "https://$cached/" 2>$null
            if ($cachedCode -eq "200") {
                Write-OK ("tunnel already alive: {0}" -f $cached)
                return $cached
            }
        }
    }

    $existing = Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match "localhost:$Port" }
    if ($existing) {
        Write-Warn "tunnel process already running but its URL is unknown"
        Write-Warn "Kill it and re-run to recreate: taskkill /IM cloudflared.exe"
        return $null
    }

    $outLog = Join-Path $env:TEMP ("{0}.log" -f $LogBase)
    $errLog = Join-Path $env:TEMP ("{0}_err.log" -f $LogBase)
    foreach ($f in @($outLog, $errLog)) {
        if (Test-Path $f) { Remove-Item $f -Force -ErrorAction SilentlyContinue }
    }

    $cfArgs = @{
        FilePath               = $Exe
        ArgumentList           = "tunnel", "--url", "http://localhost:$Port", "--metrics", "localhost:0"
        WindowStyle            = "Hidden"
        PassThru               = $true
        RedirectStandardOutput = $outLog
        RedirectStandardError  = $errLog
    }
    $proc = Start-Process @cfArgs
    Write-OK ("tunnel starting (PID {0}) - log: {1}" -f $proc.Id, $outLog)

    $tunnelHost = $null
    $attempt = 0
    while (-not $tunnelHost -and $attempt -lt 15) {
        Start-Sleep -Seconds 3
        $attempt++
        $logContent = ""
        foreach ($log in @($outLog, $errLog)) {
            if (Test-Path $log) {
                $logContent += Get-Content $log -Raw -ErrorAction SilentlyContinue
            }
        }
        if ($logContent) {
            $m = ([regex]'https://([a-zA-Z0-9\-]+\.trycloudflare\.com)').Match($logContent)
            if ($m.Success) { $tunnelHost = $m.Groups[1].Value }
        }
        if (-not $tunnelHost) {
            Write-Warn ("Waiting for tunnel URL... ({0}/15)" -f $attempt)
        }
    }

    if (-not $tunnelHost) {
        Write-Warn "tunnel did not start -- re-run to retry"
        return $null
    }

    [System.IO.File]::WriteAllText($CacheFile, $tunnelHost)
    $verified = $false
    for ($v = 0; $v -lt 10 -and -not $verified; $v++) {
        if ($v -gt 0) { Start-Sleep -Seconds 5 }
        $verified = (curl.exe -s --connect-timeout 8 -o NUL -w "%{http_code}" "https://$tunnelHost/" 2>$null) -eq "200"
    }
    if ($verified) {
        Write-OK ("tunnel reachable: https://{0}/" -f $tunnelHost)
    } else {
        Write-Warn ("tunnel started but not yet reachable (DNS warm-up): {0}" -f $tunnelHost)
    }
    return $tunnelHost
}

function Ensure-ProjectDeps {
    # Self-heal: probe the package + pinned SDKs; create the venv and install
    # if broken (first run downloads ~200 MB).
    $probePy = if (Test-Path $VenvPython) { $VenvPython } else { "python" }
    & $probePy -c "import enterprise_rag, mcp.server, chromadb, httpx" 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-OK "venv dependencies OK (enterprise_rag + pinned SDKs importable)"
        return $true
    }

    Write-Warn "venv missing or broken -- creating + installing (first run downloads ~200 MB)..."
    if (-not (Test-Path $VenvPython)) {
        $venvDir = Join-Path $ProjectRoot ".venv"
        if (Get-Command "py" -ErrorAction SilentlyContinue) { & py -3.11 -m venv $venvDir }
        else { & python -m venv $venvDir }
        if ($LASTEXITCODE -ne 0) {
            Write-Err "venv creation failed"
            return $false
        }
    }
    & $VenvPython -m pip install -r (Join-Path $ProjectRoot "requirements-dev.txt") 2>$null | Out-Null
    & $VenvPython -m pip install -e $ProjectRoot --no-deps 2>$null | Out-Null
    & $VenvPython -c "import enterprise_rag, mcp.server, chromadb, httpx" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Err "dependencies still not importable -- check the output above"
        return $false
    }
    Write-OK "Dependencies installed and importable"
    return $true
}

# ==== Step 0: dependency self-heal =========================================
if (-not (Ensure-ProjectDeps)) {
    Write-Err "Cannot start without working dependencies -- fix the errors above and re-run."
    exit 1
}

# Load .env (KEY=VALUE lines; comments skipped; never overrides existing vars)
$EnvFile = Join-Path $ProjectRoot ".env"
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        $m = [regex]::Match($_, '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$')
        if ($m.Success) {
            $key = $m.Groups[1].Value
            if ([string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable($key))) {
                [Environment]::SetEnvironmentVariable($key, $m.Groups[2].Value, "Process")
            }
        }
    }
    Write-OK (".env loaded from {0}" -f $EnvFile)
}

# Sensible repo-local defaults (never override explicit configuration)
if ([string]::IsNullOrEmpty($env:RAG_CORE_CHROMA_PATH)) {
    $env:RAG_CORE_CHROMA_PATH = Join-Path $ProjectRoot "chroma_data"
}
if ([string]::IsNullOrEmpty($env:RAG_CORE_CHROMA_COLLECTION)) {
    $env:RAG_CORE_CHROMA_COLLECTION = "meridian-kb"
}
if ([string]::IsNullOrEmpty($env:RAG_CORE_DEFAULT_TENANT)) {
    $env:RAG_CORE_DEFAULT_TENANT = "default"
}
if ([string]::IsNullOrEmpty($env:RAG_CORE_VECTOR_BACKEND)) { $env:RAG_CORE_VECTOR_BACKEND = "chroma" }
if ([string]::IsNullOrEmpty($env:RAG_CORE_KEYWORD_BACKEND)) { $env:RAG_CORE_KEYWORD_BACKEND = "bm25" }
if ([string]::IsNullOrEmpty($env:RAG_CORE_EMBED_BACKEND)) { $env:RAG_CORE_EMBED_BACKEND = "auto" }
if ([string]::IsNullOrEmpty($env:RAG_CORE_CACHE_BACKEND)) { $env:RAG_CORE_CACHE_BACKEND = "none" }
if ([string]::IsNullOrEmpty($env:EMBED_MODEL)) { $env:EMBED_MODEL = "nomic-embed-text" }
if ([string]::IsNullOrEmpty($env:OLLAMA_URL)) { $env:OLLAMA_URL = "http://localhost:11434" }

# ==== Step 1: Kill stale service on our port ===============================
Write-Step "Step 1: Killing stale processes"
$conns = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
$pids = $conns.OwningProcess | Select-Object -Unique | Where-Object { $_ -gt 0 }
if ($pids) {
    foreach ($procId in $pids) {
        $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if ($proc) {
            $proc | Stop-Process -Force
            Write-OK ("Killed {0} (PID {1}) on port {2}" -f $proc.ProcessName, $procId, $Port)
        }
    }
    Start-Sleep -Seconds 1
} else {
    Write-OK ("Port {0} is free" -f $Port)
}

# ==== Step 2: Docker / Redis Stack =========================================
Write-Step "Step 2: Docker / Redis Stack check"

$DockerCli = Find-DockerCli
$dockerRunning = $false
if ($DockerCli) {
    $env:PATH = "$(Split-Path $DockerCli);$env:PATH"
    Write-OK ("Docker CLI: {0}" -f $DockerCli)
} else {
    Write-Warn "Docker CLI not found (PATH + default install locations)"
}

if ($DockerCli) {
    & $DockerCli info 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-OK "Docker Desktop is running"
        $dockerRunning = $true
    } else {
        Write-Warn "Docker Desktop is NOT running -- attempting to start..."
        $ddExe = Find-DockerDesktopExe
        if ($ddExe) {
            Start-Process -FilePath $ddExe -WindowStyle Hidden -ErrorAction SilentlyContinue
            $waitAttempt = 0
            while ($waitAttempt -lt 45) {
                Start-Sleep -Seconds 2
                $waitAttempt++
                & $DockerCli info 2>$null | Out-Null
                if ($LASTEXITCODE -eq 0) {
                    Write-OK ("Docker Desktop ready (after ~{0}s)" -f ($waitAttempt * 2))
                    $dockerRunning = $true
                    break
                }
            }
        }
    }
}

# Redis Stack is the optional semantic-cache backend -- warn-only degradation.
if ($dockerRunning) {
    $redisNames = & $DockerCli ps --filter "publish=6379" --format "{{.Names}}" 2>$null
    if ($redisNames) {
        Write-OK ("Redis Stack already running ({0} on :6379)" -f ($redisNames -join ", "))
        $env:RAG_CORE_CACHE_BACKEND = "redisvl"
        $env:RAG_CORE_REDIS_URL = "redis://localhost:6379"
    } else {
        $composeFile = Join-Path $ProjectRoot "docker-compose.yml"
        Write-OK ("Starting Redis Stack via {0} (redis-stack service only)..." -f $composeFile)
        & $DockerCli compose -f $composeFile up -d redis-stack 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-OK "Redis Stack started on :6379"
            $env:RAG_CORE_CACHE_BACKEND = "redisvl"
            $env:RAG_CORE_REDIS_URL = "redis://localhost:6379"
        } else {
            Write-Warn "redis-stack failed to start -- semantic cache disabled (cache_backend=none)"
        }
    }
} else {
    Write-Warn "Docker unavailable -- semantic cache disabled (cache_backend=none)"
}

# ==== Step 3: Embeddings (Ollama) check ====================================
Write-Step "Step 3: Embeddings (Ollama) check"

$ollamaUp = $false
$ollamaCheck = curl.exe -s -o NUL -w "%{http_code}" "http://127.0.0.1:11434/api/tags" 2>$null
if ($ollamaCheck -eq "200") {
    Write-OK "Ollama is running"
    $ollamaUp = $true
    $modelList = curl.exe -s "http://127.0.0.1:11434/api/tags" 2>$null | & $VenvPython -c "import sys,json; print('\n'.join(m['name'].split(':')[0] for m in json.load(sys.stdin).get('models',[])))" 2>$null
    if ($modelList -match "(?m)^nomic-embed-text$") {
        Write-OK "nomic-embed-text present"
    } else {
        Write-Warn "nomic-embed-text NOT pulled -- run: ollama pull nomic-embed-text"
        Write-Warn "Prepopulate will fail without it."
    }
} else {
    Write-Warn "Ollama not reachable on port 11434"
    Write-Warn "Fix: start Ollama (winget install Ollama.Ollama) and pull nomic-embed-text"
}

# ==== Step 4: Reranker model ===============================================
Write-Step "Step 4: Reranker model"

$ModelFile = Join-Path $ProjectRoot "models\reranker\minilm-int8.onnx"
if (Test-Path $ModelFile) {
    Write-OK "Reranker model present: $ModelFile"
} else {
    Write-Warn "Reranker model missing -- downloading (22 MiB, Hugging Face)..."
    & $VenvPython -m enterprise_rag.cli download-model 2>&1 | Out-Null
    if (Test-Path $ModelFile) { Write-OK "Reranker model downloaded" }
    else { Write-Warn "Model download failed -- reranking disabled (NoOpReranker)" }
}

# ==== Step 5: Knowledge-base prepopulation =================================
Write-Step "Step 5: Knowledge-base prepopulation"

if ($SkipPrepopulate) {
    Write-Warn "Skipping prepopulate (SkipPrepopulate flag set)"
} elseif ([string]::IsNullOrEmpty($KbPath)) {
    Write-Warn "No -KbPath given -- skipping prepopulate (existing DBs are reused as-is)"
} elseif (-not (Test-Path $KbPath)) {
    Write-Err ("KbPath not found: {0}" -f $KbPath)
    exit 1
} else {
    Write-OK ("Prepopulating from {0} ..." -f $KbPath)
    & $VenvPython -m enterprise_rag.prepopulate --kb $KbPath --doc-id "meridian-kb" --tenant "default" --required-marker "meridian university" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Prepopulate failed -- see the output above (embedding endpoint must be up)"
        exit 1
    }
    Write-OK "Prepopulate complete (idempotent -- reruns skip)"
}

# ==== Step 6: Start the MCP server =========================================
Write-Step ("Step 6: Starting MCP server (port {0})" -f $Port)

foreach ($f in @($MCPLog, $MCPErrLog)) {
    if (Test-Path $f) { Remove-Item $f -Force -ErrorAction SilentlyContinue }
}
$serveArgs = @{
    FilePath               = $VenvPython
    ArgumentList           = "-m", "enterprise_rag.cli", "serve", "--host", "127.0.0.1", "--port", "$Port"
    WindowStyle            = "Hidden"
    PassThru               = $true
    RedirectStandardOutput = $MCPLog
    RedirectStandardError  = $MCPErrLog
}
$MCPProcess = Start-Process @serveArgs
Write-OK ("MCP server starting (PID {0}) - log: {1}" -f $MCPProcess.Id, $MCPLog)

# Readiness: MCP initialize over streamable HTTP must answer 200.
# The JSON body goes through a temp file (--data-binary @file) — passing
# embedded-quote JSON inline to curl.exe gets mangled by Windows arg quoting.
$initBody = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"launcher","version":"1"}}}'
$initFile = Join-Path $env:TEMP "erc_init.json"
[System.IO.File]::WriteAllText($initFile, $initBody)
$ready = $false
for ($a = 1; $a -le 30 -and -not $ready; $a++) {
    Start-Sleep -Seconds 2
    $code = curl.exe -s -o NUL -w "%{http_code}" -X POST $MCPUrl `
        -H "Content-Type: application/json" `
        -H "Accept: application/json, text/event-stream" `
        --data-binary "@$initFile" 2>$null
    if ($code -eq "200") {
        Write-OK ("MCP server responding on {0} (took ~{1}s)" -f $MCPUrl, ($a * 2))
        $ready = $true
    } else {
        Write-Warn ("Waiting for MCP server... ({0}/30, HTTP {1})" -f $a, $code)
    }
}
if (-not $ready) {
    Write-Err ("MCP server did NOT come up - check {0} / {1}" -f $MCPLog, $MCPErrLog)
    exit 1
}

# ==== Step 7: Optional tunnel ==============================================
$TunnelHost = $null
if ($WithTunnel -or $NamedTunnel) {
    Write-Step "Step 7: Cloudflare tunnel"
    $cloudflaredPath = Find-CloudflaredExe
    if (-not $cloudflaredPath) {
        Write-Err "cloudflared not found! Install with: winget install Cloudflare.cloudflared"
        Write-Err "or download from: https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/"
        exit 1
    }
    Write-OK ("cloudflared: {0}" -f $cloudflaredPath)

    if ($NamedTunnel) {
        Write-OK ("Named tunnel mode: {0}" -f $TunnelName)
        $tunnelList = & $cloudflaredPath tunnel list 2>&1
        if (-not ($tunnelList -match $TunnelName)) {
            Write-OK ("Creating named tunnel: {0} ..." -f $TunnelName)
            & $cloudflaredPath tunnel create $TunnelName 2>&1 | Out-Null
            if ($LASTEXITCODE -ne 0) {
                Write-Warn "Failed to create named tunnel -- falling back to quick tunnel..."
                $NamedTunnel = $false
            }
        }
        if ($NamedTunnel) {
            $tunnelCmd = "`"$cloudflaredPath`" tunnel run --url http://localhost:{0} {1} 2>&1" -f $Port, $TunnelName
            $cfArgs = @{
                FilePath               = "cmd"
                ArgumentList           = "/c", $tunnelCmd
                WindowStyle            = "Hidden"
                PassThru               = $true
                RedirectStandardOutput = (Join-Path $env:TEMP "erc_named_tunnel.log")
            }
            $CloudflaredProcess = Start-Process @cfArgs
            Write-OK ("Named tunnel '{0}' starting (PID {1})" -f $TunnelName, $CloudflaredProcess.Id)
        }
    }
    if (-not $NamedTunnel) {
        Write-Warn "Ephemeral tunnel mode (URL will change on next restart)"
        $TunnelHost = Start-QuickTunnel -Exe $cloudflaredPath -Port $Port -CacheFile $TunnelFile -LogBase "erc_mcp_tunnel"
    }
}

# ==== Summary ===============================================================
Write-Host ""
Write-Host ("{0}{1}ENTERPRISE RAG CORE STARTED{2}" -f $GREEN, $BOLD, $RESET)
Write-Host ""
Write-Host ("{0}MCP Server:{1}         {0}{2}{1}" -f $CYAN, $RESET, $MCPUrl)
Write-Host ("{0}Tools:{1}              execute_agent_context, retrieve_context" -f $BOLD, $RESET)
Write-Host ("{0}Vector backend:{1}     {2} (path: {3}, collection: {4})" -f $BOLD, $RESET, $env:RAG_CORE_VECTOR_BACKEND, $env:RAG_CORE_CHROMA_PATH, $env:RAG_CORE_CHROMA_COLLECTION)
Write-Host ("{0}Keyword backend:{1}    {2} (warmed at boot)" -f $BOLD, $RESET, $env:RAG_CORE_KEYWORD_BACKEND)
Write-Host ("{0}Cache backend:{1}      {2}" -f $BOLD, $RESET, $env:RAG_CORE_CACHE_BACKEND)
Write-Host ("{0}Embeddings:{1}         {2} (model: {3})" -f $BOLD, $RESET, $env:RAG_CORE_EMBED_BACKEND, $env:EMBED_MODEL)
Write-Host ("{0}Default tenant:{1}     {2}" -f $BOLD, $RESET, $env:RAG_CORE_DEFAULT_TENANT)
if ($TunnelHost) {
    Write-Host ("{0}Public tunnel:{1}      {0}https://{2}{1}" -f $CYAN, $RESET, $TunnelHost)
}
Write-Host ""
Write-Host ("{0}Quick test:{1}" -f $BOLD, $RESET)
Write-Host ("   curl -X POST {0} -H ""Content-Type: application/json"" -H ""Accept: application/json, text/event-stream"" --data-binary ""@{1}""" -f $MCPUrl, $initFile)
Write-Host ""
Write-Host ("{0}Logs:{1}" -f $BOLD, $RESET)
Write-Host ("   Server:  {0}" -f $MCPLog)
Write-Host ("   Errors:  {0}" -f $MCPErrLog)
Write-Host ""
Write-Host ("{0}Stop: taskkill /PID {1}{2}" -f $YELLOW, $MCPProcess.Id, $RESET)
Write-Host ""

# Post-summary guard: the server must still be alive.
Start-Sleep -Seconds 2
if ($MCPProcess.HasExited) {
    Write-Err ("MCP server (PID {0}) has already exited! Check {1}" -f $MCPProcess.Id, $MCPErrLog)
}
