import unittest
from importlib.metadata import PackageNotFoundError

from obsrv.dependency_guard import REQUIRED, check_dependency_versions


class TestImportGuard(unittest.TestCase):
    def test_rejects_old_obcom(self):
        with self.assertRaises(ImportError) as ctx:
            check_dependency_versions(installed=lambda dist: "1.5.0" if dist == "ocabox-common" else REQUIRED[dist])
        self.assertIn("1.6.0", str(ctx.exception))

    def test_rejects_serverish_whose_subscriber_never_receives(self):
        with self.assertRaises(ImportError) as ctx:
            check_dependency_versions(installed=lambda dist: "2.0.3" if dist == "serverish" else REQUIRED[dist])
        self.assertIn("2.2.1", str(ctx.exception))

    def test_accepts_required_and_newer(self):
        check_dependency_versions(installed=lambda dist: REQUIRED[dist])
        check_dependency_versions(installed=lambda dist: REQUIRED[dist].replace(REQUIRED[dist][0], "9", 1))

    def test_missing_distribution_is_import_error(self):
        def missing(_: str) -> str:
            raise PackageNotFoundError

        with self.assertRaises(ImportError) as ctx:
            check_dependency_versions(installed=missing)
        self.assertIn("not installed", str(ctx.exception))

    def test_current_environment_passes(self):
        check_dependency_versions()


if __name__ == "__main__":
    unittest.main()
