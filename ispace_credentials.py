import os

from cryptography.fernet import Fernet, InvalidToken


ENV_NAME = 'MAXCOURSE_ISPACE_CREDENTIAL_KEY'


class ISpaceCredentialError(RuntimeError):
    pass


def get_ispace_credential_cipher():
    raw_key = os.getenv(ENV_NAME, '').strip()
    if not raw_key:
        return None
    try:
        return Fernet(raw_key.encode('ascii'))
    except (TypeError, ValueError):
        return None


def is_ispace_credential_encryption_configured():
    return get_ispace_credential_cipher() is not None


def encrypt_ispace_password(password):
    cipher = get_ispace_credential_cipher()
    if cipher is None:
        raise ISpaceCredentialError('iSpace credential encryption is not configured')
    if not isinstance(password, str) or not password:
        raise ISpaceCredentialError('iSpace password is required')
    return cipher.encrypt(password.encode('utf-8')).decode('ascii')


def decrypt_ispace_password(token):
    cipher = get_ispace_credential_cipher()
    if cipher is None:
        raise ISpaceCredentialError('iSpace credential encryption is not configured')
    try:
        return cipher.decrypt(str(token).encode('ascii')).decode('utf-8')
    except (InvalidToken, UnicodeError, ValueError, TypeError) as error:
        raise ISpaceCredentialError('Saved iSpace credential cannot be decrypted') from error
