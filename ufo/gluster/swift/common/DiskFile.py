# Copyright (c) 2012 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import errno
from eventlet import tpool
from tempfile import mkstemp
from contextlib import contextmanager
from swift.common.utils import normalize_timestamp, renamer
from swift.common.exceptions import DiskFileNotExist
from gluster.swift.common.utils import mkdirs, rmdirs, validate_object, \
     create_object_metadata,  do_open, do_close, do_unlink, do_chown, \
     do_stat, do_listdir, read_metadata, write_metadata
from gluster.swift.common.utils import X_CONTENT_TYPE, X_CONTENT_LENGTH, \
     X_TIMESTAMP, X_PUT_TIMESTAMP, X_TYPE, X_ETAG, X_OBJECTS_COUNT, \
     X_BYTES_USED, X_OBJECT_TYPE, FILE, DIR, MARKER_DIR, OBJECT, DIR_TYPE, \
     FILE_TYPE, DEFAULT_UID, DEFAULT_GID

import logging
from swift.obj.server import DiskFile


DATADIR = 'objects'
ASYNCDIR = 'async_pending'
KEEP_CACHE_SIZE = (5 * 1024 * 1024)
# keep these lower-case
DISALLOWED_HEADERS = set('content-length content-type deleted etag'.split())


class AlreadyExistsAsDir(Exception):
    pass


