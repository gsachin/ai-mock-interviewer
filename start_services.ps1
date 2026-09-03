<#
.SYNOPSIS
    AI Mock Interviewer - One-shot launcher (Windows).
.DESCRIPTION
    Kills stale services, releases ports, starts everything fresh:
    venv self-heal for this repo, Ollama embedding + LLM checks, the
    enterprise-rag-core MCP retrieval service (inner folder, branch
    enterprise-rag-core-realtime-ready) on :8031, per-domain question-bank
    prepopulation, an MCP server restart to warm the keyword index, and the
    interviewer management plane (FastAPI) on :8010. Optional: static web app
    on :8080 and a Cloudflare quick tunnel for :8010.

    RAG wiring (the "missing links" fixed here):
      - RAG MCP is pinned to :$RagPort (8031) -- the repo's live-test/demo
        convention -- never the ERC default 8010 (collides with the
        interviewer server).
      - RAG_MCP_URL is exported so the interviewer talks to :8031/mcp.
      - Question banks are ingested per domain (doc-id bank-<domain>,
        department <domain>) -- prepopulate_banks.sh is bash-only.
      - The RAG MCP server is restarted after ingestion so the in-memory BM25
        leg warms from the vector store (RAG_CORE_WARM_KEYWORD=all).
      - The LLM is pinned to Ollama's OpenAI-compatible /v1 (qwen2.5:14b).
.PARAMETER Port
    Interviewer management plane port (default 8010).
.PARAMETER RagPort
    enterprise-rag-core MCP port (default 8031).
.PARAMETER SkipRag
    Skip the RAG service entirely (management plane still starts, retrieval
    will be unavailable).
.PARAMETER SkipPrepopulate
    Skip question-bank ingestion (existing stores are reused as-is).
.PARAMETER ForcePrepopulate
    Rebuild the question banks (default is idempotent: reruns skip).
.PARAMETER WithWeb
    Also serve the static web app (web/) on :8080.
.PARAMETER WithStreamlit
    Also launch the interactive Streamlit interview UI (web/streamlit_app.py)
    on :8501 -- chat with the interviewer, see live scores and the rubric
    cache hit rate.
.PARAMETER WithTunnel
    Start a Cloudflare quick tunnel for the management plane (:8010) and,
    with -WithStreamlit, for the Streamlit UI (:8501) too.
.PARAMETER WithVoice
    Also start the Phase-3 voice stack: livekit-server (dev mode) on :7880
    and the interviewer agent worker. The voice UI served at
    http://127.0.0.1:8010/ then runs a full spoken interview
    (faster-whisper STT + kokoro TTS + the fast voice LLM llama3.2:3b).
.EXAMPLE
    .\start_services.ps1
    .\start_services.ps1 -WithStreamlit
    .\start_services.ps1 -SkipRag
    .\start_services.ps1 -WithStreamlit -WithTunnel
    .\start_services.ps1 -WithVoice
    .\start_services.ps1 -ForcePrepopulate -WithStreamlit -WithWeb -WithTunnel
#>

param(
    [int]$Port = 8010,
    [int]$RagPort = 8031,
    [switch]$SkipRag = $false,
    [switch]$SkipPrepopulate = $false,
    [switch]$ForcePrepopulate = $false,
    [switch]$WithWeb = $false,
    [switch]$WithStreamlit = $false,
    [switch]$WithTunnel = $false,
    [switch]$WithVoice = $false
)

# ---- Config ---------------------------------------------------------------
$ProjectRoot = $PSScriptRoot
$ERCRoot     = Join-Path $ProjectRoot "enterprise-rag-core"
$ERCLauncher = Join-Path $ERCRoot "start_services.ps1"
$ERCPython   = Join-Path $ERCRoot ".venv\Scripts\python.exe"

# Prefer the project venv (created by Step 0) when present, falling back to
# system python -- keeps pre-venv setups working unchanged.
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$PythonExe  = if (Test-Path $VenvPython) { $VenvPython } else { "python" }
Write-Host "  Python: $PythonExe"

$RAGMCPUrl   = "http://127.0.0.1:$RagPort/mcp"
$HealthUrl   = "http://127.0.0.1:$Port/health"
$TunnelFile  = Join-Path $ProjectRoot ".interviewer_tunnel"
$ServerLog   = Join-Path $env:TEMP "interviewer_server.log"
$ServerErr   = Join-Path $env:TEMP "interviewer_server_err.log"
$WebLog      = Join-Path $env:TEMP "interviewer_web.log"
$ERCLog      = Join-Path $env:TEMP "erc_launcher.log"
$MCPLog      = Join-Path $env:TEMP "erc_mcp.log"
$MCPErrLog   = Join-Path $env:TEMP "erc_mcp_err.log"
$StreamlitPort = 8501
$StreamlitLog  = Join-Path $env:TEMP "interviewer_streamlit.log"
$StreamlitExe  = Join-Path $ProjectRoot ".venv\Scripts\streamlit.exe"

# Phase 3 voice (optional -WithVoice): livekit-server dev + agent worker.
$LiveKitPort   = 7880
$LiveKitServer = Join-Path $ProjectRoot ".tools\livekit\livekit-server.exe"
$LiveKitLog    = Join-Path $env:TEMP "livekit_server.log"
$VoiceWorkerLog = Join-Path $env:TEMP "voice_worker.log"
$VoiceLLMModel = "llama3.2:3b"

