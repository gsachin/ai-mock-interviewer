<#
.SYNOPSIS
    Demo launcher: full Phase 3 + Phase 4 stack in one command.

    Starts the RAG core + management plane (Skill Update page), the VOICE
    interview stack (LiveKit dev server + agent worker), the Streamlit
    text-mode UI, and Cloudflare quick tunnels for all of it — the demo is
    shareable over the internet.

    Equivalent to:  .\start_services.ps1 -WithVoice -WithStreamlit -WithTunnel

.DESCRIPTION
    Wraps start_services.ps1 with the demo flag combo so nothing has to be
    remembered:

        voice UI + Skill Update page ..... http://127.0.0.1:8010/  (tunneled)
        text-mode Streamlit interviews .. http://127.0.0.1:8501/  (tunneled)
        add question-bank skills ......... http://127.0.0.1:8010/skills.html
        RAG MCP .......................... http://127.0.0.1:8031/mcp

    Public URLs are printed by the launcher at the end (also cached in
    .interviewer_tunnel / .tunnel_livekit, git-ignored).

.PARAMETER ForcePrepopulate
    Rebuild ALL question banks from question_banks/*.md instead of the
    idempotent skip (default).

.EXAMPLE
    .\start_demo.ps1
    .\start_demo.ps1 -ForcePrepopulate        # also rebuild the banks
#>
param(
    [switch]$ForcePrepopulate
)

$ErrorActionPreference = "Stop"

# extra flags a demo operator may still want (prepopulate is the common one)
$extra = @()
if ($ForcePrepopulate) { $extra += "-ForcePrepopulate" }

Write-Host ""
Write-Host "  Starting the AI Mock Interviewer DEMO (voice + Streamlit + tunnels)..." -ForegroundColor Cyan
Write-Host "  Equivalent to:  .\start_services.ps1 -WithVoice -WithStreamlit -WithTunnel $($extra -join ' ')" -ForegroundColor DarkGray
Write-Host ""

& (Join-Path $PSScriptRoot "start_services.ps1") `
    -WithVoice -WithStreamlit -WithTunnel @extra
exit $LASTEXITCODE
