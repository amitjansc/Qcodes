import multiprocessing as mp
import sys
from datetime import datetime
import time
from traceback import print_exc
from queue import Empty

from .helpers import in_notebook

MP_ERR = 'context has already been set'


def set_mp_method(method, force=False):
    '''
    an idempotent wrapper for multiprocessing.set_start_method
    The most important use of this is to force Windows behavior
    on a Mac or Linux: set_mp_method('spawn')
    args are the same:

    method: one of:
        'fork' (default on unix/mac)
        'spawn' (default, and only option, on windows)
        'forkserver'
    force: allow changing context? default False
        in the original function, even calling the function again
        with the *same* method raises an error, but here we only
        raise the error if you *don't* force *and* the context changes
    '''
    try:
        mp.set_start_method(method, force=force)
    except RuntimeError as err:
        if err.args != (MP_ERR, ):
            raise

    mp_method = mp.get_start_method()
    if mp_method != method:
        raise RuntimeError(
            'unexpected multiprocessing method '
            '\'{}\' when trying to set \'{}\''.format(mp_method, method))


class QcodesProcess(mp.Process):
    '''
    modified multiprocessing.Process for nicer printing and automatic
    streaming of stdout and stderr to our StreamQueue singleton

    name: string to include in repr, and in the StreamQueue
        default 'QcodesProcess'
    queue_streams: should we connect stdout and stderr to the StreamQueue?
        default True
    daemon: should this process be treated as daemonic, so it gets terminated
        with the parent.
        default True, overriding the base inheritance
    any other args and kwargs are passed to multiprocessing.Process
    '''
    def __init__(self, *args, name='QcodesProcess', queue_streams=True,
                 daemon=True, **kwargs):
        # make sure the singleton StreamQueue exists
        # prior to launching a new process
        if queue_streams and in_notebook():
            self.stream_queue = get_stream_queue()
        else:
            self.stream_queue = None
        super().__init__(*args, name=name, daemon=daemon, **kwargs)

    def run(self):
        if self.stream_queue:
            self.stream_queue.connect(str(self.name))
        try:
            super().run()
        except:
            # if we let the system print the exception by itself, sometimes
            # it disconnects the stream partway through printing.
            print_exc()
        finally:
            if (self.stream_queue and
                    self.stream_queue.initial_streams is not None):
                self.stream_queue.disconnect()

    def __repr__(self):
        cname = self.__class__.__name__
        r = super().__repr__()
        r = r.replace(cname + '(', '').replace(')>', '>')
        return r.replace(', started daemon', '')


def get_stream_queue():
    '''
    convenience function to get a singleton StreamQueue
    note that this must be called from the main process before starting any
    subprocesses that will use it, otherwise the subprocess will create its
    own StreamQueue that no other processes know about
    '''
    if StreamQueue.instance is None:
        StreamQueue.instance = StreamQueue()
    return StreamQueue.instance


class StreamQueue:
    '''
    Do not instantiate this directly: use get_stream_queue so we only make one.

    Redirect child process stdout and stderr to a queue

    One StreamQueue should be created in the consumer process, and passed
    to each child process.

    In the child, we call StreamQueue.connect with a process name that will be
    unique and meaningful to the user

    The consumer then periodically calls StreamQueue.get() to read these
    messages

    inspired by http://stackoverflow.com/questions/23947281/
    '''
    instance = None

    def __init__(self, *args, **kwargs):
        self.queue = mp.Queue(*args, **kwargs)
        self.last_read_ts = mp.Value('d', time.time())
        self._last_stream = None
        self._on_new_line = True
        self.lock = mp.RLock()
        self.initial_streams = None

    def connect(self, process_name):
        if self.initial_streams is not None:
            raise RuntimeError('StreamQueue is already connected')

        self.initial_streams = (sys.stdout, sys.stderr)

        sys.stdout = _SQWriter(self, process_name)
        sys.stderr = _SQWriter(self, process_name + ' ERR')

    def disconnect(self):
        if self.initial_streams is None:
            raise RuntimeError('StreamQueue is not connected')
        sys.stdout, sys.stderr = self.initial_streams
        self.initial_streams = None

    def get(self):
        out = ''
        while not self.queue.empty():
            timestr, stream_name, msg = self.queue.get()
            line_head = '[{} {}] '.format(timestr, stream_name)

            if self._on_new_line:
                out += line_head
            elif stream_name != self._last_stream:
                out += '\n' + line_head

            out += msg[:-1].replace('\n', '\n' + line_head) + msg[-1]

            self._on_new_line = (msg[-1] == '\n')
            self._last_stream = stream_name

        self.last_read_ts.value = time.time()
        return out