# Ports the launcher owns (Streamlit only joins when requested).
$LaunchPorts = @($Port, $RagPort)
if ($WithStreamlit) { $LaunchPorts += $StreamlitPort }
if ($WithVoice) { $LaunchPorts += $LiveKitPort }

# LLM: pinned to Ollama's OpenAI-compatible endpoint (qwen2.5:14b is present
# on this machine; embeddings stay on nomic-embed-text).
$LLMBaseUrl = "http://127.0.0.1:11434/v1"
$LLMModel   = "qwen2.5:14b"

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
    # Start a Cloudflare quick tunnel for one local port and return its public
    # trycloudflare hostname ($null if it never came up). Reuses a live cached
    # URL and never double-starts a tunnel for the same port -- same
    # conventions as universityDemo's start_services.ps1.
    param(
        [string]$Exe,
        [int]$TunPort,
        [string]$Label,
        [string]$CacheFile,
        [string]$LogBase
    )

    # Reuse a live cached URL.
    if (Test-Path $CacheFile) {
        $cached = (Get-Content $CacheFile -Raw).Trim()
        if ($cached) {
            $cachedCode = curl.exe -s -o NUL -w "%{http_code}" "https://$cached/" 2>$null
            if ($cachedCode -eq "200") {
                Write-OK ("{0} tunnel already alive: {1}" -f $Label, $cached)
                return $cached
            }
        }
    }

    # Never double-start: a live cloudflared for this port may exist without
    # a usable cache URL.
    $existing = Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match "localhost:$TunPort" }
    if ($existing) {
        Write-Warn ("{0} tunnel process already running but its URL is unknown" -f $Label)
        Write-Warn "Kill it and re-run to recreate: taskkill /IM cloudflared.exe"
        return $null
    }

    $outLog = Join-Path $env:TEMP ("{0}.log" -f $LogBase)
    $errLog = Join-Path $env:TEMP ("{0}_err.log" -f $LogBase)
    # Clear stale logs so a dead URL from a previous run can never be parsed.
    foreach ($f in @($outLog, $errLog)) {
        if (Test-Path $f) { Remove-Item $f -Force -ErrorAction SilentlyContinue }
    }

    $cfArgs = @{
        FilePath               = $Exe
        ArgumentList           = "tunnel", "--url", "http://localhost:$TunPort", "--metrics", "localhost:0"
        WindowStyle            = "Hidden"
        PassThru               = $true
        RedirectStandardOutput = $outLog
        RedirectStandardError  = $errLog
    }
    $proc = Start-Process @cfArgs
    Write-OK ("{0} tunnel starting (PID {1}) - log: {2}" -f $Label, $proc.Id, $outLog)

    $tunnelHost = $null
    $attempt = 0
    while (-not $tunnelHost -and $attempt -lt 15) {
        Start-Sleep -Seconds 3
        $attempt++
        # cloudflared logs everything (incl. the URL banner) to stderr.
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
            Write-Warn ("Waiting for {0} tunnel URL... ({1}/15)" -f $Label, $attempt)
        }
    }

    if (-not $tunnelHost) {
        Write-Warn ("{0} tunnel did not start -- re-run to retry" -f $Label)
        return $null
    }

    [System.IO.File]::WriteAllText($CacheFile, $tunnelHost)
    # Fresh quick-tunnel hostnames can take a minute to resolve.
    $verified = $false
    for ($v = 0; $v -lt 10 -and -not $verified; $v++) {
        if ($v -gt 0) { Start-Sleep -Seconds 5 }
        $verified = (curl.exe -s --connect-timeout 8 -o NUL -w "%{http_code}" "https://$tunnelHost/" 2>$null) -eq "200"
    }
    if ($verified) {
        Write-OK ("{0} tunnel reachable: https://{1}/" -f $Label, $tunnelHost)
    } else {
        Write-Warn ("{0} tunnel started but not yet reachable (DNS warm-up): {1}" -f $Label, $tunnelHost)
    }
    return $tunnelHost
}

function Ensure-InterviewerDeps {
    # Self-heal: if the venv cannot import the interviewer's dependencies
    # (fresh clone, interrupted install, empty venv), create/install instead
    # of failing at the readiness probe later.
    $probePy = if (Test-Path $VenvPython) { $VenvPython } else { "python" }
    & $probePy -c "import fastapi, uvicorn, mcp, httpx" 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-OK "venv dependencies OK (fastapi + uvicorn + mcp + httpx importable)"
        return $true
    }

    Write-Warn "venv dependencies missing or broken -- creating + installing..."
    if (-not (Test-Path $VenvPython)) {
        $venvDir = Join-Path $ProjectRoot ".venv"
        if (Get-Command "py" -ErrorAction SilentlyContinue) { & py -3.11 -m venv $venvDir }
        else { & python -m venv $venvDir }
        if ($LASTEXITCODE -ne 0) {
            Write-Err "venv creation failed"
            return $false
        }
    }
    & $VenvPython -m pip install --upgrade pip 2>$null | Out-Null
    & $VenvPython -m pip install -e ".[api,dev,web]" 2>&1 | Out-Host
    if ($LASTEXITCODE -ne 0) {
        Write-Err "pip install failed (exit code $LASTEXITCODE)"
        return $false
    }

    & $VenvPython -c "import fastapi, uvicorn, mcp, httpx" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Err "dependencies still not importable -- check the output above"
        return $false
    }
    Write-OK "Dependencies installed and importable"
    return $true
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

