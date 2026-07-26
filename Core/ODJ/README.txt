IronAPI creates temporary Offline Domain Join (ODJ) provisioning blobs in
the pending subdirectory.

Each blob is named after its target computer:

    <ComputerName>.txt

WinPE downloads the blob from IronAPI, applies it, and acknowledges successful
application. IronAPI deletes the server-side blob only after that
acknowledgement.

ODJ blobs contain sensitive domain-join provisioning data. Keep this directory
local to the API server, restrict it by ACL, and never commit pending blobs.