class _SQWriter:
    MIN_READ_TIME = 3

    def __init__(self, stream_queue, stream_name):
        self.queue = stream_queue.queue
        self.last_read_ts = stream_queue.last_read_ts
        self.stream_name = stream_name

    def write(self, msg):
        try:
            if msg:
                msgtuple = (datetime.now().strftime('%H:%M:%S.%f')[:-3],
                            self.stream_name, msg)
                self.queue.put(msgtuple)

                queue_age = time.time() - self.last_read_ts.value
                if queue_age > self.MIN_READ_TIME and msg != '\n':
                    # long time since the queue was read? maybe nobody is
                    # watching it at all - send messages to the terminal too
                    # but they'll still be in the queue if someone DOES look.
                    termstr = '[{} {}] {}'.format(*msgtuple)
                    # we always want a new line this way (so I don't use
                    # end='' in the print) but we don't want an extra if the
                    # caller already included a newline.
                    if termstr[-1] == '\n':
                        termstr = termstr[:-1]
                    try:
                        print(termstr, file=sys.__stdout__)
                    except ValueError:  # pragma: no cover
                        # ValueError: underlying buffer has been detached
                        # this may just occur in testing on Windows, not sure.
                        pass
        except:
            # don't want to get an infinite loop if there's something wrong
            # with the queue - put the regular streams back before handling
            sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
            raise

    def flush(self):
        pass


class ServerManager:
    '''
    creates and manages connections to a separate server process

    name: the name of the server
    query_timeout: the default time to wait for responses
    kwargs: passed along to the server constructor
    '''
    def __init__(self, name, server_class, server_extras={}, query_timeout=2):
        self.name = name
        self._query_queue = mp.Queue()
        self._response_queue = mp.Queue()
        self._error_queue = mp.Queue()
        self._server_class = server_class
        self._server_extras = server_extras

        # query_lock is only used with queries that get responses
        # to make sure the process that asked the question is the one
        # that gets the response.
        # Any query that does NOT expect a response can just dump it in
        # and more on.
        self.query_lock = mp.RLock()

        self.query_timeout = query_timeout
        self._start_server(name)

    def _start_server(self, name):
        self._server = QcodesProcess(target=self._run_server, name=name)
        self._server.start()

    def _run_server(self):
        self._server_class(self._query_queue, self._response_queue,
                           self._error_queue, self._server_extras)

    def write(self, *query):
        '''
        Send a query to the server that does not expect a response.
        '''
        self._query_queue.put(query)
        self._check_for_errors()

    def _check_for_errors(self):
        if not self._error_queue.empty():
            errstr = self._error_queue.get()
            errhead = '*** error on {} ***'.format(self.name)
            print(errhead + '\n\n' + errstr, file=sys.stderr)
            raise RuntimeError(errhead)

    def ask(self, *query, timeout=None):
        '''
        Send a query to the server and wait for a response
        '''
        timeout = timeout or self.query_timeout

        with self.query_lock:
            self._query_queue.put(query)
            try:
                res = self._response_queue.get(timeout=timeout)
            except Empty as e:
                if self._error_queue.empty():
                    # only raise if we're not about to find a deeper error
                    raise e
            self._check_for_errors()

            return res

    def halt(self):
        '''
        Halt the server and end its process
        '''
        if self._server.is_alive():
            self.ask('halt')
        self._server.join()

    def restart(self):
        '''
        Restart the server
        '''
        self.halt()
        self._start_server()
