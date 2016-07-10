#!/usr/bin/python

#============================ adjust path =====================================

import sys
import os

if __name__ == "__main__":
    here = sys.path[0]
    sys.path.insert(0, os.path.join(here, '..', 'sol'))
    sys.path.insert(0, os.path.join(here, '..', 'sol','smartmeshsdk','libs'))

#============================ imports =========================================

import time
import threading
import json
import subprocess
import pickle
import random
import traceback
from   optparse                             import OptionParser
from   ConfigParser                         import SafeConfigParser
import logging
import logging.config

import OpenCli
import solmanager_version

# DustThread
from   SmartMeshSDK                         import sdk_version, \
                                                   ApiException
from   SmartMeshSDK.IpMgrConnectorSerial    import IpMgrConnectorSerial
from   SmartMeshSDK.IpMgrConnectorMux       import IpMgrSubscribe

# SendThread
import requests
import OpenSSL

# JsonThread
import bottle
from solobjectlib import Sol, SolVersion, SolDefines

#============================ logging =========================================

logging.config.fileConfig('logging.conf')
log = logging.getLogger('solmanager')
log.setLevel(logging.DEBUG)

#============================ defines =========================================

#===== defines

FLOW_DEFAULT                            = 'default'
FLOW_ON                                 = 'on'
FLOW_OFF                                = 'off'

CONFIGFILE                              = 'solmanager.config'
DEFAULT_CONFIGFILE                      = 'solmanager.config.default'

#===== stats
#== admin
STAT_ADM_NUM_CRASHES                    = 'ADM_NUM_CRASHES'
#== connection to manager
STAT_MGR_NUM_CONNECT_ATTEMPTS           = 'MGR_NUM_CONNECT_ATTEMPTS'
STAT_MGR_NUM_CONNECT_OK                 = 'MGR_NUM_CONNECT_OK'
STAT_MGR_NUM_DISCONNECTS                = 'MGR_NUM_DISCONNECTS'
STAT_MGR_NUM_TIMESYNC                   = 'MGR_NUM_TIMESYNC'
#== notifications from manager
# note: we count the number of notifications form the manager, for each time, e.g. NUMRX_NOTIFDATA
# all stats start with "NUMRX_"
#== publication
STAT_PUB_TOTAL_SENTTOPUBLISH            = 'PUB_TOTAL_SENTTOPUBLISH'
# to file
STAT_PUBFILE_BACKLOG                    = 'PUBFILE_BACKLOG'
STAT_PUBFILE_WRITES                     = 'PUBFILE_WRITES'
# to server
STAT_PUBSERVER_BACKLOG                  = 'PUBSERVER_BACKLOG'
STAT_PUBSERVER_SENDATTEMPTS             = 'PUBSERVER_SENDATTEMPTS'
STAT_PUBSERVER_UNREACHABLE              = 'PUBSERVER_UNREACHABLE'
STAT_PUBSERVER_SENDOK                   = 'PUBSERVER_SENDOK'
STAT_PUBSERVER_SENDFAIL                 = 'PUBSERVER_SENDFAIL'
STAT_PUBSERVER_STATS                    = 'PUBSERVER_STATS'
STAT_PUBSERVER_PULLATTEMPTS             = 'PUBSERVER_PULLATTEMPTS'
STAT_PUBSERVER_PULLOK                   = 'PUBSERVER_PULLOK'
STAT_PUBSERVER_PULLFAIL                 = 'PUBSERVER_PULLFAIL'
#== snapshot
STAT_SNAPSHOT_NUM_STARTED               = 'SNAPSHOT_NUM_STARTED'
STAT_SNAPSHOT_LASTSTARTED               = 'SNAPSHOT_LASTSTARTED'
STAT_SNAPSHOT_NUM_OK                    = 'SNAPSHOT_NUM_OK'
STAT_SNAPSHOT_NUM_FAIL                  = 'SNAPSHOT_NUM_FAIL'
#== JSON interface
STAT_JSON_NUM_REQ                       = 'JSON_NUM_REQ'
STAT_JSON_NUM_UNAUTHORIZED              = 'JSON_NUM_UNAUTHORIZED'

#============================ helpers =========================================

def currentUtcTime():
    return time.strftime("%a, %d %b %Y %H:%M:%S UTC", time.gmtime())

def logCrash(threadName,err):
    output  = []
    output += ["============================================================="]
    output += [currentUtcTime()]
    output += [""]
    output += ["CRASH in Thread {0}!".format(threadName)]
    output += [""]
    output += ["=== exception type ==="]
    output += [str(type(err))]
    output += [""]
    output += ["=== traceback ==="]
    output += [traceback.format_exc()]
    output  = '\n'.join(output)

    # update stats
    AppData().incrStats(STAT_ADM_NUM_CRASHES)
    print output
    log.critical(output)

#============================ classes =========================================

class AppData(object):
    _instance = None
    _init     = False
    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(AppData,cls).__new__(cls, *args, **kwargs)
        return cls._instance
    def __init__(self, statsfile=None):
        if self._init:
            return
        self._init      = True
        self.dataLock   = threading.RLock()
        self.statsfile  = statsfile
        try:
            with open(statsfile,'r') as f:
                self.data = pickle.load(f)
                log.info("Stats recovered from file.")
        except (EnvironmentError, pickle.PickleError):
            self.data = {
                'stats' : {},
                'flows' : {
                    FLOW_DEFAULT:           FLOW_ON,
                },
            }
            self._backupData()
    def incrStats(self,statName):
        with self.dataLock:
            if statName not in self.data['stats']:
                self.data['stats'][statName] = 0
            self.data['stats'][statName] += 1
        self._backupData()
    def updateStats(self,k,v):
        with self.dataLock:
            self.data['stats'][k] = v
        self._backupData()
    def getStats(self):
        with self.dataLock:
            stats = self.data['stats'].copy()
        stats[STAT_PUBFILE_BACKLOG]   = FileThread().getBacklogLength()
        stats[STAT_PUBSERVER_BACKLOG] = SendThread().getBacklogLength()
        return stats
    def getFlows(self):
        with self.dataLock:
            return self.data['flows'].copy()
    def setFlow(self,key,value):
        with self.dataLock:
            self.data['flows'][key] = value
        self._backupData()
    def _backupData(self):
        with self.dataLock:
            with open(self.statsfile,'w') as f:
                pickle.dump(self.data,f)

