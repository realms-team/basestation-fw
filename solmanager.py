#!/usr/bin/python

#============================ adjust path =====================================

import sys
import os

if __name__ == "__main__":
    here = sys.path[0]
    sys.path.insert(0, os.path.join(here, '..', 'sol'))
    sys.path.insert(0, os.path.join(here, '..', 'smartmeshsdk', 'libs'))

#============================ imports =========================================

# from default Python
import time
import json
import subprocess
import threading
import traceback
import logging.config
import ConfigParser

# third-party packages
import OpenSSL
import bottle
import requests

# project-specific
import solmanager_version
import connectors
from   SmartMeshSDK          import sdk_version
from   SmartMeshSDK.utils    import JsonManager, FormatUtils
from   dustCli               import DustCli
from   solobjectlib          import Sol, \
                                    SolVersion, \
                                    SolDefines, \
                                    SolExceptions, \
                                    SolUtils

#============================ logging =========================================

logging.config.fileConfig('logging.conf', disable_existing_loggers=False)
log = logging.getLogger("solmanager")

#============================ defines =========================================

CONFIGFILE         = 'solmanager.config'
CONNECTORFILE      = 'connectors.config'
STATSFILE          = 'solmanager.stats'
BACKUPFILE         = 'solmanager.backup'

ALLSTATS           = [
    #== admin
    'ADM_NUM_CRASHES',
    #== connection to manager
    'MGR_NUM_CONNECT_ATTEMPTS',
    'MGR_NUM_CONNECT_OK',
    'MGR_NUM_DISCONNECTS',
    'MGR_NUM_TIMESYNC',
    #== notifications from manager
    # note: we count the number of notifications form the manager, for each time, e.g. NUMRX_NOTIFDATA
    # all stats start with "NUMRX_"
    #== publication
    'PUB_TOTAL_SENTTOPUBLISH',
    # to file
    'PUBFILE_BACKLOG',
    'PUBFILE_WRITES',
    # to server
    'PUBSERVER_BACKLOG',
    'PUBSERVER_SENDATTEMPTS',
    'PUBSERVER_UNREACHABLE',
    'PUBSERVER_SENDOK',
    'PUBSERVER_SENDFAIL',
    'PUBSERVER_STATS',
    'PUBSERVER_PULLATTEMPTS',
    'PUBSERVER_PULLOK',
    'PUBSERVER_PULLFAIL',
    #== snapshot
    'SNAPSHOT_NUM_STARTED',
    'SNAPSHOT_LASTSTARTED',
    'SNAPSHOT_NUM_OK',
    'SNAPSHOT_NUM_FAIL',
    #== JSON interface
    'JSON_NUM_REQ',
    'JSON_NUM_UNAUTHORIZED',
]

HTTP_CHUNK_SIZE      = 10 # send batches of 10 Sol objects

#============================ classes =========================================

#======== generic abstract classes


class DoSomethingPeriodic(threading.Thread):
    """
    Abstract DoSomethingPeriodic thread
    """
    def __init__(self, periodvariable):
        self.goOn                       = True
        # start the thread
        threading.Thread.__init__(self)
        self.name                       = 'DoSomethingPeriodic'
        self.daemon                     = True
        self.periodvariable             = periodvariable*60
        self.currentDelay               = 0

    def run(self):
        try:
            self.currentDelay = 5
            while self.goOn:
                self.currentDelay -= 1
                if self.currentDelay == 0:
                    self._doSomething()
                    self.currentDelay = self.periodvariable
                time.sleep(1)
        except Exception as err:
            SolUtils.logCrash(err, SolUtils.AppStats(), threadName=self.name)

    def close(self):
        self.goOn = False

    def _doSomething(self):
        raise SystemError()  # abstract method

#======== connecting to the SmartMesh IP manager