# Sensible RAG-core defaults for processes THIS launcher spawns (prepopulate
# and the MCP restart). The ERC launcher sets these only in ITS process env,
# so children we start would otherwise crash with "EMBED_MODEL is required
# for embed_backend='auto'". Never override explicit configuration.
if ([string]::IsNullOrEmpty($env:RAG_CORE_CHROMA_PATH)) { $env:RAG_CORE_CHROMA_PATH = Join-Path $ERCRoot "chroma_data" }
if ([string]::IsNullOrEmpty($env:RAG_CORE_CHROMA_COLLECTION)) { $env:RAG_CORE_CHROMA_COLLECTION = "meridian-kb" }
if ([string]::IsNullOrEmpty($env:RAG_CORE_DEFAULT_TENANT)) { $env:RAG_CORE_DEFAULT_TENANT = "default" }
if ([string]::IsNullOrEmpty($env:RAG_CORE_VECTOR_BACKEND)) { $env:RAG_CORE_VECTOR_BACKEND = "chroma" }
if ([string]::IsNullOrEmpty($env:RAG_CORE_KEYWORD_BACKEND)) { $env:RAG_CORE_KEYWORD_BACKEND = "bm25" }
# Pin the embed backend to Ollama: ERC's "auto" resolves to vLLM when an
# NVIDIA GPU is detected (this machine has an RTX 5060 Ti) -- but no vLLM
# server is deployed; Ollama nomic-embed-text is the pinned embedder.
if ([string]::IsNullOrEmpty($env:RAG_CORE_EMBED_BACKEND)) { $env:RAG_CORE_EMBED_BACKEND = "ollama" }
if ([string]::IsNullOrEmpty($env:RAG_CORE_CACHE_BACKEND)) { $env:RAG_CORE_CACHE_BACKEND = "none" }
if ([string]::IsNullOrEmpty($env:EMBED_MODEL)) { $env:EMBED_MODEL = "nomic-embed-text" }
if ([string]::IsNullOrEmpty($env:OLLAMA_URL)) { $env:OLLAMA_URL = "http://127.0.0.1:11434" }

# ---- Export the interviewer's wiring (the RAG "missing links") ------------
# RAG MCP: pinned to :$RagPort -- the convention the repo's live tests, demo
# and run_gate all default to (config.py's :8000 default matches nothing).
$env:RAG_MCP_URL = $RAGMCPUrl
# LLM: Ollama OpenAI-compatible endpoint.
$env:INTERVIEW_LLM_BASE_URL = $LLMBaseUrl
$env:INTERVIEW_LLM_MODEL   = $LLMModel
# Interview defaults.
if ([string]::IsNullOrEmpty($env:INTERVIEW_DOMAIN)) { $env:INTERVIEW_DOMAIN = "system-design" }
if ([string]::IsNullOrEmpty($env:INTERVIEW_TOP_K)) { $env:INTERVIEW_TOP_K = "5" }

# ---- Voice (Phase 3) wiring: only exported with -WithVoice --------------
if ($WithVoice) {
    # LiveKit dev server: devkey/secret are its --dev defaults.
    $env:LIVEKIT_URL        = "http://127.0.0.1:$LiveKitPort"
    $env:LIVEKIT_API_KEY    = "devkey"
    $env:LIVEKIT_API_SECRET = "secret"
    # Self-hosted voice engines + the fast hot-path LLM (qwen2.5:14b is the
    # judge; llama3.2:3b carries the <1.5s round-trip).
    if ([string]::IsNullOrEmpty($env:INTERVIEW_STT_PROVIDER)) { $env:INTERVIEW_STT_PROVIDER = "faster-whisper" }
    if ([string]::IsNullOrEmpty($env:INTERVIEW_TTS_PROVIDER)) { $env:INTERVIEW_TTS_PROVIDER = "kokoro" }
    $env:INTERVIEW_VOICE_LLM_BASE_URL = $LLMBaseUrl
    $env:INTERVIEW_VOICE_LLM_MODEL    = $VoiceLLMModel
}

# ==== Step 0: dependency self-heal =========================================
Write-Step "Step 0: Interviewer dependencies"
if (-not (Ensure-InterviewerDeps)) {
    Write-Err "Cannot start without working dependencies -- fix the errors above and re-run."
    exit 1
}
# The installer may have just created/fixed the venv -- prefer it from here on.
if (Test-Path $VenvPython) { $PythonExe = $VenvPython }

# ==== Step 1: Kill stale processes ========================================
Write-Step "Step 1: Killing stale processes"
# NOTE: loop var is $checkPort, never $port -- PowerShell variables are
# case-insensitive, so a $port loop var would clobber the $Port parameter.
foreach ($checkPort in $LaunchPorts) {
    $connections = Get-NetTCPConnection -LocalPort $checkPort -ErrorAction SilentlyContinue
    $pids = $connections.OwningProcess | Select-Object -Unique | Where-Object { $_ -gt 0 }
    foreach ($procId in $pids) {
        $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if ($proc) {
            $proc | Stop-Process -Force
            Write-OK ("Killed {0} (PID {1}) on port {2}" -f $proc.ProcessName, $procId, $checkPort)
        }
    }
}
# The voice worker binds no port (it connects out to livekit-server) -- kill
# it by command line so a stale worker never lingers past a restart.
if ($WithVoice) {
    $staleWorkers = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match "interviewer\.voice\.worker" -and $_.ProcessId -ne $PID }
    foreach ($sw in $staleWorkers) {
        Stop-Process -Id $sw.ProcessId -Force -ErrorAction SilentlyContinue
        Write-OK ("Killed stale voice worker (PID {0})" -f $sw.ProcessId)
    }
}
Write-OK "Process cleanup complete"