class DustThread(threading.Thread):

    def __init__(self,serialport,simulation=False):

        # store params
        self.serialport      = serialport
        self.simulation      = simulation

        # local variables
        self.reconnectEvent  = threading.Event()
        self.sol             = Sol.Sol()
        self.dataLock        = threading.RLock()
        self.connector       = None
        self.goOn            = True

        # start the thread
        threading.Thread.__init__(self)
        self.name            = 'DustThread'
        self.start()

    def run(self):
        try:
            # wait for banner
            time.sleep(0.5)

            if self.simulation:
                self.runSimulation()
            else:
                self.runHardware()
        except Exception as err:
            logCrash(self.name,err)

    def runSimulation(self):

        FAKEMAC_MGR     = [0x0a]*8
        FAKEMAC_MOTE_1  = [1]*8
        FAKEMAC_MOTE_2  = [2]*8

        SIMACTIONS = [
            (
                self._notifData,
                IpMgrSubscribe.IpMgrSubscribe.NOTIFDATA,
                IpMgrConnectorSerial.IpMgrConnectorSerial.Tuple_notifData(
                    utcSecs       = 0,
                    utcUsecs      = 0,
                    macAddress    = FAKEMAC_MGR,
                    srcPort       = 1234,
                    dstPort       = 1234,
                    data          = range(10),
                ),
            ),
            (
                self._notifEvent,
                IpMgrSubscribe.IpMgrSubscribe.EVENTCOMMANDFINISHED,
                IpMgrConnectorSerial.IpMgrConnectorSerial.Tuple_eventCommandFinished(
                    eventId       = 0x11,
                    callbackId    = 0x22,
                    rc            = 0x33,
                ),
            ),
            (
                self._notifEvent,
                IpMgrSubscribe.IpMgrSubscribe.EVENTPATHCREATE,
                IpMgrConnectorSerial.IpMgrConnectorSerial.Tuple_eventPathCreate(
                    eventId       = 0x11,
                    source        = FAKEMAC_MOTE_1,
                    dest          = FAKEMAC_MOTE_2,
                    direction     = 0x33,
                ),
            ),
            (
                self._notifEvent,
                IpMgrSubscribe.IpMgrSubscribe.EVENTPATHDELETE,
                IpMgrConnectorSerial.IpMgrConnectorSerial.Tuple_eventPathDelete(
                    eventId       = 0x11,
                    source        = FAKEMAC_MOTE_1,
                    dest          = FAKEMAC_MOTE_2,
                    direction     = 0x33,
                ),
            ),
            (
                self._notifEvent,
                IpMgrSubscribe.IpMgrSubscribe.EVENTPINGRESPONSE,
                IpMgrConnectorSerial.IpMgrConnectorSerial.Tuple_eventPingResponse(
                    eventId       = 0x11,
                    callbackId    = 0x22,
                    macAddress    = FAKEMAC_MOTE_1,
                    delay         = 0x33,
                    voltage       = 0x44,
                    temperature   = 0x55,
                ),
            ),
            (
                self._notifEvent,
                IpMgrSubscribe.IpMgrSubscribe.EVENTNETWORKTIME,
                IpMgrConnectorSerial.IpMgrConnectorSerial.Tuple_eventNetworkTime(
                    eventId       = 0x11,
                    uptime        = 0x22,
                    utcSecs       = 0,
                    utcUsecs      = 0,
                    asn           = (1,1,1,1,1),
                    asnOffset     = 0x33,
                ),
            ),
            (
                self._notifEvent,
                IpMgrSubscribe.IpMgrSubscribe.EVENTNETWORKRESET,
                IpMgrConnectorSerial.IpMgrConnectorSerial.Tuple_eventNetworkReset(
                    eventId       = 0x11,
                ),
            ),
            (
                self._notifEvent,
                IpMgrSubscribe.IpMgrSubscribe.EVENTMOTEJOIN,
                IpMgrConnectorSerial.IpMgrConnectorSerial.Tuple_eventMoteJoin(
                    eventId       = 0x11,
                    macAddress    = FAKEMAC_MOTE_1,
                ),
            ),
            (
                self._notifEvent,
                IpMgrSubscribe.IpMgrSubscribe.EVENTMOTECREATE,
                IpMgrConnectorSerial.IpMgrConnectorSerial.Tuple_eventMoteCreate(
                    eventId       = 0x11,
                    macAddress    = FAKEMAC_MOTE_1,
                    moteId        = 0x22,
                ),
            ),
            (
                self._notifEvent,
                IpMgrSubscribe.IpMgrSubscribe.EVENTMOTEDELETE,
                IpMgrConnectorSerial.IpMgrConnectorSerial.Tuple_eventMoteDelete(
                    eventId       = 0x11,
                    macAddress    = FAKEMAC_MOTE_1,
                    moteId        = 0x22,
                ),
            ),
            (
                self._notifEvent,
                IpMgrSubscribe.IpMgrSubscribe.EVENTMOTELOST,
                IpMgrConnectorSerial.IpMgrConnectorSerial.Tuple_eventMoteLost(
                    eventId       = 0x11,
                    macAddress    = FAKEMAC_MOTE_1,
                ),
            ),
            (
                self._notifEvent,
                IpMgrSubscribe.IpMgrSubscribe.EVENTMOTEOPERATIONAL,
                IpMgrConnectorSerial.IpMgrConnectorSerial.Tuple_eventMoteOperational(
                    eventId       = 0x11,
                    macAddress    = FAKEMAC_MOTE_1,
                ),
            ),
            (
                self._notifEvent,
                IpMgrSubscribe.IpMgrSubscribe.EVENTMOTERESET,
                IpMgrConnectorSerial.IpMgrConnectorSerial.Tuple_eventMoteReset(
                    eventId       = 0x11,
                    macAddress    = FAKEMAC_MOTE_1,
                ),
            ),
            (
                self._notifEvent,
                IpMgrSubscribe.IpMgrSubscribe.EVENTPACKETSENT,
                IpMgrConnectorSerial.IpMgrConnectorSerial.Tuple_eventPacketSent(
                    eventId       = 0x11,
                    callbackId    = 0x22,
                    rc            = 0x33,
                ),
            ),
            (
                self._notifHealthReport,
                IpMgrSubscribe.IpMgrSubscribe.NOTIFHEALTHREPORT,
                IpMgrConnectorSerial.IpMgrConnectorSerial.Tuple_notifHealthReport(
                    macAddress    = FAKEMAC_MOTE_1,
                    payload       = [1]*10,
                ),
            ),
            (
                self._notifIPData,
                IpMgrSubscribe.IpMgrSubscribe.NOTIFIPDATA,
                IpMgrConnectorSerial.IpMgrConnectorSerial.Tuple_notifIpData(
                    utcSecs       = 0,
                    utcUsecs      = 0,
                    macAddress    = FAKEMAC_MOTE_1,
                    data          = [1]*10,
                ),
            ),
            (
                self._notifLog,
                IpMgrSubscribe.IpMgrSubscribe.NOTIFLOG,
                IpMgrConnectorSerial.IpMgrConnectorSerial.Tuple_notifLog(
                    macAddress    = FAKEMAC_MOTE_1,
                    logMsg        = [1]*10,
                ),
            ),
        ]

        # get (fake) MAC address of manager
        self.macManager = FAKEMAC_MGR

        # sync (fake) network-UTC time
        self._syncNetTsToUtc(time.time())

        lastActionIndex = 0

        while self.goOn:

            # issues the next action
            (func,notifName,notifParams) = SIMACTIONS[lastActionIndex]
            lastActionIndex = (lastActionIndex+1)%len(SIMACTIONS)
            try:
                notifParams = notifParams._replace(
                                utcSecs=int(time.time())-60+random.randint(0,60))
            except ValueError:
                pass
            func(notifName,notifParams)

            # sleep some time
            time.sleep(0.5)

    def runHardware(self):

        while self.goOn:

            try:
                # update stats
                AppData().incrStats(STAT_MGR_NUM_CONNECT_ATTEMPTS)

                print 'Connecting to {0}...'.format(self.serialport),

                # connect to the manager
                self.connector = IpMgrConnectorSerial.IpMgrConnectorSerial()
                self.connector.connect({
                    'port': self.serialport,
                })

                # update stats
                AppData().incrStats(STAT_MGR_NUM_CONNECT_OK)

                # get MAC address of manager
                temp = self.connector.dn_getSystemInfo()
                self.macManager = temp.macAddress

                # sync network-UTC time
                temp  = self.connector.dn_getTime()
                netTs = self._calcNetTs(temp)
                self._syncNetTsToUtc(netTs)

                # subscribe to notifications
                self.subscriber = IpMgrSubscribe.IpMgrSubscribe(self.connector)
                self.subscriber.start()
                self.subscriber.subscribe(
                    notifTypes =    [
                                        IpMgrSubscribe.IpMgrSubscribe.NOTIFDATA,
                                    ],
                    fun =           self._notifAll,
                    isRlbl =        False,
                )
                self.subscriber.subscribe(
                    notifTypes =    [
                                        IpMgrSubscribe.IpMgrSubscribe.NOTIFEVENT,
                                        IpMgrSubscribe.IpMgrSubscribe.NOTIFHEALTHREPORT,
                                        IpMgrSubscribe.IpMgrSubscribe.NOTIFIPDATA,
                                        IpMgrSubscribe.IpMgrSubscribe.NOTIFLOG,
                                    ],
                    fun =           self._notifAll,
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

            except Exception as err:

                print err
                print 'FAIL.'

                # update stats
                AppData().incrStats(STAT_MGR_NUM_DISCONNECTS)

                try:
                    self.connector.disconnect()
                except Exception:
                    pass

                # wait to reconnect
                time.sleep(1)

            else:
                print 'PASS.'
                self.reconnectEvent.clear()
                self.reconnectEvent.wait()

                # update stats
                AppData().incrStats(STAT_MGR_NUM_DISCONNECTS)

                try:
                    self.connector.disconnect()
                except Exception:
                    pass

    #======================== public ==========================================

    def close(self):

        try:
            self.connector.disconnect()
        except Exception:
            pass

        self.goOn = False

    #======================== private =========================================

    #=== Dust API notifications

    def _notifAll(self, notif_name, dust_notif):

        try:
            # update stats
            AppData().incrStats('NUMRX_{0}'.format(notif_name.upper()))

            # get time
            epoch       = None
            if hasattr(dust_notif,"utcSecs") and hasattr(dust_notif,"utcUsecs"):
                netTs   = self._calcNetTs(dust_notif)
                epoch   = self._netTsToEpoch(netTs)

            # convert dust notification to JSON SOL Object
            sol_jsonl = self.sol.dust_to_json(
                notif_name,
                dust_notif,
                macManager  = self.macManager,
                timestamp   = epoch,
            )

            for sol_json in sol_jsonl:
                # publish JSON SOL Object
                self._publishSolJson(sol_json)

        except Exception as err:
            logCrash(self.name,err)

    def _notifErrorFinish(self,notifName,dust_notif):

        try:
            assert notifName in [
                IpMgrSubscribe.IpMgrSubscribe.ERROR,
                IpMgrSubscribe.IpMgrSubscribe.FINISH,
            ]

            if not self.reconnectEvent.isSet():
                self.reconnectEvent.set()
        except Exception as err:
            logCrash(self.name,err)

    #=== misc

    def _calcNetTs(self,notif):
        return int(float(notif.utcSecs)+float(notif.utcUsecs/1000000.0))

    def _syncNetTsToUtc(self,netTs):
        # update stats
        AppData().incrStats(STAT_MGR_NUM_TIMESYNC)
        with self.dataLock:
            self.tsDiff = time.time()-netTs

    def _netTsToEpoch(self,netTs):
        with self.dataLock:
            return int(netTs+self.tsDiff)

    def _isActiveFlow(self,flow_type):
        flows = AppData().getFlows()
        flowState = flows.get(flow_type,flows['default'])
        return flowState==FLOW_ON

    def _publishSolJson(self,sol_json):

        # update stats
        AppData().incrStats(STAT_PUB_TOTAL_SENTTOPUBLISH)

        # publish
        FileThread().publish(sol_json)
        if self._isActiveFlow(sol_json['type']):
            SendThread().publish(sol_json)

class SnapshotThread(threading.Thread):
    _instance = None
    _init     = False
    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(SnapshotThread,cls).__new__(cls, *args, **kwargs)
        return cls._instance
    def __init__(self,dustThread=None):
        if self._init:
            return
        self._init      = True

        assert dustThread

        # store params
        self.dustThread      = dustThread

        # local variables
        self.doSnapshotSem   = threading.Semaphore(0)
        self.sol             = Sol.Sol()
        self.goOn            = True

        # start thread
        threading.Thread.__init__(self)
        self.name            = 'SnapshotThread'
        self.start()

    def _del(self):
        self.__class__._instance = None

    def run(self):
        while self.goOn:
            self.doSnapshotSem.acquire()
            if not self.goOn:
                break
            self._doSnapshot()

    #======================== public ==========================================

    def doSnapshot(self):
        self.doSnapshotSem.release()

    def close(self):
        self.goOn = False
        self.doSnapshotSem.release()

    #======================== private =========================================

    def _doSnapshot(self):
        try:
            # update stats
            AppData().incrStats(STAT_SNAPSHOT_NUM_STARTED)
            AppData().updateStats(
                STAT_SNAPSHOT_LASTSTARTED,
                currentUtcTime(),
            )

            # retrieve connector from DustThread
            connector = self.dustThread.connector

            snapshotSummary = []

            # get MAC addresses of all motes
            currentMac     = (0,0,0,0,0,0,0,0) # start getMoteConfig() iteration with the 0 MAC addr
            continueAsking = True
            while continueAsking:
                try:
                    res = connector.dn_getMoteConfig(currentMac,True)
                except ApiException.APIError:
                    continueAsking = False
                else:
                    snapshotSummary += [
                        {
                            'macAddress':       res.macAddress,
                            'moteId':           res.moteId,
                            'isAP':             res.isAP,
                            'state':            res.state,
                            'isRouting':        res.isRouting,
                        }
                    ]
                    currentMac = res.macAddress

            # getMoteInfo on all motes
            for s in snapshotSummary:
                res = connector.dn_getMoteInfo(s['macAddress'])
                s.update({
                    'numNbrs':                   res.numNbrs,
                    'numGoodNbrs':               res.numGoodNbrs,
                    'requestedBw':               res.requestedBw,
                    'totalNeededBw':             res.totalNeededBw,
                    'assignedBw':                res.assignedBw,
                    'packetsReceived':           res.packetsReceived,
                    'packetsLost':               res.packetsLost,
                    'avgLatency':                res.avgLatency,
                })

            # get path info on all paths of all motes
            for s in snapshotSummary:
                s['paths'] = []
                currentPathId  = 0
                continueAsking = True
                while continueAsking:
                    try:
                        res = connector.dn_getNextPathInfo(s['macAddress'],0,currentPathId)
                    except ApiException.APIError:
                        continueAsking = False
                    else:
                        currentPathId  = res.pathId
                        s['paths'] += [
                            {
                                'macAddress':    res.dest,
                                'direction':     res.direction,
                                'numLinks':      res.numLinks,
                                'quality':       res.quality,
                                'rssiSrcDest':   res.rssiSrcDest,
                                'rssiDestSrc':   res.rssiDestSrc,
                            }
                        ]

        except Exception as err:
            AppData().incrStats(STAT_SNAPSHOT_NUM_FAIL)
        else:
            AppData().incrStats(STAT_SNAPSHOT_NUM_OK)

            # create sensor object
            sobject = {
                'mac':       self.dustThread.macManager,
                'timestamp': int(time.time()),
                'type':      SolDefines.SOL_TYPE_DUST_SNAPSHOT,
                'value':     snapshotSummary,
            }

            # publish sensor object
            self.dustThread._publishSolJson(sobject)

class PublishThread(threading.Thread):
    def __init__(self, periodvariable):
        self.goOn                       = True
        self.solJsonObjectsToPublish    = []
        self.dataLock                   = threading.RLock()
        self.sol                        = Sol.Sol()
        # start the thread
        threading.Thread.__init__(self)
        self.name                       = 'PublishThread'
        self.start()
        self.periodvariable             = periodvariable*60
    def run(self):
        try:
            self.currentDelay = 5
            while self.goOn:
                self.currentDelay -= 1
                if self.currentDelay==0:
                    self.publishNow()
                    self.currentDelay = self.periodvariable
                time.sleep(1)
        except Exception as err:
            logCrash(self.name,err)
    def getBacklogLength(self):
        with self.dataLock:
            return len(self.solJsonObjectsToPublish)
    def close(self):
        self.goOn = False
    def publish(self,sol_json):
        with self.dataLock:
            self.solJsonObjectsToPublish += [sol_json]

class FileThread(PublishThread):
    _instance = None
    _init     = False
    # we buffer objects for BUFFER_PERIOD second to ensure they are written to
    # file chronologically
    BUFFER_PERIOD = 60
    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(FileThread,cls).__new__(cls, *args, **kwargs)
        return cls._instance
    def __init__(self, backupfile=None, fileperiodminutes=None):
        if self._init:
            return
        self._init          = True
        PublishThread.__init__(self, fileperiodminutes)
        self.name           = 'FileThread'
        self.backupfile     = backupfile

    def _del(self):
        self.__class__._instance = None

    def publishNow(self):
        # update stats
        AppData().incrStats(STAT_PUBFILE_WRITES)

        with self.dataLock:
            # order solJsonObjectsToPublish chronologically
            self.solJsonObjectsToPublish.sort(key=lambda i: i['timestamp'])

            # extract the JSON SOL objects heard more than BUFFER_PERIOD ago
            now = time.time()
            solJsonObjectsToWrite = []
            while True:
                if not self.solJsonObjectsToPublish:
                    break
                if now-self.solJsonObjectsToPublish[0]['timestamp']<self.BUFFER_PERIOD:
                    break
                solJsonObjectsToWrite += [self.solJsonObjectsToPublish.pop(0)]

        # write those to file
        if solJsonObjectsToWrite:
            self.sol.dumpToFile(
                solJsonObjectsToWrite,
                self.backupfile,
            )

class SendThread(PublishThread):
    _instance = None
    _init     = False
    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(SendThread,cls).__new__(cls, *args, **kwargs)
        return cls._instance
    def __init__(self, **kwargs):
        if self._init:
            return
        self._init              = True
        PublishThread.__init__(self, kwargs["sendperiodminutes"])
        self.name               = 'SendThread'
        self.solserver_host     = kwargs["solserver_host"]
        self.solserver_token    = kwargs["solserver_token"]
        self.solserver_cert     = kwargs["solserver_cert"]
    def _del(self):
        self.__class__._instance = None
    def publishNow(self):
        # stop if nothing to publish
        with self.dataLock:
            if not self.solJsonObjectsToPublish:
                return

        # convert all objects to publish to binary
        with self.dataLock:
            solBinObjectsToPublish = [self.sol.json_to_bin(o) for o in self.solJsonObjectsToPublish]

        # prepare http_payload
        http_payload = self.sol.bin_to_http(solBinObjectsToPublish)

        # send http_payload to server
        try:
            # update stats
            AppData().incrStats(STAT_PUBSERVER_SENDATTEMPTS)
            requests.packages.urllib3.disable_warnings()
            r = requests.put(
                'https://{0}/api/v1/o.json'.format(self.solserver_host),
                headers = {'X-REALMS-Token': self.solserver_token},
                json    = http_payload,
                verify  = self.solserver_cert,
            )
        except (requests.exceptions.RequestException, OpenSSL.SSL.SysCallError) as err:
            # update stats
            AppData().incrStats(STAT_PUBSERVER_UNREACHABLE)
            # happens when could not contact server
            if type(err) == requests.exceptions.SSLError:
                traceback.print_exc()
        else:
            # server answered

            # clear objects
            if r.status_code==200:
                # update stats
                AppData().incrStats(STAT_PUBSERVER_SENDOK)
                with self.dataLock:
                    self.solJsonObjectsToPublish = []
            else:
                # update stats
                AppData().incrStats(STAT_PUBSERVER_SENDFAIL)
                print "Error HTTP response status: "+ str(r.status_code)

class StatsThread(PublishThread):
    """
    This thread periodically publishes the solmanager statistics
    """

    _instance = None
    _init     = False
    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(StatsThread,cls).__new__(cls, *args, **kwargs)
        return cls._instance
    def __init__(self, dustThread, statsperiod):
        if self._init:
            return
        self._init          = True
        PublishThread.__init__(self, statsperiod)
        self.name           = 'StatsThread'
        self.dustThread     = dustThread
        self.sol                = Sol.Sol()
    def _del(self):
        self.__class__._instance = None
    def publishNow(self):
        # update stats
        AppData().incrStats(STAT_PUBSERVER_STATS)

        # create sensor object
        sobject = {
            'mac':       self.dustThread.macManager,
            'timestamp': int(time.time()),
            'type':      SolDefines.SOL_TYPE_SOLMANAGER_STATS,
            'value':     {
                    'sol_version'           : list(SolVersion.VERSION),
                    'solmanager_version'    : list(solmanager_version.VERSION),
                    'sdk_version'           : list(sdk_version.VERSION)
                },
        }

        # publish
        FileThread().publish(sobject)
        SendThread().publish(sobject)

class PullThread(PublishThread):
    """
    This thread periodically asks the server for actions and perform them.
    This is useful when the solmanager is not reachable by the solserver.
    """

    _instance = None
    _init     = False
    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(PullThread,cls).__new__(cls, *args, **kwargs)
        return cls._instance
    def __init__(self, **kwargs):
        if self._init:
            return
        self._init          = True
        PublishThread.__init__(self, kwargs["pullperiodminutes"])
        self.name           = 'PullThread'
        self.solserver_host     = kwargs["solserver_host"]
        self.solserver_token    = kwargs["solserver_token"]
        self.solserver_cert     = kwargs["solserver_cert"]
    def publishNow(self):
        self.pull_server()
    def pull_server(self):
        # send http_payload to server
        try:
            # update stats
            AppData().incrStats(STAT_PUBSERVER_PULLATTEMPTS)
            requests.packages.urllib3.disable_warnings()
            r = requests.get(
                'https://{0}/api/v1/getactions/'.format(self.solserver_host),
                headers = {'X-REALMS-Token': self.solserver_token},
                verify  = self.solserver_cert,
            )
        except (requests.exceptions.RequestException, OpenSSL.SSL.SysCallError) as err:
            # update stats
            AppData().incrStats(STAT_PUBSERVER_UNREACHABLE)
            # happens when could not contact server
            if type(err) == requests.exceptions.SSLError:
                traceback.print_exc()
        else:
            # server answered

            # clear objects
            if r.status_code==200:
                # update stats
                AppData().incrStats(STAT_PUBSERVER_PULLOK)
                for action in r.json():
                    self.run_action(action['action'])
            else:
                # update stats
                AppData().incrStats(STAT_PUBSERVER_PULLFAIL)
                print "Error HTTP response status: "+ str(r.status_code)

    def run_action(self, action):
        if action == "update":
            # get last repo version
            os.system("cd " + here + "/../sol/ && git checkout master && git pull origin master")
            os.system("cd " + here + " && git checkout master && git pull origin master")

            # restart program
            python = sys.executable
            os.execl(python, python, * sys.argv)

class PeriodicSnapshotThread(PublishThread):
    _instance = None
    _init     = False
    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(PeriodicSnapshotThread,cls).__new__(cls, *args, **kwargs)
        return cls._instance
    def __init__(self, snapperiod):
        if self._init:
            return
        self._init          = True
        PublishThread.__init__(self, snapperiod)
        self.name           = 'PeriodicSnapshotThread'
    def _del(self):
        self.__class__._instance = None
    def publishNow(self):
        SnapshotThread().doSnapshot()

class CherryPySSL(bottle.ServerAdapter):
    server = None
    def run(self, handler):
        from cherrypy import wsgiserver
        from cherrypy.wsgiserver.ssl_pyopenssl import pyOpenSSLAdapter
        self.server = wsgiserver.CherryPyWSGIServer((self.host, self.port), handler)
        self.server.ssl_adapter = pyOpenSSLAdapter(
            certificate           = self.options["cert"],
            private_key           = self.options["privkey"],
        )
        try:
            log.debug("Starting Bottle server")
            self.server.start()
        finally:
            self.server.stop()
    def stopserver(self):
        if self.server:
            self.server.stop()
            log.debug("Bottle server adapter stopped")

class JsonThread(threading.Thread):

    def __init__(self, dustThread, tcpport, host, token, cert, privkey, backupfile):

        # store params
        self.tcpport            = tcpport
        self.solmanager_host    = host
        self.solmanager_token   = token
        self.solmanager_cert    = cert
        self.solmanager_privkey = privkey
        self.dustThread         = dustThread
        self.backupfile         = backupfile

        # local variables
        self.sol                = Sol.Sol()

        # initialize web server
        self.web                = bottle.Bottle()
        self.web.server         = CherryPySSL(
                                    host        = self.solmanager_host,
                                    port        = self.tcpport,
                                    cert        = self.solmanager_cert,
                                    privkey     = self.solmanager_privkey,
                                )
        self.web.route(path='/api/v1/echo.json',           method='POST', callback=self._cb_echo_POST)
        self.web.route(path='/api/v1/status.json',         method='GET',  callback=self._cb_status_GET)
        self.web.route(path='/api/v1/config.json',         method='GET',  callback=self._cb_config_GET)
        self.web.route(path='/api/v1/config.json',         method='POST', callback=self._cb_config_POST)
        self.web.route(path='/api/v1/flows.json',          method='GET',  callback=self._cb_flows_GET)
        self.web.route(path='/api/v1/flows.json',          method='POST', callback=self._cb_flows_POST)
        self.web.route(path='/api/v1/resend.json',         method='POST', callback=self._cb_resend_POST)
        self.web.route(path='/api/v1/snapshot.json',       method='POST', callback=self._cb_snapshot_POST)
        self.web.route(path='/api/v1/smartmeshipapi.json', method='POST', callback=self._cb_smartmeshipapi_POST)

        # start the thread
        threading.Thread.__init__(self)
        self.name       = 'JsonThread'
        self.start()

    def run(self):
        try:
            # wait for banner
            time.sleep(0.5)
            self.web.run(
                server  = self.web.server,
                quiet   = True,
                debug   = True,
            )
        except Exception as err:
            logCrash(self.name,err)

    #======================== public ==========================================

    def close(self):
        self.web.close()
        self.web.server.stopserver()

    #======================== private ==========================================

    #=== JSON request handler

    def _cb_echo_POST(self):
        try:
            # update stats
            AppData().incrStats(STAT_JSON_NUM_REQ)

            # authorize the client
            self._authorizeClient()

            # answer with same Content-Type/body
            bottle.response.content_type = bottle.request.content_type
            return bottle.request.body.read()

        except Exception as err:
            logCrash(self.name,err)
            raise

    def _cb_status_GET(self):
        try:
            # update stats
            AppData().incrStats(STAT_JSON_NUM_REQ)

            # authorize the client
            self._authorizeClient()

            # format response
            returnVal = {}
            returnVal['version solmanager']     = solmanager_version.VERSION
            returnVal['version SmartMesh SDK']  = sdk_version.VERSION
            returnVal['version Sol']            = SolVersion.VERSION
            returnVal['uptime computer']        = self._exec_cmd('uptime')
            returnVal['utc']                    = int(time.time())
            returnVal['date']                   = currentUtcTime()
            returnVal['last reboot']            = self._exec_cmd('last reboot')
            returnVal['stats']                  = AppData().getStats()

            # send response
            raise bottle.HTTPResponse(
                status  = 200,
                headers = {'Content-Type': 'application/json'},
                body    = json.dumps(returnVal),
            )

        except bottle.HTTPResponse:
            raise
        except Exception as err:
            logCrash(self.name,err)
            raise

    def _cb_config_GET(self):
        try:
            # update stats
            AppData().incrStats(STAT_JSON_NUM_REQ)

            # authorize the client
            self._authorizeClient()

            # handle
            raise NotImplementedError("getAllConfig() is not available anymore")
            """ getAllConfig() is not available anymore TODO
            allConfig = AppData().getAllConfig()
            for hidden in ['statsfile','solserver_token','solmanager_token']:
                if hidden in allConfig.keys():
                    del allConfig[hidden]
            return allConfig
            """

        except Exception as err:
            logCrash(self.name,err)
            raise

    def _cb_config_POST(self):
        try:
            # update stats
            AppData().incrStats(STAT_JSON_NUM_REQ)

            # authorize the client
            self._authorizeClient()

            # abort if malformed JSON body
            if bottle.request.json==None:
                raise bottle.HTTPResponse(
                    status  = 400,
                    headers = {'Content-Type': 'application/json'},
                    body    = json.dumps({'error': 'Malformed JSON body'}),
                )

            # handle
            raise NotImplementedError("setConfig() is not available anymore")
            """ setConfig() is not available anymore TODO
            for (k,v) in bottle.request.json.items():
                AppData().setConfig(k,v)
            """

            # send response
            raise bottle.HTTPResponse(
                status  = 200,
                headers = {'Content-Type': 'application/json'},
                body    = json.dumps({'status': 'config changed'}),
            )

        except bottle.HTTPResponse:
            raise
        except Exception as err:
            logCrash(self.name,err)
            raise

    def _cb_flows_GET(self):
        try:
            # update stats
            AppData().incrStats(STAT_JSON_NUM_REQ)

            # authorize the client
            self._authorizeClient()

            # handle
            return AppData().getFlows()

        except Exception as err:
            logCrash(self.name,err)
            raise

    def _cb_flows_POST(self):
        try:
            # update stats
            AppData().incrStats(STAT_JSON_NUM_REQ)

            # authorize the client
            self._authorizeClient()

            # abort if malformed JSON body
            if bottle.request.json==None:
                raise bottle.HTTPResponse(
                    status  = 400,
                    headers = {'Content-Type': 'application/json'},
                    body    = json.dumps({'error': 'Malformed JSON body'}),
                )

            # handle
            for (k,v) in bottle.request.json.items():
                try:
                    k = int(k)
                except:
                    pass
                assert v in [FLOW_ON,FLOW_OFF]
                AppData().setFlow(k,v)

            # send response
            raise bottle.HTTPResponse(
                status  = 200,
                headers = {'Content-Type': 'application/json'},
                body    = json.dumps({'status': 'flows changed'}),
            )

        except bottle.HTTPResponse:
            raise
        except Exception as err:
            logCrash(self.name,err)
            raise

    def _cb_resend_POST(self):
        try:
            # update stats
            AppData().incrStats(STAT_JSON_NUM_REQ)

            # authorize the client
            self._authorizeClient()

            # abort if malformed JSON body
            if bottle.request.json==None:
                raise bottle.HTTPResponse(
                    status  = 400,
                    headers = {'Content-Type': 'application/json'},
                    body    = json.dumps({'error': 'Malformed JSON body'}),
                )

            # verify all fields are present
            required_fields = ["action","startTimestamp","endTimestamp"]
            for field in required_fields:
                if field not in bottle.request.json:
                    raise bottle.HTTPResponse(
                        status  = 400,
                        headers = {'Content-Type': 'application/json'},
                        body    = json.dumps({'error': 'Missing field {0}'.format(field)}),
                    )

            # handle
            action          = bottle.request.json["action"]
            startTimestamp  = bottle.request.json["startTimestamp"]
            endTimestamp    = bottle.request.json["endTimestamp"]
            if action == "count":
                sol_jsonl = self.sol.loadFromFile(self.backupfile,startTimestamp,endTimestamp)
                # send response
                raise bottle.HTTPResponse(
                    status  = 200,
                    headers = {'Content-Type': 'application/json'},
                    body    = json.dumps({'numObjects': len(sol_jsonl)}),
                )
            elif action == "resend":
                sol_jsonl = self.sol.loadFromFile(self.backupfile,startTimestamp,endTimestamp)
                # publish
                for sobject in sol_jsonl:
                    SendThread().publish(sobject)
                # send response
                raise bottle.HTTPResponse(
                    status  = 200,
                    headers = {'Content-Type': 'application/json'},
                    body    = json.dumps({'numObjects': len(sol_jsonl)}),
                )
            else:
                raise bottle.HTTPResponse(
                    status  = 400,
                    headers = {'Content-Type': 'application/json'},
                    body    = json.dumps({'error': 'Unknown action {0}'.format(action)}),
                )

        except bottle.HTTPResponse:
            raise
        except Exception as err:
            logCrash(self.name,err)
            raise

    def _cb_snapshot_POST(self):
        try:
            # update stats
            AppData().incrStats(STAT_JSON_NUM_REQ)

            # authorize the client
            self._authorizeClient()

            # start the snapshot
            SnapshotThread().doSnapshot()

            # send response
            raise bottle.HTTPResponse(
                status  = 200,
                headers = {'Content-Type': 'application/json'},
                body    = json.dumps({'status': 'snapshot requested'}),
            )

        except bottle.HTTPResponse:
            raise
        except Exception as err:
            logCrash(self.name,err)
            raise

    def _cb_smartmeshipapi_POST(self):
        try:
            # update stats
            AppData().incrStats(STAT_JSON_NUM_REQ)

            # authorize the client
            self._authorizeClient()

            # abort if malformed JSON body
            if bottle.request.json==None or \
                    sorted(bottle.request.json.keys())!=sorted(["commandArray","fields"]):
                raise bottle.HTTPResponse(
                    status  = 400,
                    headers = {'Content-Type': 'application/json'},
                    body    = json.dumps({'error': 'Malformed JSON body'}),
                )

            # abort if trying to subscribe
            if bottle.request.json["commandArray"]==["subscribe"]:
                raise bottle.HTTPResponse(
                    status  = 403,
                    headers = {'Content-Type': 'application/json'},
                    body    = json.dumps({'error': 'You cannot issue a "subscribe" command'}),
                )

            # retrieve connector from DustThread
            connector = self.dustThread.connector

            # issue command
            try:
                res = connector.send(
                    commandArray = bottle.request.json["commandArray"],
                    fields       = bottle.request.json["fields"],
                )
            except ApiException.CommandError as err:
                raise bottle.HTTPResponse(
                    status  = 400,
                    headers = {'Content-Type': 'application/json'},
                    body    = json.dumps({'error': str(err)}),
                )
            except ApiException.APIError as err:
                raise bottle.HTTPResponse(
                    status  = 200,
                    headers = {'Content-Type': 'application/json'},
                    body    = json.dumps(
                        {
                            'commandArray': bottle.request.json["commandArray"],
                            'fields':       {
                                'RC':          err.rc,
                            },
                            'desc': str(err),
                        }
                    ),
                )

            raise bottle.HTTPResponse(
                status  = 200,
                headers = {'Content-Type': 'application/json'},
                body    = json.dumps(
                    {
                        'commandArray': bottle.request.json["commandArray"],
                        'fields': res,
                    }
                ),
            )

        except bottle.HTTPResponse:
            raise
        except Exception as err:
            logCrash(self.name,err)
            raise

    #=== misc

    def _authorizeClient(self):
        if bottle.request.headers.get('X-REALMS-Token') != self.solmanager_token:
            AppData().incrStats(STAT_JSON_NUM_UNAUTHORIZED)
            raise bottle.HTTPResponse(
                status  = 401,
                headers = {'Content-Type': 'application/json'},
                body    = json.dumps({'error': 'Unauthorized'}),
            )

    def _exec_cmd(self,cmd):
        returnVal = None
        try:
            returnVal = subprocess.check_output(cmd, shell=False)
        except:
            returnVal = "ERROR"
        return returnVal

class SolManager(threading.Thread):

    def __init__(self, configs):
        self.goOn           = True
        self.threads        = {
                "dustThread"            : None,
                "snapshotThread"        : None,
                "periodSnapThread"      : None,
                "fileThread"            : None,
                "sendThread"            : None,
                "jsonThread"            : None,
                }
        self.serialport     = configs["serialport"]
        self.snapperiod     = configs["snapperiodminutes"]
        self.statsperiod    = configs["statsperiodminutes"]
        self.filet_configs  = {
                    "backupfile"        : configs["backupfile"],
                    "fileperiodminutes" : configs["fileperiodminutes"],
                }
        self.sendt_configs  = {
                    "solserver_host"    : configs["solserver_host"],
                    "solserver_token"   : configs["solserver_token"],
                    "solserver_cert"    : configs["solserver_cert"],
                    "sendperiodminutes" : configs["sendperiodminutes"],
                }
        self.pullt_configs  = {
                    "solserver_host"    : configs["solserver_host"],
                    "solserver_token"   : configs["solserver_token"],
                    "solserver_cert"    : configs["solserver_cert"],
                    "pullperiodminutes" : configs["pullperiodminutes"],
                }
        self.jsont_configs  = {
                    "tcpport"           : configs["tcpport"],
                    "host"              : configs["solmanager_host"],
                    "token"             : configs["solmanager_token"],
                    "cert"              : configs["solmanager_cert"],
                    "privkey"           : configs["solmanager_privkey"],
                    "backupfile"        : configs["backupfile"],
                }
        AppData(configs["statsfile"])
        # start the thread
        threading.Thread.__init__(self)
        self.name                       = 'SolManager'
        self.start()

    def run(self):
        try:
            self.startThreads()
            while self.goOn:
                # verify that all threads are running
                all_running = True
                for t in self.threads.itervalues():
                    if not t.isAlive():
                        all_running = False
                        log.debug("Thread {0} is not running. Restarting.".format(t.name))
                if not all_running:
                    self.close()
                    self.startThreads()
                time.sleep(1)
            self.close()
        except Exception as err:
            logCrash(self.name,err)

    def startThreads(self):
        log.debug("starting thread")
        self.threads["dustThread"]      = DustThread(self.serialport,simulation=False)
        self.threads["snapshotThread"]  = SnapshotThread(self.threads["dustThread"])
        self.threads["periodSnapThread"]= PeriodicSnapshotThread(self.snapperiod)
        self.threads["fileThread"]      = FileThread(**self.filet_configs)
        self.threads["sendThread"]      = SendThread(**self.sendt_configs)
        self.threads["pullThread"]      = PullThread(**self.pullt_configs)
        self.threads["jsonThread"]      = JsonThread(self.threads["dustThread"],**self.jsont_configs)
        self.threads["statsThread"]     = StatsThread(self.threads["dustThread"],self.statsperiod)

        # verify that all threads are started
        all_started = False
        while not all_started and self.goOn:
            all_started = True
            for t in self.threads.itervalues():
                if not t.isAlive():
                    all_started = False
                    log.debug("Waiting for {0} to start".format(t.name))
            time.sleep(5)
        log.debug("All threads started")

    def close(self):
        for t in self.threads.itervalues():
            t.close()

        # wait for the theads to close
        time.sleep(2)

        # verify that all threads are closed
        all_closed = False
        while not all_closed and self.goOn:
            all_closed = True
            for t in self.threads.itervalues():
                if t.isAlive():
                    all_closed = False
                    log.debug("Waiting for {0} to stop".format(t.name))
            time.sleep(2)

        for t in self.threads.itervalues():
            if hasattr(t,"_del"):
                t._del()
            t = None

        log.debug("All threads closed")

#============================ main ============================================

solmanager = None

def quitCallback():
    log.info("Quitting.")
    solmanager.goOn = False

def returnStatsGroup(stats,prefix):
    keys = []
    for (k,v) in stats.items():
        if k.startswith(prefix):
            keys+=[k]
    returnVal = []
    for k in sorted(keys):
        returnVal += ['   {0:<30}: {1}'.format(k,stats[k])]
    return returnVal

def cli_cb_stats(params):
    stats = AppData().getStats()
    output  = []
    output += ['#== admin']
    output += returnStatsGroup(stats,'ADM_')
    output += ['#== connection to manager']
    output += returnStatsGroup(stats,'MGR_')
    output += ['#== notifications from manager']
    output += returnStatsGroup(stats,'NUMRX_')
    output += ['#== publication']
    output += returnStatsGroup(stats,'PUB_')
    output += ['# to file']
    output += returnStatsGroup(stats,'PUBFILE_')
    output += ['# to server']
    output += returnStatsGroup(stats,'PUBSERVER_')
    output += ['#== snapshot']
    output += returnStatsGroup(stats,'SNAPSHOT_')
    output += ['#== JSON interface']
    output += returnStatsGroup(stats,'JSON_')
    output = '\n'.join(output)
    print output

def main(configs):
    global solmanager

    # create the solmanager instance
    solmanager = SolManager(configs)

    # start the CLI interface
    cli = OpenCli.OpenCli(
        "SolManager",
        solmanager_version.VERSION,
        quitCallback,
        [
            ("SmartMesh SDK",sdk_version.VERSION),
            ("Sol",SolVersion.VERSION),
        ],
    )
    cli.registerCommand(
        'stats',
        's',
        'print the stats',
        [],
        cli_cb_stats
    )

if __name__ == '__main__':
    # defines configurations
    default_configs = {}
    configs         = {}
    config_list_str = [
                "statsfile", "backupfile", "serialport", "solmanager_tcpport",
                "solmanager_host", "solmanager_token", "solmanager_cert", "solmanager_privkey",
                "solserver_host", "solserver_token", "solserver_cert"
            ]
    config_list_int = [
                "sendperiodminutes", "fileperiodminutes", "snapperiodminutes",
                "statsperiodminutes", "pullperiodminutes"
            ]

    # load configurations from default config file
    cf_defparser = SafeConfigParser()
    cf_defparser.readfp(open(DEFAULT_CONFIGFILE))
    for config_name in config_list_str:
        default_configs[config_name] = cf_defparser.get('config', config_name)
    for config_name in config_list_int:
        default_configs[config_name] = cf_defparser.getint('config', config_name)

    # load configurations from custom user defined config file
    cf_parser = SafeConfigParser(default_configs)
    cf_parser.readfp(open(CONFIGFILE))
    for config_name in config_list_str:
        configs[config_name] = cf_parser.get('config', config_name)
    for config_name in config_list_int:
        configs[config_name] = cf_parser.getint('config', config_name)

    # parse the command line and update configurations
    parser = OptionParser("usage: %prog [options]")
    parser.add_option(
        "-s", "--serialport", dest="serialport",
        default=configs["serialport"],
        help="Serial port of the SmartMesh IP manager."
    )
    parser.add_option(
        "-t", "--tcpport", dest="tcpport",
        default=configs["solmanager_tcpport"],
        help="TCP port to start the JSON API on."
    )
    (options, args)         = parser.parse_args()
    configs["serialport"]   = options.serialport
    configs["tcpport"]      = options.tcpport

    # log configuration
    log.debug("============== Configuration ================")
    for config_name in configs:
        log.debug("==== {0}: {1}".format(config_name, configs[config_name]))

    main(configs)