class MgrThread(object):
    """
    Asbtract class which connects to a SmartMesh IP manager, either over serial
    or through a JsonServer.
    """
    def __init__(self, solmanager_thread):

        # local variables
        self.sol = Sol.Sol()
        self.macManager = None
        self.solmanager_thread = solmanager_thread
        self.dataLock = threading.RLock()

    def getMacManager(self):
        if self.macManager is None:
            resp = self.issueRawApiCommand(
                {
                    "manager": 0,
                    "command": "getMoteConfig",
                    "fields": {
                        "macAddress": [0, 0, 0, 0, 0, 0, 0, 0],
                        "next": True
                    }
                }
            )
            assert resp['isAP'] is True
            self.macManager = resp['macAddress']
        return self.macManager

    def _handler_dust_notifs(self, dust_notif):
        try:
            # filter raw HealthReport notifications
            if dust_notif['name'] == "notifHealthReport":
                return

            # update stats
            SolUtils.AppStats().increment('NUMRX_{0}'.format(dust_notif['name']))

            # get time
            epoch = None
            if hasattr(dust_notif, "utcSecs") and hasattr(dust_notif, "utcUsecs"):
                netTs = self._calcNetTs(dust_notif)
                epoch = self._netTsToEpoch(netTs)

            # convert dust notification to JSON SOL Object
            sol_jsonl = self.sol.dust_to_json(
                dust_notif  = dust_notif,
                mac_manager = self.getMacManager(),
                timestamp   = epoch,
            )

            for sol_json in sol_jsonl:
                # update stats
                SolUtils.AppStats().increment('PUB_TOTAL_SENTTOPUBLISH')

                # publish
                log.debug("Publishing stats")
                self.solmanager_thread.publish(sol_json)

        except Exception as err:
            SolUtils.logCrash(err, SolUtils.AppStats())

    def close(self):
        pass

    # === misc

    def _calcNetTs(self, notif):
        return int(float(notif.utcSecs) + float(notif.utcUsecs / 1000000.0))

    def _syncNetTsToUtc(self, netTs):
        with self.dataLock:
            self.tsDiff = time.time() - netTs

    def _netTsToEpoch(self, netTs):
        with self.dataLock:
            return int(netTs + self.tsDiff)

class MgrThreadSerial(MgrThread):

    def __init__(self, solmanager_thread):

        # initialize the parent class
        super(MgrThreadSerial, self).__init__(solmanager_thread)

        # initialize JsonManager
        self.jsonManager          = JsonManager.JsonManager(
            serialport            = SolUtils.AppConfig().get("serialport"),
            notifCb               = self._notif_cb,
        )

    def issueRawApiCommand(self, json_payload):
        fields = {}
        if "fields" in json_payload:
            fields = json_payload["fields"]

        if "command" not in json_payload or "manager" not in json_payload:
            return json.dumps({'error': 'Missing parameter.'})

        return self.jsonManager.raw_POST(
            manager          = json_payload['manager'],
            commandArray     = [json_payload['command']],
            fields           = fields,
        )

    def _notif_cb(self, notifName, notifJson):
        super(MgrThreadSerial, self)._handler_dust_notifs(
            notifJson,
        )

class MgrThreadJsonServer(MgrThread, threading.Thread):

    def __init__(self, solmanager_thread):

        # initialize the parent class
        super(MgrThreadJsonServer, self).__init__(solmanager_thread)

        # initialize web server
        self.web            = bottle.Bottle()
        self.web.route(
            path        = [
                '/hr',
                '/notifData',
                '/oap',
                '/notifLog',
                '/notifIpData',
                '/event',
            ],
            method      = 'POST',
            callback    = self._webhandler_all_POST
        )

        # start the thread
        threading.Thread.__init__(self)
        self.name       = 'MgrThreadJsonServer'
        self.daemon     = True
        self.start()

    def run(self):
        try:
            # wait for banner
            time.sleep(0.5)
            self.web.run(
                host   = '0.0.0.0',
                port   = SolUtils.AppConfig().get("solmanager_tcpport_jsonserver"),
                quiet  = True,
                debug  = False,
            )
        except Exception as err:
            SolUtils.logCrash(err, SolUtils.AppStats(), threadName=self.name)

    def issueRawApiCommand(self, json_payload):
        r = requests.post(
            'http://{0}/api/v1/raw'.format(SolUtils.AppConfig().get("jsonserver_host")),
            json    = json_payload,
        )
        return json.loads(r.text)

    def _webhandler_all_POST(self):
        super(MgrThreadJsonServer, self)._handler_dust_notifs(
            json.loads(bottle.request.body.read()),
        )