# ==== Step 2: Verify ports are free ========================================
Write-Step "Step 2: Verifying ports are free"
foreach ($checkPort in $LaunchPorts) {
    $attempt = 0
    $portBusy = $true
    while ($portBusy -and $attempt -lt 10) {
        $conn = Get-NetTCPConnection -LocalPort $checkPort -ErrorAction SilentlyContinue
        if (-not $conn) {
            $portBusy = $false
        } else {
            $attempt++
            Write-Warn ("Port {0} still in use - waiting ({1}/10)..." -f $checkPort, $attempt)
            Start-Sleep -Seconds 1
        }
    }
    if ($portBusy) {
        Write-Warn ("Port {0} is STILL busy after 10s - may be TIME_WAIT" -f $checkPort)
        if ($conn) {
            $conn.OwningProcess | Select-Object -Unique | Where-Object { $_ -gt 0 } | ForEach-Object {
                Get-Process -Id $_ -ErrorAction SilentlyContinue | Stop-Process -Force
            }
        }
        Start-Sleep -Seconds 2
    } else {
        Write-OK ("Port {0} is free" -f $checkPort)
    }
}

# ==== Step 3: Ollama check (embeddings + LLM) =============================
Write-Step "Step 3: Ollama check (embeddings + LLM)"

$ollamaUp = $false
$ollamaCheck = curl.exe -s -o NUL -w "%{http_code}" "http://127.0.0.1:11434/api/tags" 2>$null
if ($ollamaCheck -eq "200") {
    Write-OK "Ollama is running"
    $ollamaUp = $true
    $modelList = curl.exe -s "http://127.0.0.1:11434/api/tags" 2>$null | & $PythonExe -c "import sys,json; print('\n'.join(m['name'] for m in json.load(sys.stdin).get('models',[])))" 2>$null
    if ($modelList -match "(?m)^nomic-embed-text") {
        Write-OK "Embeddings: nomic-embed-text present"
    } else {
        Write-Warn "nomic-embed-text NOT pulled -- run: ollama pull nomic-embed-text"
        Write-Warn "RAG prepopulate/retrieval will fail without it."
    }
    if ($modelList -match "(?m)^qwen2.5:14b") {
        Write-OK ("LLM: qwen2.5:14b present (endpoint {0})" -f $LLMBaseUrl)
    } else {
        Write-Warn "qwen2.5:14b NOT pulled -- run: ollama pull qwen2.5:14b"
        Write-Warn "Interview turns need it (INTERVIEW_LLM_MODEL=$LLMModel)."
    }
} else {
    Write-Warn "Ollama not reachable on port 11434"
    Write-Warn "Fix: start Ollama (winget install Ollama.Ollama) and pull nomic-embed-text + qwen2.5:14b"
}

# ==== Step 4: Enterprise RAG Core =========================================
Write-Step "Step 4: Enterprise RAG Core MCP service (folder: enterprise-rag-core)"

if ($SkipRag) {
    Write-Warn "Skipping RAG (SkipRag flag set) -- retrieval will be unavailable"
} elseif (-not (Test-Path $ERCRoot)) {
    Write-Err "enterprise-rag-core not found at $ERCRoot -- cannot start RAG"
    Write-Err "Clone the branch enterprise-rag-core-realtime-ready into this folder first."
} elseif (-not (Test-Path $ERCLauncher)) {
    Write-Err "start_services.ps1 not found inside enterprise-rag-core -- cannot start RAG"
} else {
    # Route the ERC launcher through cmd.exe with FILE redirection -- never a
    # PowerShell output capture. A $out = powershell.exe ... 2>&1 capture
    # pumps anonymous pipes; the launcher's MCP server (a long-lived
    # grandchild) inherits those pipe write-handles, so the pipes never reach
    # EOF and the capture hangs forever even after the launcher exits.
    if (Test-Path $ERCLog) { Remove-Item $ERCLog -Force -ErrorAction SilentlyContinue }
    Write-OK ("Starting ERC launcher (first run creates its venv + installs ~200 MB; reranker model 22 MiB)...")
    cmd /c "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$ERCLauncher`" -Port $RagPort -SkipPrepopulate > `"$ERCLog`" 2>&1"
    if ($LASTEXITCODE -eq 0) {
        Write-OK ("ERC launcher finished (MCP should be serving on {0})" -f $RAGMCPUrl)
    } else {
        Write-Warn ("ERC launcher exited with code {0} -- see {1}" -f $LASTEXITCODE, $ERCLog)
        Get-Content $ERCLog -Tail 5 -ErrorAction SilentlyContinue | ForEach-Object { Write-Warn $_ }
    }
}

# ==== Step 5: Prepopulate question banks ==================================
Write-Step "Step 5: Prepopulate question banks (per domain)"