class Gluster_DiskFile(DiskFile):
    """
    Manage object files on disk.

    :param path: path to devices on the node/mount path for UFO.
    :param device: device name/account_name for UFO.
    :param partition: partition on the device the object lives in
    :param account: account name for the object
    :param container: container name for the object
    :param obj: object name for the object
    :param keep_data_fp: if True, don't close the fp, otherwise close it
    :param disk_chunk_Size: size of chunks on file reads
    :param uid: user ID disk object should assume (file or directory)
    :param gid: group ID disk object should assume (file or directory)
    """

    def __init__(self, path, device, partition, account, container, obj,
                 logger, keep_data_fp=False, disk_chunk_size=65536,
                 uid=DEFAULT_UID, gid=DEFAULT_GID):
        self.disk_chunk_size = disk_chunk_size
        #Don't support obj_name ending/begining with '/', like /a, a/, /a/b/ etc
        obj = obj.strip('/')
        if '/' in obj:
            self.obj_path, self.obj = obj.rsplit('/', 1)
        else:
            self.obj_path = ''
            self.obj = obj

        if self.obj_path:
            self.name = '/'.join((container, self.obj_path))
        else:
            self.name = container
        #Absolute path for obj directory.
        self.datadir = os.path.join(path, device, self.name)
        self.device_path = os.path.join(path, device)
        self.container_path = os.path.join(path, device, container)
        self.tmpdir = os.path.join(path, device, 'tmp')
        self.logger = logger
        self.metadata = {}
        self.data_file = None
        self.fp = None
        self.iter_etag = None
        self.started_at_0 = False
        self.read_to_eof = False
        self.quarantined_dir = None
        self.keep_cache = False
        self.is_dir = False
        self.is_valid = True
        self.uid = int(uid)
        self.gid = int(gid)
        if not os.path.exists(self.datadir + '/' + self.obj):
            return

        self.data_file = os.path.join(self.datadir, self.obj)
        self.metadata = read_metadata(self.datadir + '/' + self.obj)
        if not self.metadata:
            create_object_metadata(self.datadir + '/' + self.obj)
            self.metadata = read_metadata(self.datadir + '/' + self.obj)

        if not validate_object(self.metadata):
            create_object_metadata(self.datadir + '/' + self.obj)
            self.metadata = read_metadata(self.datadir + '/' +
                                        self.obj)

        self.filter_metadata()

        if os.path.isdir(self.datadir + '/' + self.obj):
            self.is_dir = True
        else:
            self.fp = do_open(self.data_file, 'rb')
            if not keep_data_fp:
                self.close(verify_file=False)

    def close(self, verify_file=True):
        """
        Close the file. Will handle quarantining file if necessary.

        :param verify_file: Defaults to True. If false, will not check
                            file to see if it needs quarantining.
        """
        #Marker directory
        if self.is_dir:
            return
        if self.fp:
            do_close(self.fp)
            self.fp = None

    def is_deleted(self):
        """
        Check if the file is deleted.

        :returns: True if the file doesn't exist or has been flagged as
                  deleted.
        """
        return not self.data_file

    def create_dir_object(self, dir_path):
        #TODO: if object already exists???
        if os.path.exists(dir_path) and not os.path.isdir(dir_path):
            self.logger.error("Deleting file %s", dir_path)
            do_unlink(dir_path)
        #If dir aleady exist just override metadata.
        mkdirs(dir_path)
        do_chown(dir_path, self.uid, self.gid)
        create_object_metadata(dir_path)

    def put_metadata(self, metadata):
        obj_path = self.datadir + '/' + self.obj
        write_metadata(obj_path, metadata)
        self.metadata = metadata

    def put(self, fd, tmppath, metadata, extension=''):
        """
        Finalize writing the file on disk, and renames it from the temp file to
        the real location.  This should be called after the data has been
        written to the temp file.

        :params fd: file descriptor of the temp file
        :param tmppath: path to the temporary file being used
        :param metadata: dictionary of metadata to be written
        :param extention: extension to be used when making the file
        """
        if extension == '.ts':
            # TombStone marker (deleted)
            return

        # Fix up the metadata to ensure it has a proper value for the
        # Content-Type metadata, as well as an X_TYPE and X_OBJECT_TYPE
        # metadata values.

        content_type = metadata['Content-Type']
        if not content_type:
            # FIXME: How can this be some object that evaluates to False?
            #
            # FIXME: If the file exists, we would already know it is a
            # directory.
            metadata['Content-Type'] = FILE_TYPE
            x_object_type = FILE
        else:
            x_object_type = MARKER_DIR if content_type.lower() == DIR_TYPE else FILE
        metadata[X_TYPE] = OBJECT
        metadata[X_OBJECT_TYPE] = x_object_type

        if extension == '.meta':
            # Metadata recorded separately from the file, we just update the
            # metadata for the file.
            #
            # FIXME: If the file does not exist, this call will fail.
            self.put_metadata(metadata)
            return

        # Our caller will use '.data' here; we just ignore it since we map the
        # URL directly to the file system.
        extension = ''

        if metadata[X_OBJECT_TYPE] == MARKER_DIR:
            # FIXME: If we know it already exists, why call
            # create_dir_object()?
            self.create_dir_object(os.path.join(self.datadir, self.obj))
            self.put_metadata(metadata)
            self.data_file = self.datadir + '/' + self.obj
            return

        # Check if directory already exists.
        if self.is_dir:
            # FIXME: How can we have a directory and it not be marked as a
            # MARKER_DIR (see above)?
            raise AlreadyExistsAsDir('File object already exists ' \
                        'as a directory: %s/%s' % (self.datadir , self.obj))

        timestamp = normalize_timestamp(metadata[X_TIMESTAMP])
        write_metadata(tmppath, metadata)
        if X_CONTENT_LENGTH in metadata:
            self.drop_cache(fd, 0, int(metadata[X_CONTENT_LENGTH]))
        tpool.execute(os.fsync, fd)
        if self.obj_path:
            dir_objs = self.obj_path.split('/')
            assert len(dir_objs) >= 1
            tmp_path = ''
            for dir_name in dir_objs:
                if tmp_path:
                    tmp_path = tmp_path + '/' + dir_name
                else:
                    tmp_path = dir_name
                self.create_dir_object(
                    os.path.join(self.container_path, tmp_path))

        renamer(tmppath, os.path.join(self.datadir,
                                      self.obj + extension))
        do_chown(os.path.join(self.datadir, self.obj + extension), \
              self.uid, self.gid)
        self.metadata = metadata
        self.data_file = self.datadir + '/' + self.obj + extension
        return

    def unlinkold(self, timestamp):
        """
        Remove any older versions of the object file.  Any file that has an
        older timestamp than timestamp will be deleted.

        :param timestamp: timestamp to compare with each file
        """
        if not self.metadata or self.metadata['X-Timestamp'] >= timestamp:
            return

        if self.is_dir:
            # Marker directory object
            if not rmdirs(os.path.join(self.datadir, self.obj)):
                logging.error('Unable to delete dir %s' % os.path.join(self.datadir, self.obj))
                return
        else:
            # File object
            for fname in do_listdir(self.datadir):
                if fname == self.obj:
                    do_unlink(os.path.join(self.datadir, fname))

        self.metadata = {}
        self.data_file = None

    def get_data_file_size(self):
        """
        Returns the os.path.getsize for the file.  Raises an exception if this
        file does not match the Content-Length stored in the metadata. Or if
        self.data_file does not exist.

        :returns: file size as an int
        :raises DiskFileError: on file size mismatch.
        :raises DiskFileNotExist: on file not existing (including deleted)
        """
        #Marker directory.
        if self.is_dir:
            return 0
        try:
            file_size = 0
            if self.data_file:
                file_size = os.path.getsize(self.data_file)
                if  X_CONTENT_LENGTH in self.metadata:
                    metadata_size = int(self.metadata[X_CONTENT_LENGTH])
                    if file_size != metadata_size:
                        self.metadata[X_CONTENT_LENGTH] = file_size
                        self.update_object(self.metadata)

                return file_size
        except OSError as err:
            if err.errno != errno.ENOENT:
                raise
        raise DiskFileNotExist('Data File does not exist.')

    def update_object(self, metadata):
        obj_path = self.datadir + '/' + self.obj
        write_metadata(obj_path, metadata)
        self.metadata = metadata

    def filter_metadata(self):
        if X_TYPE in self.metadata:
            self.metadata.pop(X_TYPE)
        if X_OBJECT_TYPE in self.metadata:
            self.metadata.pop(X_OBJECT_TYPE)

    @contextmanager
    def mkstemp(self):
        """Contextmanager to make a temporary file."""

        if not os.path.exists(self.tmpdir):
            mkdirs(self.tmpdir)
        fd, tmppath = mkstemp(dir=self.tmpdir)
        try:
            yield fd, tmppath
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(tmppath)
            except OSError:
                pass