#======== periodically do something


class SnapshotThread(DoSomethingPeriodic):
    """ publish network snapshot """

    def __init__(self, mgr_thread, solmanager_thread):

        # store params
        self.mgr_thread         = mgr_thread
        self.solmanager_thread  = solmanager_thread

        # initialize parent class
        super(SnapshotThread, self).__init__(SolUtils.AppConfig().get("period_snapshot_min"))
        self.name            = 'SnapshotThread'
        self.start()

        # initialize local attributes
        self.last_snapshot = None

    def _doSomething(self):
        self._doSnapshot()

    def _doSnapshot(self):
        try:
            # update stats
            SolUtils.AppStats().increment('SNAPSHOT_NUM_STARTED')
            SolUtils.AppStats().update(
                'SNAPSHOT_LASTSTARTED',
                SolUtils.currentUtcTime(),
            )

            '''
            [
                {   'macAddress':          [0x01,0x02,0x03,0x04,0x05,0x06,0x07,0x08],
                    'moteId':              0x090a,      # INT16U  H
                    'isAP':                0x0b,        # BOOL    B
                    'state':               0x0c,        # INT8U   B
                    'isRouting':           0x0d,        # BOOL    B
                    'numNbrs':             0x0e,        # INT8U   B
                    'numGoodNbrs':         0x0f,        # INT8U   B
                    'requestedBw':         0x10111213,  # INT32U  I
                    'totalNeededBw':       0x14151617,  # INT32U  I
                    'assignedBw':          0x18191a1b,  # INT32U  I
                    'packetsReceived':     0x1c1d1e1f,  # INT32U  I
                    'packetsLost':         0x20212223,  # INT32U  I
                    'avgLatency':          0x24252627,  # INT32U  I
                    'paths': [
                        {
                            'macAddress':   [0x11,0x12,0x13,0x14,0x15,0x16,0x17,0x18],
                            'direction':    0x2c,       # INT8U   B
                            'numLinks':     0x2d,       # INT8U   B
                            'quality':      0x2e,       # INT8U   B
                            'rssiSrcDest':  -1,         # INT8    b
                            'rssiDestSrc':  -2,         # INT8    b
                        },
                        {
                            'macAddress':   [0x21,0x22,0x23,0x24,0x25,0x26,0x27,0x28],
                            'direction':    0x2c,       # INT8U  B
                            'numLinks':     0x2d,       # INT8U  B
                            'quality':      0x2e,       # INT8U  B
                            'rssiSrcDest':  -1,         # INT8   b
                            'rssiDestSrc':  -2,         # INT8   b
                        },
                    ],
                },
                {
                    'macAddress':           [0x31,0x32,0x33,0x34,0x35,0x36,0x37,0x38],
                    'moteId':               0x090a,     # INT16U
                    'isAP':                 0x0b,       # BOOL
                    'state':                0x0c,       # INT8U
                    'isRouting':            0x0d,       # BOOL
                    'numNbrs':              0x0e,       # INT8U
                    'numGoodNbrs':          0x0f,       # INT8U
                    'requestedBw':          0x10111213, # INT32U
                    'totalNeededBw':        0x14151617, # INT32U
                    'assignedBw':           0x18191a1b, # INT32U
                    'packetsReceived':      0x1c1d1e1f, # INT32U
                    'packetsLost':          0x20212223, # INT32U
                    'avgLatency':           0x24252627, # INT32U
                    'paths': [
                        {
                            'macAddress':   [0x41,0x42,0x43,0x44,0x45,0x46,0x47,0x48],
                            'direction':    0x2c,       # INT8U
                            'numLinks':     0x2d,       # INT8U
                            'quality':      0x2e,       # INT8U
                            'rssiSrcDest':  -1,         # INT8
                            'rssiDestSrc':  -2,         # INT8
                        },
                    ],
                },
            ]
            '''

            snapshot = []

            # getMoteConfig() on all motes
            currentMac = [0]*8
            while True:
                resp = self.mgr_thread.issueRawApiCommand(
                    {
                        "manager": 0,
                        "command": "getMoteConfig",
                        "fields": {
                            "macAddress": currentMac,
                            "next": True
                        }
                    }
                )
                if resp['RC'] != 0:
                    break
                snapshot    += [resp]
                currentMac   = resp['macAddress']

            # getMoteInfo() on all motes
            for mote in snapshot:
                resp = self.mgr_thread.issueRawApiCommand(
                    {
                        "manager": 0,
                        "command": "getMoteInfo",
                        "fields": {
                            "macAddress": mote['macAddress'],
                        }
                    }
                )
                mote.update(resp)

            # getPathInfo() on all paths on all motes
            for mote in snapshot:
                mote['paths'] = []
                currentPathId  = 0
                while True:
                    resp = self.mgr_thread.issueRawApiCommand(
                        {
                            "manager": 0,
                            "command": "getNextPathInfo",
                            "fields": {
                                "macAddress": mote['macAddress'],
                                "filter":     0,
                                "pathId":     currentPathId
                            }
                        }
                    )
                    if resp["RC"] != 0:
                        break
                    mote['paths'] += [
                        {
                            'macAddress':    resp["dest"],
                            'direction':     resp["direction"],
                            'numLinks':      resp["numLinks"],
                            'quality':       resp["quality"],
                            'rssiSrcDest':   resp["rssiSrcDest"],
                            'rssiDestSrc':   resp["rssiDestSrc"],
                        }
                    ]
                    currentPathId  = resp["pathId"]

        except Exception as err:
            SolUtils.AppStats().increment('SNAPSHOT_NUM_FAIL')
            log.warning("Cannot do Snapshot: %s", err)
            traceback.print_exc()
        else:
            if self.mgr_thread.getMacManager() is not None:
                SolUtils.AppStats().increment('SNAPSHOT_NUM_OK')

                # create sensor object
                sobject = {
                    'mac':       self.mgr_thread.getMacManager(),
                    'timestamp': int(time.time()),
                    'type':      SolDefines.SOL_TYPE_DUST_SNAPSHOT,
                    'value':     snapshot,
                }
                self.last_snapshot = sobject

                # publish sensor object
                self.solmanager_thread.publish(sobject)


