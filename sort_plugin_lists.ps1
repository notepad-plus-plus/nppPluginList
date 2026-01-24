function Sort-Json($file)
{
    $a = Get-Content $file | ConvertFrom-Json
    $a.'npp-plugins' = $a.'npp-plugins' | sort -Property 'display-name'
    $a | ConvertTo-Json > $file
    #replace 2 spaces by tab
    $content = [IO.File]::ReadAllText($file)
    $content = $content -replace ' {2}', "`t"
    [IO.File]::WriteAllText($file, $content)
}

Sort-Json ".\src\pl.x64.json"
Sort-Json ".\src\pl.x86.json"
Sort-Json ".\src\pl.arm64.json"

