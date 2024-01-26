import unittest
from definitions import TEST_DIR


def main():
    loader = unittest.TestLoader()
    start_dir = TEST_DIR
    suite = loader.discover(start_dir)

    runner = unittest.TextTestRunner()
    runner.run(suite)


if __name__ == '__main__':
    main()
