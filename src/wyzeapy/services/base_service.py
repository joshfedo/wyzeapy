#  Copyright (c) 2021. Mulliken, LLC - All Rights Reserved
#  You may use, distribute and modify this code under the terms
#  of the attached license. You should have received a copy of
#  the license with this file. If not, please write to:
#  katie@mulliken.net to receive a copy
import asyncio
import json
import logging
import time
from typing import List, Tuple, Any, Dict, Optional

import aiohttp

from .update_manager import DeviceUpdater, UpdateManager
from ..const import PHONE_SYSTEM_TYPE, APP_VERSION, APP_VER, PHONE_ID, APP_NAME, OLIVE_APP_ID, APP_INFO, SC, SV, APP_PLATFORM, SOURCE
from ..crypto import olive_create_signature
from ..payload_factory import olive_create_hms_patch_payload, olive_create_hms_payload, \
    olive_create_hms_get_payload, ford_create_payload, olive_create_get_payload, olive_create_post_payload, \
    olive_create_user_info_payload, devicemgmt_create_capabilities_payload, devicemgmt_get_iot_props_list
from ..types import PropertyIDs, Device, DeviceMgmtToggleType
from ..utils import check_for_errors_standard, check_for_errors_hms, check_for_errors_lock, \
    check_for_errors_iot, wyze_encrypt, check_for_errors_devicemgmt
from ..wyze_auth_lib import WyzeAuthLib

_LOGGER = logging.getLogger(__name__)


