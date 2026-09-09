# Read-only PnP inventory. Called only for AUTO driver modes, before disk changes.
function Get-IronDriverInventory {
    $warnings = @()
    $computer = $null
    try { $computer = Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction Stop } catch {
        $warnings += "Computer inventory unavailable: $($_.Exception.Message)"
    }
    $devices = @()
    try {
        $devices = @(Get-CimInstance -ClassName Win32_PnPEntity -ErrorAction Stop | ForEach-Object {
            @{
                instance_id = [string]$_.PNPDeviceID
                hardware_ids = @($_.HardwareID | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
                compatible_ids = @($_.CompatibleID | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
                device_class = [string]$_.PNPClass
                status = [string]$_.Status
                problem_code = if ($null -ne $_.ConfigManagerErrorCode) { [int]$_.ConfigManagerErrorCode } else { $null }
            }
        } | Where-Object { -not [string]::IsNullOrWhiteSpace($_.instance_id) })
    } catch {
        $warnings += "PnP inventory unavailable: $($_.Exception.Message)"
    }
    return @{
        manufacturer = [string]$computer.Manufacturer
        model = [string]$computer.Model
        family = [string]$computer.SystemFamily
        sku = [string]$computer.SystemSKUNumber
        architecture = [string]$env:PROCESSOR_ARCHITECTURE
        devices = @($devices)
        warnings = @($warnings)
    }
}
