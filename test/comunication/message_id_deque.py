import logging
import time
import unittest

from obsrv.comunication.message_id_deque import MessageIdDeque

logger = logging.getLogger(__name__.rsplit('.')[-1])


class MessageIdDequeTest(unittest.TestCase):

    def setUp(self):
        super().setUp()

    def tearDown(self) -> None:
        super().tearDown()

    def test_initialize_deque(self):
        """
        test initialization deque

        :return:
        """
        sample_size = 12
        mid = MessageIdDeque(min_size=sample_size)
        self.assertEqual(len(mid._free_id), sample_size)
        self.assertEqual(mid._free_id.pop(), sample_size - 1)

    def test_functionality_deque(self):
        """
        test all functionality deque.
        - get id
        - realise id
        - create new id if deque is empty
        - back to original length when ids is not needed

        :return:
        """
        sample_size = 3
        multiplier = 2
        mid = MessageIdDeque(min_size=sample_size)

        # before any getting values have to have initial range
        self.assertEqual(len(mid._free_id), sample_size)
        used_ = []

        for i in range(sample_size * multiplier):
            nr = mid.get_id()
            used_.append(nr)
        # after getting more values than was initial have to have 0 values
        self.assertEqual(len(mid._free_id), 0)
        self.assertEqual(mid._size, sample_size * multiplier)

        # has no repeat numbers in used
        for i in range(sample_size * multiplier):
            self.assertEqual(used_[i], i)

        # realise some number but not last
        sample_realise_nr= used_.pop(sample_size-1)
        mid.release_id(sample_realise_nr)
        self.assertEqual(len(mid._free_id), 1)
        # again get this value - should be the same witch realise earlier
        nr = mid.get_id()
        used_.append(nr)
        self.assertEqual(sample_realise_nr, nr)
        # return all and check if deque reduce length should reduce one or more length
        for i in used_:
            mid.release_id(i)
        self.assertTrue(mid._size < sample_size*multiplier)
        self.assertTrue(len(mid._free_id) < sample_size*multiplier)

        # when this finished (have to finished) size of the deque should be  and reduce
        t0 = time.time()
        while mid._size > sample_size:
            nr = mid.get_id()
            mid.release_id(nr)
            # breakpoint - if this loop will be running longer than timeout that mean something is wrong with deque
            timeout = 2
            t1 = time.time()
            self.assertFalse(t1-t0 > timeout)

        self.assertEqual(mid._size, sample_size)
        self.assertEqual(len(mid._free_id), sample_size)


if __name__ == '__main__':
    unittest.main()
