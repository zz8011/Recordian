"""Optional build-time Rust core; never compiles at application startup."""
import os
import runpy
from pathlib import Path

from setuptools import Distribution, setup
from setuptools.command.build_py import build_py

try:
    from setuptools.command.bdist_wheel import bdist_wheel
except ImportError:  # setuptools 68–69 uses the command supplied by wheel.
    from wheel.bdist_wheel import bdist_wheel

ROOT = Path(__file__).resolve().parent
NATIVE = ROOT / "src" / "recordian" / "_native"
NATIVE_FILES = ("librecordian_core.so", "librecordian_core.json")


def build_requested():
    return os.environ.get("RECORDIAN_BUILD_NATIVE") == "1"


class NativeDistribution(Distribution):
    def has_ext_modules(self):
        # ctypes uses a C ABI rather than the CPython extension ABI, but the
        # shared library still makes this distribution platform dependent.
        return build_requested() or (NATIVE / NATIVE_FILES[0]).is_file()


class BuildPy(build_py):
    def run(self):
        output = Path(self.build_lib) / "recordian" / "_native"
        if not self.editable_mode:
            # A previous native build must never contaminate a later pure wheel.
            for name in NATIVE_FILES:
                (output / name).unlink(missing_ok=True)
        super().run()
        if build_requested():
            helper = runpy.run_path(str(ROOT / "scripts" / "build_native_core.py"))
            helper["build"](NATIVE if self.editable_mode else output)

    def get_outputs(self, include_bytecode=1):
        outputs = super().get_outputs(include_bytecode)
        if self.distribution.has_ext_modules():
            outputs.extend(str(Path(self.build_lib) / "recordian" / "_native" / name) for name in NATIVE_FILES)
        return list(dict.fromkeys(outputs))

    def get_output_mapping(self):
        mapping = super().get_output_mapping()
        if self.editable_mode and self.distribution.has_ext_modules():
            for name in NATIVE_FILES:
                mapping[str(Path(self.build_lib) / "recordian" / "_native" / name)] = str(NATIVE / name)
        return mapping


class NativeWheel(bdist_wheel):
    def get_tag(self):
        python, abi, platform = super().get_tag()
        if not self.root_is_pure:
            return "py3", "none", platform
        return python, abi, platform


setup(distclass=NativeDistribution, cmdclass={"build_py": BuildPy, "bdist_wheel": NativeWheel})
