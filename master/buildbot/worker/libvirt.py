# This file is part of Buildbot.  Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Portions Copyright Buildbot Team Members
# Portions Copyright 2010 Isotoma Limited

import os

from twisted.internet import defer
from twisted.internet import utils
from twisted.python import log

from buildbot import config
from buildbot.interfaces import LatentWorkerFailedToSubstantiate
from buildbot.util.queue import ConnectableThreadQueue
from buildbot.warnings import warn_deprecated
from buildbot.worker import AbstractLatentWorker

try:
    import libvirt
except ImportError:
    libvirt = None


def handle_connect_close(conn, reason, opaque):
    opaque.close_connection()


class ThreadWithQueue(ConnectableThreadQueue):
    def __init__(self, pool, uri, *args, **kwargs):
        self.pool = pool  # currently used only for testing
        self.uri = uri
        super().__init__(*args, **kwargs)

    def on_close_connection(self, conn):
        self.close_connection()

    def close_connection(self):
        conn = self.conn
        super().close_connection()
        conn.close()

    def libvirt_open(self):
        return libvirt.open(self.uri)

    def create_connection(self):
        try:
            log.msg("Connecting to {}".format(self.uri))
            conn = self.libvirt_open()
            conn.registerCloseCallback(handle_connect_close, self)
            log.msg("Connected to {}".format(self.uri))
            return conn
        except Exception as e:
            log.err("Error connecting to {}: {}, will retry later".format(self.uri, e))
            return None


class ServerThreadPool:
    ThreadClass = ThreadWithQueue

    def __init__(self):
        self.threads = {}

    def do(self, uri, func, *args, **kwargs):
        # returns a Deferred
        if uri not in self.threads:
            self.threads[uri] = self.ThreadClass(self, uri)

        def logging_func(conn, *args, **kwargs):
            try:
                return func(conn, *args, **kwargs)
            except Exception as e:
                log.err("libvirt: Exception on {}: {}".format(uri, str(e)))
                raise

        return self.threads[uri].execute_in_thread(logging_func, *args, **kwargs)

    def is_connected(self, uri):
        if uri in self.threads:
            return self.threads[uri].conn is not None
        return False

    def is_connecting(self, uri):
        if uri in self.threads:
            return self.threads[uri].connecting
        return False

    @defer.inlineCallbacks
    def get_or_create_connection(self, uri):
        if uri not in self.threads:
            yield self.do(uri, lambda: None)
        return self.threads[uri].conn

    def reset_connection(self, uri):
        if uri in self.threads:
            self.threads[uri].close_connection()
        else:
            log.err('libvirt.ServerThreadPool: Unknown connection {}'.format(uri))


# A module is effectively a singleton class, so this is OK
threadpool = ServerThreadPool()


class Connection:
    def __init__(self, uri):
        self.uri = uri


class LibVirtWorker(AbstractLatentWorker):
    pool = threadpool

    def __init__(self, name, password, connection=None, hd_image=None, base_image=None,
                 uri="system:///", xml=None, **kwargs):
        super().__init__(name, password, **kwargs)
        if not libvirt:
            config.error(
                "The python module 'libvirt' is needed to use a LibVirtWorker")

        if connection is not None:
            warn_deprecated('3.2.0', 'LibVirtWorker connection argument has been deprecated: ' +
                            'please use uri')
            if uri != "system:///":
                config.error('connection and uri arguments cannot be used together')
            uri = connection.uri

        self.uri = uri
        self.image = hd_image
        self.base_image = base_image
        self.xml = xml

        self.cheap_copy = True
        self.graceful_shutdown = False

    def _pool_do(self, func):
        return self.pool.do(self.uri, func)

    @defer.inlineCallbacks
    def _get_domain(self):
        try:
            domain = yield self._pool_do(lambda conn: conn.lookupByName(self.workername))
            return domain
        except libvirt.libvirtError as e:
            log.err('LibVirtWorker: got error when accessing domain: {}'.format(e))
            try:
                self.pool.reset_connection(self.uri)
            except Exception as e1:
                log.err('LibVirtWorker: got error when resetting connection: {}'.format(e1))
            raise e

    @defer.inlineCallbacks
    def _get_domain_id(self):
        domain = yield self._get_domain()
        if domain is None:
            return -1
        domain_id = yield self._pool_do(lambda conn: domain.ID())
        return domain_id

    @defer.inlineCallbacks
    def _prepare_base_image(self):
        """
        I am a private method for creating (possibly cheap) copies of a
        base_image for start_instance to boot.
        """
        if not self.base_image:
            return

        if self.cheap_copy:
            clone_cmd = "qemu-img"
            clone_args = "create -b %(base)s -f qcow2 %(image)s"
        else:
            clone_cmd = "cp"
            clone_args = "%(base)s %(image)s"

        clone_args = clone_args % {
            "base": self.base_image,
            "image": self.image,
        }

        log.msg("Cloning base image: {} {}'".format(clone_cmd, clone_args))

        try:
            rc = yield utils.getProcessValue(clone_cmd, clone_args.split())
            log.msg("Cloning exit code was: {}".format(rc))
        except Exception as e:
            log.err("Cloning failed: {}".format(e))
            raise

    @defer.inlineCallbacks
    def start_instance(self, build):
        """
        I start a new instance of a VM.

        If a base_image is specified, I will make a clone of that otherwise i will
        use image directly.

        If i'm not given libvirt domain definition XML, I will look for my name
        in the list of defined virtual machines and start that.
        """

        try:
            domain_id = yield self._get_domain_id()
            if domain_id != -1:
                raise LatentWorkerFailedToSubstantiate(
                    "{}: Cannot start_instance as it's already active".format(self))
        except Exception as e:
            raise LatentWorkerFailedToSubstantiate(
                '{}: Got error while retrieving domain ID: {}'.format(self, e))

        yield self._prepare_base_image()

        try:
            if self.xml:
                yield self._pool_do(lambda conn: conn.createXML(self.xml, 0))
            else:
                domain = yield self._get_domain()
                yield self._pool_do(lambda conn: domain.create())

        except Exception as e:
            raise LatentWorkerFailedToSubstantiate(
                '{}: Got error while starting VM: {}'.format(self, e))

        return True

    @defer.inlineCallbacks
    def stop_instance(self, fast=False):
        """
        I attempt to stop a running VM.
        I make sure any connection to the worker is removed.
        If the VM was using a cloned image, I remove the clone
        When everything is tidied up, I ask that bbot looks for work to do
        """

        domain_id = yield self._get_domain_id()
        if domain_id == -1:
            log.msg("{}: Domain is unexpectedly not running".format(self))
            return

        domain = yield self._get_domain()

        if self.graceful_shutdown and not fast:
            log.msg("Graceful shutdown chosen for {}".format(self.workername))
            try:
                yield self._pool_do(lambda conn: domain.shutdown())
            except Exception as e:
                log.msg('{}: Graceful shutdown failed ({}). Force destroying domain'.format(
                    self, e))
                # Don't re-throw to stop propagating shutdown error if destroy was successful.
                yield self._pool_do(lambda conn: domain.destroy())

        else:
            yield self._pool_do(lambda conn: domain.destroy())

        if self.base_image:
            log.msg('{}: Removing image {}'.format(self, self.image))
            os.remove(self.image)
