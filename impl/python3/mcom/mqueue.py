import io
from queue import Queue, SimpleQueue, Empty
import threading

class MQueuePool(object):
    def __init__(self):
        self.with_new_dat = set()
        self.e = threading.Event()
        self.lock = threading.Lock()

    def get_queue(self, block=True, timeout=None):
        with self.lock:
            if self.with_new_dat:
                self.e.clear()
                q = self.with_new_dat.pop()
                return q

        if block:
            self.e.wait(timeout=timeout)

        with self.lock:
            if not self.e.isSet():
                raise Empty()
            self.e.clear()
            q = self.with_new_dat.pop()
            return q

    def put(self, q: Queue):
        with self.lock:
            self.with_new_dat.add(q)
            self.e.set()

class MQueue(Queue):
    """Queue that can be added to a pool of others"""
    def __init__(self, maxsize=0, *, id = None, pool: MQueuePool = None):
        self._pool = pool
        self._id = id
        super().__init__(maxsize)

    @property
    def id(self):
        """object: queue identifier"""
        return self._id

    @id.setter
    def id(self, val):
        """object: queue identifier"""
        self._id = val

    @property
    def pool(self):
        """MQueuePool: queue pool"""
        return self._pool

    @pool.setter
    def pool(self, val):
        """MQueuePool: queue pool"""
        self._pool = val

    def put(self, item, block=True, timeout=None):
        """Put item into the queue.
        The method never blocks and always succeeds
        (except for potential low-level errors such as failure to allocate memory).
        The optional args block and timeout are ignored and only provided for
        compatibility with Queue.put()."""
        self.put_nowait(item)

    def put_nowait(self, item):
        """Equivalent to put(item),
        provided for compatibility with Queue.put_nowait()."""
        super().put(item,block=False)
        if self._pool is None:
            return
        self._pool.put(self)

    def put_empty(self):
        self._pool.put(self)

if __name__ == "__main__":
    import threading
    import time

    pool = MQueuePool()

    def howdy(pool):
        for q in iter(pool.get_queue, None):
            while not q.empty():
                print(q.id,q.get())

    queues = []
    for i in range(0,2):
        q = MQueue(id=i,pool=pool)
        queues.append(q)

    thread = threading.Thread(target=howdy, args=[pool])
    thread.start()

    queues[0].put("0")
    queues[1].put("a")
    queues[1].put("b")
    time.sleep(1)
    queues[0].put("1")
    time.sleep(1)
    pool.put(None)

    thread.join()
