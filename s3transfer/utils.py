# Copyright 2016 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.
import random
import time
import functools
import os
import string
import logging
import threading
from collections import defaultdict

from s3transfer.compat import rename_file


logger = logging.getLogger(__name__)


def unique_id(name):
    """
    Generate a unique ID that includes the given name,
    a timestamp and a random number.
    """
    return '{0}-{1}-{2}'.format(name, int(time.time()),
                                random.randint(0, 10000))


def random_file_extension(num_digits=8):
    return ''.join(random.choice(string.hexdigits) for _ in range(num_digits))


def disable_upload_callbacks(request, operation_name, **kwargs):
    if operation_name in ['PutObject', 'UploadPart'] and \
            hasattr(request.body, 'disable_callback'):
        request.body.disable_callback()


def enable_upload_callbacks(request, operation_name, **kwargs):
    if operation_name in ['PutObject', 'UploadPart'] and \
            hasattr(request.body, 'enable_callback'):
        request.body.enable_callback()


def calculate_range_parameter(part_size, part_index, num_parts,
                              total_size=None):
    """Calculate the range parameter for multipart downloads/copies

    :type part_size: int
    :param part_size: The size of the part

    :type part_index: int
    :param part_index: The index for which this parts starts. This index starts
        at zero

    :type num_parts: int
    :param num_parts: The total number of parts in the transfer

    :returns: The value to use for Range parameter on downloads or
        the CopySourceRange parameter for copies
    """
    # Used to calculate the Range parameter
    start_range = part_index * part_size
    if part_index == num_parts - 1:
        end_range = ''
        if total_size is not None:
            end_range = str(total_size - 1)
    else:
        end_range = start_range + part_size - 1
    range_param = 'bytes=%s-%s' % (start_range, end_range)
    return range_param


def get_callbacks(transfer_future, callback_type):
    """Retrieves callbacks from a subscriber

    :type transfer_future: s3transfer.futures.TransferFuture
    :param transfer_future: The transfer future the subscriber is associated
        to.

    :type callback_type: str
    :param callback_type: The type of callback to retrieve from the subscriber.
        Valid types include:
            * 'queued'
            * 'progress'
            * 'done'

    :returns: A list of callbacks for the type specified. All callbacks are
        preinjected with the transfer future.
    """
    callbacks = []
    for subscriber in transfer_future.meta.call_args.subscribers:
        callback_name = 'on_' + callback_type
        if hasattr(subscriber, callback_name):
            callbacks.append(
                functools.partial(
                    getattr(subscriber, callback_name),
                    future=transfer_future
                )
            )
    return callbacks


def invoke_progress_callbacks(callbacks, bytes_transferred):
    """Calls all progress callbacks

    :param callbacks: A list of progress callbacks to invoke
    :param bytes_transferred: The number of bytes transferred. This is passed
        to the callbacks. If no bytes were transferred the callbacks will not
        be invoked because no progress was achieved. It is also possible
        to receive a negative amount which comes from retrying a transfer
        request.
    """
    # Only invoke the callbacks if bytes were actually transferred.
    if bytes_transferred:
        for callback in callbacks:
            callback(bytes_transferred=bytes_transferred)


class CallArgs(object):
    def __init__(self, **kwargs):
        """A class that records call arguments

        The call arguments must be passed as keyword arguments. It will set
        each keyword argument as an attribute of the object along with its
        associated value.
        """
        for arg, value in kwargs.items():
            setattr(self, arg, value)


class FunctionContainer(object):
    """An object that contains a function and any args or kwargs to call it

    When called the provided function will be called with provided args
    and kwargs.
    """
    def __init__(self, func, *args, **kwargs):
        self._func = func
        self._args = args
        self._kwargs = kwargs

    def __repr__(self):
        return 'Function: %s with args %s and kwargs %s' % (
            self._func, self._args, self._kwargs)

    def __call__(self):
        return self._func(*self._args, **self._kwargs)


class OSUtils(object):
    def get_file_size(self, filename):
        return os.path.getsize(filename)

    def open_file_chunk_reader(self, filename, start_byte, size, callbacks):
        return ReadFileChunk.from_filename(filename, start_byte,
                                           size, callbacks,
                                           enable_callbacks=False)

    def open_file_chunk_reader_from_fileobj(self, fileobj, chunk_size,
                                            full_file_size, callbacks):
        return ReadFileChunk(
            fileobj, chunk_size, full_file_size,
            callbacks=callbacks, enable_callbacks=False)

    def open(self, filename, mode):
        return open(filename, mode)

    def remove_file(self, filename):
        """Remove a file, noop if file does not exist."""
        # Unlike os.remove, if the file does not exist,
        # then this method does nothing.
        try:
            os.remove(filename)
        except OSError:
            pass

    def rename_file(self, current_filename, new_filename):
        rename_file(current_filename, new_filename)


