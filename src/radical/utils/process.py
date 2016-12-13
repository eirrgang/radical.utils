
__author__    = "Radical.Utils Development Team"
__copyright__ = "Copyright 2016, RADICAL@Rutgers"
__license__   = "MIT"


import os
import sys
import time
import Queue
import select
import socket
import signal
import threading       as mt
import multiprocessing as mp

from .debug  import print_stacktrace, print_stacktraces
from .logger import get_logger

_ALIVE_MSG     = 'alive'  # message to use as alive signal
_ALIVE_TIMEOUT = 5.0      # time to wait for process startup signal.
                          # startup signal: 'alive' message on the socketpair;
                          # is sent in both directions to ensure correct setup
_WATCH_TIMEOUT = 0.5      # time between thread and process health polls.
                          # health poll: check for recv, error and abort
                          # on the socketpair; is done in a watcher thread.
_START_TIMEOUT = 5.0      # time between starting child and finding it alive
_STOP_TIMEOUT  = 5.0      # time between temination signal and killing child
_BUFSIZE       = 1024     # default buffer size for socket recvs


# ------------------------------------------------------------------------------
#
# This Process class is a thin wrapper around multiprocessing.Process which
# specifically manages the process lifetime in a more cautious and copperative
# way than the base class:
#
#  - self._rup_term  signals child to terminate voluntarily before being killed
#  - self.work()     is the childs main loop (must be overloaded), and is called
#                    as long as self._rup_term is not set
#  - *no* attempt on signal handling is made, we expect to exclusively
#    communicate via the above events.
#
# NOTE: Only the watcher thread is ever closeing the socket endpoint, so only
#       the watcher can send messages over that EP w/o races.  We thus create
#       a private message queue all threads can write to, and which will be
#       forwarded to the EP by the watcher -- and ognored after EP close.
# NOTE: At this point we do not implement the full mp.Process constructor.
#
#
# Semantics:
#
#   def start(timeout): mp.Process.start()
#                       self._alive.wait(timeout)
#   def run(self):      # must NOT be overloaded
#                         self.initialize() # overload
#                         self._alive.set()
#
#                         while True:
#                           if self._rup_term.is_set():
#                             break
#
#                           if not parent.is_alive():
#                             break
#
#                           try:
#                               self.work()
#                           except:
#                             break
#
#                         self.finalize()  # overload
#                         sys.exit()
#
#   def stop():         self._rup_term.set()
#   def terminate():    mp.Process.terminate()
#   def join(timeout):  mp.Process.join(timeout)
#
# TODO: We should switch to fork/*exec*, if possible.
#
# TODO: At the moment, we receive messages from the other process, log it, and
#       then forget about it.  We should keep a stack or queue of those messages
#       to be consumed by the application, so that the channel can be used for
#       a parent/child communication protocol.  
#       We could even implement such a protocol framework, if the need for such
#       arises.
#
class Process(mp.Process):

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, log=None):

        # At this point, we only initialize members which we need before fork...
        self._rup_log       = log            # ru.logger for debug output

        # ... or are *shared* between parent and child process.
        self._rup_ppid      = os.getpid()    # get parent process ID
        self._rup_timeout   = _ALIVE_TIMEOUT # interval to check peer health

        # most importantly, we create a socketpair.  Both parent and child will
        # watch one end of that socket, which thus acts as a lifeline between
        # the processes, to detect abnormal termination in the process tree.
        # The socket is also used to send messages back and forth.
        self._rup_sp = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM, 0)

        # all others are set to None to make pylint happy, but are actually
        # initialized later in `start()`/`run()`, and the various initializers.
        # `initialize_parent()` and `initialize_child()`.
        
        self._rup_is_parent = None           # set in start()
        self._rup_is_child  = None           # set in run()
        self._rup_endpoint  = None           # socket endpoint for sent/recv
        self._rup_term      = None           # set to terminate watcher
        self._rup_watcher   = None           # watcher thread

        if not self._rup_log:
            # if no logger is passed down, log to stdout
            self._rup_log = get_logger('radical.util.process', target='null')

        mp.Process.__init__(self, name=name)

        # we don't want processes to linger around, waiting for children to
        # terminate, and thus create sub-processes as daemons.
        #
        # NOTE: this requires the `patchy.mc_patchface()` fix in `start()`.
      # self.daemon = True


    # --------------------------------------------------------------------------
    #
    def _rup_msg_send(self, msg):
        '''
        send new message to self._rup_endpoint.  We make sure that the
        message is not larger than the _BUFSIZE define in the RU source code.
        '''

        # NOTE:  this method should only be called by the watcher thread, which
        #        owns the endpoint.
        # FIXME: BUFSIZE should not be hardcoded
        # FIXME: check for socket health

        if len(msg) > _BUFSIZE:
            raise ValueError('message is larger than %s: %s' % (_BUFSIZE, msg))

      # print 'parent?%s sends msg %s' % (self._rup_is_parent, msg)
        try:
            self._rup_log.info('send message: %s', msg)
            self._rup_endpoint.send('%s\n' % msg)
        except Exception as e:
            self._rup_log.exception('failed to send message %s', msg)


    # --------------------------------------------------------------------------
    #
    def _rup_msg_recv(self, size=_BUFSIZE):
        '''
        receive a message from self._rup_endpoint.  We only check for messages
        of *up to* `size`.  This call is non-blocking: if no message is
        available, return an empty string.
        '''

        # NOTE:  this method should only be called by the watcher thread, which
        #        owns the endpoint (no lock used!)
        # FIXME: BUFSIZE should not be hardcoded
        # FIXME: check for socket health

        try:
            msg = self._rup_endpoint.recv(size, socket.MSG_DONTWAIT)
            self._rup_log.info('recv message: %s', msg)
            return msg
        except Exception as e:
            self._rup_log.exception('failed to recv message')
            return ''


    # --------------------------------------------------------------------------
    #
    def _rup_watch(self):
        '''
        When `start()` is called, the parent process will create a socket pair.
        after fork, one end of that pair will be watched by the parent and
        client, respectively, in separate watcher threads.  If any error
        condition or hangup is detected on the socket, it is assumed that the
        process on the other end died, and termination is initiated.

        Since the watch happens in a subthread, any termination requires the
        attention and cooperation of the main thread.  No attempt is made on
        interrupting the main thread, we only set self._rup_term which needs to
        be checked by the main threads in certain intervals.
        '''

        # for alive checks, we poll socket state for
        #   * data:   some message from the other end, logged
        #   * error:  child failed   - terminate
        #   * hangup: child finished - terminate
        # Basically, whatever happens, we terminate... :-P
        poller = select.poll()
        poller.register(self._rup_endpoint, select.POLLERR | select.POLLHUP | select.POLLIN)

        # we watch threads and processes as long as we live, and also take care
        # of messages to be sent.
        #
        # NOTE: messages are usually delayed by _WATCH_TIMEOUT seconds, and
        #       should not be used for time-critical communication
        try:
            while not self._rup_term.is_set() :

                time.sleep(0.3) # FIXME
              
                # first check health of parent/child relationship
                events = poller.poll(_WATCH_TIMEOUT)
                for _,event in events:

                    # check for error conditions
                    if  event & select.POLLHUP or  \
                        event & select.POLLERR     :

                        # something happened on the other end, we are about to die
                        # out of solidarity (or panic?).  
                        self._rup_log.warn('endpoint disappeard')
                        raise RuntimeError('endpoint disappeard')
                    
                    # check for messages
                    elif event & select.POLLIN:
                
                        # Lets first though check if we
                        # can get any information about the remote termination cause.
                        #
                        # FIXME: BUFSIZE should not be hardcoded
                        # FIXME: we do nothing with the message yet, should be
                        #        stored in a message queue.
                        msg = self._rup_msg_recv(_BUFSIZE)
                        self._rup_log.info('message received: %s' % msg)

                    # FIXME: also *send* any pending messages to the child.
                  # # check if any messages need to be sent.  
                  # while True:
                  #     try:
                  #         msg = self._rup_msg_out.get_nowait()
                  #         print 'out: %s' % msg
                  #         self._rup_msg_send(msg)
                  #
                  #     except Queue.Empty:
                  #         # nothing more to send
                  #         break


        except Exception as e:
            # mayday... mayday...
            self._rup_log.exception('watcher failed')

        finally:
            # no matter why we fell out of the loop: terminate the child by
            # closing the socketpair.
            self._rup_endpoint.close()

            # `self.stop()` will be called from the main thread upon checking
            # `self._rup_term` via `self.is_alive()`.
            # FIXME: check
            self._rup_term.set()



    # --------------------------------------------------------------------------
    #
    def start(self, timeout=_START_TIMEOUT):
        '''
        Overload the `mp.Process.start()` method, and block (with timeout) until
        the child signals to be alive via a message over our socket pair.
        '''

        self._rup_log.debug('start process')

        if timeout != None:
            self._rup_timeout = timeout

        # Daemon processes can't fork child processes in Python, because...
        # Well, they just can't.  We want to use daemons though to avoid hanging
        # processes if, for some reason, communication of termination conditions
        # fails.
        #
        # Patchy McPatchface to the rescue (no, I am not kidding): we remove
        # that useless assert (of all things!) on the fly.
        #
        # NOTE: while this works, we seem to have the socketpair-based detection
        #       stable enough to not need the monkeypatch.
        #
      # _daemon_fork_patch = '''\
      #     *** process_orig.py  Sun Nov 20 20:02:23 2016
      #     --- process_fixed.py Sun Nov 20 20:03:33 2016
      #     ***************
      #     *** 5,12 ****
      #           assert self._popen is None, 'cannot start a process twice'
      #           assert self._parent_pid == os.getpid(), \\
      #                  'can only start a process object created by current process'
      #     -     assert not _current_process._daemonic, \\
      #     -            'daemonic processes are not allowed to have children'
      #           _cleanup()
      #           if self._Popen is not None:
      #               Popen = self._Popen
      #     --- 5,10 ----
      #     '''
      #
      # import patchy
      # patchy.mc_patchface(mp.Process.start, _daemon_fork_patch)

        # start `self.run()` in the child process, and wait for it's
        # initalization to finish, which will send the 'alive' message.
        mp.Process.start(self)


        # this is the parent now - set role flags.
        self._rup_is_parent = True
        self._rup_is_child  = False

        # select different ends of the socketpair for further communication.
        self._rup_endpoint  = self._rup_sp[0]
        self._rup_sp[1].close()

        # from now on we should invoke `self.stop()` for a clean termination.
        # Having said that: the daemonic watcher thread and the socket lifeline
        # to the child should ensure that both will terminate in all cases, but
        # possibly somewhat delayed and apruptly.
        #
        # Either way: use a try/except to ensure `stop()` being called.
        try: 

            # we expect an alive message message from the child, within timeout
            #
            # NOTE: If the child does not bootstrap fast enough, the timeout will
            #       kick in, and the child will be considered dead, failed and/or
            #       hung, and will be terminated!  Timeout can be set as parameter
            #       to the `start()` method.
            # FIXME: rup_timeout shuld be rup_start_timeout, or something.
            try:
                self._rup_endpoint.settimeout(self._rup_timeout)
                msg = self._rup_msg_recv(len(_ALIVE_MSG))

                if msg != 'alive':
                    # attempt to read remainder of message and barf
                    msg += self._rup_msg_recv()
                    raise RuntimeError('unexpected child message (%s)' % msg)

            except socket.timeout:
                raise RuntimeError('no alive message from child')


            # When we got the alive messages, only then will we call the parent
            # initializers.  This way those initializers can make some
            # assumptions about successful child process startup.
            self._rup_initialize()

            # if we got this far, then all is well, we are done.
            self._rup_log.debug('child process started')

        except Exception as e:
            self._rup_log.exception('initialization failed')
            self.stop()
            raise

        # child is alive and initialized, parent is initialized, watcher thread
        # is started - Wohoo!


    # --------------------------------------------------------------------------
    #
    def run(self):
        '''
        This method MUST NOT be overloaded!

        This is the workload of the child process.  It will first call
        `self.initialize_child()`, and then repeatedly call `self.work()`, until
        being terminated.  When terminated, it will call `self.finalize_child()`
        and exit.

        The implementation of `work()` needs to make sure that this process is
        not spinning idly -- if there is nothing to do in `work()` at any point
        in time, the routine should at least sleep for a fraction of a second or
        something.

        `finalize_child()` is only guaranteed to get executed on `self.stop()`
        -- a hard kill via `self.terminate()` may or may not be trigger to run
        `self.finalize_child()`.

        The child process will automatically terminate when the parent process
        dies (then including the call to `self.finalize_child()`).  It is not
        possible to create daemon or orphaned processes -- which is an explicit
        purpose of this implementation.
        '''

        # this is the child now - set role flags
        self._rup_is_parent = False
        self._rup_is_child  = True

        # select different ends of the socketpair for further communication.
        self._rup_endpoint  = self._rup_sp[1]
        self._rup_sp[0].close()

        try:
            # we consider the invocation of the child initializers to be part of
            # the bootstrap process, which includes starting the watcher thread
            # to watch the parent's health (via the socket healt).
            self._rup_initialize()

            # initialization done - we only now send the alive signal, so the
            # parent can make some assumptions about the child's state
            self._rup_msg_send(_ALIVE_MSG)

            # enter the main loop and repeatedly call 'work()'.  
            #
            # If `work()` ever returns `False`, we break out of the loop to call the
            # finalizers and terminate.
            #
            # In each iteration, we also check if the socket is still open -- if it
            # is closed, we assume the parent to be dead and terminate (break the
            # loop).
            while not self._rup_term.is_set() and \
                      self._parent_is_alive()     :
            
                # des Pudel's Kern
                if not self.work():
                    self._rup_msg_send('work finished')
                    break

                time.sleep(0.001)  # FIXME: make configurable

        except BaseException as e:

            # This is a very global except, also catches 
            # sys.exit(), keyboard interrupts, etc.  
            # Ignore pylint and PEP-8, we want it this way!
            self._rup_log.exception('abort')
            self._rup_msg_send(repr(e))


        try:
            # note that we *always* call the finalizers, even if an exception
            # got raised during initialization or in the work loop
            # initializers failed for some reason or the other...
            self._rup_finalize()

        except BaseException as e:
            self._rup_log.exception('finalization error')
            self._rup_msg_send('finalize(): %s' % repr(e))

        self._rup_msg_send('terminating')
        self._rup_term.set()

        # tear down child watcher
        if self._rup_watcher:
            self._rup_watcher.join(_STOP_TIMEOUT)

        # all is done and said - begone!
        sys.exit(0)


    # --------------------------------------------------------------------------
    #
    def stop(self, timeout=_STOP_TIMEOUT):
        '''
        `stop()` can only be called by the parent (symmetric to `start()`).

        We wait for some  `timeout` seconds to make sure the child is dead, and
        otherwise send a hard kill signal.  The default timeout is 5 seconds.

        NOTE: `stop()` implies `join()`!  Use `terminate()` if that is not
              wanted.

        NOTE: The given timeout is, in some cases, applied *twice*.
        '''

        # FIXME: This method should reduce to 
        #
        #   self.terminate(timeout)
        #   self.join(timeout)
        #
        # and the signal/kill semantics should move to terminate.  Note that the
        # timeout can

        # We leave the actual stop handling via the socketpair to the watcher.
        #
        # The parent will fall back to a terminate if the watcher does not
        # appear to be able to kill the child

        assert(self._rup_is_parent)

        self._rup_log.info('parent stops child')

        # call finalizers
        self._rup_finalize()

        # tear down watcher
        if self._rup_watcher:
            self._rup_term.set()
            self._rup_watcher.join(timeout)

        mp.Process.join(self, timeout)

        # make sure child is gone
        if mp.Process.is_alive(self):
            self._rup_log.error('failed to stop child - terminate')
            self.terminate()  # hard kill

        # don't exit - but signal if child survives
        if mp.Process.is_alive(self):
            raise RuntimeError('failed to stop child')


    # --------------------------------------------------------------------------
    #
    def join(self, timeout=_STOP_TIMEOUT):

      # raise RuntimeError('call stop instead!')
      #
      # we can't really raise the exception above, as the mp module calls this
      # join via at_exit :/
        mp.Process.join(self)


    # --------------------------------------------------------------------------
    #
    def _rup_initialize(self):
        '''
        Perform basic settings, then call common and parent/child initializers.
        '''

        try:
            # call parent and child initializers, respectively
            if self._rup_is_parent:
                self._rup_initialize_common()
                self._rup_initialize_parent

                self.initialize_common()
                self.initialize_parent()

            elif self._rup_is_child:
                self._rup_initialize_common()
                self._rup_initialize_child()

                self.initialize_common()
                self.initialize_child()

        except Exception as e:
            self._rup_log.exception('initialization error')
            raise RuntimeError('initialize: %s' % repr(e))


    # --------------------------------------------------------------------------
    #
    def _rup_initialize_common(self):

        pass


    # --------------------------------------------------------------------------
    #
    def _rup_initialize_parent(self):

        # Start a separate thread which watches our end of the socket.  If that
        # thread detects any failure on that socket, it will set
        # `self._rup_term`, to signal its demise and prompt an exception from
        # the main thread.  
        #
        # NOTE: For several reasons, the watcher thread has no valid/stable
        #       means of directly signaling the main thread of any error
        #       conditions, it is thus necessary to manually check the child
        #       state from time to time, via `self.is_alive()`.
        #
        # NOTE: https://bugs.python.org/issue1856  (01/2008)
        #       `sys.exit()` can segfault Python if daemon threads are active.
        #       https://bugs.python.org/issue21963 (07/2014)
        #       This will not be fixed in python 2.x.
        #
        #       We make the Watcher thread a daemon anyway:
        #
        #       - when `sys.exit()` is called in a child process, we don't care
        #         about the process anymore anyway, and all terminfo data are
        #         sent to the parent anyway.
        #       - when `sys.exit()` is called in the parent on unclean shutdown,
        #         the same holds.
        #       - when `sys.exit()` is called in the parent on clean shutdown,
        #         then the watcher threads should already be terminated when the
        #         `sys.exit()` invocation happens
        #
        # FIXME: check the conditions above
        self._rup_term    = mt.Event()
        self._rup_watcher = mt.Thread(target=self._rup_watch)
      # self._rup_watcher.daemon = True
        self._rup_watcher.start()

        self._rup_log.info('child is alive')


    # --------------------------------------------------------------------------
    #
    def _rup_initialize_child(self):

        # TODO: should we also get an alive from parent?

        # start the watcher thread
        self._rup_term    = mt.Event()
        self._rup_watcher = mt.Thread(target=self._rup_watch)
      # self._rup_watcher.daemon = True
        self._rup_watcher.start()

        self._rup_log.info('child (me) is alive')


    # --------------------------------------------------------------------------
    #
    def initialize_common(self):
        '''
        This method can be overloaded, and will then be executed *once* during
        `start()`, for both the parent and the child process (individually).  If
        this fails on either side, the process startup is considered failed.
        '''

        self._rup_log.debug('initialize_common (NOOP)')


    # --------------------------------------------------------------------------
    #
    def initialize_parent(self):
        '''
        This method can be overloaded, and will then be executed *once* during
        `start()`, in the parent process.  If this fails, the process startup is
        considered failed.
        '''

        self._rup_log.debug('initialize_child (NOOP)')


    # --------------------------------------------------------------------------
    #
    def initialize_child(self):
        '''
        This method can be overloaded, and will then be executed *once* during
        `start()`, in the child process.  If this fails, the process startup is
        considered failed.
        '''

        self._rup_log.debug('initialize_child (NOOP)')


    # --------------------------------------------------------------------------
    #
    def _rup_finalize(self):
        '''
        Call common and parent/child initializers.  
        
        Note that finalizers are called in inverse order of initializers.
        '''

        try:
            # call parent and child finalizers, respectively
            if self._rup_is_parent:
                self.finalize_parent()
                self.finalize_common()

                self._rup_finalize_parent
                self._rup_finalize_common()

            elif self._rup_is_child:
                self.finalize_child()
                self.finalize_common()

                self._rup_finalize_child()
                self._rup_finalize_common()

        except Exception as e:
            self._rup_log.exception('finalization error')
            raise RuntimeError('finalize: %s' % repr(e))


    # --------------------------------------------------------------------------
    #
    def _rup_finalize_common(self):
    
        pass


    # --------------------------------------------------------------------------
    #
    def _rup_finalize_parent(self):
    
        pass


    # --------------------------------------------------------------------------
    #
    def _rup_finalize_child(self):
    
        pass


    # --------------------------------------------------------------------------
    #
    def finalize_common(self):
        '''
        This method can be overloaded, and will then be executed *once* during
        `stop()` or process child termination, in the parent process, in both
        the parent and the child process (individually).
        '''

        self._rup_log.debug('finalize_common (NOOP)')


    # --------------------------------------------------------------------------
    #
    def finalize_parent(self):
        '''
        This method can be overloaded, and will then be executed *once* during
        `stop()` or process child termination, in the parent process.
        '''

        self._rup_log.debug('finalize_parent (NOOP)')


    # --------------------------------------------------------------------------
    #
    def finalize_child(self):
        '''
        This method can be overloaded, and will then be executed *once* during
        `stop()` or process child termination, in the child process.
        '''

        self._rup_log.debug('finalize_child (NOOP)')


    # --------------------------------------------------------------------------
    #
    def work(self):
        '''
        This method MUST be overloaded.  It represents the workload of the
        process, and will be called over and over again.

        This has several implications:

          * `work()` needs to enforce any call rate limits on its own!
          * in order to terminate the child, `work()` needs to either raise an
            exception, or call `sys.exit()` (which actually also raises an
            exception).

        Before the first invocation, `self.initialize_child()` will be called.
        After the last invocation, `self.finalize_child()` will be called, if
        possible.  The latter will not always be possible if the child is
        terminated by a signal, such as when the parent process calls
        `child.terminate()` -- `child.stop()` should be used instead.

        The overloaded method MUST return `True` or `False` -- the child will
        continue to work upon `True`, and otherwise (on `False`) begin
        termination.
        '''

        raise NotImplementedError('ru.Process.work() MUST be overloaded')


    # --------------------------------------------------------------------------
    #
    def is_alive(self):

        return mp.Process.is_alive(self)


    # --------------------------------------------------------------------------
    #
    def _parent_is_alive(self):
        '''
        This private method checks if the parent process is still alive.  This
        obviously only makes sense when being called in the child process.

        Note that there is a (however unlikely) race: PIDs are reused, and the
        process could be replaced by another process with the same PID inbetween
        tests.  We thus also except *all* exception, including permission
        errors, to capture at least some of those races.
        '''

        # This method is an additional fail-safety check to the socketpair
        # watching performed by the watcher thread -- either one should
        # actually suffice.

        assert(self._rup_is_child)

        try:
            os.kill(self._rup_ppid, 0)
            return True

        except:
            return False


# ------------------------------------------------------------------------------