$banksPrepopulated = $false
if ($SkipRag) {
    Write-Warn "Skipping prepopulate (RAG skipped)"
} elseif ($SkipPrepopulate) {
    Write-Warn "Skipping prepopulate (SkipPrepopulate flag set) -- existing stores reused as-is"
} elseif (-not (Test-Path $ERCPython)) {
    Write-Err "ERC venv not found ($ERCPython) -- the ERC launcher must have failed"
    Write-Err ("Check the tail of {0} above" -f $ERCLog)
} else {
    $forceArg = @()
    if ($ForcePrepopulate) { $forceArg = @("--force") }
    foreach ($domain in @("system-design", "ios", "dsa", "devops")) {
        $kb = Join-Path $ProjectRoot ("question_banks\{0}.md" -f $domain)
        if (-not (Test-Path $kb)) {
            Write-Warn ("Bank file not found: {0} -- skipping" -f $kb)
            continue
        }
        Write-OK ("Prepopulating {0} (doc-id bank-{1}, department {1}) ..." -f $domain, $domain)
        & $ERCPython -m enterprise_rag.prepopulate `
            --kb $kb `
            --doc-id ("bank-{0}" -f $domain) `
            --tenant "default" `
            --department $domain `
            @forceArg 2>&1 | Out-Host
        if ($LASTEXITCODE -ne 0) {
            Write-Warn ("Prepopulate failed for {0} (exit {1}) -- Ollama embeddings must be up" -f $domain, $LASTEXITCODE)
        } else {
            $banksPrepopulated = $true
        }
    }
    if ($banksPrepopulated) { Write-OK "Question-bank prepopulation complete (idempotent -- reruns skip)" }
}

# ==== Step 6: Restart RAG MCP to warm keyword index + readiness ===========
Write-Step ("Step 6: RAG MCP readiness (port {0})" -f $RagPort)

if ($SkipRag) {
    Write-Warn "Skipping MCP readiness (RAG skipped)"
} else {
    if ($banksPrepopulated) {
        # BM25 is in-memory and warms from the vector store at serve boot.
        # Banks ingested after boot would be invisible to the keyword leg, so
        # restart the MCP server fresh (RAG_CORE_WARM_KEYWORD=all).
        Write-OK "Prepopulate changed stores -- restarting RAG MCP to warm the keyword index..."
        $conns = Get-NetTCPConnection -LocalPort $RagPort -ErrorAction SilentlyContinue
        $conns.OwningProcess | Select-Object -Unique | Where-Object { $_ -gt 0 } | ForEach-Object {
            Get-Process -Id $_ -ErrorAction SilentlyContinue | Stop-Process -Force
        }
        # Wait until the port is truly free -- otherwise the readiness probe
        # below can answer against the dying server and mask a crash.
        $released = $false
        for ($w = 0; $w -lt 15 -and -not $released; $w++) {
            Start-Sleep -Seconds 1
            if (-not (Get-NetTCPConnection -LocalPort $RagPort -ErrorAction SilentlyContinue)) {
                $released = $true
            }
        }
        if (-not $released) {
            Write-Warn ("Port {0} still busy after kill -- proceeding anyway" -f $RagPort)
        } else {
            Write-OK ("Port {0} released" -f $RagPort)
        }
        $env:RAG_CORE_WARM_KEYWORD = "all"
    }

    if (-not (Test-Path $ERCPython)) {
        Write-Err "ERC venv not found -- MCP server cannot be started"
        Write-Err "RAG retrieval will be unavailable (management plane still starts)"
    } else {
        # Never double-start: if a server already owns the port (e.g. the ERC
        # launcher's, when prepopulate skipped), reuse it -- the probe below
        # verifies it either way.
        $existing = Get-NetTCPConnection -LocalPort $RagPort -ErrorAction SilentlyContinue
        if ($existing) {
            Write-OK ("RAG MCP already listening on {0} -- reusing it" -f $RAGMCPUrl)
            $MCPProcess = $null
        } else {
            foreach ($f in @($MCPLog, $MCPErrLog)) {
                if (Test-Path $f) { Remove-Item $f -Force -ErrorAction SilentlyContinue }
            }
            $serveArgs = @{
                FilePath               = $ERCPython
                ArgumentList           = "-m", "enterprise_rag.cli", "serve", "--host", "127.0.0.1", "--port", "$RagPort"
                WindowStyle            = "Hidden"
                PassThru               = $true
                RedirectStandardOutput = $MCPLog
                RedirectStandardError  = $MCPErrLog
            }
            $MCPProcess = Start-Process @serveArgs
            Write-OK ("RAG MCP server starting (PID {0}) - log: {1}" -f $MCPProcess.Id, $MCPLog)
        }

        # Readiness: MCP initialize over streamable HTTP must answer 200.
        # The JSON body goes through a temp file (--data-binary @file) --
        # passing embedded-quote JSON inline to curl.exe gets mangled by
        # Windows arg quoting.
        $initBody = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"launcher","version":"1"}}}'
        $initFile = Join-Path $env:TEMP "interviewer_init.json"
        [System.IO.File]::WriteAllText($initFile, $initBody)
        $ready = $false
        for ($a = 1; $a -le 30 -and -not $ready; $a++) {
            Start-Sleep -Seconds 2
            $code = curl.exe -s -o NUL -w "%{http_code}" -X POST $RAGMCPUrl `
                -H "Content-Type: application/json" `
                -H "Accept: application/json, text/event-stream" `
                --data-binary "@$initFile" 2>$null
            if ($code -eq "200") {
                Write-OK ("RAG MCP responding on {0} (took ~{1}s)" -f $RAGMCPUrl, ($a * 2))
                $ready = $true
            } else {
                Write-Warn ("Waiting for RAG MCP... ({0}/30, HTTP {1})" -f $a, $code)
            }
        }
        if (-not $ready) {
            Write-Err ("RAG MCP did NOT come up - check {0} / {1}" -f $MCPLog, $MCPErrLog)
        } elseif ($MCPProcess -and $MCPProcess.HasExited) {
            Write-Err ("RAG MCP exited right after readiness - check {0}" -f $MCPErrLog)
            Get-Content $MCPErrLog -Tail 8 -ErrorAction SilentlyContinue | ForEach-Object { Write-Warn $_ }
        }
    }
}

