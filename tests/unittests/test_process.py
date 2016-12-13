
__author__    = "Radical.Utils Development Team"
__copyright__ = "Copyright 2016, RADICAL@Rutgers"
__license__   = "MIT"


''' 
Unit tests for ru.Process()
'''

import os
import sys
import time

import radical.utils as ru


# ------------------------------------------------------------------------------
#
def test_process_basic():
    '''
    start a 'sleep 0.1', and expect this to finish within 0.x seconds
    '''

    class P(ru.Process):
        def __init__(self):
            return ru.Process.__init__(self, 'ru.test')
        def work(self):
            time.sleep(0.2)
            return False

    p = P()   ; t1 = time.time()
    p.start() ; t2 = time.time()
    p.join()  ; t3 = time.time()

    assert(t2-t1 > 0.0), t2-t1
    assert(t2-t1 < 0.1), t2-t1  # process startup should be quick
    assert(t3-t2 > 0.2), t3-t2  # expect exactly one work iteration
    assert(t3-t2 < 0.4), t3-t2


# ------------------------------------------------------------------------------
#
def test_process_init_fail():
    '''
    make sure the parent gets notified on failing init
    '''

    class P(ru.Process):
        def __init__(self):
            return ru.Process.__init__(self, 'ru.test')
        def initialize_child(self):
            raise RuntimeError('oops init')
        def work(self):
            time.sleep(0.1)
            return True

    try:
        p = P()
        p.start()
    except RuntimeError as e:
        assert('oops init' in str(e)), str(e)
    else:
        assert(False), 'missing exception'

    assert(not p.is_alive())


# ------------------------------------------------------------------------------
#
def test_process_final_fail():
    '''
    make sure the parent gets notified on failing finalize
    '''

    class P(ru.Process):
        def __init__(self):
            return ru.Process.__init__(self, 'ru.test')
        def initialize_child(self):
            self.i = 0
        def work(self):
            self.i += 1
            if self.i == 5:
                time.sleep(0.1)
                return False
            return True
        def finalize_child(self):
            raise RuntimeError('oops final')

    try:
        p = P()
        p.start()
        p.stop()
    except Exception as e:
        assert('oops final' in str(e)), str(e)
    else:
        pass
      # assert(False)

    assert(not p.is_alive())


# ------------------------------------------------------------------------------
#
def test_process_parent_fail():
    '''
    make sure the child dies when the parent dies
    '''

    class Parent(ru.Process):

        def __init__(self):
            ru.Process.__init__(self, name='ru.test')


        def initialize_child(self):
            self._c = Child()
            self._c.start()
            assert(self._c.is_alive())

        def work(self):
            sys.exit()  # parent dies

        def finalize_child(self):
          # # below is what's needed for *clean* termination
          # self._c.stop()
            pass


    class Child(ru.Process):

        def __init__(self):
            with open('/tmp/c_pid.%d' % os.getuid(), 'w') as f:
                f.write(str(os.getpid()))
            ru.Process.__init__(self, name='ru.test.child')

        def work(self):
            return True

    
    p = Parent()
    p.start()
    with open('/tmp/c_pid.%d' % os.getuid(), 'r') as f:
        c_pid = int(f.read().strip())
    os.unlink('/tmp/c_pid.%d' % os.getuid())
    os.kill(p.pid, 9)

    # leave some time for child to die
    time.sleep(0.01)
    try:
        os.kill(c_pid, 0)
    except OSError as e:
        pass # child is gone
    except:
        assert(False)

    assert(not p.is_alive())


# ------------------------------------------------------------------------------
# run tests if called directly
if __name__ == "__main__":

    N = 10000

    for i in range(N):
        test_process_final_fail()
        print '.',
        test_process_init_fail()
        print '.',
        test_process_parent_fail()
        print '.',
        test_process_basic()
        print '.',
        print i
   
    sys.exit()


# ------------------------------------------------------------------------------

