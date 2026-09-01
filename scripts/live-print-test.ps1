#requires -Version 5.1

<#
.SYNOPSIS
    Live end-to-end test: a PDF on disk to a page out of the tray, using the
    app's own conversion and job configuration.

.DESCRIPTION
    Runs the same decisions the Function App makes, against the real printer,
    with no Function App, no SharePoint and no Entra app registration:

      1  sign in (device code, Microsoft Graph PowerShell first-party client)
      2  read the share's LIVE capabilities
      3  ask functionapp/printing which profile applies and what configuration
         it would send  --  python -m printing.plan
      4  convert the PDF with that profile's resolution  --  python -m printing
      5  create the job with the PROFILE'S configuration
      6  upload, start, poll to a terminal state

    WHY STEP 3 MATTERS. The raster and the job configuration are a matched pair:
    the converter renders full-bleed at the media size and relies on
    `scaling: fit` plus the device margins to place it on the sheet. A bench
    script carrying its own hardcoded configuration can print perfectly while the
    deployed app prints cropped. This one borrows the app's answer, so a green
    run is evidence about the code that ships.

    Running this sends one physical print job.

.PARAMETER PdfPath
    The PDF to print. Required.

.PARAMETER ShareId
    Printer share id. Defaults to Noble's Brother MFC-L5800DW.

.PARAMETER KeepRaster
    Keep the generated .pwg instead of deleting it. Useful when a page comes out
    wrong and you want to inspect the file that produced it.

.PARAMETER PlanOnly
    Stop after step 4. Converts and reports, prints nothing.

.EXAMPLE
    .\scripts\live-print-test.ps1 -PdfPath .\samples\invoice.pdf

.EXAMPLE
    .\scripts\live-print-test.ps1 -PdfPath .\samples\invoice.pdf -PlanOnly
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $PdfPath,

    [string] $ShareId = "4429bf4e-6294-4bcf-bd92-b5f3c3ff47c5",

    [switch] $KeepRaster,
    [switch] $PlanOnly,

    [ValidateRange(30, 1800)]
    [int] $PollSeconds = 180
)

$ErrorActionPreference = "Stop"

function Write-Step($text) {
    Write-Host ""
    Write-Host ("=" * 72) -ForegroundColor DarkGray
    Write-Host $text -ForegroundColor Cyan
    Write-Host ("=" * 72) -ForegroundColor DarkGray
}

$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$AppDir = Join-Path $Root "functionapp"

if (-not (Test-Path $Python)) {
    throw "No project venv at $Python. Create it, then: " +
          "$Python -m pip install -r functionapp\requirements.txt"
}

$Pdf = Get-Item -LiteralPath $PdfPath
if ($Pdf.Extension -ne ".pdf") {
    Write-Host "  note: $($Pdf.Name) does not end in .pdf" -ForegroundColor Yellow
}

# --- 1. sign in ---------------------------------------------------------------

Write-Step "1. Sign in"

Import-Module Microsoft.Graph.Authentication

$Scopes = @(
    "PrinterShare.ReadBasic.All"
    "Printer.Read.All"
    "PrintJob.Create"
    "PrintJob.ReadWriteBasic"
)

if (-not (Get-MgContext)) {
    Connect-MgGraph -Scopes $Scopes -UseDeviceAuthentication
}
$Context = Get-MgContext

$Missing = $Scopes | Where-Object { $Context.Scopes -notcontains $_ }
if ($Missing) {
    throw "Missing Microsoft Graph scopes: $($Missing -join ', '). " +
          "Run Disconnect-MgGraph and sign in again."
}

Write-Host "  Signed in as : $($Context.Account)" -ForegroundColor Green
Write-Host "  Print jobs are attributed to this identity."

# --- 2. live capabilities -----------------------------------------------------

Write-Step "2. Printer share, live from Graph"

$ShareUri = "https://graph.microsoft.com/v1.0/print/shares/$ShareId" +
            '?$select=id,displayName,isAcceptingJobs,status,capabilities' +
            '&$expand=printer($select=id)'

try {
    $Share = Invoke-MgGraphRequest -Method GET -Uri $ShareUri
} catch {
    Write-Host "  The share id did not resolve. Re-sharing a printer mints a NEW" -ForegroundColor Red
    Write-Host "  share id; read the current one from Universal Print > Printers >" -ForegroundColor Red
    Write-Host "  the printer > Overview, or run .\scripts\live-printer-check.ps1." -ForegroundColor Red
    throw
}