# ==== Step 7: Interviewer management plane ================================
Write-Step ("Step 7: Interviewer management plane (port {0})" -f $Port)

foreach ($f in @($ServerLog, $ServerErr)) {
    if (Test-Path $f) { Remove-Item $f -Force -ErrorAction SilentlyContinue }
}
$serverArgs = @{
    FilePath               = $PythonExe
    ArgumentList           = "-m", "uvicorn", "interviewer.server:app", "--host", "127.0.0.1", "--port", "$Port"
    WindowStyle            = "Hidden"
    PassThru               = $true
    RedirectStandardOutput = $ServerLog
    RedirectStandardError  = $ServerErr
}
$InterviewerProcess = Start-Process @serverArgs
Write-OK ("Interviewer server starting (PID {0}) - log: {1}" -f $InterviewerProcess.Id, $ServerLog)

$attempt = 0
$serverReady = $false
while (-not $serverReady -and $attempt -lt 30) {
    Start-Sleep -Seconds 2
    $attempt++
    $curlResult = curl.exe -s -o NUL -w "%{http_code}" $HealthUrl 2>$null
    if ($curlResult -eq "200") {
        Write-OK ("Interviewer server responding (took ~{0}s)" -f ($attempt * 2))
        $serverReady = $true
    } else {
        Write-Warn ("Waiting for interviewer server... ({0}/30)" -f $attempt)
    }
}

if (-not $serverReady) {
    Write-Err ("Interviewer server did NOT come up - check {0} / {1}" -f $ServerLog, $ServerErr)
    exit 1
}

# Verify the RAG wiring end-to-end (health reports the MCP URL the server
# was started with).
$healthBody = curl.exe -s $HealthUrl 2>$null
if ($healthBody -match $RAGMCPUrl) {
    Write-OK ("RAG link verified: management plane reports {0}" -f $RAGMCPUrl)
} else {
    Write-Warn "Management plane health does not report $RAGMCPUrl -- check exported env"
    if ($healthBody) { Write-Warn ("Health body: {0}" -f $healthBody) }
}

# ==== Step 8: Optional Streamlit interview UI =============================
$StreamlitProcess = $null
$StreamlitTunnelHost = $null
$WebProcess = $null
$TunnelHost = $null
if ($WithStreamlit) {
    Write-Step "Step 8: Streamlit interview UI (port $StreamlitPort)"
    $appPath = Join-Path $ProjectRoot "web\streamlit_app.py"
    if (-not (Test-Path $appPath)) {
        Write-Err ("streamlit_app.py not found: {0}" -f $appPath)
    } else {
        $slExe = if (Test-Path $StreamlitExe) { $StreamlitExe } else { "streamlit" }
        if (Test-Path $StreamlitLog) { Remove-Item $StreamlitLog -Force -ErrorAction SilentlyContinue }
        $slArgs = @{
            FilePath               = $slExe
            ArgumentList           = "run", $appPath, "--server.port", "$StreamlitPort", "--server.headless", "true"
            WindowStyle            = "Hidden"
            PassThru               = $true
            RedirectStandardOutput = $StreamlitLog
        }
        $StreamlitProcess = Start-Process @slArgs
        Write-OK ("Streamlit UI starting (PID {0}) -> http://localhost:{1} - log: {2}" -f $StreamlitProcess.Id, $StreamlitPort, $StreamlitLog)

        # Readiness: Streamlit answers 200 on / once up.
        $attempt = 0
        $uiReady = $false
        while (-not $uiReady -and $attempt -lt 30) {
            Start-Sleep -Seconds 2
            $attempt++
            $code = curl.exe -s -o NUL -w "%{http_code}" ("http://127.0.0.1:{0}/" -f $StreamlitPort) 2>$null
            if ($code -eq "200") {
                Write-OK ("Streamlit responding (took ~{0}s)" -f ($attempt * 2))
                $uiReady = $true
            } else {
                Write-Warn ("Waiting for Streamlit... ({0}/30, HTTP {1})" -f $attempt, $code)
            }
        }
        if (-not $uiReady) {
            Write-Err ("Streamlit did NOT come up - check {0}" -f $StreamlitLog)
        }
    }
}

# ==== Step 8b: Static web app (optional) ==================================
if ($WithWeb) {
    Write-Step "Step 8b: Static web app (port 8080)"
    $webArgs = @{
        FilePath               = $PythonExe
        ArgumentList           = "-m", "http.server", "8080", "--bind", "127.0.0.1", "--directory", (Join-Path $ProjectRoot "web")
        WindowStyle            = "Hidden"
        PassThru               = $true
        RedirectStandardOutput = $WebLog
    }
    $WebProcess = Start-Process @webArgs
    Write-OK ("Web app starting (PID {0}) -> http://127.0.0.1:8080" -f $WebProcess.Id)
    Write-Warn "The web page is a LiveKit voice-room client (Phase 3) -- it is inert without a LiveKit server + token endpoint."
    Write-Warn "The runnable path today is the text-mode interviewer: python -m interviewer.demo --questions 2"
}