class DeferredOpenFile(object):
    OPEN_METHOD = open

    def __init__(self, filename, start_byte=0):
        """A class that defers the opening of a file till needed

        This is useful for deffering opening of a file till it is needed
        in a separate thread, as there is a limit of how many open files
        there can be in a single thread for most operating systems. The
        file gets opened in the following methods: ``read()``, ``seek()``,
        and ``__enter__()``

        :type filename: str
        :param filename: The name of the file to open

        :type start_byte: int
        :param start_byte: The byte to seek to when the file is opened.
        """
        self._filename = filename
        self._fileobj = None
        self._start_byte = start_byte

    def _open_if_needed(self):
        if self._fileobj is None:
            self._fileobj = self.OPEN_METHOD(self._filename, 'rb')
            self._fileobj.seek(self._start_byte)

    def read(self, amount=None):
        self._open_if_needed()
        return self._fileobj.read(amount)

    def seek(self, where):
        self._open_if_needed()
        self._fileobj.seek(where)

    def tell(self):
        if self._fileobj is None:
            return self._start_byte
        return self._fileobj.tell()

    def close(self):
        if self._fileobj:
            self._fileobj.close()

    def __enter__(self):
        self._open_if_needed()
        return self

    def __exit__(self, *args, **kwargs):
        self.close()


class ReadFileChunk(object):
    def __init__(self, fileobj, chunk_size, full_file_size,
                 callbacks=None, enable_callbacks=True):
        """

        Given a file object shown below::

            |___________________________________________________|
            0          |                 |                 full_file_size
                       |----chunk_size---|
                    f.tell()

        :type fileobj: file
        :param fileobj: File like object

        :type chunk_size: int
        :param chunk_size: The max chunk size to read.  Trying to read
            pass the end of the chunk size will behave like you've
            reached the end of the file.

        :type full_file_size: int
        :param full_file_size: The entire content length associated
            with ``fileobj``.

        :type callbacks: A list of function(amount_read)
        :param callbacks: Called whenever data is read from this object in the
            order provided.

        """
        self._fileobj = fileobj
        self._start_byte = self._fileobj.tell()
        self._size = self._calculate_file_size(
            self._fileobj, requested_size=chunk_size,
            start_byte=self._start_byte, actual_file_size=full_file_size)
        self._amount_read = 0
        self._callbacks = callbacks
        if callbacks is None:
            self._callbacks = []
        self._callbacks_enabled = enable_callbacks

    @classmethod
    def from_filename(cls, filename, start_byte, chunk_size, callbacks=None,
                      enable_callbacks=True):
        """Convenience factory function to create from a filename.

        :type start_byte: int
        :param start_byte: The first byte from which to start reading.

        :type chunk_size: int
        :param chunk_size: The max chunk size to read.  Trying to read
            pass the end of the chunk size will behave like you've
            reached the end of the file.

        :type full_file_size: int
        :param full_file_size: The entire content length associated
            with ``fileobj``.

        :type callbacks: function(amount_read)
        :param callbacks: Called whenever data is read from this object.

        :type enable_callbacks: bool
        :param enable_callbacks: Indicate whether to invoke callback
            during read() calls.

        :rtype: ``ReadFileChunk``
        :return: A new instance of ``ReadFileChunk``

        """
        f = open(filename, 'rb')
        f.seek(start_byte)
        file_size = os.fstat(f.fileno()).st_size
        return cls(f, chunk_size, file_size, callbacks, enable_callbacks)

    def _calculate_file_size(self, fileobj, requested_size, start_byte,
                             actual_file_size):
        max_chunk_size = actual_file_size - start_byte
        return min(max_chunk_size, requested_size)

    def read(self, amount=None):
        if amount is None:
            amount_to_read = self._size - self._amount_read
        else:
            amount_to_read = min(self._size - self._amount_read, amount)
        data = self._fileobj.read(amount_to_read)
        self._amount_read += len(data)
        if self._callbacks is not None and self._callbacks_enabled:
            invoke_progress_callbacks(self._callbacks, len(data))
        return data

    def enable_callback(self):
        self._callbacks_enabled = True

    def disable_callback(self):
        self._callbacks_enabled = False

    def seek(self, where):
        self._fileobj.seek(self._start_byte + where)
        if self._callbacks is not None and self._callbacks_enabled:
            # To also rewind the callback() for an accurate progress report
            invoke_progress_callbacks(
                self._callbacks, bytes_transferred=where - self._amount_read)
        self._amount_read = where

    def close(self):
        self._fileobj.close()

    def tell(self):
        return self._amount_read

    def __len__(self):
        # __len__ is defined because requests will try to determine the length
        # of the stream to set a content length.  In the normal case
        # of the file it will just stat the file, but we need to change that
        # behavior.  By providing a __len__, requests will use that instead
        # of stat'ing the file.
        return self._size

    def __enter__(self):
        return self

    def __exit__(self, *args, **kwargs):
        self.close()

    def __iter__(self):
        # This is a workaround for http://bugs.python.org/issue17575
        # Basically httplib will try to iterate over the contents, even
        # if its a file like object.  This wasn't noticed because we've
        # already exhausted the stream so iterating over the file immediately
        # stops, which is what we're simulating here.
        return iter([])


