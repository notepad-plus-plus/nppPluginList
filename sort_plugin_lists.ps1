function Sort-Json($file)
{
    $a = Get-Content $file -Raw | ConvertFrom-Json
    $a.'npp-plugins' = $a.'npp-plugins' | Sort-Object -Property 'display-name'
    $content = $a | ConvertTo-Json -Depth 10
    $content = $content -replace ' {2}', "`t"
    [IO.File]::WriteAllText($file, $content)
}

Sort-Json ".\src\pl.x64.json"
Sort-Json ".\src\pl.x86.json"
Sort-Json ".\src\pl.arm64.json"