class BaseService:
    _devices: Optional[List[Device]] = None
    _last_updated_time: time = 0  # preload a value of 0 so that comparison will succeed on the first run
    _min_update_time = 1200  # lets let the device_params update every 20 minutes for now. This could probably reduced signicficantly.
    _update_lock: asyncio.Lock = asyncio.Lock()
    _update_manager: UpdateManager = UpdateManager()
    _update_loop = None
    _updater: DeviceUpdater = None
    _updater_dict = {}

    def __init__(self, auth_lib: WyzeAuthLib):
        self._auth_lib = auth_lib

    @staticmethod
    async def start_update_manager():
        if BaseService._update_loop is None:
            BaseService._update_loop = asyncio.get_event_loop()
            BaseService._update_loop.create_task(BaseService._update_manager.update_next())

    def register_updater(self, device: Device, interval):
        self._updater = DeviceUpdater(self, device, interval)
        BaseService._update_manager.add_updater(self._updater)
        self._updater_dict[self._updater.device] = self._updater

    def unregister_updater(self, device: Device):
        if self._updater:
            BaseService._update_manager.del_updater(self._updater_dict[device])
            del self._updater_dict[device]

    async def set_push_info(self, on: bool) -> None:
        await self._auth_lib.refresh_if_should()

        url = "https://api.wyzecam.com/app/user/set_push_info"
        payload = {
            "phone_system_type": PHONE_SYSTEM_TYPE,
            "app_version": APP_VERSION,
            "app_ver": APP_VER,
            "push_switch": "1" if on else "2",
            "sc": SC,
            "ts": int(time.time()),
            "sv": SV,
            "access_token": self._auth_lib.token.access_token,
            "phone_id": PHONE_ID,
            "app_name": APP_NAME
        }

        response_json = await self._auth_lib.post(url, json=payload)

        check_for_errors_standard(self, response_json)

    async def get_user_profile(self) -> Dict[Any, Any]:
        await self._auth_lib.refresh_if_should()

        payload = olive_create_user_info_payload()
        signature = olive_create_signature(payload, self._auth_lib.token.access_token)
        headers = {
            'Accept-Encoding': 'gzip',
            'User-Agent': 'myapp',
            'appid': OLIVE_APP_ID,
            'appinfo': APP_INFO,
            'phoneid': PHONE_ID,
            'access_token': self._auth_lib.token.access_token,
            'signature2': signature
        }

        url = 'https://wyze-platform-service.wyzecam.com/app/v2/platform/get_user_profile'

        response_json = await self._auth_lib.get(url, headers=headers, params=payload)

        return response_json

    async def get_object_list(self) -> List[Device]:
        """
        Wraps the api.wyzecam.com/app/v2/home_page/get_object_list endpoint

        :return: List of devices
        """
        await self._auth_lib.refresh_if_should()

        payload = {
            "phone_system_type": PHONE_SYSTEM_TYPE,
            "app_version": APP_VERSION,
            "app_ver": APP_VER,
            "sc": "9f275790cab94a72bd206c8876429f3c",
            "ts": int(time.time()),
            "sv": "9d74946e652647e9b6c9d59326aef104",
            "access_token": self._auth_lib.token.access_token,
            "phone_id": PHONE_ID,
            "app_name": APP_NAME
        }

        response_json = await self._auth_lib.post("https://api.wyzecam.com/app/v2/home_page/get_object_list",
                                                  json=payload)

        check_for_errors_standard(self, response_json)
        # Cache the devices so that update calls can pull more recent device_params
        BaseService._devices = [Device(device) for device in response_json['data']['device_list']]

        garage_doors = []
        for device in self._devices:
            if 'dongle_product_model' not in device.device_params:
                continue
            if device.device_params['dongle_product_model'] == "HL_CGDC":
                garage_doors.append(Device({
                    "product_type": "GarageDoor",
                    "product_model": "HL_CGDC",
                    "mac": device.mac,
                    "device_params": device.device_params
                }))
        BaseService._devices.extend(garage_doors)

        return BaseService._devices

    async def get_updated_params(self, device_mac: str = None) -> Dict[str, Optional[Any]]:
        if time.time() - BaseService._last_updated_time >= BaseService._min_update_time:
            await self.get_object_list()
            BaseService._last_updated_time = time.time()
        ret_params = {}
        for dev in BaseService._devices:
            if dev.mac == device_mac:
                ret_params = dev.device_params
        return ret_params

    async def _get_property_list(self, device: Device) -> List[Tuple[PropertyIDs, Any]]:
        """
        Wraps the api.wyzecam.com/app/v2/device/get_property_list endpoint

        :param device: Device to get properties for
        :return: List of PropertyIDs and values
        """

        await self._auth_lib.refresh_if_should()

        payload = {
            "phone_system_type": PHONE_SYSTEM_TYPE,
            "app_version": APP_VERSION,
            "app_ver": APP_VER,
            "sc": "9f275790cab94a72bd206c8876429f3c",
            "ts": int(time.time()),
            "sv": "9d74946e652647e9b6c9d59326aef104",
            "access_token": self._auth_lib.token.access_token,
            "phone_id": PHONE_ID,
            "app_name": APP_NAME,
            "device_model": device.product_model,
            "device_mac": device.mac,
            "target_pid_list": []
        }

        response_json = await self._auth_lib.post("https://api.wyzecam.com/app/v2/device/get_property_list",
                                                  json=payload)

        check_for_errors_standard(self, response_json)
        properties = response_json['data']['property_list']
        property_list = []
        for prop in properties:
            try:
                property_id = PropertyIDs(prop['pid'])
                property_list.append((
                    property_id,
                    prop['value']
                ))
            except ValueError:
                pass

        return property_list

    async def _set_property_list(self, device: Device, plist: List[Dict[str, str]]) -> None:
        """
        Wraps the api.wyzecam.com/app/v2/device/set_property_list endpoint

        :param device: The device for which to set the property(ies)
        :param plist: A list of properties [{"pid": pid, "pvalue": pvalue},...]
        :return:
        """

        await self._auth_lib.refresh_if_should()

        payload = {
            "phone_system_type": PHONE_SYSTEM_TYPE,
            "app_version": APP_VERSION,
            "app_ver": APP_VER,
            "sc": "9f275790cab94a72bd206c8876429f3c",
            "ts": int(time.time()),
            "sv": "9d74946e652647e9b6c9d59326aef104",
            "access_token": self._auth_lib.token.access_token,
            "phone_id": PHONE_ID,
            "app_name": APP_NAME,
            "property_list": plist,
            "device_model": device.product_model,
            "device_mac": device.mac
        }

        response_json = await self._auth_lib.post("https://api.wyzecam.com/app/v2/device/set_property_list",
                                                  json=payload)

        check_for_errors_standard(self, response_json)

    async def _run_action_list(self, device: Device, plist: List[Dict[Any, Any]]) -> None:
        """
        Wraps the api.wyzecam.com/app/v2/auto/run_action_list endpoint

        :param device: The device for which to run the action list
        :param plist: A list of properties [{"pid": pid, "pvalue": pvalue},...]
        """
        await self._auth_lib.refresh_if_should()

        payload = {
            "phone_system_type": PHONE_SYSTEM_TYPE,
            "app_version": APP_VERSION,
            "app_ver": APP_VER,
            "sc": "9f275790cab94a72bd206c8876429f3c",
            "ts": int(time.time()),
            "sv": "9d74946e652647e9b6c9d59326aef104",
            "access_token": self._auth_lib.token.access_token,
            "phone_id": PHONE_ID,
            "app_name": APP_NAME,
            "action_list": [
                {
                    "instance_id": device.mac,
                    "action_params": {
                        "list": [
                            {
                                "mac": device.mac,
                                "plist": plist
                            }
                        ]
                    },
                    "provider_key": device.product_model,
                    "action_key": "set_mesh_property"
                }
            ]
        }

        response_json = await self._auth_lib.post("https://api.wyzecam.com/app/v2/auto/run_action_list",
                                                  json=payload)

        check_for_errors_standard(self, response_json)

    async def _get_event_list(self, count: int) -> Dict[Any, Any]:
        """
        Wraps the api.wyzecam.com/app/v2/device/get_event_list endpoint

        :param count: Number of events to gather
        :return: Response from the server
        """

        await self._auth_lib.refresh_if_should()

        payload = {
            "phone_id": PHONE_ID,
            "begin_time": int((time.time() - (60 * 60)) * 1000),
            "event_type": "",
            "app_name": APP_NAME,
            "count": count,
            "app_version": APP_VERSION,
            "order_by": 2,
            "event_value_list": [
                "1",
                "13",
                "10",
                "12"
            ],
            "sc": "9f275790cab94a72bd206c8876429f3c",
            "device_mac_list": [],
            "event_tag_list": [],
            "sv": "782ced6909a44d92a1f70d582bbe88be",
            "end_time": int(time.time() * 1000),
            "phone_system_type": PHONE_SYSTEM_TYPE,
            "app_ver": APP_VER,
            "ts": 1623612037763,
            "device_mac": "",
            "access_token": self._auth_lib.token.access_token
        }

        response_json = await self._auth_lib.post("https://api.wyzecam.com/app/v2/device/get_event_list",
                                                  json=payload)

        check_for_errors_standard(self, response_json)
        return response_json

    async def _run_action(self, device: Device, action: str) -> None:
        """
        Wraps the api.wyzecam.com/app/v2/auto/run_action endpoint

        :param device: The device for which to run the action
        :param action: The action to run
        :return:
        """

        await self._auth_lib.refresh_if_should()

        payload = {
            "phone_system_type": PHONE_SYSTEM_TYPE,
            "app_version": APP_VERSION,
            "app_ver": APP_VER,
            "sc": "9f275790cab94a72bd206c8876429f3c",
            "ts": int(time.time()),
            "sv": "9d74946e652647e9b6c9d59326aef104",
            "access_token": self._auth_lib.token.access_token,
            "phone_id": PHONE_ID,
            "app_name": APP_NAME,
            "provider_key": device.product_model,
            "instance_id": device.mac,
            "action_key": action,
            "action_params": {},
            "custom_string": "",
        }

        response_json = await self._auth_lib.post("https://api.wyzecam.com/app/v2/auto/run_action",
                                                  json=payload)

        check_for_errors_standard(self, response_json)
    
    async def _run_action_devicemgmt(self, device: Device, type: str, value: str) -> None:
        """
        Wraps the devicemgmt-service-beta.wyze.com/device-management/api/action/run_action endpoint

        :param device: The device for which to run the action
        :param state: on or off
        :return:
        """

        await self._auth_lib.refresh_if_should()

        capabilities = devicemgmt_create_capabilities_payload(type, value)

        payload = {
            "capabilities": [
                capabilities
            ],
            "nonce": int(time.time() * 1000),
            "targetInfo": {
                "id": device.mac,
                "productModel": device.product_model,
                "type": "DEVICE"
            },
            "transactionId": "0a5b20591fedd4du1b93f90743ba0csd" # OG cam needs this (doesn't matter what the value is)
        }

        headers = {
            "authorization": self._auth_lib.token.access_token,
        }

        response_json = await self._auth_lib.post("https://devicemgmt-service-beta.wyze.com/device-management/api/action/run_action",
                                                  json=payload, headers=headers)

        check_for_errors_iot(self, response_json)
    
    async def _set_toggle(self, device: Device, toggleType: DeviceMgmtToggleType, state: str) -> None:
        """
        Wraps the ai-subscription-service-beta.wyzecam.com/v4/subscription-service/toggle-management endpoint

        :param device: The device for which to get the state
        :param toggleType: Enum for the toggle type
        :param state: String state to set for the toggle
        """

        await self._auth_lib.refresh_if_should()

        payload = {
            "data": [
                {
                    "device_firmware": "1234567890",
                    "device_id": device.mac,
                    "device_model": device.product_model,
                    "page_id": [
                        toggleType.pageId
                    ],
                    "toggle_update": [
                        {
                            "toggle_id": toggleType.toggleId,
                            "toggle_status": state
                        }
                    ]
                }
            ],
            "nonce": str(int(time.time() * 1000))
        }


        signature = olive_create_signature(payload, self._auth_lib.token.access_token)
        headers = {
            "access_token": self._auth_lib.token.access_token,
            "timestamp": str(int(time.time() * 1000)),
            "appid": OLIVE_APP_ID,
            "source": SOURCE,
            "signature2": signature,
            "appplatform": APP_PLATFORM,
            "appversion": APP_VERSION,
            "requestid": "35374158s4s313b9a2be7c057f2da5d1"
        }

        response_json = await self._auth_lib.put("https://ai-subscription-service-beta.wyzecam.com/v4/subscription-service/toggle-management",
                                                  json=payload, headers=headers)
        
        check_for_errors_devicemgmt(self, response_json)
    
    async def _get_iot_prop_devicemgmt(self, device: Device) -> Dict[str, Any]:
        """
        Wraps the devicemgmt-service-beta.wyze.com/device-management/api/device-property/get_iot_prop endpoint

        :param device: The device for which to get the state
        :return:
        """

        await self._auth_lib.refresh_if_should()

        payload = {
            "capabilities": devicemgmt_get_iot_props_list(device.product_model),
            "nonce": int(time.time() * 1000),
            "targetInfo": {
                "id": device.mac,
                "productModel": device.product_model,
                "type": "DEVICE"
            }
        }

        headers = {
            "authorization": self._auth_lib.token.access_token,
        }

        response_json = await self._auth_lib.post("https://devicemgmt-service-beta.wyze.com/device-management/api/device-property/get_iot_prop",
                                                  json=payload, headers=headers)
        
        check_for_errors_iot(self, response_json)

        return response_json

    async def _set_property(self, device: Device, pid: str, pvalue: str) -> None:
        """
        Wraps the api.wyzecam.com/app/v2/device/set_property endpoint

        :param device: The device for which to set the property
        :param pid: The property id
        :param pvalue: The property value
        """
        await self._auth_lib.refresh_if_should()

        payload = {
            "phone_system_type": PHONE_SYSTEM_TYPE,
            "app_version": APP_VERSION,
            "app_ver": APP_VER,
            "sc": "9f275790cab94a72bd206c8876429f3c",
            "ts": int(time.time()),
            "sv": "9d74946e652647e9b6c9d59326aef104",
            "access_token": self._auth_lib.token.access_token,
            "phone_id": PHONE_ID,
            "app_name": APP_NAME,
            "pvalue": pvalue,
            "pid": pid,
            "device_model": device.product_model,
            "device_mac": device.mac
        }

        response_json = await self._auth_lib.post("https://api.wyzecam.com/app/v2/device/set_property",
                                                  json=payload)

        check_for_errors_standard(self, response_json)

    async def _monitoring_profile_active(self, hms_id: str, home: int, away: int) -> None:
        """
        Wraps the hms.api.wyze.com/api/v1/monitoring/v1/profile/active endpoint

        :param hms_id: The hms id
        :param home: 1 for home 0 for not
        :param away: 1 for away 0 for not
        :return:
        """
        await self._auth_lib.refresh_if_should()

        url = "https://hms.api.wyze.com/api/v1/monitoring/v1/profile/active"
        query = olive_create_hms_patch_payload(hms_id)
        signature = olive_create_signature(query, self._auth_lib.token.access_token)
        headers = {
            'Accept-Encoding': 'gzip',
            'User-Agent': 'myapp',
            'appid': OLIVE_APP_ID,
            'appinfo': APP_INFO,
            'phoneid': PHONE_ID,
            'access_token': self._auth_lib.token.access_token,
            'signature2': signature,
            'Authorization': self._auth_lib.token.access_token
        }
        payload = [
            {
                "state": "home",
                "active": home
            },
            {
                "state": "away",
                "active": away
            }
        ]
        response_json = await self._auth_lib.patch(url, headers=headers, params=query, json=payload)
        check_for_errors_hms(self, response_json)

    async def _get_plan_binding_list_by_user(self) -> Dict[Any, Any]:
        """
        Wraps the wyze-membership-service.wyzecam.com/platform/v2/membership/get_plan_binding_list_by_user endpoint

        :return: The response to gathering the plan for the current user
        """

        if self._auth_lib.should_refresh:
            await self._auth_lib.refresh()

        url = "https://wyze-membership-service.wyzecam.com/platform/v2/membership/get_plan_binding_list_by_user"
        payload = olive_create_hms_payload()
        signature = olive_create_signature(payload, self._auth_lib.token.access_token)
        headers = {
            'Accept-Encoding': 'gzip',
            'User-Agent': 'myapp',
            'appid': OLIVE_APP_ID,
            'appinfo': APP_INFO,
            'phoneid': PHONE_ID,
            'access_token': self._auth_lib.token.access_token,
            'signature2': signature
        }

        response_json = await self._auth_lib.get(url, headers=headers, params=payload)
        check_for_errors_hms(self, response_json)
        return response_json

    async def _disable_reme_alarm(self, hms_id: str) -> None:
        """
        Wraps the hms.api.wyze.com/api/v1/reme-alarm endpoint

        :param hms_id: The hms_id for the account
        """
        await self._auth_lib.refresh_if_should()

        url = "https://hms.api.wyze.com/api/v1/reme-alarm"
        payload = {
            "hms_id": hms_id,
            "remediation_id": "emergency"
        }
        headers = {
            "Authorization": self._auth_lib.token.access_token
        }

        response_json = await self._auth_lib.delete(url, headers=headers, json=payload)

        check_for_errors_hms(self, response_json)

    async def _monitoring_profile_state_status(self, hms_id: str) -> Dict[Any, Any]:
        """
        Wraps the hms.api.wyze.com/api/v1/monitoring/v1/profile/state-status endpoint

        :param hms_id: The hms_id
        :return: The response that includes the status
        """
        if self._auth_lib.should_refresh:
            await self._auth_lib.refresh()

        url = "https://hms.api.wyze.com/api/v1/monitoring/v1/profile/state-status"
        query = olive_create_hms_get_payload(hms_id)
        signature = olive_create_signature(query, self._auth_lib.token.access_token)
        headers = {
            'User-Agent': 'myapp',
            'appid': OLIVE_APP_ID,
            'appinfo': APP_INFO,
            'phoneid': PHONE_ID,
            'access_token': self._auth_lib.token.access_token,
            'signature2': signature,
            'Authorization': self._auth_lib.token.access_token,
            'Content-Type': "application/json"
        }

        response_json = await self._auth_lib.get(url, headers=headers, params=query)

        check_for_errors_hms(self, response_json)
        return response_json

    async def _lock_control(self, device: Device, action: str) -> None:
        await self._auth_lib.refresh_if_should()

        url_path = "/openapi/lock/v1/control"

        device_uuid = device.mac.split(".")[-1]

        payload = {
            "uuid": device_uuid,
            "action": action  # "remoteLock" or "remoteUnlock"
        }
        payload = ford_create_payload(self._auth_lib.token.access_token, payload, url_path, "post")

        url = "https://yd-saas-toc.wyzecam.com/openapi/lock/v1/control"

        response_json = await self._auth_lib.post(url, json=payload)

        check_for_errors_lock(self, response_json)

    async def _get_lock_info(self, device: Device) -> Dict[str, Optional[Any]]:
        await self._auth_lib.refresh_if_should()

        url_path = "/openapi/lock/v1/info"

        device_uuid = device.mac.split(".")[-1]

        payload = {
            "uuid": device_uuid,
            "with_keypad": "1"
        }

        payload = ford_create_payload(self._auth_lib.token.access_token, payload, url_path, "get")

        url = "https://yd-saas-toc.wyzecam.com/openapi/lock/v1/info"

        response_json = await self._auth_lib.get(url, params=payload)

        check_for_errors_lock(self, response_json)

        return response_json

    async def _get_device_info(self, device: Device) -> Dict[Any, Any]:
        await self._auth_lib.refresh_if_should()

        payload = {
            "phone_system_type": PHONE_SYSTEM_TYPE,
            "app_version": APP_VERSION,
            "app_ver": APP_VER,
            "device_mac": device.mac,
            "sc": "9f275790cab94a72bd206c8876429f3c",
            "ts": int(time.time()),
            "device_model": device.product_model,
            "sv": "c86fa16fc99d4d6580f82ef3b942e586",
            "access_token": self._auth_lib.token.access_token,
            "phone_id": PHONE_ID,
            "app_name": APP_NAME
        }

        response_json = await self._auth_lib.post("https://api.wyzecam.com/app/v2/device/get_device_Info",
                                                  json=payload)

        check_for_errors_standard(self, response_json)

        return response_json

    async def _get_iot_prop(self, url: str, device: Device, keys: str) -> Dict[Any, Any]:
        await self._auth_lib.refresh_if_should()

        payload = olive_create_get_payload(device.mac, keys)
        signature = olive_create_signature(payload, self._auth_lib.token.access_token)
        headers = {
            'Accept-Encoding': 'gzip',
            'User-Agent': 'myapp',
            'appid': OLIVE_APP_ID,
            'appinfo': APP_INFO,
            'phoneid': PHONE_ID,
            'access_token': self._auth_lib.token.access_token,
            'signature2': signature
        }

        response_json = await self._auth_lib.get(url, headers=headers, params=payload)

        check_for_errors_iot(self, response_json)

        return response_json

    async def _set_iot_prop(self, url: str, device: Device, prop_key: str, value: Any) -> None:
        await self._auth_lib.refresh_if_should()

        payload = olive_create_post_payload(device.mac, device.product_model, prop_key, value)
        signature = olive_create_signature(json.dumps(payload, separators=(',', ':')),
                                           self._auth_lib.token.access_token)
        headers = {
            'Accept-Encoding': 'gzip',
            'Content-Type': 'application/json',
            'User-Agent': 'myapp',
            'appid': OLIVE_APP_ID,
            'appinfo': APP_INFO,
            'phoneid': PHONE_ID,
            'access_token': self._auth_lib.token.access_token,
            'signature2': signature
        }

        payload_str = json.dumps(payload, separators=(',', ':'))

        response_json = await self._auth_lib.post(url, headers=headers, data=payload_str)

        check_for_errors_iot(self, response_json)

    async def _local_bulb_command(self, bulb, plist):
        # await self._auth_lib.refresh_if_should()

        characteristics = {
            "mac": bulb.mac.upper(),
            "index": "1",
            "ts": str(int(time.time_ns() // 1000000)),
            "plist": plist
        }

        characteristics_str = json.dumps(characteristics, separators=(',', ':'))
        characteristics_enc = wyze_encrypt(bulb.enr, characteristics_str)

        payload = {
            "request": "set_status",
            "isSendQueue": 0,
            "characteristics": characteristics_enc
        }

        # JSON likes to add a second \ so we have to remove it for the bulb to be happy
        payload_str = json.dumps(payload, separators=(',', ':')).replace('\\\\', '\\')

        url = "http://%s:88/device_request" % bulb.ip

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=payload_str) as response:
                    print(await response.text())
        except aiohttp.ClientConnectionError:
            _LOGGER.warning("Failed to connect to bulb %s, reverting to cloud." % bulb.mac)
            await self._run_action_list(bulb, plist)
            bulb.cloud_fallback = True

    async def _get_plug_history(
        self, device: Device, start_time, end_time
    ) -> Dict[Any, Any]:
        """Wraps the https://api.wyzecam.com/app/v2/plug/usage_record_list endpoint"""

        await self._auth_lib.refresh_if_should()

        payload = {
            "phone_id": PHONE_ID,
            "date_begin": start_time,
            "date_end": end_time,
            "app_name": APP_NAME,
            "app_version": APP_VERSION,
            "sc": SC,
            "device_mac": device.mac,
            "sv": SV,
            "phone_system_type": PHONE_SYSTEM_TYPE,
            "app_ver": APP_VER,
            "ts": int(time.time()),
            "access_token": self._auth_lib.token.access_token,
        }

        response_json = await self._auth_lib.post(
            "https://api.wyzecam.com/app/v2/plug/usage_record_list", json=payload
        )

        check_for_errors_standard(self, response_json)

        return response_json["data"]["usage_record_list"]