$Capabilities = $Share["capabilities"]
$PrinterId = $Share["printer"]["id"]

Write-Host "  Printer      : $($Share['displayName'])"
Write-Host "  Printer id   : $PrinterId"
Write-Host "  State        : $($Share['status']['state'])"
Write-Host "  Content types: $(@($Capabilities['contentTypes']) -join ', ')"

if (-not $Share["isAcceptingJobs"]) {
    throw "The printer share is not accepting jobs."
}

# --- 3. ask the app what it would do ------------------------------------------

Write-Step "3. The app's decision  (functionapp/printing)"

$CapsFile = Join-Path ([System.IO.Path]::GetTempPath()) "noble-print-caps.json"
$Capabilities | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $CapsFile -Encoding utf8

Push-Location $AppDir
try {
    $PlanJson = & $Python -m printing.plan `
        --capabilities $CapsFile `
        --share-id $ShareId `
        --printer-id $PrinterId `
        --display-name $Share["displayName"]
    $PlanExit = $LASTEXITCODE
} finally {
    Pop-Location
    Remove-Item -LiteralPath $CapsFile -ErrorAction SilentlyContinue
}

# PowerShell captures multi-line stdout as a string ARRAY. ConvertFrom-Json on
# PS 5.1 wants one string, so join before parsing or this fails on line 2.
$Plan = ($PlanJson -join [Environment]::NewLine) | ConvertFrom-Json

if ($PlanExit -ne 0) {
    Write-Host "  $($Plan.reason)" -ForegroundColor Red
    throw "No conversion path exists for this printer. The app would fail every file."
}

Write-Host "  Profile          : $($Plan.profile)" -ForegroundColor Green
Write-Host "  Upload as        : $($Plan.uploadContentType)"
Write-Host "  Conversion needed: $($Plan.conversionRequired)"
Write-Host "  Raster dpi       : $($Plan.rasterDpi)"
Write-Host ""
Write-Host "  Job configuration the app would send:" -ForegroundColor Cyan
$Plan.jobConfiguration | ConvertTo-Json -Depth 10 | Write-Host

# --- 4. produce the document --------------------------------------------------

Write-Step "4. Prepare the document"

$Upload = $null
$Temporary = $null