class StatsThread(DoSomethingPeriodic):
    """
    Publish application statistics every period_stats_min.
    """

    def __init__(self, mgr_thread, solmanager_thread):

        # store params
        self.mgr_thread         = mgr_thread
        self.solmanager_thread  = solmanager_thread

        # initialize parent class
        super(StatsThread, self).__init__(SolUtils.AppConfig().get("period_stats_min"))
        self.name            = 'StatsThread'
        self.start()

    def _doSomething(self):

        # create sensor object
        sobject = {
            'mac':       self.mgr_thread.getMacManager(),
            'timestamp': int(time.time()),
            'type':      SolDefines.SOL_TYPE_SOLMANAGER_STATS,
            'value':     {
                'sol_version'           : list(SolVersion.VERSION),
                'solmanager_version'    : list(solmanager_version.VERSION),
                'sdk_version'           : list(sdk_version.VERSION)
            },
        }

        # publish
        self.solmanager_thread.publish(sobject)

        # update stats
        SolUtils.AppStats().increment('PUBSERVER_STATS')


class JsonApiThread(threading.Thread):
    """JSON API to trigger actions on the SolManager"""

    class HTTPSServer(bottle.ServerAdapter):
        def run(self, handler):
            from cheroot.wsgi import Server as WSGIServer
            from cheroot.ssl.pyopenssl import pyOpenSSLAdapter
            server = WSGIServer((self.host, self.port), handler)
            server.ssl_adapter = pyOpenSSLAdapter(
                certificate = SolUtils.AppConfig().get("solmanager_certificate"),
                private_key = SolUtils.AppConfig().get("solmanager_private_key"),
            )
            try:
                server.start()
                log.info("Server started")
            finally:
                server.stop()

    def __init__(self, mgr_thread, snapshot_thread, solmanager_thread):

        # store params
        self.mgr_thread         = mgr_thread
        self.snapshot_thread    = snapshot_thread
        self.solmanager_thread  = solmanager_thread

        # local variables
        self.sol                = Sol.Sol()

        # check if files exist
        fcert = open(SolUtils.AppConfig().get("solmanager_certificate"))
        fcert.close()
        fkey = open(SolUtils.AppConfig().get("solmanager_private_key"))
        fkey.close()

        # initialize web server
        self.web                = bottle.Bottle()

        ENDPOINTS = [("echo", "POST"), ("status", "GET"), ("resend", "POST"), ("smartmeshipapi", "POST"),
                     ("snapshot", "POST")]
        API_VERSION = 1
        for endpoint in ENDPOINTS:
            self.web.route(
                path        = '/api/v{0}/{1}.json'.format(API_VERSION, endpoint[0]),
                method      = endpoint[1],
                callback    = getattr(self, "_webhandler_{0}_{1}".format(*endpoint)),
            )

        # start the thread
        threading.Thread.__init__(self)
        self.name       = 'JsonThread'
        self.daemon     = True
        self.start()

    def run(self):
        try:
            # wait for banner
            time.sleep(0.5)
            self.web.run(
                host   = '0.0.0.0',
                port   = SolUtils.AppConfig().get("solmanager_tcpport_solserver"),
                server = self.HTTPSServer,
                quiet  = True,
                debug  = False,
            )
        except Exception as err:
            SolUtils.logCrash(err, SolUtils.AppStats(), threadName=self.name)

    #======================== public ==========================================

    def close(self):
        self.web.close()

    #======================== private ==========================================

    #=== webhandlers

    # decorator
    def _authorized_webhandler(func):
        def hidden_decorator(self):
            try:
                # update stats
                SolUtils.AppStats().increment('JSON_NUM_REQ')

                # authorize the client
                self._authorizeClient()

                # retrieve the return value
                returnVal = func(self)

                # send back answer
                return bottle.HTTPResponse(
                    status  = 200,
                    headers = {'Content-Type': 'application/json'},
                    body    = json.dumps(returnVal),
                )

            except SolExceptions.UnauthorizedError:
                return bottle.HTTPResponse(
                    status  = 401,
                    headers = {'Content-Type': 'application/json'},
                    body    = json.dumps({'error': 'Unauthorized'}),
                )
            except Exception as err:

                crashMsg = SolUtils.logCrash(err, SolUtils.AppStats())

                return bottle.HTTPResponse(
                    status  = 500,
                    headers = {'Content-Type': 'application/json'},
                    body    = json.dumps(crashMsg),
                )
        return hidden_decorator

    @_authorized_webhandler
    def _webhandler_echo_POST(self):
        return bottle.request.body.read()

    @_authorized_webhandler
    def _webhandler_status_GET(self):
        return {
            'version solmanager':     solmanager_version.VERSION,
            'version SmartMesh SDK':  sdk_version.VERSION,
            'version Sol':            SolVersion.VERSION,
            'uptime computer':        self._exec_cmd('uptime'),
            'utc':                    int(time.time()),
            'date':                   SolUtils.currentUtcTime(),
            'last reboot':            self._exec_cmd('last reboot'),
            'stats':                  SolUtils.AppStats().get(),
        }

    @_authorized_webhandler
    def _webhandler_resend_POST(self):
        # abort if malformed JSON body
        if bottle.request.json is None:
            return {'error': 'Malformed JSON body'}

        # verify all fields are present
        required_fields = ["action", "startTimestamp", "endTimestamp"]
        for field in required_fields:
            if field not in bottle.request.json:
                return {'error': 'Missing field {0}'.format(field)}

        # handle
        action          = bottle.request.json["action"]
        startTimestamp  = bottle.request.json["startTimestamp"]
        endTimestamp    = bottle.request.json["endTimestamp"]
        if action == "count":
            sol_jsonl = self.sol.loadFromFile(BACKUPFILE, startTimestamp, endTimestamp)
            # send response
            return {'numObjects': len(sol_jsonl)}
        elif action == "resend":
            sol_jsonl = self.sol.loadFromFile(BACKUPFILE, startTimestamp, endTimestamp)
            # publish
            for sobject in sol_jsonl:
                self.solmanager_thread.publish(sobject)
            # send response
            return {'numObjects': len(sol_jsonl)}
        else:
            return {'error': 'Unknown action {0}'.format(action)}

    @_authorized_webhandler
    def _webhandler_smartmeshipapi_POST(self):
        return self.mgr_thread.issueRawApiCommand(bottle.request.json)

    @_authorized_webhandler
    def _webhandler_snapshot_POST(self):
        if self.snapshot_thread.last_snapshot:
            return self.snapshot_thread.last_snapshot
        else:
            self.snapshot_thread._doSnapshot()
            return "snapshot started"

    #=== misc

    def _authorizeClient(self):
        if bottle.request.headers.get('X-REALMS-Token') != SolUtils.AppConfig().get("solmanager_token"):
            SolUtils.AppStats().increment('JSON_NUM_UNAUTHORIZED')
            raise SolExceptions.UnauthorizedError()

    def _exec_cmd(self, cmd):
        returnVal = None
        try:
            returnVal = subprocess.check_output(cmd, shell=False)
        except:
            returnVal = "ERROR"
        return returnVal

