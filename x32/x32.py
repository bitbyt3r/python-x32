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
import queue
from collections import namedtuple
import json
import sys
import math
import socket
from . import OSC, x32parameters

ReceivedMessage = namedtuple("ReceivedMessage", "address, tags, data, client_address")

def answers_to_queue_thread(server, queue):

    def add_to_queue(addr, tags, data, client_address):
        msg = ReceivedMessage(address=addr, 
                              tags=tags, 
                              data=data, 
                              client_address = client_address)
        queue.put(msg)

    server.addMsgHandler("default", add_to_queue)    
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    return thread

setting_paths = x32parameters.get_settings()

class TimeoutError(Exception):
    pass

class BehringerX32(object):
    def __init__(self, x32_address, server_port, verbose, timeout=10, behringer_port=10023):
        self._verbose = verbose
        self._timeout = timeout
        self._found_addr = -1

        # if no address is given, search for mixer
        if not x32_address:
            addr_subnet = self.__search_mixer__(server_port)
            x32_address = f"{addr_subnet}.{self._found_addr}"
            behringer_port = self._found_port

        self._server = OSC.OSCServer(("", server_port))
        self._client = OSC.OSCClient(server=self._server) #This makes sure that client and server uses same socket. This has to be this way, as the X32 sends notifications back to same port as the /xremote message came from
        
        self._client.connect((x32_address, behringer_port))
        
        self._input_queue = queue.Queue()
        self._listener_thread = answers_to_queue_thread(self._server, queue=self._input_queue)

    def __del__(self):
        self._server.close()
        self._client.close()

    def __search_mixer__(self, local_port):
        addr_subnet = '.'.join(self.__get_ip__().split('.')[0:3]) # only use first three numbers of local IP address
        while self._found_addr < 0:
            for j in range(10024, 10022, -1): # X32:10023, XAIR:10024 -> check both
                if self._found_addr < 0:
                    for i in range(2, 255):
                        threading.Thread(target = self.__try_to_ping_mixer__, args = (addr_subnet, local_port + 1, i, j, )).start()
                        if self._found_addr >= 0:
                            break
                if self._found_addr < 0:
                    time.sleep(2) # time-out is 1 second -> wait two-times the time-out
        return addr_subnet

    def __try_to_ping_mixer__(self, addr_subnet, start_port, i, j):
        search_mixer = BehringerX32(f"{addr_subnet}.{i}", start_port + i + j, False, 1, j) # just one second time-out
        try:
            search_mixer.ping()
            search_mixer.__del__() # important to delete object before changing found_addr
            self._found_addr = i
            self._found_port = j
        except:
            search_mixer.__del__()

    def __get_ip__(self):
        # taken from stack overflow "Finding local IP addresses using Python's stdlib"
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        try:
            # doesn't even have to be reachable
            s.connect(('10.255.255.255', 1))
            IP = s.getsockname()[0]
        except Exception:
            IP = '127.0.0.1'
        finally:
            s.close()
        return IP

    def ping(self):
        self.get_value(path="/info", safe_get=False)
    
    def get_value(self, path, safe_get=True):
        while True:
            try:
                self._input_queue.get_nowait()
            except queue.Empty:
                break            
        if not safe_get:
            self._client.send(OSC.OSCMessage(path))
            return self._input_queue.get(timeout=self._timeout).data
        else:
            start_time = time.time()
            while True:
                try:
                    self._client.send(OSC.OSCMessage(path))
                    mess = self._input_queue.get(timeout=self._timeout)
                    if mess.address == path:
                        return mess.data
                    if time.time() - start_time > self._timeout:
                        raise TimeoutError("Timeout while readback of path %s" % path,)
                    time.sleep(0.001)
                except queue.Empty:
                    continue

    def get_msg_from_queue(self):
        return self._input_queue.get(timeout=self._timeout)

    def put_msg_on_queue(self, msg):
        self._input_queue.put(msg)

    def db_to_float(self, d, is_bus=False): # based on UNOFFICIAL_X32_OSC_REMOTE_PROTOCOL.pdf
        if d < -60:
            f = (d + 90) / 480
        elif d < -30:
            f = (d + 70) / 160
        elif d < -10:
            f = (d + 50) / 80
        else:
            f = (d + 30) / 40
        return float(round(f * 160) / 160 if is_bus else round(f * 1023) / 1023)

    def freq_to_float(self, f, max=20000):
        return float(round(math.log(f / 20) / math.log(max / 20) * 200) / 200)

    def q_to_float(self, q):
        return float(1 - round(math.log(q / 0.3) / math.log(10 / 0.3) * 71) / 71)

    def set_value(self, path, value, readback=True):
        self._client.send(OSC.OSCMessage(path, value))
        if readback:
            start_time = time.time()
            while True:
                read_back_value = self.get_value(path)[0]
                #Special case for nans
                if len(value) == 1 and len(read_back_value)==1:
                    if type(value[0]) is float and math.isnan(value[0]) and math.isnan(read_back_value[0]):
                        break
                if value and type(value[0]) is float and round(read_back_value[0], 6) == round(value[0], 6):
                    break
                if read_back_value == value:
                    break
                if time.time() - start_time > self._timeout:
                    raise TimeoutError("Timeout while readback of path %s, value=%s, read_back_value=%s" % (path, value, read_back_value)) 
                time.sleep(0.001)
                self._client.send(OSC.OSCMessage(path, value))
                
    def get_state(self):        
        state = {}
        for index, path in enumerate(setting_paths):
            if self._verbose and index % 100 == 0:
                print("Reading parameter %d of %d from x32" % (index, len(setting_paths)))
            value = self.get_value(path)
            assert len(value) == 1
            state[path] = value[0]
        return state
    
    def set_state(self, state):
        """Set state will first set all faders to 0, then load all values except for faders and at the end will it restore the faders.
        
        This is to avoid feedbacks/high volume during restore, if some in between setting would cause problems.
        """
        fader_keys = sorted(key for key in state if key.endswith("fader"))
        parameters = [(key, 0.0) for key in fader_keys]
        parameters.extend((key, state[key]) for key in sorted(state.iterkeys()) if key not in fader_keys)
        parameters.extend((key, state[key]) for key in fader_keys)
        
        for index, my_tuple in enumerate(parameters):
            key, value = my_tuple
            if self._verbose and index % 100 == 0:
                print("Writing parameter %d of %d to x32" % (index, len(state)))
            self.set_value(path=key, value=[value], readback=True)
        return
            
    def save_state_to_file(self, outputfile, state):
        my_dict = {"x32_state": state,
                   }
        json.dump(my_dict, outputfile, sort_keys=True, indent=4)

    def read_state_from_file(self, inputfile):
        my_dict = json.load(inputfile)
        return my_dict["x32_state"]

