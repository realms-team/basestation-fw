#!/usr/bin/python

#============================ adjust path =====================================

import sys
import os
if __name__ == "__main__":
    here = sys.path[0]
    sys.path.insert(0, os.path.join(here, 'smartmeshsdk-master'))
    sys.path.insert(0, os.path.join(here, '..', 'Sol'))

#============================ imports =========================================

import time
import threading
import json
from   optparse                             import OptionParser

import OpenCli
import basestation_version

# DustThread
from   SmartMeshSDK                         import FormatUtils, \
                                                   HrParser,    \
                                                   sdk_version
from   SmartMeshSDK.IpMgrConnectorSerial    import IpMgrConnectorSerial
from   SmartMeshSDK.IpMgrConnectorMux       import IpMgrSubscribe

# JsonThread
import bottle
import Sol
import SolVersion
import SolDefines

#============================ defines =========================================

DEFAULT_SERIALPORT = 'COM14'
DEFAULT_TCPPORT    = 8080

#============================ helpers =========================================

def printException(err):
    output  = []
    output += ["ERROR:"]
    output += [str(err)]
    output  = '\n'.join(output)
    print output

#============================ classes =========================================

class DustThread(threading.Thread):
    
    def __init__(self, serialport):
        
        # store params
        self.serialport      = serialport
        
        # local variables
        self.reconnectEvent  = threading.Event()
        self.hrParser        = HrParser.HrParser()
        self.sol             = Sol.Sol()
        self.dataLock        = threading.RLock()
        self.goOn            = True
        
        # start the thread
        threading.Thread.__init__(self)
        self.name            = 'DustThread'
        self.start()
    
    def run(self):
        
        # wait for banner to print
        time.sleep(0.5)
        
        while self.goOn:
            
            try:
                print 'Connecting to {0}...'.format(self.serialport),
                
                # connect to the manager
                self.connector          = IpMgrConnectorSerial.IpMgrConnectorSerial()
                self.connector.connect({
                    'port': self.serialport,
                })
                
                # get MAC address of manager
                temp = self.dn_getSystemInfo()
                self.managerMac = temp.macAddress
                
                # sync network-UTC time
                temp  = self.dn_getTime()
                netTs = self._calcNetTs(temp)
                self._syncNetTsToUtc(netTs)
                
                # subscribe to notifications
                self.subscriber = IpMgrSubscribe.IpMgrSubscribe(self.connector)
                self.subscriber.start()
                self.subscriber.subscribe(
                    notifTypes =    [
                                        IpMgrSubscribe.IpMgrSubscribe.NOTIFDATA,
                                    ],
                    fun =           self._notifData,
                    isRlbl =        False,
                )
                self.subscriber.subscribe(
                    notifTypes =    [
                                        IpMgrSubscribe.IpMgrSubscribe.NOTIFEVENT,
                                    ],
                    fun =           self._notifEvent,
                    isRlbl =        True,
                )
                self.subscriber.subscribe(
                    notifTypes =    [
                                        IpMgrSubscribe.IpMgrSubscribe.NOTIFHEALTHREPORT,
                                    ],
                    fun =           self._notifHealthReport,
                    isRlbl =        True,
                )
                self.subscriber.subscribe(
                    notifTypes =    [
                                        IpMgrSubscribe.IpMgrSubscribe.NOTIFIPDATA,
                                    ],
                    fun =           self._notifIPData,
                    isRlbl =        False,
                )
                self.subscriber.subscribe(
                    notifTypes =    [
                                        IpMgrSubscribe.IpMgrSubscribe.NOTIFLOG,
                                    ],
                    fun =           self._notifLog,
                    isRlbl =        True,
                )
                self.subscriber.subscribe(
                    notifTypes =    [
                                        IpMgrSubscribe.IpMgrSubscribe.ERROR,
                                        IpMgrSubscribe.IpMgrSubscribe.FINISH,
                                    ],
                    fun =           self._notifErrorFinish,
                    isRlbl =        True,
                )
                
                print "TODO: periodically sync NetTs to UTC (#4)" 
                
            except Exception as err:
                print 'FAIL.'
                
                try:
                   self.connector.disconnect()
                except:
                   pass
                
                # wait to reconnect
                time.sleep(1)
                
            else:
                print 'PASS.'
                self.reconnectEvent.clear()
                self.reconnectEvent.wait()
                
                try:
                   self.connector.disconnect()
                except:
                   pass
    
    #======================== public ==========================================
    
    def close(self):
        
        try:
            self.connector.disconnect()
        except:
            pass
        
        self.goOn = False
    
    #======================== private =========================================
    
    #=== Dust API notifications
    
    def _notifData(self, notifName, notifParams):
        
        try:
            assert notifName==IpMgrSubscribe.IpMgrSubscribe.NOTIFDATA
            
            # extract the important data
            netTs      = self._calcNetTs(notifParams)
            macAddress = notifParams.macAddress
            srcPort    = notifParams.srcPort
            dstPort    = notifParams.dstPort
            data       = notifParams.data
            
            isSol = False
            
            if dstPort==SolDefines.SOL_PORT:
                # try to decode as objects
                try:
                    raise NotImplementedError()
                except:
                    pass
                else:
                    isSol = True
            
            if not isSol:
                
                # create sensor object (NOTIF_DATA_RAW)
                sobject = {
                    'mac':       macAddress,
                    'timestamp': self._netTsToEpoch(netTs),
                    'type':      SolDefines.SOL_TYPE_NOTIF_DATA_RAW,
                    'value':     self.sol.create_value_NOTIF_DATA_RAW(
                        srcPort = srcPort,
                        dstPort = dstPort,
                        payload = data,
                    ),
                }
            
            # publish sensor object
            self._publishObject(sobject)
            
        except Exception as err:
            printException(err)
    
    def _notifEvent(self,notifName,notifParams):
        
        try:
            assert notifName==IpMgrSubscribe.IpMgrSubscribe.NOTIFEVENT
            
            # extract the important data
            eventId    = notifParams.eventId
            eventType  = notifParams.eventType
            data       = notifParams.eventData
            
            # create sensor object (SOL_TYPE_NOTIF_EVENT)
            sobject = {
                'mac':       self.managerMac,
                'timestamp': time.gmtime(),
                'type':      SolDefines.SOL_TYPE_NOTIF_EVENT,
                'value':     self.sol.create_value_NOTIF_EVENT(
                    eventID       = eventId,
                    eventType     = eventType,
                    payload       = data,
                ),
            }
            
            # publish sensor object
            self._publishObject(sobject)
            
        except Exception as err:
            printException(err)
    
    def _notifHealthReport(self,notifName,notifParams):
        
        try:
            assert notifName==IpMgrSubscribe.IpMgrSubscribe.NOTIFHEALTHREPORT 
            
            # extract the important data
            netTs      = self._calcNetTs(notifParams)
            macAddress = notifParams.macAddress
            data       = notifParams.data
            
            # create sensor object (SOL_TYPE_NOTIF_HEALTHREPORT)
            sobject = {
                'mac':       macAddress,
                'timestamp': self._netTsToEpoch(netTs),
                'type':      SolDefines.SOL_TYPE_NOTIF_HEALTHREPORT,
                'value':     data,
            }
            
            # publish sensor object
            self._publishObject(sobject)
            
        except Exception as err:
            printException(err)
    
    def _notifIPData(self, notifName, notifParams):
        
        try:
            assert notifName==IpMgrSubscribe.IpMgrSubscribe.NOTIFIPDATA
            
            # create sensor object (SOL_TYPE_NOTIF_IPDATA)
            sobject = {
                'mac':       macAddress,
                'timestamp': time.gmtime(),
                'type':      SolDefines.SOL_TYPE_NOTIF_IPDATA,
                'value':     data,
            }
            
            # publish sensor object
            self._publishObject(sobject)
            
        except Exception as err:
            printException(err)
    
    def _notifErrorFinish(self,notifName,notifParams):
        
        try:
            assert notifName in [
                IpMgrSubscribe.IpMgrSubscribe.ERROR,
                IpMgrSubscribe.IpMgrSubscribe.FINISH,
            ]
            
            if not self.reconnectEvent.isSet():
                self.reconnectEvent.set()
        except Exception as err:
            printException(err)
    
    #=== misc
    
    def _calcNetTs(self,notif):
        return float(notif.utcSecs)+float(notif.utcUsecs/1000000.0)
    
    def _syncNetTsToUtc(self,netTs):
        with self.dataLock:
            self.tsDiff = time.gmtime()-netTs
    
    def _netTsToEpoch(self,netTs):
        with self.dataLock:
            return netTs+self.tsDiff
    
    def _publishObject(self,object):
        print "========== _publishObject"
        print "TODO store to file" 
        print "TODO send to server" 

