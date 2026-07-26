# Copy this file to deploy.config.ps1 and replace every CHANGE_ME value.
# Deployment credentials stay server-side in Api\.env and are never embedded.

$ShareDrive = "Z:"
$ApiBaseUrl = "http://CHANGE_ME_DEPLOY_SERVER:8000"
$ValidateApiServerCertificate = $false
$ApiServerCertificateType = "self_signed"
$ApiServerCertificateBase64 = ""
$ImagesPath = "Z:\Images"
$DriversPath = "Z:\Drivers"
$ProgramsPath = "Z:\Programs"
$ImageIndex = 4
$SetupLocalAdminName = "localadmin"
$EnableBuiltInAdministrator = $true
$EnableSetupLocalAdmin = $true
$EnableGuiImageApplyProgress = $true
