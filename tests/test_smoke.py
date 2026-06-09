import mailflow


def test_package_imports_and_has_version():
    assert mailflow.__version__ == "0.1.0"
