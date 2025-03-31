#  Copyright (c) 2021. Mulliken, LLC - All Rights Reserved
#  You may use, distribute and modify this code under the terms
#  of the attached license. You should have received a copy of
#  the license with this file. If not, please write to:
#  katie@mulliken.net to receive a copy
import base64
import binascii
import hashlib
from typing import Dict, Any, List, Optional

from Crypto.Cipher import AES

from .exceptions import ParameterError, AccessTokenError, UnknownApiError
from .types import ResponseCodes, PropertyIDs, Device, Event

PADDING = bytes.fromhex("05")


def pad(plain_text):
    """
    func to pad cleartext to be multiples of 8-byte blocks.
    If you want to encrypt a text message that is not multiples of 8-byte
    blocks, the text message must be padded with additional bytes to make the
    text message to be multiples of 8-byte blocks.
    """
    raw = plain_text.encode("ascii")

    pad_num = AES.block_size - len(raw) % AES.block_size
    raw += PADDING * pad_num

    return raw


def wyze_encrypt(key, text):
    """
    Reimplementation of the Wyze app's encryption mechanism.

    The decompiled code can be found here 👇
    https://paste.sr.ht/~joshmulliken/e9f67e05c4a774004b226d2ac1f070b6d341cb39
    """
    raw = pad(text)
    key = key.encode("ascii")
    iv = key  # Wyze uses the secret key for the iv as well
    cipher = AES.new(key, AES.MODE_CBC, iv)
    enc = cipher.encrypt(raw)
    b64_enc = base64.b64encode(enc).decode("ascii")
    b64_enc = b64_enc.replace("/", r'\/')
    return b64_enc


def wyze_decrypt(key, enc):
    """
    Reimplementation of the Wyze app's decryption mechanism.

    The decompiled code can be found here 👇
    https://paste.sr.ht/~joshmulliken/e9f67e05c4a774004b226d2ac1f070b6d341cb39
    """
    enc = base64.b64decode(enc)

    key = key.encode('ascii')
    iv = key
    cipher = AES.new(key, AES.MODE_CBC, iv)
    decrypt = cipher.decrypt(enc)

    decrypt_txt = decrypt.decode("ascii")

    return decrypt_txt


def wyze_decrypt_cbc(key: str, enc_hex_str: str) -> str:
    key_hash = hashlib.md5(key.encode("utf-8")).digest()
    
    iv = b"0123456789ABCDEF"
    cipher = AES.new(key_hash, AES.MODE_CBC, iv)
    
    encrypted_bytes = binascii.unhexlify(enc_hex_str)
    decrypted_bytes = cipher.decrypt(encrypted_bytes)
    
    # PKCS5Padding
    padding_length = decrypted_bytes[-1]
    return decrypted_bytes[:-padding_length].decode()


def create_password(password: str) -> str:
    hex1 = hashlib.md5(password.encode()).hexdigest()
    hex2 = hashlib.md5(hex1.encode()).hexdigest()
    return hashlib.md5(hex2.encode()).hexdigest()


def check_for_errors_standard(service, response_json: Dict[str, Any]) -> None:
    response_code = response_json['code']
    if response_code != ResponseCodes.SUCCESS.value:
        if response_code == ResponseCodes.PARAMETER_ERROR.value:
            raise ParameterError(response_code, response_json['msg'])
        elif response_code == ResponseCodes.ACCESS_TOKEN_ERROR.value:
            service._auth_lib.token.expired = True
            raise AccessTokenError(response_code, "Access Token expired, attempting to refresh")
        elif response_code == ResponseCodes.DEVICE_OFFLINE.value:
            return
        else:
            raise UnknownApiError(response_code, response_json['msg'])


def check_for_errors_lock(service, response_json: Dict[str, Any]) -> None:
    if response_json['ErrNo'] != 0:
        if response_json.get('code') == ResponseCodes.PARAMETER_ERROR.value:
            raise ParameterError(response_json)
        elif response_json.get('code') == ResponseCodes.ACCESS_TOKEN_ERROR.value:
            service._auth_lib.token.expired = True
            raise AccessTokenError("Access Token expired, attempting to refresh")
        else:
            raise UnknownApiError(response_json)


def check_for_errors_devicemgmt(service, response_json: Dict[Any, Any]) -> None:
    if response_json['status'] != 200:
        if "InvalidTokenError>" in response_json['response']['errors'][0]['message']:
            service._auth_lib.token.expired = True
            raise AccessTokenError("Access Token expired, attempting to refresh")
        else:
            raise UnknownApiError(response_json)


def check_for_errors_iot(service, response_json: Dict[Any, Any]) -> None:
    if response_json['code'] != 1:
        if str(response_json['code']) == ResponseCodes.ACCESS_TOKEN_ERROR.value:
            service._auth_lib.token.expired = True
            raise AccessTokenError("Access Token expired, attempting to refresh")
        else:
            raise UnknownApiError(response_json)

def check_for_errors_hms(service, response_json: Dict[Any, Any]) -> None:
    if response_json['message'] is None:
        service._auth_lib.token.expired = True
        raise AccessTokenError("Access Token expired, attempting to refresh")


def return_event_for_device(device: Device, events: List[Event]) -> Optional[Event]:
    for event in events:
        if event.device_mac == device.mac:
            return event

    return None


def create_pid_pair(pid_enum: PropertyIDs, value: str) -> Dict[str, str]:
    return {"pid": pid_enum.value, "pvalue": value}