if ($WithTunnel) {
    Write-Step "Step 8c: Cloudflare quick tunnel (port $Port)"
    $cloudflaredPath = Find-CloudflaredExe
    if (-not $cloudflaredPath) {
        Write-Err "cloudflared not found! Install with: winget install Cloudflare.cloudflared"
    } else {
        Write-OK ("cloudflared: {0}" -f $cloudflaredPath)
        $TunnelHost = Start-QuickTunnel -Exe $cloudflaredPath -TunPort $Port `
            -Label "Interviewer" `
            -CacheFile $TunnelFile `
            -LogBase "interviewer_tunnel"
        if ($TunnelHost) {
            [System.IO.File]::WriteAllText($TunnelFile, $TunnelHost)
            $env:TUNNEL_HOST = $TunnelHost
        }
        if ($StreamlitProcess) {
            $StreamlitTunnelHost = Start-QuickTunnel -Exe $cloudflaredPath -TunPort $StreamlitPort `
                -Label "Streamlit UI" `
                -CacheFile (Join-Path $ProjectRoot ".tunnel_8501") `
                -LogBase "interviewer_streamlit_tunnel"
        }
    }
}

# ==== Step 8d: Voice stack (optional -WithVoice) ==========================
$LiveKitProcess = $null
$VoiceWorkerProcess = $null
if ($WithVoice) {
    Write-Step "Step 8d: Voice stack (LiveKit :$LiveKitPort + agent worker)"

    if (-not (Test-Path $LiveKitServer)) {
        $lkMissing = ("livekit-server not found at {0} -- download the windows_amd64 release zip " -f $LiveKitServer) +
            "from github.com/livekit/livekit/releases into .tools/livekit/"
        Write-Err $lkMissing
    } else {
        if (Test-Path $LiveKitLog) { Remove-Item $LiveKitLog -Force -ErrorAction SilentlyContinue }
        $lkArgs = @{
            FilePath               = $LiveKitServer
            ArgumentList           = "--dev", "--node-ip", "127.0.0.1"
            WindowStyle            = "Hidden"
            PassThru               = $true
            RedirectStandardOutput = $LiveKitLog
        }
        $LiveKitProcess = Start-Process @lkArgs
        Write-OK ("livekit-server starting (PID {0}) -> http://127.0.0.1:{1} - log: {2}" -f $LiveKitProcess.Id, $LiveKitPort, $LiveKitLog)

        $attempt = 0
        $lkReady = $false
        while (-not $lkReady -and $attempt -lt 20) {
            Start-Sleep -Seconds 2
            $attempt++
            $code = curl.exe -s -o NUL -w "%{http_code}" ("http://127.0.0.1:{0}/" -f $LiveKitPort) 2>$null
            if ($code -eq "200") {
                Write-OK ("livekit-server responding (took ~{0}s)" -f ($attempt * 2))
                $lkReady = $true
            } else {
                Write-Warn ("Waiting for livekit-server... ({0}/20, HTTP {1})" -f $attempt, $code)
            }
        }
        if (-not $lkReady) {
            Write-Err ("livekit-server did NOT come up - check {0}" -f $LiveKitLog)
        }
    }

    # The agent worker: registers with livekit-server, then serves one
    # interview per room (VAD -> STT -> LLM+RAG -> TTS -> playback).
    if (Test-Path $VoiceWorkerLog) { Remove-Item $VoiceWorkerLog -Force -ErrorAction SilentlyContinue }
    $vwArgs = @{
        FilePath               = $PythonExe
        ArgumentList           = "-u", "-m", "interviewer.voice.worker"
        WindowStyle            = "Hidden"
        PassThru               = $true
        RedirectStandardOutput = $VoiceWorkerLog
    }
    $VoiceWorkerProcess = Start-Process @vwArgs
    Write-OK ("Voice agent worker starting (PID {0}) - log: {1}" -f $VoiceWorkerProcess.Id, $VoiceWorkerLog)

    $attempt = 0
    $workerReady = $false
    while (-not $workerReady -and $attempt -lt 30) {
        Start-Sleep -Seconds 2
        $attempt++
        if ((Test-Path $VoiceWorkerLog) -and (Select-String -Path $VoiceWorkerLog -Pattern "registered worker" -Quiet -ErrorAction SilentlyContinue)) {
            Write-OK ("Voice worker registered with livekit-server (took ~{0}s)" -f ($attempt * 2))
            $workerReady = $true
        } else {
            Write-Warn ("Waiting for the voice worker to register... ({0}/30)" -f $attempt)
        }
    }
    if (-not $workerReady) {
        Write-Err ("Voice worker did NOT register - check {0}" -f $VoiceWorkerLog)
        if (Test-Path $VoiceWorkerLog) { Get-Content $VoiceWorkerLog -Tail 8 -ErrorAction SilentlyContinue | ForEach-Object { Write-Warn $_ } }
    }

    Write-OK "Voice interview UI: http://127.0.0.1:$Port/  (mic + speakers). Start an interview promptly after boot."
    Write-Warn "Dev caveat: livekit-server (dev mode) drops a worker that idles ~20s; if a session fails to join, re-run with -WithVoice to restart the pair."
}