class JsonThread(threading.Thread):
    
    def __init__(self,tcpport):
        
        # store params
        self.tcpport    = tcpport
        
        # initialize web server
        self.web        = bottle.Bottle()
        self.web.route(path='/api/v1/echo.json',     method='POST', callback=self._cb_echo_POST)
        self.web.route(path='/api/v1/status.json',   method='GET',  callback=self._cb_status_GET)
        self.web.route(path='/api/v1/config.json',   method='POST', callback=self._cb_config_POST)
        self.web.route(path='/api/v1/config.json',   method='GET',  callback=self._cb_config_GET)
        self.web.route(path='/api/v1/flows.json',    method='GET',  callback=self._cb_flows_GET)
        self.web.route(path='/api/v1/flows.json',    method='POST', callback=self._cb_flows_POST)
        self.web.route(path='/api/v1/resend.json',   method='POST', callback=self._cb_resend_POST)
        self.web.route(path='/api/v1/snapshot.json', method='POST', callback=self._cb_snapshot_POST)
        
        # start the thread
        threading.Thread.__init__(self)
        self.name       = 'JsonThread'
        self.start()
    
    def run(self):
        
        # wait for banner to print
        time.sleep(0.5)
        
        self.web.run(
            host   = 'localhost',
            port   = self.tcpport,
            quiet  = True,
            debug  = False,
        )
    
    #======================== public ==========================================
    
    def close(self):
        print 'TODO JsonThread.close() (#5)'
    
    #======================== private ==========================================
    
    def _cb_echo_POST(self):
        bottle.response.content_type = bottle.request.content_type
        return bottle.request.body.read()
    
    def _cb_status_GET(self):
        # TODO: implement (#7)
        bottle.response.status = 501
        bottle.response.content_type = 'application/json'
        return json.dumps({'error': 'Not Implemented yet :-('})
    
    def _cb_config_POST(self):
        # TODO: implement (#8)
        bottle.response.status = 501
        bottle.response.content_type = 'application/json'
        return json.dumps({'error': 'Not Implemented yet :-('})
    
    def _cb_config_GET(self):
        # TODO: implement (#9)
        bottle.response.status = 501
        bottle.response.content_type = 'application/json'
        return json.dumps({'error': 'Not Implemented yet :-('})
    
    def _cb_flows_GET(self):
        # TODO: implement (#10)
        bottle.response.status = 501
        bottle.response.content_type = 'application/json'
        return json.dumps({'error': 'Not Implemented yet :-('})
    
    def _cb_flows_POST(self):
        # TODO: implement (#11)
        bottle.response.status = 501
        bottle.response.content_type = 'application/json'
        return json.dumps({'error': 'Not Implemented yet :-('})
    
    def _cb_resend_POST(self):
        # TODO: implement (#12)
        bottle.response.status = 501
        bottle.response.content_type = 'application/json'
        return json.dumps({'error': 'Not Implemented yet :-('})
    
    def _cb_snapshot_POST(self):
        # TODO: implement (#13)
        bottle.response.status = 501
        bottle.response.content_type = 'application/json'
        return json.dumps({'error': 'Not Implemented yet :-('})