if ($Plan.conversionRequired) {
    $Temporary = Join-Path ([System.IO.Path]::GetTempPath()) `
        ("noble-print-{0}.pwg" -f [guid]::NewGuid().ToString("N"))

    Push-Location $AppDir
    try {
        # The SAME dpi the job configuration declares. If these disagree the
        # page prints scaled, and nothing in the response would tell you.
        & $Python -m printing $Pdf.FullName $Temporary --dpi $Plan.rasterDpi | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "Conversion failed." }
    } finally {
        Pop-Location
    }

    $Upload = Get-Item -LiteralPath $Temporary
} else {
    Write-Host "  Printer accepts the document as-is; uploading the PDF unchanged."
    $Upload = $Pdf
}

Write-Host "  Uploading    : $($Upload.Name)"
Write-Host "  Size         : $('{0:N0}' -f $Upload.Length) bytes"

if ($Upload.Length -ge 10MB) {
    throw "Document is $($Upload.Length) bytes. Graph caps a single PUT below " +
          "10 MB and this script uploads in one request. Lower --dpi, or use " +
          "the Function App, which chunks."
}

if ($Plan.conversionRequired) {
    $First4 = [Text.Encoding]::ASCII.GetString(
        [byte[]](Get-Content -LiteralPath $Upload.FullName -Encoding Byte -TotalCount 4))
    if ($First4 -ne "RaS2") {
        throw "Converted file does not start with RaS2 (found '$First4')."
    }
    Write-Host "  PWG signature: RaS2" -ForegroundColor Green
}

if ($PlanOnly) {
    Write-Host ""
    Write-Host "-PlanOnly: stopping before anything is submitted." -ForegroundColor Cyan
    if ($Temporary -and -not $KeepRaster) {
        Remove-Item -LiteralPath $Temporary -ErrorAction SilentlyContinue
    }
    if ($KeepRaster -and $Temporary) { Write-Host "Raster kept at: $Temporary" }
    return
}

# --- 5-7. create, upload, start -----------------------------------------------

try {
    Write-Step "5. Create the print job"

    $JobBody = @{ configuration = $Plan.jobConfiguration } | ConvertTo-Json -Depth 10

    $Job = Invoke-MgGraphRequest -Method POST `
        -Uri "https://graph.microsoft.com/v1.0/print/shares/$ShareId/jobs" `
        -ContentType "application/json" -Body $JobBody

    $JobId = [string] $Job["id"]
    $DocumentId = [string] $Job["documents"][0]["id"]
    if (-not $JobId -or -not $DocumentId) {
        throw "Graph returned no job or document id."
    }
    Write-Host "  Job id       : $JobId" -ForegroundColor Green

    Write-Step "6. Upload"

    $SessionBody = @{
        properties = @{
            documentName = $Upload.Name
            contentType  = $Plan.uploadContentType
            size         = [int64] $Upload.Length
        }
    } | ConvertTo-Json -Depth 5

    $Session = Invoke-MgGraphRequest -Method POST `
        -Uri "https://graph.microsoft.com/v1.0/print/shares/$ShareId/jobs/$JobId/documents/$DocumentId/createUploadSession" `
        -ContentType "application/json" -Body $SessionBody

    $UploadUrl = [string] $Session["uploadUrl"]
    if (-not $UploadUrl) { throw "Graph returned no uploadUrl." }

    $Size = [int64] $Upload.Length
    $LastByte = $Size - 1

    # No Authorization header: the upload URL carries its own token and Graph
    # documents that adding one "might result in an HTTP 401".
    # -UseBasicParsing: without it PowerShell 5.1 hands the response to the IE
    # engine and can block on an interactive prompt mid-PUT.
    $Result = Invoke-WebRequest -Method PUT -Uri $UploadUrl `
        -Headers @{ "Content-Range" = "bytes 0-$LastByte/$Size" } `
        -ContentType "application/octet-stream" `
        -InFile $Upload.FullName -UseBasicParsing

    if ([int] $Result.StatusCode -ne 201) {
        throw "Upload returned HTTP $($Result.StatusCode); expected 201."
    }
    Write-Host "  Upload       : HTTP 201" -ForegroundColor Green

    Write-Step "7. Start"

    $Started = Invoke-MgGraphRequest -Method POST `
        -Uri "https://graph.microsoft.com/v1.0/print/shares/$ShareId/jobs/$JobId/start"
    Write-Host "  State        : $($Started['state'])"

    # --- 8. poll --------------------------------------------------------------

    Write-Step "8. Poll"

    $Deadline = (Get-Date).AddSeconds($PollSeconds)
    $State = ""
    $Current = $null

    do {
        Start-Sleep -Seconds 5
        $Current = Invoke-MgGraphRequest -Method GET `
            -Uri "https://graph.microsoft.com/v1.0/print/shares/$ShareId/jobs/$JobId"
        $Status = $Current["status"]
        $State = [string] $Status["state"]
        Write-Host ("  {0}  state={1}  acquired={2}  {3}" -f `
            (Get-Date -Format "HH:mm:ss"), $State,
            $Status["isAcquiredByPrinter"], (@($Status["details"]) -join ", "))
    } while ($State -notin @("completed", "canceled", "aborted") -and
             (Get-Date) -lt $Deadline)

    Write-Step "Result"
    Write-Host "  Job id       : $JobId"
    Write-Host "  Final state  : $State"

    if ($State -eq "completed") {
        Write-Host ""
        Write-Host "  PRINTED. The app's profile, conversion and job configuration" -ForegroundColor Green
        Write-Host "  all work against this printer." -ForegroundColor Green
        Write-Host "  Check the sheet: full page, nothing cropped at the edges." -ForegroundColor Yellow
    }

    if ($State -ne "completed") {
        Write-Host ""
        Write-Host "  Did NOT complete. Full job below." -ForegroundColor Red
        $Current | ConvertTo-Json -Depth 20 | Write-Host
        exit 1
    }
} finally {
    if ($Temporary -and (Test-Path $Temporary)) {
        if ($KeepRaster) {
            Write-Host ""
            Write-Host "Raster kept at: $Temporary"
        } else {
            Remove-Item -LiteralPath $Temporary -ErrorAction SilentlyContinue
        }
    }
}