# ==== Step 9: Summary =====================================================
Write-Host ""
Write-Host ("{0}{1}AI MOCK INTERVIEWER STARTED{2}" -f $GREEN, $BOLD, $RESET)
Write-Host ""
Write-Host ("{0}Management plane:{1}   {0}http://127.0.0.1:{2}/health{1}" -f $CYAN, $RESET, $Port)
Write-Host ("{0}RAG MCP:{1}            {0}{2}{1}  (enterprise-rag-core, folder: {3})" -f $CYAN, $RESET, $RAGMCPUrl, $ERCRoot)
Write-Host ("{0}  Tools:{1}            retrieve_context, execute_agent_context, interview_bank, interview_question, interview_followup" -f $BOLD, $RESET)
Write-Host ("{0}LLM:{1}                {0}{2}{1} (model {3})" -f $CYAN, $RESET, $LLMBaseUrl, $LLMModel)
Write-Host ("{0}Embeddings:{1}         Ollama nomic-embed-text (via RAG core auto backend)" -f $CYAN, $RESET)
Write-Host ("{0}Interview env:{1}      RAG_MCP_URL, INTERVIEW_LLM_BASE_URL, INTERVIEW_LLM_MODEL, INTERVIEW_DOMAIN={2}" -f $CYAN, $RESET, $env:INTERVIEW_DOMAIN)
if ($LiveKitProcess) {
    Write-Host ("{0}LiveKit:{1}              {0}http://127.0.0.1:{2}{1}  (dev mode, key devkey)" -f $CYAN, $RESET, $LiveKitPort)
}
if ($VoiceWorkerProcess) {
    Write-Host ("{0}Voice worker:{1}         PID {0}{2}{1}  (agent: interviewer-agent)" -f $CYAN, $RESET, $VoiceWorkerProcess.Id)
    Write-Host ("{0}Voice interview:{1}      {0}http://127.0.0.1:{2}/{1}  -- spoken interview in the browser" -f $CYAN, $RESET, $Port)
}
if ($StreamlitProcess) {
    Write-Host ("{0}Streamlit UI:{1}        {0}http://localhost:{2}{1}  (interview chat)" -f $CYAN, $RESET, $StreamlitPort)
}
if ($WebProcess) {
    Write-Host ("{0}Web app:{1}            {0}http://127.0.0.1:8080{1}" -f $CYAN, $RESET)
}
if ($TunnelHost) {
    Write-Host ("{0}Public tunnel:{1}      {0}https://{2}{1}" -f $CYAN, $RESET, $TunnelHost)
}
if ($StreamlitTunnelHost) {
    Write-Host ("{0}Streamlit tunnel:{1}   {0}https://{2}{1}" -f $CYAN, $RESET, $StreamlitTunnelHost)
}
Write-Host ""
Write-Host ("{0}Text-mode interview (proves LLM + RAG + FSM end-to-end):{1}" -f $BOLD, $RESET)
Write-Host ("   {0}python -m interviewer.demo --questions 2{1}" -f $CYAN, $RESET)
Write-Host ("{0}Live integration tests:{1}" -f $BOLD, $RESET)
Write-Host ("   {0}python -m pytest tests/ -m live{1}" -f $CYAN, $RESET)
Write-Host ""
Write-Host ("{0}Logs:{1}" -f $BOLD, $RESET)
Write-Host ("   Interviewer:  {0}" -f $ServerLog)
Write-Host ("   Interviewer:  {0}  (errors)" -f $ServerErr)
Write-Host ("   RAG MCP:      {0}" -f $MCPLog)
Write-Host ("   RAG MCP:      {0}  (errors)" -f $MCPErrLog)
Write-Host ("   ERC launcher: {0}" -f $ERCLog)
if ($WebProcess) { Write-Host ("   Web:          {0}" -f $WebLog) }
if ($StreamlitProcess) { Write-Host ("   Streamlit:    {0}" -f $StreamlitLog) }
Write-Host ""
Write-Host ("{0}Stop:{1} taskkill /F /IM python.exe /FI ""PID NE $PID""  (or Ctrl+C in this console then re-run to restart)" -f $YELLOW, $RESET)
Write-Host ("{0}Re-run: .\start_services.ps1{1}" -f $CYAN, $RESET)
Write-Host ""

# Process-alive guard
Start-Sleep -Seconds 2
$anyExited = $false
if ($InterviewerProcess -and $InterviewerProcess.HasExited) {
    Write-Err ("Interviewer server (PID {0}) has already exited! Check {1}" -f $InterviewerProcess.Id, $ServerErr)
    $anyExited = $true
}
if ($MCPProcess -and $MCPProcess.HasExited) {
    Write-Err ("RAG MCP server (PID {0}) has already exited! Check {1}" -f $MCPProcess.Id, $MCPErrLog)
    $anyExited = $true
}
if ($StreamlitProcess -and $StreamlitProcess.HasExited) {
    Write-Err ("Streamlit UI (PID {0}) has already exited! Check {1}" -f $StreamlitProcess.Id, $StreamlitLog)
    $anyExited = $true
}
if (-not $anyExited) {
    Write-OK "All launched processes are alive."
}