class Basestation(object):
    
    def __init__(self,serialport,tcpport):
        self.dustThread = DustThread(serialport)
        # TODO: add DataThread which periodically stores data to file (#14)
        # TODO: add SendThread which periodically sends data to server (#15)
        self.jsonThread = JsonThread(tcpport)
    
    def close(self):
        self.dustApiThread.close()
        self.jsonThread.close()

#============================ main ============================================

basestation = None

def quitCallback():
    global basestation
    
    basestation.close()

def main(serialport,tcpport):
    global basestation
    
    # create the basestation instance
    basestation = Basestation(
        serialport,
        tcpport,
    )
    
    # start the CLI interface
    OpenCli.OpenCli(
        "Basestation",
        basestation_version.VERSION,
        quitCallback,
        [
            ("SmartMesh SDK",sdk_version.VERSION),
            (
                "Sol",
                (
                    SolVersion.SOL_VERSION['SOL_VERSION_MAJOR'],
                    SolVersion.SOL_VERSION['SOL_VERSION_MINOR'],
                    SolVersion.SOL_VERSION['SOL_VERSION_PATCH'],
                    SolVersion.SOL_VERSION['SOL_VERSION_BUILD'],
                ),
            ),
        ],
    )

if __name__ == '__main__':
    
    # parse the command line
    parser = OptionParser("usage: %prog [options]")
    parser.add_option(
        "-s", "--serialport", dest="serialport", 
        default=DEFAULT_SERIALPORT,
        help="Serial port of the SmartMesh IP manager."
    )
    parser.add_option(
        "-t", "--tcpport", dest="tcpport", 
        default=DEFAULT_TCPPORT,
        help="TCP port to start the JSON API on."
    )
    (options, args) = parser.parse_args()
    
    main(
        options.serialport,
        options.tcpport,
    )
