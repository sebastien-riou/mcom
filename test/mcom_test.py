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

device_rx_buf_size = 12
device_tx_buf_size = 12
host_rx_buf_size   = 12
host_tx_buf_size   = 12

first_test_message="hello world".encode('utf-8')

max_frame = 100
host_rx_cnt=0
host_tx_cnt=0
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
    device.open_channel(name="test1",num=1,
        rx_buf_size=device_rx_buf_size,
        tx_buf_size=device_tx_buf_size,
        description="channel test1 description (device side)"  )
    device.start_com()
    print("Device init done")

    device_tx_frame_cnt=0
    device_rx_frame_cnt=0
    def spy_frame_tx(data):
        global device_tx_frame_cnt
        #print(time.time(),end=" ")
        print("DEVICE FRAME TX:",mcom.Utils.hexstr(data),end=", ")
        print(mcom.MCom.Frame.from_bytes(data),flush=True)
        device_tx_frame_cnt+=1
        assert(device_tx_frame_cnt<max_frame)

    def spy_frame_rx(data):
        global device_rx_frame_cnt
        #print(time.time(),end=" ")
        print("DEVICE FRAME RX:",mcom.Utils.hexstr(data),end=", ")
        print(mcom.MCom.Frame.from_bytes(data),flush=True)
        device_rx_frame_cnt+=1
        assert(device_rx_frame_cnt<max_frame)

    device.spy_frame_tx = spy_frame_tx
    device.spy_frame_rx = spy_frame_rx

    #dat,chan = device.rx(length=1)
    #dat += device.rx(channel=chan,length=1024,block=False)
    chan=1
    dat = device.rx(channel=chan,length=len(first_test_message))
    print("device rx:",dat)
    #time.sleep(1)
    device.tx(channel=chan,data=dat)

    time.sleep(.5)
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

    host_tx_frame_cnt=0
    host_rx_frame_cnt=0

    def spy_frame_tx(data):
        global host_tx_frame_cnt
        global host_tx_cnt
        #print(time.time(),end=" ")
        print("HOST FRAME TX:",mcom.Utils.hexstr(data),end=", ")
        print(mcom.MCom.Frame.from_bytes(data),flush=True)
        host_tx_cnt += len(data)
        host_tx_frame_cnt+=1
        assert(host_tx_frame_cnt<max_frame)

    def spy_frame_rx(data):
        global host_rx_frame_cnt
        global host_rx_cnt
        #print(time.time(),end=" ")
        print("HOST FRAME RX:",mcom.Utils.hexstr(data),end=", ")
        print(mcom.MCom.Frame.from_bytes(data),flush=True)
        host_rx_cnt += len(data)
        host_rx_frame_cnt+=1
        assert(host_rx_frame_cnt<max_frame)

    host.spy_frame_tx = spy_frame_tx
    host.spy_frame_rx = spy_frame_rx

    chan=1
    host.open_channel(name="test1",num=1,
        rx_buf_size=host_rx_buf_size,
        tx_buf_size=host_tx_buf_size,
        description="channel test1 description (host side)"  )
    host.start_com()
    dat=first_test_message
    print("sending: ",dat)
    host.tx(channel=chan,data=dat)
    print("receiving:")
    response = host.rx(channel=chan,length=len(dat))
    print(response, flush=True)
    print(response.decode('utf-8'), flush=True)

    time.sleep(.5)
    host.close_connection()
    print("host_tx_cnt = %d"%host_tx_cnt)
    print("host_rx_cnt = %d"%host_rx_cnt)
    print("done", flush=True)
    #input("Press enter to quit ")
