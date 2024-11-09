import asyncio
import json
import logging
import re
import ssl
from inspect import iscoroutinefunction
from typing import Callable, Coroutine, Dict, Union

import requests
from requests_toolbelt import MultipartEncoder, MultipartEncoderMonitor
from websockets import client

FACTORIO_ZONE_ENDPOINT = 'factorio.zone'


class ServerStatus:
    OFFLINE = 'OFFLINE'
    STARTING = 'STARTING'
    STOPPING = 'STOPPING'
    RUNNING = 'RUNNING'


class FZClient:
    def __init__(self, token: str = None):
        self.socket = None
        self.user_token = token
        self.visit_secret = None
        self.regions = {}
        self.versions = {}
        self.slots = {}
        self.saves = {}
        self.mods = []
        self.running = False
        self.launch_id = None
        self.region = None
        self.server_address = None
        self.server_status = ServerStatus.OFFLINE
        self.logs_listeners: list[Callable[[str], Union[Coroutine, Callable]]] = []
        self.message_listeners: list[Callable[[Dict[str, str]], Union[Coroutine, Callable]]] = []
        self.mods_sync = False
        self.saves_sync = False
        self.last_handled_message_num = -1

    async def connect(self):
        ssl_context = ssl.SSLContext()
        ssl_context.verify_mode = ssl.CERT_NONE
        ssl_context.check_hostname = False
        self.socket = await client.connect(
            f'wss://{FACTORIO_ZONE_ENDPOINT}/ws',
            ping_interval=30,
            ping_timeout=10,
            ssl=ssl_context
        )
        logging.info("Connection to Factorio.zone established")
        while True:
            message = await self.socket.recv()
            data = json.loads(message)

            current_num = data.get('num')
            if current_num:
                if current_num <= self.last_handled_message_num:
                    continue
                self.last_handled_message_num = current_num

            if data['type'] == 'visit':
                self.visit_secret = data['secret']
                self.login()
            elif data['type'] == 'options':
                if data['name'] == 'regions':
                    self.regions = data['options']
                elif data['name'] == 'versions':
                    self.versions = data['options']
                elif data['name'] == 'saves':
                    self.saves = data['options']
                    self.saves_sync = True
            elif data['type'] == 'mods':
                self.mods = data['mods']
                self.mods_sync = True
            elif data['type'] == 'idle':
                self.running = False
                self.launch_id = None
                self.server_status = ServerStatus.OFFLINE
                self.last_handled_message_num = -1
                self.server_address = None
            elif data['type'] == 'starting':
                self.running = True
                self.launch_id = data.get('launchId')
                self.server_status = ServerStatus.STARTING
            elif data['type'] == 'stopping':
                self.running = True
                self.launch_id = data.get('launchId')
                self.server_status = ServerStatus.STOPPING
            elif data['type'] == 'running':
                self.running = True
                self.launch_id = data.get('launchId')
                self.server_address = data.get('socket')
                self.server_status = ServerStatus.RUNNING
            elif data['type'] == 'slot':
                self.slots[data['slot']] = data
            elif data['type'] == 'log':
                log = data.get('line')
                await self.on_new_log(log)
            elif data['type'] == 'info':
                line = data.get('line')
                if len(match := re.findall('selecting connection (\d+\.\d+\.\d+\.\d+:\d+)', line)):
                    self.server_address = match[0]
                    self.server_status = ServerStatus.STARTING
            elif data['type'] == 'warn':
                pass
            elif data['type'] == 'error':
                pass

            await self.on_new_message(data)

    async def wait_sync(self):
        while not self.mods_sync or not self.saves_sync:
            await asyncio.sleep(1)

    def add_logs_listener(self, listener):
        self.logs_listeners.append(listener)

    def remove_logs_listener(self, listener):
        self.logs_listeners.remove(listener)

    async def on_new_log(self, log: str):
        for listener in self.logs_listeners:
            if iscoroutinefunction(listener):
                await listener(log)
            else:
                listener(log)

    def add_message_listener(self, listener):
        self.message_listeners.append(listener)

    def remove_message_listener(self, listener):
        self.message_listeners.remove(listener)

    async def on_new_message(self, message_data: Dict[str, str]):
        for listener in self.message_listeners:
            if iscoroutinefunction(listener):
                await listener(message_data)
            else:
                listener(message_data)

    # ------ USER APIs ------------------------------------------------------------------
    def login(self):
        resp = requests.post(
            url=f'https://{FACTORIO_ZONE_ENDPOINT}/api/user/login',
            data={
                'userToken': self.user_token,
                'visitSecret': self.visit_secret,
                'reconnected': False
            })
        if resp.ok:
            body = resp.json()
            self.user_token = body['userToken']
        else:
            raise Exception(f'Error logging in: {resp.text}')

    # ------ MODs APIs ------------------------------------------------------------------
    class Mod:
        def __init__(self, name, file_path, size):
            self.name = name
            self.filePath = file_path
            self.size = size

    async def toggle_mod(self, mod_id: int, enabled: bool):
        self.mods_sync = False
        resp = requests.post(
            url=f'https://{FACTORIO_ZONE_ENDPOINT}/api/mod/toggle',
            data={
                'visitSecret': self.visit_secret,
                'modId': mod_id,
                'enabled': enabled
            })
        if not resp.ok:
            self.mods_sync = True
            raise Exception(f'Error in toggling mod: {resp.text}')

    async def delete_mod(self, mod_id: int):
        self.mods_sync = False
        resp = requests.post(
            url=f'https://{FACTORIO_ZONE_ENDPOINT}/api/mod/delete',
            data={
                'visitSecret': self.visit_secret,
                'modId': mod_id
            })
        if not resp.ok:
            self.mods_sync = True
            raise Exception(f'Error in deleting mod: {resp.text}')

    async def upload_mod(self, mod: Mod, cb: Callable = None):
        file = open(mod.filePath, 'rb')
        if mod.size > 268435456:  # 256MB
            raise Exception(f'Mod file must be under 256MB')

        encoder = MultipartEncoder({
            'visitSecret': self.visit_secret,
            'file': (mod.name, file, 'application/x-zip-compressed'),
            'size': str(mod.size)
        })
        monitor = MultipartEncoderMonitor(encoder, cb)
        self.mods_sync = False
        resp = requests.post(
            f'https://{FACTORIO_ZONE_ENDPOINT}/api/mod/upload',
            headers={'content-type': monitor.content_type},
            data=monitor
        )
        if not resp.ok:
            self.mods_sync = True
            raise Exception(f'Error uploading mod: {resp.text}')

    # ------ SAVE APIs ------------------------------------------------------------------
    class Save:
        def __init__(self, name: str, file_path: str, size: int, slot: str):
            self.name = name
            self.filePath = file_path
            self.size = size
            self.slot = slot

    async def delete_save_slot(self, slot: str):
        self.saves_sync = False
        resp = requests.post(
            url=f'https://{FACTORIO_ZONE_ENDPOINT}/api/save/delete',
            data={
                'visitSecret': self.visit_secret,
                'save': slot
            })
        if resp.status_code != 200:
            self.saves_sync = True
            raise Exception(f'Error deleting save: {resp.text}')

    async def download_save_slot(self, slot: str, file_path: str, cb: Callable):
        with requests.post(
                url=f'https://{FACTORIO_ZONE_ENDPOINT}/api/save/download',
                data={
                    'visitSecret': self.visit_secret,
                    'save': slot
                },
                stream=True
        ) as resp:
            if resp.status_code != 200:
                self.saves_sync = True
                raise Exception(f'Error downloading save: {resp.text}')
            with open(file_path, 'wb') as file:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        file.write(chunk)
                        cb(file.tell())
                file.close()

    async def upload_save(self, save: Save, cb: Callable = None):
        file = open(save.filePath, 'rb')
        if save.size > 100663296:  # 96MB
            raise Exception('Save file must be under 96MB')
        encoder = MultipartEncoder({
            'visitSecret': self.visit_secret,
            'file': (save.name, file, 'application/x-zip-compressed'),
            'size': str(save.size),
            'save': save.slot
        })
        monitor = MultipartEncoderMonitor(encoder, cb)
        self.saves_sync = False
        resp = requests.post(
            url=f'https://{FACTORIO_ZONE_ENDPOINT}/api/save/upload',
            headers={'content-type': monitor.content_type},
            data=monitor
        )
        if not resp.ok:
            self.saves_sync = True
            raise Exception(f'Error uploading save: {resp.text}')

    # ------ INSTANCE APIs --------------------------------------------------------------
    def send_command(self, command):
        resp = requests.post(
            url=f'https://{FACTORIO_ZONE_ENDPOINT}/api/instance/console',
            data={
                'visitSecret': self.visit_secret,
                'launchId': self.launch_id,
                'input': command
            })
        if resp.status_code != 200:
            raise Exception(f'Error sending console command: {resp.text}')

    def start_instance(self, region, version, save, ipv6=True):
        resp = requests.post(
            url=f'https://{FACTORIO_ZONE_ENDPOINT}/api/instance/start',
            data={
                'visitSecret': self.visit_secret,
                'region': region,
                'version': version,
                'save': save,
                'ipv6': ipv6,
            })
        if resp.status_code != 200:
            raise Exception(f'Error starting instance: {resp.text}')
        self.launch_id = resp.json()['launchId']

    def stop_instance(self):
        resp = requests.post(
            url=f'https://{FACTORIO_ZONE_ENDPOINT}/api/instance/stop',
            data={
                'visitSecret': self.visit_secret,
                'launchId': self.launch_id,
            },
            timeout=3600
        )
        if resp.status_code != 200:
            raise Exception(f'Error stopping instance: {resp.text}')
