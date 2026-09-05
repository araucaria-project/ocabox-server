import unittest
from importlib.metadata import PackageNotFoundError

from obsrv.dependency_guard import check_dependency_versions


class TestImportGuard(unittest.TestCase):
    def test_rejects_old_obcom(self):
        with self.assertRaises(ImportError) as ctx:
            check_dependency_versions(installed=lambda _: "1.3.4")
        self.assertIn("1.4.0", str(ctx.exception))

    def test_accepts_required_and_newer(self):
        check_dependency_versions(installed=lambda _: "1.4.0")
        check_dependency_versions(installed=lambda _: "1.9.0")

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