usage = """This is a utility to load or save the settings of a Behringer X32 mixing desk. All document settings are loaded/stored, and some undocumented.

It is possible to edit the state-files (they are in JSON-format) to delete properties which one does not want to restore.

Beware: This is alpha software, fileformats may change in future.
"""
    
if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description=usage)
    parser.add_argument('--address', default="192.168.208.99",                      
                        help='name/ip-address of Behringer X32 mixing desk')
    parser.add_argument('--filename', default = None, required=True,                         
                        help='Filename')
    parser.add_argument("--to_mixer", default = False, 
                        action = "store_true",
                        help="Load settings to mixer")
    parser.add_argument("--from_mixer", default = False, 
                        action = "store_true",
                        help="Save settings from mixer")
    parser.add_argument("-v", "--verbose", default = False, 
                        action = "store_true",
                        help="Make program output some state messages")
    parser.add_argument('--port', default = 10300,                        
                        help='UDP-port to open on this machine.')

    args = parser.parse_args()

    mixer = BehringerX32(x32_address=args.address, server_port=args.port, verbose=args.verbose)
    mixer.ping()
    
    if args.to_mixer and args.from_mixer:
        print("Only one of to_mixer and from_mixer must be present at same time.")
        parser.print_help()
        sys.exit(1)
    elif args.from_mixer:        
        state = mixer.get_state()
        mixer.save_state_to_file(open(args.filename, "wt"), state)
    elif args.to_mixer:
        read_back_state = mixer.read_state_from_file(inputfile=open(args.filename, "rt"))
        mixer.set_state(state=read_back_state)
    else:
        print("One of to_mixer and from_mixer must be present.")
        parser.print_help()
        sys.exit(1)

