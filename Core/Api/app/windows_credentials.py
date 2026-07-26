from dataclasses import dataclass

import win32cred


@dataclass(frozen=True)
class WindowsCredential:
    username: str
    password: str


def read_generic_credential(target: str) -> WindowsCredential:
    credential = win32cred.CredRead(
        target,
        win32cred.CRED_TYPE_GENERIC,
        0,
    )
    username = credential.get("UserName", "")
    password = _decode_credential_blob(credential.get("CredentialBlob", b""))

    if not username or not password:
        raise ValueError(
            f"Windows credential '{target}' must contain a username and password"
        )

    return WindowsCredential(username=username, password=password)


def _decode_credential_blob(blob: bytes | str) -> str:
    if isinstance(blob, str):
        return blob

    try:
        return blob.decode("utf-16-le").rstrip("\x00")
    except UnicodeDecodeError:
        return blob.decode("utf-8")