class StreamReaderProgress(object):
    """Wrapper for a read only stream that adds progress callbacks."""
    def __init__(self, stream, callbacks=None):
        self._stream = stream
        self._callbacks = callbacks
        if callbacks is None:
            self._callbacks = []

    def read(self, *args, **kwargs):
        value = self._stream.read(*args, **kwargs)
        invoke_progress_callbacks(self._callbacks, len(value))
        return value


class NoResourcesAvailable(Exception):
    pass


class SlidingWindowSemaphore(object):
    """A semaphore used to coordinate sequential resource access.

    This class is similar to the stdlib BoundedSemaphore:

    * It's initialized with a count.
    * Each call to ``acquire()`` decrements the counter.
    * If the count is at zero, then ``acquire()`` will either block until the
      count increases, or if ``blocking=False``, then it will raise
      a NoResourcesAvailable exception indicating that it failed to acquire the
      semaphore.

    The main difference is that this semaphore is used to limit
    access to a resource that requires sequential access.  For example,
    if I want to access resource R that has 20 subresources R_0 - R_19,
    this semaphore can also enforce that you only have a max range of
    10 at any given point in time.  You must also specify a tag name
    when you acquire the semaphore.  The sliding window semantics apply
    on a per tag basis.  The internal count will only be incremented
    when the minimum sequence number for a tag is released.

    """
    def __init__(self, count):
        self._count = count
        # Dict[tag, next_sequence_number].
        self._tag_sequences = defaultdict(int)
        self._lowest_sequence = {}
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        # Dict[tag, List[sequence_number]]
        self._pending_release = {}

    def current_count(self):
        with self._lock:
            return self._count

    def acquire(self, tag, blocking=True):
        logger.debug("Acquiring %s", tag)
        self._condition.acquire()
        try:
            if self._count == 0:
                if not blocking:
                    raise NoResourcesAvailable("Cannot acquire tag '%s'" % tag)
                else:
                    while self._count == 0:
                        self._condition.wait()
            # self._count is no longer zero.
            # First, check if this is the first time we're seeing this tag.
            sequence_number = self._tag_sequences[tag]
            if sequence_number == 0:
                # First time seeing the tag, so record we're at 0.
                self._lowest_sequence[tag] = sequence_number
            self._tag_sequences[tag] += 1
            self._count -= 1
            return sequence_number
        finally:
            self._condition.release()

    def release(self, tag, sequence_number):
        logger.debug("Releasing acquire %s/%s", tag, sequence_number)
        self._condition.acquire()
        try:
            if tag not in self._tag_sequences:
                raise ValueError("Attempted to release unknown tag: %s" % tag)
            max_sequence = self._tag_sequences[tag]
            if self._lowest_sequence[tag] == sequence_number:
                # We can immediately process this request and free up
                # resources.
                self._lowest_sequence[tag] += 1
                self._count += 1
                self._condition.notify()
                queued = self._pending_release.get(tag, [])
                while queued:
                    if self._lowest_sequence[tag] == queued[-1]:
                        queued.pop()
                        self._lowest_sequence[tag] += 1
                        self._count += 1
                    else:
                        break
            elif self._lowest_sequence[tag] < sequence_number < max_sequence:
                # We can't do anything right now because we're still waiting
                # for the min sequence for the tag to be released.  We have
                # to queue this for pending release.
                self._pending_release.setdefault(tag, []).append(sequence_number)
                self._pending_release[tag].sort(reverse=True)
            else:
                raise ValueError("Attempted to release unknown sequence number "
                                 "%s for tag: %s" % (sequence_number, tag))
        finally:
            self._condition.release()