#======== main application thread


class SolManager(threading.Thread):

    def __init__(self):
        self.goOn           = True
        self.threads        = {
            "mgrThread"                : None,
            "snapshotThread"           : None,
            "statsThread"              : None,
            "jsonApiThread"            : None,
        }
        self.connectors = {}

        # init Singletons -- must be first init
        SolUtils.AppConfig(config_file=CONFIGFILE)
        SolUtils.AppStats(stats_file=STATSFILE, stats_list=ALLSTATS)

        # CLI interface
        self.cli                       = DustCli.DustCli("SolManager", self._clihandle_quit)
        self.cli.registerCommand(
            name                       = 'stats',
            alias                      = 's',
            description                = 'print the stats',
            params                     = [],
            callback                   = self._clihandle_stats,
        )
        self.cli.registerCommand(
            name                       = 'versions',
            alias                      = 'v',
            description                = 'print the versions of the different components',
            params                     = [],
            callback                   = self._clihandle_versions,
        )
        self.cli.start()

        # start myself
        threading.Thread.__init__(self)
        self.name                      = 'SolManager'
        self.daemon                    = True
        self.start()

    def run(self):
        try:
            # start threads
            log.debug("Starting threads")
            if SolUtils.AppConfig().get('managerconnectionmode') == 'serial':
                self.threads["mgrThread"]            = MgrThreadSerial(solmanager_thread=self)
            else:
                self.threads["mgrThread"]            = MgrThreadJsonServer(solmanager_thread=self)
            self.threads["snapshotThread"]           = SnapshotThread(
                mgr_thread              = self.threads["mgrThread"],
                solmanager_thread       = self
            )
            self.threads["statsThread"]              = StatsThread(
                mgr_thread              = self.threads["mgrThread"],
                solmanager_thread       = self
            )
            self.threads["jsonApiThread"]            = JsonApiThread(
                mgr_thread              = self.threads["mgrThread"],
                snapshot_thread         = self.threads["snapshotThread"],
                solmanager_thread       = self
            )

            # wait for all threads to have started
            all_started = False
            while not all_started and self.goOn:
                all_started = True
                for t in self.threads.itervalues():
                    try:
                        if not t.isAlive():
                            all_started = False
                            log.debug("Waiting for %s to start", t.name)
                    except AttributeError:
                        pass  # happens when not a real thread
                time.sleep(5)
            log.debug("All threads started")

            # start connector
            self.start_connectors()

            # return as soon as one thread not alive
            while self.goOn:
                # verify that all threads are running
                all_running = True
                for t in self.threads.itervalues():
                    try:
                        if not t.isAlive():
                            all_running = False
                            log.debug("Thread {0} is not running. Quitting.".format(t.name))
                    except AttributeError:
                        pass  # happens when not a real thread
                if not all_running:
                    self.goOn = False
                time.sleep(5)
        except Exception as err:
            SolUtils.logCrash(err, SolUtils.AppStats(), threadName=self.name)
        self.close()

    def close(self):
        os._exit(0)  # bypass CLI thread

    def publish(self, sol_json, topic="o.json"):
        sol_json["manager"] = FormatUtils.formatBuffer(self.threads["mgrThread"].macManager)
        for connector_name, connector in self.connectors.iteritems():
            connector.publish(sol_json, topic)

    def start_connectors(self):
        """start all the connectors defined in the configuration file"""
        # get the configuration
        config = ConfigParser.ConfigParser()
        config.read(CONNECTORFILE)
        connector_config = {s: dict(config.items(s)) for s in config.sections()}

        # get additional authentication information
        auth_id = FormatUtils.formatBuffer(self.threads["mgrThread"].macManager)

        # start the connectors
        for connector_name, connector in connector_config.iteritems():
            # add authentication information
            connector["auth_id"] = auth_id
            # create the connector
            self.connectors[connector_name] = connectors.connector.create(connector)
            # subscribe to the 'command' topic
            self.connectors[connector_name].subscribe("command", self._handle_command)

    def _handle_command(self, command):
        # TODO call all command functions
        if command == "snapshot":
            self.threads["snapshotThread"]._doSnapshot()
        else:
            log.debug("command not known: {0}".format(command))

    def _clihandle_quit(self):
        time.sleep(.3)
        print "bye bye."
        # all threads as daemonic, will close automatically

    def _clihandle_stats(self, params):
        stats = SolUtils.AppStats().get()
        output  = []
        output += ['#== admin']
        output += self._returnStatsGroup(stats, 'ADM_')
        output += ['#== connection to manager']
        output += self._returnStatsGroup(stats, 'MGR_')
        output += ['#== notifications from manager']
        output += self._returnStatsGroup(stats, 'NUMRX_')
        output += ['#== publication']
        output += self._returnStatsGroup(stats, 'PUB_')
        output += ['# to file']
        output += self._returnStatsGroup(stats, 'PUBFILE_')
        output += ['# to server']
        output += self._returnStatsGroup(stats, 'PUBSERVER_')
        output += ['#== snapshot']
        output += self._returnStatsGroup(stats, 'SNAPSHOT_')
        output += ['#== JSON interface']
        output += self._returnStatsGroup(stats, 'JSON_')
        output = '\n'.join(output)
        print output

    def _clihandle_versions(self, params):
        output  = []
        for (k, v) in [
                ('SolManager',    solmanager_version.VERSION),
                ('Sol',           SolVersion.VERSION),
                ('SmartMesh SDK', sdk_version.VERSION),
            ]:
            output += ["{0:>15} {1}".format(k, '.'.join([str(b) for b in v]))]
        output = '\n'.join(output)
        print output

    def _returnStatsGroup(self, stats, prefix):
        keys = []
        for (k, v) in stats.items():
            if k.startswith(prefix):
                keys += [k]
        returnVal = []
        for k in sorted(keys):
            returnVal += ['   {0:<30}: {1}'.format(k, stats[k])]
        return returnVal

#============================ main ============================================


def main():
    SolManager()

if __name__ == '__main__':
    main()
