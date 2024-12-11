"""

This software is licensed under the Modified BSD License:

Copyright (c) 2013, Sigve Tjora
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:
    * Redistributions of source code must retain the above copyright
      notice, this list of conditions and the following disclaimer.
    * Redistributions in binary form must reproduce the above copyright
      notice, this list of conditions and the following disclaimer in the
      documentation and/or other materials provided with the distribution.
    * Neither the name of the <organization> nor the
      names of its contributors may be used to endorse or promote products
      derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL <COPYRIGHT HOLDER> BE LIABLE FOR ANY
DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""

import time
import threading
import math
import queue
from . import OSC, x32parameters

setting_paths = x32parameters.get_settings()

class TimeoutError(Exception):
    pass

class BehringerX32(object):
    def __init__(self, x32_address, server_port, verbose, timeout=10, behringer_port=10023):
        self._settings = {}
        self._verbose = verbose
        self._timeout = timeout
        self.callbacks = []
        self.send_queue = queue.Queue()

        self._server = OSC.OSCServer(("", server_port))
        self._client = OSC.OSCClient(server=self._server) #This makes sure that client and server uses same socket. This has to be this way, as the X32 sends notifications back to same port as the /xremote message came from
        
        self._client.connect((x32_address, behringer_port))
        self._server.addMsgHandler("default", self.handle_message)

        self._thread = threading.Thread(target=self._server.serve_forever)
        self._thread.daemon = True
        self._thread.start()
        
        self._ping_thread = threading.Thread(target=self.ping_thread)
        self._ping_thread.daemon = True
        self._ping_thread.start()
        
        self._send_thread = threading.Thread(target=self.send_thread)
        self._send_thread.daemon = True
        self._send_thread.start()

    def __del__(self):
        self._server.close()
        self._client.close()
        
    def send_thread(self):
        while True:
            msg = self.send_queue.get()
            self._client.send(msg)
            time.sleep(0.001)
        
    def ping_thread(self):
        while True:
            self._client.send(OSC.OSCMessage("/xremote"))
            time.sleep(7)
        
    def handle_message(self, addr, tags, data, client_address):
        self._settings[addr] = {
            "timestamp": time.monotonic(),
            "data": data
        }
        print(f"Got message {addr} {data}")
        if self.callbacks:
            print(f"Handling callback {addr} {data}")
            setting = setting_paths.get(addr)
            if setting:
                value = setting.deserialize(data)
                for callback in self.callbacks:
                    callback(addr, value)
                    
    def register_callback(self, callback):
        self.callbacks.append(callback)
        
    def clear_callback(self, callback):
        if callback in self.callbacks:
            self.callbacks.remove(callback)

    def get_value(self, path, max_age=None):
        if not path in setting_paths:
            raise ValueError(f"Unknown setting {path}")
        setting = setting_paths[path]
        start = time.monotonic()
        if not max_age is None:
            if path in self._settings and (start - self._settings[path]["timestamp"]) < max_age:
                return setting.deserialize(self._settings[path]["data"])

        while time.monotonic() - start < self._timeout:
            self._client.send(OSC.OSCMessage(path))
            time.sleep(0.005)
            if self._settings.get(path, {}).get("timestamp", 0) > start:
                return setting.deserialize(self._settings[path]["data"])

    def set_value(self, path, value):
        if path in setting_paths:
            setting = setting_paths[path]
            if not setting.validate(value):
                raise ValueError(f"{value} is not a valid value for {path}")
            serialized = setting.serialize(value)
            self.send_queue.put(OSC.OSCMessage(path, serialized))
        else:
            raise ValueError(f"Unknown setting {path}")

    def freq_to_float(self, f, max=20000):
        return float(round(math.log(f / 20) / math.log(max / 20) * 200) / 200)

    def q_to_float(self, q):
        return float(1 - round(math.log(q / 0.3) / math.log(10 / 0.3) * 71) / 71)