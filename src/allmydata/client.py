
import os, sha, stat, time
from foolscap import Referenceable, SturdyRef
from zope.interface import implements
from allmydata.interfaces import RIClient
from allmydata import node

from twisted.internet import defer, reactor
from twisted.application.internet import TimerService

import allmydata
from allmydata.Crypto.Util.number import bytes_to_long
from allmydata.storageserver import StorageServer
from allmydata.upload import Uploader
from allmydata.download import Downloader
from allmydata.vdrive import DirectoryNode
from allmydata.webish import WebishServer
from allmydata.control import ControlServer
from allmydata.introducer import IntroducerClient

class Client(node.Node, Referenceable):
    implements(RIClient)
    PORTNUMFILE = "client.port"
    STOREDIR = 'storage'
    NODETYPE = "client"
    WEBPORTFILE = "webport"
    INTRODUCER_FURL_FILE = "introducer.furl"
    GLOBAL_VDRIVE_FURL_FILE = "vdrive.furl"
    MY_FURL_FILE = "myself.furl"
    SUICIDE_PREVENTION_HOTLINE_FILE = "suicide_prevention_hotline"

    # we're pretty narrow-minded right now
    OLDEST_SUPPORTED_VERSION = allmydata.__version__

    def __init__(self, basedir="."):
        node.Node.__init__(self, basedir)
        self.my_furl = None
        self.introducer_client = None
        self._connected_to_vdrive = False
        self.add_service(StorageServer(os.path.join(basedir, self.STOREDIR)))
        self.add_service(Uploader())
        self.add_service(Downloader())
        WEBPORTFILE = os.path.join(self.basedir, self.WEBPORTFILE)
        if os.path.exists(WEBPORTFILE):
            f = open(WEBPORTFILE, "r")
            webport = f.read().strip() # strports string
            f.close()
            self.add_service(WebishServer(webport))

        INTRODUCER_FURL_FILE = os.path.join(self.basedir,
                                            self.INTRODUCER_FURL_FILE)
        f = open(INTRODUCER_FURL_FILE, "r")
        self.introducer_furl = f.read().strip()
        f.close()

        self.global_vdrive_furl = None
        GLOBAL_VDRIVE_FURL_FILE = os.path.join(self.basedir,
                                               self.GLOBAL_VDRIVE_FURL_FILE)
        if os.path.exists(GLOBAL_VDRIVE_FURL_FILE):
            f = open(GLOBAL_VDRIVE_FURL_FILE, "r")
            self.global_vdrive_furl = f.read().strip()
            f.close()
            #self.add_service(VDrive())

        hotline_file = os.path.join(self.basedir,
                                    self.SUICIDE_PREVENTION_HOTLINE_FILE)
        if os.path.exists(hotline_file):
            hotline = TimerService(1.0, self._check_hotline, hotline_file)
            hotline.setServiceParent(self)

    def _check_hotline(self, hotline_file):
        if os.path.exists(hotline_file):
            mtime = os.stat(hotline_file)[stat.ST_MTIME]
            if mtime > time.time() - 10.0:
                return
        self.log("hotline missing or too old, shutting down")
        reactor.stop()

    def tub_ready(self):
        self.log("tub_ready")

        my_old_name = None
        MYSELF_FURL_PATH = os.path.join(self.basedir, self.MY_FURL_FILE)
        if os.path.exists(MYSELF_FURL_PATH):
            my_old_furl = open(MYSELF_FURL_PATH, "r").read().strip()
            sturdy = SturdyRef(my_old_furl)
            my_old_name = sturdy.name

        self.my_furl = self.tub.registerReference(self, my_old_name)
        f = open(MYSELF_FURL_PATH, "w")
        f.write(self.my_furl)
        f.close()

        ic = IntroducerClient(self.tub, self.introducer_furl, self.my_furl)
        self.introducer_client = ic
        ic.setServiceParent(self)

        self.register_control()

        if self.global_vdrive_furl:
            self.vdrive_connector = self.tub.connectTo(self.global_vdrive_furl,
                                                       self._got_vdrive)

    def register_control(self):
        c = ControlServer()
        c.setServiceParent(self)
        control_url = self.tub.registerReference(c)
        f = open("control.furl", "w")
        f.write(control_url + "\n")
        f.close()
        os.chmod("control.furl", 0600)

    def _got_vdrive(self, vdrive_server):
        # vdrive_server implements RIVirtualDriveServer
        self.log("connected to vdrive server")
        d = vdrive_server.callRemote("get_public_root_furl")
        d.addCallback(self._got_vdrive_root_furl, vdrive_server)

    def _got_vdrive_root_furl(self, vdrive_root_furl, vdrive_server):
        root = DirectoryNode(vdrive_root_furl, self)
        self.log("got vdrive root")
        self._vdrive_server = vdrive_server
        self._vdrive_root = root
        self._connected_to_vdrive = True

        #vdrive = self.getServiceNamed("vdrive")
        #vdrive.set_server(vdrive_server)
        #vdrive.set_root(vdrive_root)

        if "webish" in self.namedServices:
            webish = self.getServiceNamed("webish")
            webish.set_vdrive_root(root)

    def remote_get_versions(self):
        return str(allmydata.__version__), str(self.OLDEST_SUPPORTED_VERSION)

    def remote_get_service(self, name):
        if name in ("storageserver",):
            return self.getServiceNamed(name)
        raise RuntimeError("I am unwilling to give you service %s" % name)

    def get_remote_service(self, nodeid, servicename):
        if nodeid not in self.introducer_client.connections:
            return defer.fail(IndexError("no connection to that peer"))
        peer = self.introducer_client.connections[nodeid]
        d = peer.callRemote("get_service", name=servicename)
        return d


    def get_all_peerids(self):
        if not self.introducer_client:
            return []
        return self.introducer_client.connections.iterkeys()

    def get_permuted_peers(self, key):
        """
        @return: list of (permuted-peerid, peerid, connection,)
        """
        results = []
        for peerid, connection in self.introducer_client.connections.iteritems():
            assert isinstance(peerid, str)
            permuted = bytes_to_long(sha.new(key + peerid).digest())
            results.append((permuted, peerid, connection))
        results.sort()
        return results

    def connected_to_vdrive(self):
        return self._connected_to_vdrive

    def connected_to_introducer(self):
        if self.introducer_client:
            return self.introducer_client.connected_to_introducer()
        return False
