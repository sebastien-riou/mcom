#!/usr/bin/env python3
import subprocess
import sys
import binascii
import io
import os
from threading import Thread
import pprint
import time

import sys, os
implpath = os.path.abspath(os.path.join(os.path.dirname(__file__),'..', 'impl', 'python3'))
assert(os.path.exists(implpath))
sys.path.append(implpath)

import mcom



import socket

#parameters that must be shared between master and slave
port = 5000
#side specific parameters
buffer_length = 4
long_test=False

if (len(sys.argv) > 4) or (len(sys.argv) < 2) or (sys.argv[1] not in ['device', 'host']):
    print("ERROR: needs at least 1 arguement, accept at most 3 arguments")
    print("[device | host] port long_test")
    exit()

if len(sys.argv)>2:
    port = int(sys.argv[2])

if len(sys.argv)>3:
    long_test = sys.argv[3]=='1'

refdat = bytearray()
for i in range(0,(1<<16)+1):
    refdat.append(i & 0xFF)

refdatle = bytearray()
for i in range(0,(1<<16)+1):
    refdatle.append((i & 0xFF) ^ 0xFF)

if sys.argv[1]=='device':
    serversocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    serversocket.settimeout(None)
    serversocket.bind(('localhost', port))
    serversocket.listen(1) # become a server socket, maximum 1 connections

    connection, address = serversocket.accept()
    assert(serversocket.gettimeout() == None)

    link = connection
    device_com = mcom.SocketComDriver(link)

    print("Device connected")
    device  = mcom.MCom(is_host=False,com_driver=device_com)
    print("Device init done")
    device.open_channel(name="test1",num=1,rx_buf_size=32, tx_buf_size=32, description="channel test1 description (device side)"  )

    def spy_frame_tx(data):
        print("DEVICE FRAME TX:",mcom.Utils.hexstr(data))

    def spy_frame_rx(data):
        print("DEVICE FRAME RX:",mcom.Utils.hexstr(data))

    device.spy_frame_tx = spy_frame_tx
    device.spy_frame_rx = spy_frame_rx

    dat,chan = device.rx(length=1)
    dat += device.rx(channel=chan,length=1024,block=False)
    print("device rx:",dat)
    device.tx(channel=chan,data=dat)

    time.sleep(1)
    device.close_connection()
    print("device done")
    exit()

else:
    clientsocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    clientsocket.settimeout(None)
    clientsocket.connect(('localhost', port))
    assert(clientsocket.gettimeout() == None)

    link = clientsocket
    host_com = mcom.SocketComDriver(link)
    host = mcom.MCom(is_host=True,com_driver=host_com)

    def spy_frame_tx(data):
        print("HOST FRAME TX:",mcom.Utils.hexstr(data))

    def spy_frame_rx(data):
        print("HOST FRAME RX:",mcom.Utils.hexstr(data))

    host.spy_frame_tx = spy_frame_tx
    host.spy_frame_rx = spy_frame_rx

    chan=1
    host.open_channel(name="test1",num=1,rx_buf_size=32, tx_buf_size=32, description="channel test1 description (host side)"  )
    time.sleep(1)
    dat="hello world".encode('utf-8')
    print("sending: ",dat)
    host.tx(channel=chan,data=dat)
    print("receiving:")
    response = host.rx(channel=chan,length=len(dat))
    print(response, flush=True)
    print(response.decode('utf-8'), flush=True)

    time.sleep(1)
    host.close_connection()
    print("done", flush=True)
    #input("Press enter to quit ")
